"""The recordings archive as JSON documents: the API's contract lives here.

``ArchiveService`` knows nothing about HTTP. It keeps a summary of every
session, refreshed only when the session's files change (see
``RecordingStore.signature``), and a few full session documents. Values
that depend on the current time (retention countdowns, the length of a
session still being recorded) are added per call, never cached.

Discord ids are strings in every document: they do not fit a JavaScript
number. Times are epoch seconds (UTC); ``info()`` names the timezone the
transcripts use. Positions are seconds into the session's audio mix.

Audio URLs in documents carry the mix version (``?v=<fingerprint>``):
a player keeps getting the same bytes for as long as it plays, and the
positions in the document always belong to the audio at that URL.
"""

from __future__ import annotations

import asyncio
import collections
import datetime as dt
import logging
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import quote

from ttsbot.archive import mix as mixing
from ttsbot.archive.media import BuildFailed, MediaCache, run_in_thread
from ttsbot.archive.retention import RetentionFacts, RetentionPolicy
from ttsbot.archive.store import RecordingStore, SessionData, SessionKey, SessionNotFound
from ttsbot.recording.transcript import merge_lines, utterances

log = logging.getLogger("tts_web")

API_VERSION = 1
API_PREFIX = f"/api/v{API_VERSION}"
MIX_FILE = "mix.ogg"
PLAN_FILE = "plan.json"
MP3_FILE = "audio.mp3"
MP3_TIMEOUT_SECONDS = 1800
# While a session is being recorded its files close every minute or so;
# a mix this fresh is reused instead of rebuilding for every page view.
LIVE_MIX_MAX_AGE_SECONDS = 120.0


class AudioUnavailable(LookupError):
    """The session has no audio left to play (deleted by retention, or none recorded)."""


class SessionRecording(RuntimeError):
    """Not available until the session has ended."""


@dataclass(slots=True)
class _Indexed:
    """What the listing needs of one session, cached until its files change."""

    signature: tuple
    error_files: tuple[str, ...]
    summary: dict  # time-independent part of the summary document
    facts: RetentionFacts
    participants: frozenset[str]
    started_at: float
    live: bool  # still being recorded
    mix_fingerprint: str | None  # None: no audio to mix


@dataclass(frozen=True, slots=True)
class AudioFile:
    path: Path
    version: str  # the mix fingerprint
    exact: bool  # the version that was asked for (safe to cache for good)


class ArchiveService:
    def __init__(self, store: RecordingStore, cache: MediaCache, retention: RetentionPolicy, *,
                 tz: dt.tzinfo = dt.timezone.utc, gap_ms: int = 500, detail_cache_size: int = 4,
                 clock: Callable[[], float] = time.time) -> None:
        self.store = store
        self.cache = cache
        self.retention = retention
        self.tz = tz
        self.gap_ms = gap_ms
        self.clock = clock
        self._index: dict[SessionKey, _Indexed] = {}
        self._index_lock = asyncio.Lock()
        self._details: collections.OrderedDict[SessionKey, tuple[tuple, str | None, dict]] = \
            collections.OrderedDict()
        self._detail_cache_size = detail_cache_size
        self._prebuild_failed: set[str] = set()

    # -- documents -----------------------------------------------------

    def info(self) -> dict:
        return {
            "api_version": API_VERSION,
            "timezone": getattr(self.tz, "key", None) or str(self.tz),
            "retention": self.retention.to_json(),
            "mix_gap_ms": self.gap_ms,
            "features": ["sessions", "transcripts", "audio_mix", "audio_mp3", "transcript_markdown"],
        }

    async def list_sessions(self, *, guild: str | None = None, user: str | None = None,
                            since: float | None = None, until: float | None = None,
                            limit: int = 50, offset: int = 0) -> dict:
        entries = await self._refresh_index()
        now = self.clock()
        selected = [
            entry for entry in entries
            if (guild is None or entry.summary["guild"]["id"] == guild)
            and (user is None or user in entry.participants)
            and (since is None or entry.started_at >= since)
            and (until is None or entry.started_at < until)
        ]
        page = selected[offset:offset + limit]
        return {"total": len(selected), "limit": limit, "offset": offset,
                "items": [self._summary(entry, now) for entry in page]}

    async def guilds(self) -> dict:
        guilds: dict[str, dict] = {}
        for entry in await self._refresh_index():  # newest first: the first name seen is the latest
            info = entry.summary["guild"]
            doc = guilds.setdefault(info["id"], {"id": info["id"], "name": info["name"], "sessions": 0,
                                                 "last_started_at": entry.started_at})
            doc["name"] = doc["name"] or info["name"]
            doc["sessions"] += 1
        return {"items": list(guilds.values())}

    async def session(self, session_id: str) -> dict:
        key = SessionKey.from_id(session_id)
        entry = await self._refresh_one(key)
        cached = self._details.get(key)
        if cached is not None and cached[0] == entry.signature:
            self._details.move_to_end(key)
            document = cached[2]
        else:
            data = await run_in_thread(self.store.read, key)
            plan, audio, version = await self._mix(key, entry, data)
            document = {**entry.summary, "audio": audio, "lines": self._lines(data, plan)}
            if audio["status"] == "none":
                document["links"] = {**document["links"], "audio": None, "audio_mp3": None}
            if audio["status"] != "error":  # retry a failed mix on the next request
                self._details[key] = (entry.signature, version, document)
                while len(self._details) > self._detail_cache_size:
                    self._details.popitem(last=False)
        return {**document, **self._dynamic(entry, self.clock())}

    async def audio_file(self, session_id: str, version: str | None = None) -> AudioFile:
        """The session's mix (Ogg Opus): the ``version`` asked for while it
        is still cached, else the current one, built first if needed."""
        key = SessionKey.from_id(session_id)
        if version:
            try:
                folder = self.cache.peek(key.id, "mix", version)
            except ValueError:
                folder = None  # not a fingerprint
            if folder is not None and (folder / MIX_FILE).is_file():
                return AudioFile(folder / MIX_FILE, version, True)
        entry = await self._refresh_one(key)
        current, folder = await self._mix_folder(key, entry)
        path = folder / MIX_FILE
        if not path.is_file():
            raise AudioUnavailable(key.id)
        return AudioFile(path, current, current == version)

    async def mp3_file(self, session_id: str) -> Path | None:
        """The mix as MP3, or None while it is being made (in the background).

        Only for finished sessions: a live one changes every minute.
        """
        key = SessionKey.from_id(session_id)
        entry = await self._refresh_one(key)
        if entry.live:
            raise SessionRecording(key.id)
        mix = await self.audio_file(session_id)
        folder = self.cache.peek(key.id, "mp3", mix.version)
        if folder is not None:
            return folder / MP3_FILE
        if self.cache.failure(key.id, "mp3", mix.version) is not None:
            raise BuildFailed(self.cache.failure(key.id, "mp3", mix.version))
        source = self.cache.path(key.id, "mix", mix.version) / MIX_FILE

        def build(out: Path) -> None:
            if not source.is_file():  # evicted while this build waited in the queue
                raise FileNotFoundError("the mix to transcode is gone")
            _transcode_mp3(source, out / MP3_FILE)

        self.cache.ensure_in_background(key.id, "mp3", mix.version, build, lane="transcode")
        return None

    async def transcript_markdown(self, session_id: str) -> Path:
        key = SessionKey.from_id(session_id)
        path = self.store.transcript_markdown(key)
        if path is None:
            raise SessionNotFound(f"{key.id}: no transcript.md")
        return path

    async def health(self) -> dict:
        readable = self.store.root.is_dir()
        stats = await run_in_thread(self.cache.stats)
        return {"status": "ok" if readable else "degraded", "recordings_readable": readable,
                "sessions_indexed": len(self._index), "cache": stats}

    # -- maintenance ---------------------------------------------------

    async def maintain(self, prebuild_recent: int = 0) -> None:
        """Refresh the index, drop cache files of changed or deleted
        sessions, and build the mixes of the newest finished sessions so
        their pages open at once."""
        entries = await self._refresh_index()
        live = {}
        for entry in entries:
            fp = entry.mix_fingerprint
            live[entry.summary["id"]] = {f"mix-{fp}", f"mp3-{fp}"} if fp else set()
        await run_in_thread(self.cache.collect, live)
        candidates = [e for e in entries if e.mix_fingerprint is not None and not e.live][:prebuild_recent]
        for entry in candidates:
            fp = entry.mix_fingerprint
            if fp in self._prebuild_failed or self.cache.peek(entry.summary["id"], "mix", fp) is not None:
                continue
            try:
                await self.audio_file(entry.summary["id"])
            except (BuildFailed, AudioUnavailable, SessionNotFound):
                self._prebuild_failed.add(fp)  # a request may still retry it; upkeep will not
        await run_in_thread(self.cache.enforce_limit)

    # -- index ---------------------------------------------------------

    async def _refresh_index(self) -> list[_Indexed]:
        async with self._index_lock:  # the index is only changed under this lock
            entries = await run_in_thread(self._refresh_all)
        for gone in [key for key in self._details if key not in self._index]:
            del self._details[gone]
        return entries

    async def _refresh_one(self, key: SessionKey) -> _Indexed:
        async with self._index_lock:
            entry = await run_in_thread(self._refresh, key)
        if entry is None:
            self._details.pop(key, None)
            raise SessionNotFound(key.id)
        return entry

    def _refresh_all(self) -> list[_Indexed]:
        keys = self.store.keys()
        for gone in set(self._index) - set(keys):
            self._index.pop(gone, None)
        entries = []
        for key in keys:
            try:
                entry = self._refresh(key)
            except Exception:
                log.exception("Session index failed session=%s", key.id)
                continue
            if entry is not None:
                entries.append(entry)
        return entries

    def _refresh(self, key: SessionKey) -> _Indexed | None:
        known = self._index.get(key)
        try:
            signature = self.store.signature(key, known.error_files if known else ())
            if known is not None and known.signature == signature:
                return known
            data = self.store.read(key)
        except SessionNotFound as exc:
            if known is not None or str(exc) != key.id:
                log.warning("Session unavailable: %s", exc)
            self._index.pop(key, None)
            return None
        if data.problems:
            log.warning("Session %s has damaged files: %s", key.id, "; ".join(data.problems[:5]))
        entry = self._index[key] = self._summarize(data, signature)
        return entry

    def _summarize(self, data: SessionData, signature: tuple) -> _Indexed:
        info = data.info
        key = data.key
        results = {c.file: c.result for c in data.chunks if c.result is not None}
        merged = merge_lines(utterances([c.entry for c in data.chunks], results, data.tts))
        people: dict[int, dict] = {}

        def person(user_id: int) -> dict:
            return people.setdefault(user_id, {"user_id": str(user_id), "name": data.names.get(user_id),
                                               "speech_seconds": 0.0, "voice_lines": 0, "tts_lines": 0})

        for event in sorted([(c.entry["started_at"], c.user_id) for c in data.chunks]
                            + [(t["at"], t["user_id"]) for t in data.tts]):
            person(event[1])  # in order of first appearance
        for chunk in data.chunks:
            person(chunk.user_id)["speech_seconds"] += chunk.entry.get("audio_seconds") or 0.0
        for line in merged:
            person(line["user_id"])["voice_lines" if line["kind"] == "voice" else "tts_lines"] += 1
        for doc in people.values():
            doc["speech_seconds"] = round(doc["speech_seconds"], 1)
        statuses = collections.Counter(chunk.status for chunk in data.chunks)
        last_activity = max([info["started_at"]] + [c.ended_at for c in data.chunks] + [t["at"] for t in data.tts])
        live = info.get("ended_at") is None
        if live:
            status = "recording"
        elif statuses["pending"]:
            status = "transcribing"
        elif statuses["failed"]:
            status = "partial"
        else:
            status = "done"
        base = _base(key)
        has_audio = any(chunk.audio is not None for chunk in data.chunks)
        summary = {
            "id": key.id,
            "guild": {"id": key.guild, "name": info.get("guild_name")},
            "channel": {"id": str(info.get("channel_id", key.name.rsplit("-", 1)[-1])),
                        "name": info.get("channel_name")},
            "started_at": info["started_at"],
            "ended_at": info.get("ended_at"),
            "last_activity_at": round(last_activity, 3),
            "status": status,
            "participants": list(people.values()),
            "speech_seconds": round(sum(p["speech_seconds"] for p in people.values()), 1),
            "line_count": len(merged),
            "transcription": {"files": len(data.chunks), "done": statuses["done"],
                              "pending": statuses["pending"], "failed": statuses["failed"],
                              "recording": data.open_files},
            "problems": data.problems[:20],
            "links": {
                "self": base,
                "audio": f"{base}/audio" if has_audio else None,
                "audio_mp3": f"{base}/audio.mp3" if has_audio and not live else None,
                "transcript_markdown": f"{base}/transcript.md",
            },
        }
        return _Indexed(
            signature=signature, error_files=data.error_files, summary=summary,
            facts=self.retention.facts(data), participants=frozenset(p["user_id"] for p in people.values()),
            started_at=info["started_at"], live=live,
            mix_fingerprint=mixing.fingerprint(data.chunks, self.gap_ms) if has_audio else None,
        )

    def _dynamic(self, entry: _Indexed, now: float) -> dict:
        end = entry.summary["ended_at"]
        return {"duration_seconds": round((end if end is not None else now) - entry.started_at, 1),
                "retention": self.retention.describe(entry.facts, now)}

    def _summary(self, entry: _Indexed, now: float) -> dict:
        return {**entry.summary, **self._dynamic(entry, now)}

    # -- session document ----------------------------------------------

    async def _mix_folder(self, key: SessionKey, entry: _Indexed,
                          data: SessionData | None = None) -> tuple[str, Path]:
        """(fingerprint, folder) of the mix to use, built first if needed."""
        if entry.mix_fingerprint is None:
            raise AudioUnavailable(key.id)
        if entry.live:
            newest = self.cache.newest(key.id, "mix")
            if newest is not None and newest[2] < LIVE_MIX_MAX_AGE_SECONDS:
                folder = self.cache.peek(key.id, "mix", newest[0])
                if folder is not None:
                    return newest[0], folder
        folder = await self.cache.ensure(key.id, "mix", entry.mix_fingerprint,
                                         self._mix_builder(key, data), lane="mix")
        return entry.mix_fingerprint, folder

    async def _mix(self, key: SessionKey, entry: _Indexed,
                   data: SessionData) -> tuple[mixing.MixPlan | None, dict, str | None]:
        audio = {"status": "none", "url": None, "mp3_url": None, "version": None, "duration": None,
                 "missing_files": sum(1 for c in data.chunks if c.audio is None), "damaged_files": [],
                 "error": None}
        if entry.mix_fingerprint is None:
            return None, audio, None
        try:
            version, folder = await self._mix_folder(key, entry, data)
            plan = await run_in_thread(mixing.load_plan, folder / PLAN_FILE)
        except (BuildFailed, OSError, ValueError) as exc:
            log.warning("Session %s audio unavailable: %s", key.id, exc)
            audio.update(status="error", error="build_failed")
            return None, audio, None
        if plan.empty:
            return None, audio, version
        base = _base(key)
        audio.update(status="ready", url=f"{base}/audio?v={version}", version=version,
                     mp3_url=None if entry.live else f"{base}/audio.mp3",
                     duration=plan.duration, damaged_files=plan.damaged_files)
        return plan, audio, version

    def _mix_builder(self, key: SessionKey, data: SessionData | None = None) -> Callable[[Path], None]:
        def build(folder: Path) -> None:
            session = data if data is not None else self.store.read(key)
            plan = mixing.build_mix(session.path, session.chunks, folder / MIX_FILE, self.gap_ms)
            mixing.save_plan(plan, folder / PLAN_FILE)

        return build

    def _lines(self, data: SessionData, plan: mixing.MixPlan | None) -> list[dict]:
        results = {c.file: c.result for c in data.chunks if c.result is not None}
        lines = []
        for index, line in enumerate(merge_lines(utterances([c.entry for c in data.chunks], results, data.tts))):
            segments = []
            for part in line["parts"]:
                span = None
                if plan is not None and part["kind"] == "voice":
                    span = plan.span(part["file"], part["offset"], part["offset_end"])
                elif plan is not None:
                    at = plan.position_at(part["at"])
                    span = None if at is None else (at, at)
                segments.append({"at": part["at"], "until": part["until"], "text": part["text"],
                                 "position": span[0] if span else None,
                                 "position_end": span[1] if span else None})
            placed = [s for s in segments if s["position"] is not None]
            lines.append({
                "index": index,
                "user_id": str(line["user_id"]),
                "name": data.names.get(line["user_id"]),
                "kind": line["kind"],
                "at": line["at"],
                "until": line["until"],
                "text": line["text"],
                "position": placed[0]["position"] if placed else None,
                "position_end": placed[-1]["position_end"] if placed else None,
                # TTS is not in the recording: its position is the moment it was spoken at
                "exact": line["kind"] == "voice",
                "segments": segments,
            })
        return lines


def _base(key: SessionKey) -> str:
    return f"{API_PREFIX}/sessions/{quote(key.guild)}/{quote(key.name)}"


def _transcode_mp3(source: Path, target: Path) -> None:
    command = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(source),
               "-c:a", "libmp3lame", "-b:a", "96k", str(target)]
    if shutil.which("nice"):
        command = ["nice", "-n", "10", *command]  # behind the bot for the CPU
    try:
        process = subprocess.run(command, capture_output=True, timeout=MP3_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"ffmpeg took over {MP3_TIMEOUT_SECONDS}s") from None
    if process.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {process.stderr.decode(errors='replace')[:200]}")
