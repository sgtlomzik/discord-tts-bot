"""Write each speaker's received Opus packets into Ogg chunk files.

One ``ChannelRecorder`` covers one stay of the bot in one voice channel
(a session folder). The voice socket thread calls ``submit()``; a writer
thread owns every file, so the socket thread never waits for the disk.

Discord sends nothing while a user is silent, so a file holds speech
only: an utterance ("piece") starts after more than
``piece_gap_seconds`` without packets and is separated from the previous
one by a fixed ``piece_pause_ms`` of silence. Short gaps inside an
utterance (lost packets, brief pauses) are kept as silence from the RTP
timestamps. A file is closed once it holds ``chunk_seconds`` of audio (at
the next utterance boundary) or after ``idle_close_seconds`` of silence
from that user, and a line in ``chunks.jsonl`` then records its pieces
as ``[offset in the file (s), wall-clock start (epoch s)]``: that is what
maps transcript timestamps back to real time.
"""

from __future__ import annotations

import collections
import json
import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from ttsbot.ogg_opus import UnsupportedOpusStream, opus_packet_samples
from ttsbot.recording.ogg import OggOpusWriter

log = logging.getLogger("tts_bot")

_STOP = object()
_STATS_INTERVAL = 60.0


@dataclass(frozen=True, slots=True)
class RecorderSettings:
    root: Path
    chunk_seconds: float = 600.0
    idle_close_seconds: float = 60.0
    piece_gap_seconds: float = 2.0
    piece_pause_ms: int = 500


@dataclass
class _Track:
    user_id: int
    index: int = 0
    writer: OggOpusWriter | None = None
    pieces: list[list[float]] = field(default_factory=list)
    started_at: float = 0.0
    last_wall: float = 0.0
    last_seen: float = 0.0  # monotonic time the last packet was written
    last_timestamp: int = 0
    last_samples: int = 0
    packets: int = 0


def _utc_stamp(epoch: float) -> str:
    return time.strftime("%Y%m%d-%H%M%SZ", time.gmtime(epoch))


class ChannelRecorder:
    def __init__(self, settings: RecorderSettings, guild_id: int, channel_id: int) -> None:
        self.settings = settings
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.started_at = time.time()
        self.path = settings.root / str(guild_id) / f"{_utc_stamp(self.started_at)}-{channel_id}"
        self.counters: collections.Counter[str] = collections.Counter()
        self._queue: queue.SimpleQueue = queue.SimpleQueue()
        self._tracks: dict[int, _Track] = {}
        self._thread = threading.Thread(
            target=self._run, name=f"voice-recorder-{guild_id}", daemon=True
        )

    # -- called from other threads ------------------------------------

    def start(self) -> None:
        self.path.mkdir(parents=True, exist_ok=True)
        self._write_session(ended_at=None)
        self._thread.start()
        log.info("Recording started guild=%s channel=%s path=%s", self.guild_id, self.channel_id, self.path)

    def submit(self, user_id: int, timestamp: int, opus: bytes, wall: float | None = None) -> None:
        self._queue.put((user_id, timestamp, opus, time.time() if wall is None else wall))

    def count(self, name: str, amount: int = 1) -> None:
        self.counters[name] += amount

    def stop(self, timeout: float = 10.0) -> None:
        if not self._thread.is_alive():
            return
        self._queue.put(_STOP)
        self._thread.join(timeout)
        self._write_session(ended_at=time.time())
        self._log_stats(final=True)

    # -- writer thread -------------------------------------------------

    def _run(self) -> None:
        next_stats = time.monotonic() + _STATS_INTERVAL
        while True:
            try:
                item = self._queue.get(timeout=1.0)
            except queue.Empty:
                item = None
            if item is _STOP:
                break
            try:
                if item is not None:
                    self._handle(*item)
                self._close_idle(time.monotonic())
            except Exception:
                log.exception("Recording writer failed guild=%s", self.guild_id)
            if time.monotonic() >= next_stats:
                next_stats = time.monotonic() + _STATS_INTERVAL
                self._log_stats()
        # Drain what arrived before the stop, then close every file.
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            if item is not _STOP:
                try:
                    self._handle(*item)
                except Exception:
                    log.exception("Recording writer failed guild=%s", self.guild_id)
        for track in self._tracks.values():
            self._close_chunk(track, reason="stop")

    def _handle(self, user_id: int, timestamp: int, opus: bytes, wall: float) -> None:
        settings = self.settings
        track = self._tracks.get(user_id)
        if track is None:
            track = self._tracks[user_id] = _Track(user_id=user_id)
        try:
            samples = opus_packet_samples(opus)
        except UnsupportedOpusStream:
            self.count("bad_opus")
            return

        new_piece = track.writer is None or wall - track.last_wall > settings.piece_gap_seconds
        gap = 0
        if not new_piece:
            delta = (timestamp - track.last_timestamp) & 0xFFFFFFFF
            if delta == 0 or delta >= 0x80000000:
                self.count("late_packets")  # a duplicate, or older than the last one written
                return
            gap = max(0, delta - track.last_samples)
            if gap > settings.piece_gap_seconds * 48000:
                new_piece = True  # the sender's clock jumped
        if new_piece and track.writer is not None and track.writer.seconds >= settings.chunk_seconds:
            self._close_chunk(track, reason="full")
        if track.writer is None:
            self._open_chunk(track, wall)
        elif new_piece:
            track.writer.write_silence(settings.piece_pause_ms * 48)
            track.pieces.append([round(track.writer.seconds, 3), round(wall, 3)])
        elif gap > 0:
            track.writer.write_silence(gap)
            self.count("filled_ms", gap // 48)
        track.writer.write(opus)
        track.last_wall = wall
        track.last_seen = time.monotonic()
        track.last_timestamp = timestamp
        track.last_samples = samples
        track.packets += 1
        self.count("packets")

    def _open_chunk(self, track: _Track, wall: float) -> None:
        folder = self.path / str(track.user_id)
        folder.mkdir(exist_ok=True)
        track.index += 1
        path = folder / f"{track.index:04d}.ogg"
        track.writer = OggOpusWriter(path, serial=(track.user_id ^ track.index))
        track.pieces = [[0.0, round(wall, 3)]]
        track.started_at = wall
        track.packets = 0

    def _close_idle(self, now: float) -> None:
        for track in self._tracks.values():
            if track.writer is not None and now - track.last_seen > self.settings.idle_close_seconds:
                self._close_chunk(track, reason="idle")

    def _close_chunk(self, track: _Track, reason: str) -> None:
        writer = track.writer
        if writer is None:
            return
        track.writer = None
        writer.close()
        entry = {
            "user_id": track.user_id,
            "file": str(writer.path.relative_to(self.path)),
            "started_at": round(track.started_at, 3),
            "ended_at": round(track.last_wall + track.last_samples / 48000, 3),
            "audio_seconds": round(writer.seconds, 3),
            "bytes": writer.bytes_written,
            "packets": track.packets,
            "pieces": track.pieces,
            "reason": reason,
        }
        with (self.path / "chunks.jsonl").open("a", encoding="utf-8") as manifest:
            manifest.write(json.dumps(entry) + "\n")
        log.info(
            "Recording chunk closed guild=%s user=%s file=%s audio_s=%.1f pieces=%d bytes=%d reason=%s",
            self.guild_id, track.user_id, entry["file"], writer.seconds, len(track.pieces),
            writer.bytes_written, reason,
        )

    def _write_session(self, ended_at: float | None) -> None:
        data = {
            "guild_id": self.guild_id,
            "channel_id": self.channel_id,
            "started_at": round(self.started_at, 3),
            "ended_at": None if ended_at is None else round(ended_at, 3),
        }
        (self.path / "session.json").write_text(json.dumps(data, indent=2), encoding="utf-8")

    def _log_stats(self, final: bool = False) -> None:
        counters = dict(self.counters)
        if not counters and not final:
            return
        log.info(
            "Recording %s guild=%s channel=%s users=%d %s",
            "finished" if final else "stats", self.guild_id, self.channel_id, len(self._tracks),
            " ".join(f"{name}={value}" for name, value in sorted(counters.items())) or "packets=0",
        )
