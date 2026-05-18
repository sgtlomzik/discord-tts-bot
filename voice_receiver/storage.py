import asyncio
import json
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .config import VoiceRecorderConfig
from .metrics import RecorderMetrics
from .models import SpeechSegment


@dataclass
class StorageSession:
    path: Path
    session_id: str
    guild_id: int
    channel_id: int
    started_at_utc: datetime
    metrics: RecorderMetrics
    next_segment_id: int = 1


class SegmentStorage:
    def __init__(self, config: VoiceRecorderConfig, encoder: Callable[[bytes, Path], None] | None = None) -> None:
        self.config = config
        self.encoder = encoder or self._encode_flac

    async def start_session(self, guild_id: int, channel_id: int, session_id: str) -> StorageSession:
        started = datetime.now(timezone.utc)
        path = (
            self.config.storage_root
            / started.strftime("%Y-%m-%d")
            / f"guild_{guild_id}"
            / f"channel_{channel_id}"
            / f"session_{session_id}"
        )
        path.mkdir(parents=True, exist_ok=True)
        session = StorageSession(path, session_id, guild_id, channel_id, started, RecorderMetrics())
        await self.write_session_json(session, ended_at_utc=None)
        await self.append_event(session, {"type": "session_started", "ts": isoformat_z(started)})
        return session

    async def save_segment(self, session: StorageSession, segment: SpeechSegment) -> Path:
        segment_id = f"{session.next_segment_id:06d}"
        session.next_segment_id += 1
        user_dir = session.path / f"user_{segment.user_id}"
        user_dir.mkdir(parents=True, exist_ok=True)
        stem = segment.start_utc.strftime("%Y-%m-%dT%H-%M-%S.") + f"{segment.start_utc.microsecond // 1000:03d}Z__seg_{segment_id}"
        audio_path = user_dir / f"{stem}.flac"
        metadata_path = user_dir / f"{stem}.json"
        status = "saved"
        try:
            await asyncio.to_thread(self.encoder, segment.pcm, audio_path)
            session.metrics.segments_saved += 1
        except Exception:
            status = "storage_error"
            session.metrics.storage_errors += 1

        metadata = {
            "schema_version": 1,
            "session_id": session.session_id,
            "guild_id": str(session.guild_id),
            "channel_id": str(session.channel_id),
            "user_id": str(segment.user_id),
            "username_snapshot": segment.username_snapshot,
            "segment_id": segment_id,
            "start_utc": isoformat_z(segment.start_utc),
            "end_utc": isoformat_z(segment.end_utc),
            "duration_ms": segment.duration_ms,
            "sample_rate": self.config.sample_rate,
            "channels": 1,
            "sample_format": "s16le",
            "container": "flac",
            "file": audio_path.name,
            "vad": {
                "engine": self.config.vad_engine,
                "mode": self.config.vad_mode,
                "frame_ms": self.config.frame_ms,
                "pre_roll_ms": self.config.pre_roll_ms,
                "post_roll_ms": self.config.post_roll_ms,
                "end_silence_ms": self.config.end_silence_ms,
                "min_segment_ms": self.config.min_segment_ms,
                "max_segment_ms": self.config.max_segment_ms,
                "merge_gap_ms": self.config.merge_gap_ms,
            },
            "status": status,
        }
        metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        await self.append_event(
            session,
            {
                "type": "segment_saved" if status == "saved" else "storage_error",
                "ts": isoformat_z(datetime.now(timezone.utc)),
                "user_id": str(segment.user_id),
                "segment_id": segment_id,
                "duration_ms": segment.duration_ms,
                "file": audio_path.name,
            },
        )
        return metadata_path

    async def stop_session(self, session: StorageSession, interrupted: bool = False) -> None:
        ended = datetime.now(timezone.utc)
        await self.write_session_json(session, ended_at_utc=ended, interrupted=interrupted)
        await self.append_event(
            session,
            {"type": "session_stopped", "ts": isoformat_z(ended), "interrupted": interrupted},
        )

    async def write_session_json(
        self,
        session: StorageSession,
        ended_at_utc: datetime | None,
        interrupted: bool = False,
    ) -> None:
        payload = {
            "schema_version": 1,
            "session_id": session.session_id,
            "guild_id": str(session.guild_id),
            "channel_id": str(session.channel_id),
            "started_at_utc": isoformat_z(session.started_at_utc),
            "ended_at_utc": isoformat_z(ended_at_utc) if ended_at_utc else None,
            "interrupted": interrupted,
            "config": {
                "sample_rate": self.config.sample_rate,
                "channels": 1,
                "frame_ms": self.config.frame_ms,
                "vad_engine": self.config.vad_engine,
                "vad_mode": self.config.vad_mode,
                "pre_roll_ms": self.config.pre_roll_ms,
                "post_roll_ms": self.config.post_roll_ms,
                "end_silence_ms": self.config.end_silence_ms,
                "min_segment_ms": self.config.min_segment_ms,
                "max_segment_ms": self.config.max_segment_ms,
                "merge_gap_ms": self.config.merge_gap_ms,
                "storage_format": "flac",
            },
            "stats": session.metrics.__dict__,
        }
        (session.path / "session.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    async def append_event(self, session: StorageSession, payload: dict) -> None:
        with (session.path / "session.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def _encode_flac(self, pcm: bytes, target: Path) -> None:
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "s16le",
                "-ar",
                str(self.config.sample_rate),
                "-ac",
                "1",
                "-i",
                "pipe:0",
                "-c:a",
                "flac",
                str(target),
            ],
            input=pcm,
            check=True,
        )


def isoformat_z(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
