"""A disk cache for files derived from recordings (a session mix, its MP3, ...).

An artifact is a folder ``<root>/<scope>/<kind>-<fingerprint>/``: the
fingerprint covers everything it was built from, so a stale one is never
served, only replaced. Building is safe to interrupt and to race:

- a builder writes into a temporary folder that is renamed into place
  only when it is complete (readers see all of an artifact or none);
- one build per artifact at a time (others wait for it) and at most
  ``max_builds`` at once per lane, in worker threads, off the event
  loop (slow transcodes get their own lane, so they never hold up the
  quick builds a page is waiting for);
- a failed build is not retried for ``retry_after`` seconds, so broken
  input does not cost a rebuild on every request;
- older fingerprints of the same kind go once a new one is built, and
  the least recently used artifacts once the cache is over ``max_bytes``.

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
from typing import Callable

log = logging.getLogger("tts_web")

_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
_TMP_PREFIX = ".tmp-"


class BuildFailed(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class _Failure:
    message: str
    until: float


class MediaCache:
    def __init__(self, root: Path, *, max_bytes: int, max_builds: int = 1,
                 retry_after: float = 300.0, clock: Callable[[], float] = time.monotonic) -> None:
        self.root = root
        self.max_bytes = max_bytes
        self.retry_after = retry_after
        self._clock = clock
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
        if len(parts) != 2 or not all(_NAME.fullmatch(p) for p in [*parts, kind, fingerprint]):
            raise ValueError(f"bad artifact name {scope}/{kind}-{fingerprint}")
        return self.root.joinpath(*parts, f"{kind}-{fingerprint}")

    def peek(self, scope: str, kind: str, fingerprint: str) -> Path | None:
        """The artifact if it is built, else None (never builds)."""
        path = self.path(scope, kind, fingerprint)
        if not path.is_dir():
            return None
        self._touch(path)
        return path

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
                    path = await asyncio.to_thread(self._build, scope, kind, fingerprint, build)
                except Exception as exc:
                    message = f"{exc.__class__.__name__}: {exc}"[:300]
                    self._failures[(scope, kind, fingerprint)] = _Failure(message, self._clock() + self.retry_after)
                    log.exception("Cache build failed %s/%s-%s", scope, kind, fingerprint)
                    raise BuildFailed(message) from exc
            self._failures.pop((scope, kind, fingerprint), None)
            log.info("Cache built %s/%s-%s in %.2fs size_mb=%.1f", scope, kind, fingerprint,
                     time.monotonic() - started, _size(path) / 1_048_576)
        await asyncio.to_thread(self.enforce_limit, keep=path)
        return path

    def ensure_in_background(self, scope: str, kind: str, fingerprint: str,
                             build: Callable[[Path], None], lane: str = "default") -> None:
        """Start ``ensure`` without waiting for it (for slow builds)."""
        key = (scope, kind, fingerprint)
        if self.building(*key):
            return

        async def run() -> None:
            with contextlib.suppress(BuildFailed):
                await self.ensure(scope, kind, fingerprint, build, lane)

        task = asyncio.get_running_loop().create_task(run(), name=f"cache-{kind}")
        self._background[key] = task
        task.add_done_callback(lambda _: self._background.pop(key, None))

    async def aclose(self) -> None:
        tasks = list(self._background.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    # -- housekeeping (blocking: run in a thread) ----------------------

    def collect(self, live: dict[str, set[str]]) -> int:
        """Delete scopes not in ``live`` and, inside the live ones, any
        artifact whose ``kind-fingerprint`` is not listed. Returns
        folders deleted."""
        now = self._clock()
        self._failures = {k: f for k, f in self._failures.items() if f.until > now}
        deleted = 0
        for scope_dir in self._scope_dirs():
            scope = scope_dir.relative_to(self.root).as_posix()
            keep = live.get(scope)
            if keep is None:
                shutil.rmtree(scope_dir, ignore_errors=True)
                deleted += 1
                with contextlib.suppress(OSError):
                    scope_dir.parent.rmdir()
                continue
            for artifact in scope_dir.iterdir():
                if artifact.name.startswith(_TMP_PREFIX) or artifact.name in keep:
                    continue
                shutil.rmtree(artifact, ignore_errors=True)
                deleted += 1
        if deleted:
            log.info("Cache collected %d stale folders", deleted)
        return deleted

    def enforce_limit(self, keep: Path | None = None) -> int:
        """Delete least recently used artifacts until under ``max_bytes``."""
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
            if artifact == keep:
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
            for item in tmp.iterdir():
                with open(item, "rb+") as handle:
                    os.fsync(handle.fileno())  # complete on disk before it becomes visible
            os.replace(tmp, final)
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
        for old in final.parent.glob(f"{kind}-*"):
            if old != final:
                shutil.rmtree(old, ignore_errors=True)
        return final

    def _scope_dirs(self) -> list[Path]:
        """Folders holding artifacts: the cache is ``<guild>/<session>/<artifact>``."""
        if not self.root.is_dir():
            return []
        return [d for g in self.root.iterdir() if g.is_dir() for d in g.iterdir() if d.is_dir()]

    @staticmethod
    def _touch(path: Path) -> None:
        with contextlib.suppress(OSError):
            os.utime(path)  # recently used: last to go when over the limit


def _size(path: Path) -> int:
    total = 0
    with contextlib.suppress(OSError):
        for item in path.iterdir():
            with contextlib.suppress(OSError):
                total += item.stat().st_size
    return total
