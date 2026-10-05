"""Read-only access to the session folders under RECORDING_DIR.

Layout (written by ``ttsbot.recording``)::

    <guild_id>/<YYYYMMDD-HHMMSSZ>-<channel_id>/
        session.json        guild/channel ids (and names), start and end time
        chunks.jsonl        one line per closed Ogg file, with its pieces
        names.json          user id -> display name
        tts.jsonl           what the bot spoke for whom, and when
        <user_id>/NNNN.ogg  speech of one user (deleted by audio retention)
        <user_id>/NNNN.json transcription result
        <user_id>/NNNN.error.json  failed transcription attempts
        <user_id>/NNNN.pieces.jsonl  a file still being recorded

The bot writes these while they are read here, so nothing is trusted: a
bad line or file is skipped and noted in ``SessionData.problems``, and
only an unreadable ``session.json`` makes a whole session unavailable.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from ttsbot.recording.transcript import MAX_ATTEMPTS

log = logging.getLogger("tts_web")

_GUILD = re.compile(r"\d{1,20}")
_SESSION = re.compile(r"\d{8}-\d{6}Z-\d{1,20}")
_CHUNK_FILE = re.compile(r"(\d{1,20})/(\d{4,9})\.ogg")


class SessionNotFound(LookupError):
    pass


@dataclass(frozen=True, order=True, slots=True)
class SessionKey:
    """A session folder, validated: safe to join to the root as a path."""

    guild: str
    name: str

    @classmethod
    def parse(cls, guild: str, name: str) -> "SessionKey":
        if not _GUILD.fullmatch(guild or "") or not _SESSION.fullmatch(name or ""):
            raise SessionNotFound(f"{guild}/{name}")
        return cls(guild, name)

    @classmethod
    def from_id(cls, session_id: str) -> "SessionKey":
        guild, _, name = (session_id or "").partition("/")
        return cls.parse(guild, name)

    @property
    def id(self) -> str:
        return f"{self.guild}/{self.name}"


@dataclass(slots=True)
class Chunk:
    """One Ogg file of the manifest and what is known about it."""

    entry: dict
    result: dict | None = None
    error: dict | None = None
    audio: tuple[int, int] | None = None  # (size, mtime_ns) while the Ogg file exists

    @property
    def file(self) -> str:
        return self.entry["file"]

    @property
    def user_id(self) -> int:
        return self.entry["user_id"]

    @property
    def ended_at(self) -> float:
        return self.entry.get("ended_at") or self.entry["started_at"] + self.entry.get("audio_seconds", 0.0)

    @property
    def status(self) -> str:
        """done / pending / failed (Groq rejected it MAX_ATTEMPTS times)."""
        if self.result is not None:
            return "done"
        if self.error is not None and self.error.get("attempts", 0) >= MAX_ATTEMPTS:
            return "failed"
        return "pending"


@dataclass(slots=True)
class SessionData:
    key: SessionKey
    path: Path
    info: dict
    chunks: list[Chunk] = field(default_factory=list)
    names: dict[int, str] = field(default_factory=dict)
    tts: list[dict] = field(default_factory=list)
    open_files: int = 0  # Ogg files still being written (not in the manifest yet)
    problems: list[str] = field(default_factory=list)
    error_files: tuple[str, ...] = ()  # watched by RecordingStore.signature

    @property
    def ended_at(self) -> float | None:
        return self.info.get("ended_at")


class RecordingStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def keys(self) -> list[SessionKey]:
        """Every session folder, newest first (folder names start with the UTC start time)."""
        keys = []
        for guild in self._subdirs(self.root):
            if not _GUILD.fullmatch(guild.name):
                continue
            for session in self._subdirs(guild):
                if _SESSION.fullmatch(session.name) and (session / "session.json").is_file():
                    keys.append(SessionKey(guild.name, session.name))
        keys.sort(key=lambda key: (key.name, key.guild), reverse=True)
        return keys

    def path(self, key: SessionKey) -> Path:
        return self.root / key.guild / key.name

    def signature(self, key: SessionKey, error_files: tuple[str, ...] = ()) -> tuple:
        """Changes whenever ``read(key)`` could return something different.

        Files are replaced or appended to as the bot works, which changes
        their own or their folder's mtime; an ``.error.json`` is rewritten
        in place, so the known ones are watched by name.
        """
        path = self.path(key)
        parts = []
        try:
            with os.scandir(path) as entries:
                for entry in entries:
                    if entry.is_dir(follow_symlinks=False):
                        # A fresh stat: Windows' scandir caches a folder's mtime.
                        parts.append((entry.name, os.stat(entry.path).st_mtime_ns, -1))
                    else:
                        stat = entry.stat(follow_symlinks=False)
                        parts.append((entry.name, stat.st_mtime_ns, stat.st_size))
        except (FileNotFoundError, NotADirectoryError):
            raise SessionNotFound(key.id) from None
        for name in error_files:
            try:
                stat = (path / name).stat()
                parts.append((name, stat.st_mtime_ns, stat.st_size))
            except OSError:
                parts.append((name, 0, -1))
        return tuple(sorted(parts))

    def read(self, key: SessionKey) -> SessionData:
        path = self.path(key)
        try:
            info = json.loads((path / "session.json").read_text(encoding="utf-8"))
            if not isinstance(info, dict) or not isinstance(info.get("started_at"), (int, float)):
                raise ValueError("no started_at")
        except FileNotFoundError:
            raise SessionNotFound(key.id) from None
        except (OSError, ValueError) as exc:
            raise SessionNotFound(f"{key.id}: unreadable session.json ({exc})") from None
        data = SessionData(key, path, info)
        error_files = []
        for entry in self._jsonl(data, "chunks.jsonl"):
            match = _CHUNK_FILE.fullmatch(str(entry.get("file", "")))
            if (not match or int(match.group(1)) != entry.get("user_id")
                    or not isinstance(entry.get("started_at"), (int, float))
                    or not self._valid_pieces(entry.get("pieces"))):
                data.problems.append(f"chunks.jsonl: bad entry {str(entry.get('file'))[:40]!r}")
                continue
            chunk = Chunk(entry)
            audio = path / chunk.file
            chunk.result = self._json(data, audio.with_suffix(".json"))
            if chunk.result is not None:
                segments = chunk.result.get("segments")
                if not isinstance(segments, list):
                    data.problems.append(f"{chunk.file}: result without segments")
                    chunk.result = None
                else:
                    valid = [s for s in segments if self._valid_segment(s)]
                    if len(valid) != len(segments):
                        data.problems.append(f"{chunk.file}: {len(segments) - len(valid)} bad segments")
                    chunk.result = {**chunk.result, "segments": valid}
            error = audio.with_suffix(".error.json")
            chunk.error = self._json(data, error)
            if chunk.error is not None or error.exists():  # watched even while unreadable
                error_files.append(str(error.relative_to(path)).replace(os.sep, "/"))
            try:
                stat = audio.stat()
                chunk.audio = (stat.st_size, stat.st_mtime_ns)
            except OSError:
                pass  # pruned by retention
            data.chunks.append(chunk)
        data.error_files = tuple(error_files)
        names = self._json(data, path / "names.json") or {}
        data.names = {int(k): str(v) for k, v in names.items() if str(k).isdigit() and v}
        data.tts = [item for item in self._jsonl(data, "tts.jsonl")
                    if isinstance(item.get("user_id"), int) and isinstance(item.get("at"), (int, float))
                    and isinstance(item.get("text"), str)]
        data.open_files = sum(1 for _ in path.glob("*/*.pieces.jsonl"))
        return data

    def transcript_markdown(self, key: SessionKey) -> Path | None:
        path = self.path(key) / "transcript.md"
        return path if path.is_file() else None

    # -- helpers -------------------------------------------------------

    @staticmethod
    def _subdirs(path: Path) -> list[Path]:
        try:
            with os.scandir(path) as entries:
                return [Path(entry.path) for entry in entries if entry.is_dir(follow_symlinks=False)]
        except OSError:
            return []

    @staticmethod
    def _valid_pieces(pieces) -> bool:
        return (isinstance(pieces, list) and bool(pieces)
                and all(isinstance(p, list) and len(p) == 2 and all(isinstance(v, (int, float)) for v in p)
                        for p in pieces))

    @staticmethod
    def _valid_segment(segment) -> bool:
        return (isinstance(segment, dict) and isinstance(segment.get("text"), str)
                and all(isinstance(segment.get(k), (int, float)) for k in ("start", "at", "until")))

    @staticmethod
    def _json(data: SessionData, path: Path) -> dict | None:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            data.problems.append(f"{path.name}: {exc.__class__.__name__}")
            return None
        if not isinstance(value, dict):
            data.problems.append(f"{path.name}: not an object")
            return None
        return value

    @staticmethod
    def _jsonl(data: SessionData, name: str) -> list[dict]:
        try:
            lines = (data.path / name).read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        except OSError as exc:
            data.problems.append(f"{name}: {exc.__class__.__name__}")
            return []
        items = []
        for number, line in enumerate(lines, 1):
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except ValueError:
                if number < len(lines):  # the last line may be an append in progress
                    data.problems.append(f"{name}: bad line {number}")
                continue
            if isinstance(item, dict):
                items.append(item)
        return items
