"""Background worker: send closed recording chunks to Groq, oldest first.

The disk is the queue: a chunk listed in a session's ``chunks.jsonl`` is
pending until its ``NNNN.json`` result exists, so nothing is lost across
restarts. A chunk Groq rejects for good (a 4xx other than 429) gets an
``NNNN.error.json`` and is retried up to ``max_attempts`` times in all.
The worker waits whenever ``AudioBudget`` says the next file would go
over Groq's limits, and rebuilds the session transcript after each
result and once more when the session has ended.

It also applies the retention limits as it starts and then every
``prune_seconds`` (0: only at start): ``keep_audio_days`` deletes the Ogg
files of transcribed chunks older than that, ``keep_transcript_days``
deletes whole finished sessions older than that; 0 keeps everything.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import contextlib
import json
import logging
import math
import os
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

from ttsbot.recording import recorder
from ttsbot.recording.groq import MAX_UPLOAD_BYTES, AudioBudget, GroqTranscriber, TranscriptionError
from ttsbot.recording.fsutil import write_atomic
from ttsbot.recording.transcript import MAX_ATTEMPTS, build_session_transcript, chunk_result, result_path

log = logging.getLogger("tts_bot")


@dataclass(frozen=True, slots=True)
class PendingChunk:
    session: Path
    entry: dict

    @property
    def path(self) -> Path:
        return self.session / self.entry["file"]


def error_path(session: Path, chunk_file: str) -> Path:
    return (session / chunk_file).with_suffix(".error.json")


class TranscriptionWorker:
    def __init__(self, root: Path, transcriber: GroqTranscriber, budget: AudioBudget, *,
                 name_for: Callable[[int, int], Awaitable[str | None]], tz: dt.tzinfo,
                 scan_seconds: float = 30.0, max_attempts: int = MAX_ATTEMPTS,
                 ready: Callable[[], Awaitable] | None = None,
                 keep_audio_days: int = 0, keep_transcript_days: int = 0,
                 prune_seconds: float = 86400.0) -> None:
        self.root = root
        self.transcriber = transcriber
        self.budget = budget
        self.name_for = name_for
        self.tz = tz
        self.scan_seconds = scan_seconds
        self.max_attempts = max_attempts
        self._ready = ready
        self.keep_audio_days = keep_audio_days
        self.keep_transcript_days = keep_transcript_days
        self.prune_seconds = prune_seconds
        self._next_prune = math.inf  # time.monotonic() of the next sweep; run() arms it
        self._finished: set[Path] = set()  # ended sessions with nothing left to do
        self._waiting_logged: str | None = None

    async def run(self) -> None:
        if self._ready is not None:
            await self._ready()  # names come from the member cache first
        log.info(
            "Transcription worker started root=%s model=%s per_hour=%.0f per_day=%.0f used_day_s=%.0f",
            self.root, self.transcriber.model, self.budget.per_hour, self.budget.per_day,
            self.budget.used(86400),
        )
        await self._sweep()
        while True:
            try:
                delay = await self.step()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Transcription worker step failed")
                delay = self.scan_seconds
            if delay:
                await asyncio.sleep(delay)

    async def step(self) -> float:
        """Do one unit of work; return how long to sleep before the next."""
        pending, ended = await asyncio.to_thread(self.scan)
        for session in ended:
            await self.rebuild(session)
            self._finished.add(session)
        if time.monotonic() >= self._next_prune:
            await self._sweep()
        if not pending:
            return self.scan_seconds
        chunk = pending[0]
        seconds = chunk.entry["audio_seconds"]
        wait = self.budget.wait_for(seconds)
        if wait > 0:
            # Waits up to a few seconds are the request spacing, not a limit.
            if wait > 5 and self._waiting_logged != chunk.entry["file"]:
                self._waiting_logged = chunk.entry["file"]
                log.info(
                    "Transcription waiting for Groq budget wait_s=%.0f file=%s/%s audio_s=%.1f "
                    "pending=%d hour_used_s=%.0f day_used_s=%.0f",
                    wait, chunk.session.name, chunk.entry["file"], seconds, len(pending),
                    self.budget.used(3600), self.budget.used(86400),
                )
            return min(wait, self.scan_seconds)
        await self.transcribe(chunk, pending=len(pending) - 1)
        return 0.0

    def scan(self) -> tuple[list[PendingChunk], list[Path]]:
        """Pending chunks of every session, oldest first, and the sessions
        that have ended with nothing left to send."""
        pending: list[PendingChunk] = []
        ended: list[Path] = []
        for session in sorted(self.root.glob("*/*/")):
            if session in self._finished or not (session / "session.json").exists():
                continue
            info = json.loads((session / "session.json").read_text(encoding="utf-8"))
            if info.get("ended_at") is None and session not in recorder.ACTIVE_SESSIONS:
                recorder.recover_session(session)  # left open by a crash or restart
                info = json.loads((session / "session.json").read_text(encoding="utf-8"))
            todo = [PendingChunk(session, entry) for entry in self._entries(session) if self._needs_work(session, entry)]
            pending.extend(todo)
            if not todo and info.get("ended_at") is not None and session not in recorder.ACTIVE_SESSIONS:
                ended.append(session)
        pending.sort(key=lambda chunk: chunk.entry["started_at"])
        return pending, ended

    async def _sweep(self) -> None:
        """One retention sweep, off the event loop; schedules the next one."""
        self._next_prune = time.monotonic() + self.prune_seconds if self.prune_seconds > 0 else math.inf
        try:
            await asyncio.to_thread(self.prune)
        except Exception:
            log.exception("Recording retention sweep failed")

    def prune(self, now: float | None = None) -> None:
        """Apply both retention limits: old sessions first, then old audio."""
        self.prune_transcripts(now)
        self.prune_audio(now)

    def prune_transcripts(self, now: float | None = None) -> tuple[int, int]:
        """Delete finished sessions older than ``keep_transcript_days``; return (sessions, bytes).

        The whole session folder goes (results, transcripts and any audio
        left). A session is kept while it is being recorded, has not ended,
        or still has a chunk waiting for transcription.
        """
        if self.keep_transcript_days <= 0:
            return 0, 0
        cutoff = (time.time() if now is None else now) - self.keep_transcript_days * 86400
        sessions = freed = 0
        for session in sorted(self.root.glob("*/*/")):
            if session in recorder.ACTIVE_SESSIONS:
                continue
            try:
                info = json.loads((session / "session.json").read_text(encoding="utf-8"))
                ended = info.get("ended_at")
                if ended is None or ended >= cutoff:
                    continue
                if any(self._needs_work(session, entry) for entry in self._entries(session)):
                    continue
                size = sum(path.stat().st_size for path in session.rglob("*") if path.is_file())
                shutil.rmtree(session)
            except FileNotFoundError:
                continue  # not a session folder
            except (OSError, ValueError, KeyError):
                log.exception("Transcript pruning failed session=%s", session.name)
                continue
            self._finished.discard(session)
            sessions += 1
            freed += size
            with contextlib.suppress(OSError):
                session.parent.rmdir()  # the guild folder, once it is empty
        if sessions:
            log.info("Recording sessions pruned sessions=%d freed_mb=%.1f keep_days=%d",
                     sessions, freed / 1_048_576, self.keep_transcript_days)
        return sessions, freed

    def prune_audio(self, now: float | None = None) -> tuple[int, int]:
        """Delete Ogg files older than ``keep_audio_days``; return (files, bytes).

        Only a chunk whose result ``NNNN.json`` exists goes (the audio of an
        untranscribed chunk is the only copy of that speech), and never one
        of a session still being recorded. Results, manifest and transcripts
        are kept.
        """
        if self.keep_audio_days <= 0:
            return 0, 0
        cutoff = (time.time() if now is None else now) - self.keep_audio_days * 86400
        files = freed = 0
        for session in sorted(self.root.glob("*/*/")):
            if session in recorder.ACTIVE_SESSIONS or not (session / "session.json").exists():
                continue
            try:
                entries = self._entries(session)
            except (OSError, ValueError):
                log.exception("Audio pruning could not read the manifest session=%s", session.name)
                continue
            for entry in entries:
                ended = entry.get("ended_at", entry["started_at"] + entry.get("audio_seconds", 0.0))
                if ended >= cutoff or not result_path(session, entry["file"]).exists():
                    continue
                audio = session / entry["file"]
                try:
                    size = audio.stat().st_size
                    audio.unlink()
                except FileNotFoundError:
                    continue  # deleted by an earlier sweep
                except OSError:
                    log.exception("Audio pruning failed file=%s/%s", session.name, entry["file"])
                    continue
                recorder.pieces_path(audio).unlink(missing_ok=True)
                files += 1
                freed += size
        if files:
            log.info("Recording audio pruned files=%d freed_mb=%.1f keep_days=%d",
                     files, freed / 1_048_576, self.keep_audio_days)
        return files, freed

    @staticmethod
    def _entries(session: Path) -> list[dict]:
        manifest = session / "chunks.jsonl"
        if not manifest.exists():
            return []
        return [json.loads(line) for line in manifest.read_text(encoding="utf-8").splitlines() if line]

    def _needs_work(self, session: Path, entry: dict) -> bool:
        if result_path(session, entry["file"]).exists():
            return False
        error = error_path(session, entry["file"])
        if error.exists():
            return json.loads(error.read_text(encoding="utf-8")).get("attempts", 0) < self.max_attempts
        return True

    async def transcribe(self, chunk: PendingChunk, pending: int = 0) -> None:
        entry = chunk.entry
        upload = chunk.path
        temp: Path | None = None
        try:
            if upload.stat().st_size > MAX_UPLOAD_BYTES:
                temp = upload = await self._shrink(upload)
            result = await self.transcriber.transcribe(upload)
        except TranscriptionError as exc:
            if exc.retry:
                self.budget.block(exc.wait)
                log.warning("Transcription failed, retry in %.0fs file=%s/%s: %s",
                            exc.wait, chunk.session.name, entry["file"], exc)
            else:
                self._record_error(chunk, str(exc))
            return
        finally:
            if temp is not None:
                temp.unlink(missing_ok=True)
        self.budget.record(entry["audio_seconds"], result.remaining_audio_seconds)
        stored = chunk_result(entry, result.data, model=self.transcriber.model, took=result.took)
        target = result_path(chunk.session, entry["file"])
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(stored, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(target)
        error_path(chunk.session, entry["file"]).unlink(missing_ok=True)
        log.info(
            "Transcription done file=%s/%s user=%s audio_s=%.1f took=%.2fs chars=%d segments=%d "
            "dropped=%d groq_remaining_s=%s day_used_s=%.0f pending=%d",
            chunk.session.name, entry["file"], entry["user_id"], entry["audio_seconds"], result.took,
            len(stored["text"]), len(stored["segments"]), len(stored["dropped"]),
            "-" if result.remaining_audio_seconds is None else f"{result.remaining_audio_seconds:.0f}",
            self.budget.used(86400), pending,
        )
        await self.rebuild(chunk.session)

    def _record_error(self, chunk: PendingChunk, message: str) -> None:
        path = error_path(chunk.session, chunk.entry["file"])
        attempts = 1
        if path.exists():
            attempts = json.loads(path.read_text(encoding="utf-8")).get("attempts", 0) + 1
        write_atomic(path, json.dumps({"attempts": attempts, "error": message, "at": round(time.time(), 3)},
                                      ensure_ascii=False))
        log.error("Transcription rejected attempt=%d/%d file=%s/%s: %s",
                  attempts, self.max_attempts, chunk.session.name, chunk.entry["file"], message)
        self.budget.block(60.0)

    async def rebuild(self, session: Path) -> None:
        """Rewrite the session transcript, looking up names it lacks."""
        info = json.loads((session / "session.json").read_text(encoding="utf-8"))
        names_file = session / "names.json"
        names = json.loads(names_file.read_text(encoding="utf-8")) if names_file.exists() else {}
        speakers = {entry["user_id"] for entry in self._entries(session)}
        tts = session / "tts.jsonl"
        if tts.exists():
            speakers |= {json.loads(line)["user_id"] for line in tts.read_text(encoding="utf-8").splitlines() if line}
        missing = [user_id for user_id in speakers if str(user_id) not in names]
        for user_id in missing:
            name = await self.name_for(info["guild_id"], user_id)
            if name:
                names[str(user_id)] = name
        if missing:
            write_atomic(names_file, json.dumps(names, ensure_ascii=False, indent=1))
        summary = await asyncio.to_thread(build_session_transcript, session, str, self.tz)
        log.debug("Transcript rebuilt session=%s %s", session.name, summary)

    @staticmethod
    async def _shrink(path: Path) -> Path:
        """Re-encode a file over Groq's 25 MB limit as 32 kb/s mono Opus."""
        fd, name = tempfile.mkstemp(suffix=".ogg")
        os.close(fd)
        process = await asyncio.create_subprocess_exec(
            "ffmpeg", "-v", "error", "-y", "-i", str(path), "-ac", "1", "-c:a", "libopus", "-b:a", "32k", name,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await process.communicate()
        if process.returncode != 0:
            Path(name).unlink(missing_ok=True)
            raise TranscriptionError(f"ffmpeg failed: {stderr.decode(errors='replace')[:200]}", retry=False)
        log.info("Transcription upload shrunk file=%s from=%d to=%d bytes", path.name,
                 path.stat().st_size, Path(name).stat().st_size)
        return Path(name)
