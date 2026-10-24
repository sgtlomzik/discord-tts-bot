"""A disk cache for files derived from recordings (a session mix, its MP3, ...).

An artifact is a folder ``<root>/<guild>/<session>/<kind>-<fingerprint>/``:
the fingerprint covers everything it was built from, so a stale one is
never mistaken for a fresh one. Building is safe to interrupt and to race:

- a builder writes into a temporary folder that is renamed into place
  only when it is complete (readers see all of an artifact or none);
- one build per artifact at a time (others wait for it) and at most
  ``max_builds`` at once per lane, in worker threads, off the event
  loop (slow transcodes get their own lane, so they never hold up the
  quick builds a page is waiting for); a cancelled request does not let
  the next build start before the running one has finished;
- a failed build is not retried for ``retry_after`` seconds, so broken
  input does not cost a rebuild on every request;
- nothing is deleted while it may still be in use: stale fingerprints
  and least recently used artifacts (over ``max_bytes``) go only once
  unused for ``grace`` seconds, and never while their scope is building.

New kinds need no change here: a kind is just a name and a builder.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, TypeVar

log = logging.getLogger("tts_web")

_SCOPE_PART = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
_KIND = re.compile(r"[a-z0-9_]{1,32}")  # no "-": it separates kind and fingerprint
_FINGERPRINT = re.compile(r"[A-Za-z0-9_]{1,64}")
_TMP_PREFIX = ".tmp-"

T = TypeVar("T")


class BuildFailed(RuntimeError):
    """The artifact could not be built; the message is for logs, not clients."""


class TooBusy(RuntimeError):
    """Too many background builds are queued already."""


@dataclass(frozen=True, slots=True)
class _Failure:
    message: str
    until: float


async def run_in_thread(func: Callable[..., T], *args) -> T:
    """``asyncio.to_thread`` that, if the caller is cancelled, still waits
    for the thread before letting the cancellation through: locks held
    around the call keep covering the work they protect."""
    task = asyncio.ensure_future(asyncio.to_thread(func, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        with contextlib.suppress(Exception):
            await asyncio.wait({task})
        raise


class MediaCache:
    def __init__(self, root: Path, *, max_bytes: int, max_builds: int = 1, max_background: int = 4,
                 retry_after: float = 300.0, grace: float = 600.0,
                 clock: Callable[[], float] = time.monotonic, wall: Callable[[], float] = time.time) -> None:
        self.root = root
        self.max_bytes = max_bytes
        self.retry_after = retry_after
        self.grace = grace
        self.max_background = max_background
        self._clock = clock
        self._wall = wall
        self._max_builds = max_builds
        self._lanes: dict[str, asyncio.Semaphore] = {}
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._failures: dict[tuple[str, str, str], _Failure] = {}
        self._background: dict[tuple[str, str, str], asyncio.Task] = {}

    def setup(self) -> None:
        """Create the root and drop half-built folders a crash left behind."""
        self.root.mkdir(parents=True, exist_ok=True)
        for tmp in self.root.glob(f"*/*/{_TMP_PREFIX}*"):
            shutil.rmtree(tmp, ignore_errors=True)

    def path(self, scope: str, kind: str, fingerprint: str) -> Path:
        parts = scope.split("/")  # "<guild>/<session>"
        if (len(parts) != 2 or not all(_SCOPE_PART.fullmatch(p) for p in parts)
                or not _KIND.fullmatch(kind) or not _FINGERPRINT.fullmatch(fingerprint)):
            raise ValueError(f"bad artifact name {scope}/{kind}-{fingerprint}")
        return self.root.joinpath(*parts, f"{kind}-{fingerprint}")

    def peek(self, scope: str, kind: str, fingerprint: str) -> Path | None:
        """The artifact if it is built, else None (never builds). Counts as a use."""
        path = self.path(scope, kind, fingerprint)
        if not path.is_dir():
            return None
        self._touch(path)
        return path

    def newest(self, scope: str, kind: str) -> tuple[str, Path, float] | None:
        """The most recently built artifact of ``kind`` in ``scope``:
        (fingerprint, folder, age in seconds since it was built)."""
        folder = self.path(scope, kind, "x").parent
        best = None
        with contextlib.suppress(OSError):
            for item in folder.iterdir():
                name, _, fingerprint = item.name.partition("-")
                if name != kind or not fingerprint or not item.is_dir():
                    continue
                built = (item / ".built").stat().st_mtime if (item / ".built").exists() else item.stat().st_mtime
                if best is None or built > best[2]:
                    best = (fingerprint, item, built)
        if best is None:
            return None
        return best[0], best[1], max(0.0, self._wall() - best[2])

    def failure(self, scope: str, kind: str, fingerprint: str) -> str | None:
        failure = self._failures.get((scope, kind, fingerprint))
        return failure.message if failure and failure.until > self._clock() else None

    def building(self, scope: str, kind: str, fingerprint: str) -> bool:
        task = self._background.get((scope, kind, fingerprint))
        return task is not None and not task.done()

    async def ensure(self, scope: str, kind: str, fingerprint: str,
                     build: Callable[[Path], None], lane: str = "default") -> Path:
        """The artifact's folder, built by ``build(folder)`` first if needed.

        Raises ``BuildFailed`` if the build fails (or failed less than
        ``retry_after`` seconds ago).
        """
        path = self.peek(scope, kind, fingerprint)
        if path is not None:
            return path
        lock = self._locks.setdefault((scope, kind), asyncio.Lock())
        async with lock:
            path = self.peek(scope, kind, fingerprint)
            if path is not None:
                return path
            message = self.failure(scope, kind, fingerprint)
            if message is not None:
                raise BuildFailed(message)
            async with self._lanes.setdefault(lane, asyncio.Semaphore(self._max_builds)):
                started = time.monotonic()
                try:
                    path = await run_in_thread(self._build, scope, kind, fingerprint, build)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    message = f"{exc.__class__.__name__}: {exc}"[:300]
                    self._failures[(scope, kind, fingerprint)] = _Failure(message, self._clock() + self.retry_after)
                    log.exception("Cache build failed %s/%s-%s", scope, kind, fingerprint)
                    raise BuildFailed(message) from exc
            self._failures.pop((scope, kind, fingerprint), None)
            log.info("Cache built %s/%s-%s in %.2fs size_mb=%.1f", scope, kind, fingerprint,
                     time.monotonic() - started, _size(path) / 1_048_576)
        await run_in_thread(self.enforce_limit)
        return path

    def ensure_in_background(self, scope: str, kind: str, fingerprint: str,
                             build: Callable[[Path], None], lane: str = "default") -> None:
        """Start ``ensure`` without waiting for it (for slow builds).

        Raises ``TooBusy`` when ``max_background`` builds are already queued.
        """
        key = (scope, kind, fingerprint)
        if self.building(*key):
            return
        if sum(1 for task in self._background.values() if not task.done()) >= self.max_background:
            raise TooBusy(f"{self.max_background} builds queued")

        async def run() -> None:
            with contextlib.suppress(BuildFailed):
                await self.ensure(scope, kind, fingerprint, build, lane)

        task = asyncio.get_running_loop().create_task(run(), name=f"cache-{kind}")
        self._background[key] = task

        def forget(done: asyncio.Task) -> None:
            if self._background.get(key) is done:
                del self._background[key]

        task.add_done_callback(forget)

    async def aclose(self) -> None:
        tasks = list(self._background.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    # -- housekeeping (blocking: run in a thread) ----------------------

    def collect(self, live: dict[str, set[str]]) -> int:
        """Delete what is no longer wanted: scopes not in ``live`` and,
        inside live ones, artifacts whose ``kind-fingerprint`` is not
        listed. Spared: anything used in the last ``grace`` seconds and
        scopes with a build running. Returns folders deleted."""
        now = self._clock()
        self._failures = {k: f for k, f in self._failures.items() if f.until > now}
        deleted = 0
        for scope_dir in self._scope_dirs():
            scope = scope_dir.relative_to(self.root).as_posix()
            if self._busy(scope):
                continue
            keep = live.get(scope, set())
            for artifact in list(scope_dir.iterdir()):
                if artifact.name in keep or artifact.name.startswith(_TMP_PREFIX) or self._recent(artifact):
                    continue
                shutil.rmtree(artifact, ignore_errors=True)
                deleted += 1
            if scope not in live:
                with contextlib.suppress(OSError):
                    scope_dir.rmdir()  # only once empty
                    scope_dir.parent.rmdir()
                for key in [k for k in self._locks if k[0] == scope and not self._locks[k].locked()]:
                    del self._locks[key]
        if deleted:
            log.info("Cache collected %d stale folders", deleted)
        return deleted

    def enforce_limit(self) -> int:
        """Delete least recently used artifacts until under ``max_bytes``
        (never one used in the last ``grace`` seconds)."""
        artifacts = []
        for scope_dir in self._scope_dirs():
            for artifact in scope_dir.iterdir():
                if artifact.is_dir() and not artifact.name.startswith(_TMP_PREFIX):
                    with contextlib.suppress(OSError):
                        artifacts.append((artifact.stat().st_mtime, _size(artifact), artifact))
        total = sum(size for _, size, _ in artifacts)
        deleted = 0
        for _, size, artifact in sorted(artifacts, key=lambda a: a[0]):
            if total <= self.max_bytes:
                break
            if self._recent(artifact):
                continue
            shutil.rmtree(artifact, ignore_errors=True)
            total -= size
            deleted += 1
        if deleted:
            log.info("Cache over its limit: %d least recently used folders deleted, %.1f MB left",
                     deleted, total / 1_048_576)
        return deleted

    def stats(self) -> dict:
        sizes = [_size(a) for d in self._scope_dirs() for a in d.iterdir() if a.is_dir()]
        return {"artifacts": len(sizes), "bytes": sum(sizes), "max_bytes": self.max_bytes}

    # -- internals -----------------------------------------------------

    def _build(self, scope: str, kind: str, fingerprint: str, build: Callable[[Path], None]) -> Path:
        final = self.path(scope, kind, fingerprint)
        final.parent.mkdir(parents=True, exist_ok=True)
        tmp = final.parent / f"{_TMP_PREFIX}{kind}-{uuid.uuid4().hex[:8]}"
        tmp.mkdir()
        try:
            build(tmp)
            (tmp / ".built").touch()  # build time; the folder's own mtime tracks use
            for item in tmp.iterdir():
                if item.is_file():
                    with open(item, "rb+") as handle:
                        os.fsync(handle.fileno())  # complete on disk before it becomes visible
            os.replace(tmp, final)
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
        return final

    def _busy(self, scope: str) -> bool:
        return any(lock.locked() for (s, _), lock in list(self._locks.items()) if s == scope)

    def _recent(self, artifact: Path) -> bool:
        try:
            return self._wall() - artifact.stat().st_mtime < self.grace
        except OSError:
            return False

    def _scope_dirs(self) -> list[Path]:
        """Folders holding artifacts: the cache is ``<guild>/<session>/<artifact>``."""
        if not self.root.is_dir():
            return []
        return [d for g in self.root.iterdir() if g.is_dir() for d in g.iterdir() if d.is_dir()]

    @staticmethod
    def _touch(path: Path) -> None:
        with contextlib.suppress(OSError):
            os.utime(path)  # recently used: spared by clean-up for a while


def _size(path: Path) -> int:
    total = 0
    with contextlib.suppress(OSError):
        for item in path.iterdir():
            with contextlib.suppress(OSError):
                total += item.stat().st_size
    return total

