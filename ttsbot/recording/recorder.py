"""Write each speaker's received Opus packets into Ogg chunk files.

One ``ChannelRecorder`` covers one stay of the bot in one voice channel
(a session folder). The voice socket thread calls ``submit()``; a writer
thread owns every file, so the socket thread never waits for the disk.

Each RTP stream (user and SSRC) gets its own track. Its packets wait in a
small reorder window (``REORDER_PACKETS``) so ones the network delivered
out of order are written in place; only duplicates and packets older
than the window are dropped (``late_packets``).

A file holds speech only. Discord sends nothing while a user is silent,
and ``detector_factory`` (``vad.py``) drops what it does send between
words: breathing, room noise, digital silence. A gap in what is kept
longer than ``pause_ms`` is cut to ``pause_ms`` of silence and starts a
new "piece"; a shorter gap (a pause between words, lost packets) is kept
as silence from the RTP timestamps. A file is closed once it holds ``chunk_seconds`` of audio (at
the next utterance boundary) or after ``idle_close_seconds`` of silence
from that user, and a line in ``chunks.jsonl`` then records its pieces
as ``[offset in the file (s), wall-clock start (epoch s)]``: that is what
maps transcript timestamps back to real time. Until then the pieces are
appended to ``NNNN.pieces.jsonl`` as they start, so a file left open by
a crash can be recovered (``recover_session``).

Text the bot spoke for someone (TTS) goes to ``tts.jsonl`` so the
transcript can show it in place, and each speaker's display name to
``names.json``, so the transcript keeps it after they leave the server.
"""

from __future__ import annotations

import bisect
import collections
import json
import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ttsbot.ogg_opus import UnsupportedOpusStream, opus_packet_samples
from ttsbot.recording.fsutil import read_jsonl, write_atomic
from ttsbot.recording.ogg import OggOpusWriter
from ttsbot.recording.vad import DetectorFactory, SpeechGate

log = logging.getLogger("tts_bot")

_STOP = object()
_NAME = object()
_TEXT = object()
_STATS_INTERVAL = 60.0
# Packets held per stream to put reordered ones back in place (160 ms).
REORDER_PACKETS = 8
# A stream that sent nothing for this long has its held packets written.
REORDER_FLUSH_SECONDS = 0.3
# A wall-clock gap this long between packets means the sender paused, so
# nothing older is still on its way.
_SENDER_PAUSE_SECONDS = 0.2


def _signed(delta: int) -> int:
    """An RTP timestamp difference (mod 2**32) as a signed number."""
    delta &= 0xFFFFFFFF
    return delta - 0x100000000 if delta >= 0x80000000 else delta

# Session folders a recorder in this process is still writing to.
ACTIVE_SESSIONS: set[Path] = set()


@dataclass(frozen=True, slots=True)
class RecorderSettings:
    root: Path
    chunk_seconds: float = 600.0
    idle_close_seconds: float = 60.0
    pause_ms: int = 500
    detector_factory: DetectorFactory | None = None  # None keeps every packet


@dataclass
class _Track:
    user_id: int
    ssrc: int = 0
    index: int = 0
    writer: OggOpusWriter | None = None
    pieces: list[list[float]] = field(default_factory=list)
    started_at: float = 0.0
    last_wall: float = 0.0
    last_seen: float = 0.0  # monotonic time the last packet was written
    last_timestamp: int = 0
    last_samples: int = 0
    packets: int = 0
    detector: Callable[[bytes], bool] | None = None
    gate: SpeechGate = field(default_factory=SpeechGate)
    last_received: float = float("-inf")  # wall time of the last packet heard
    # Reorder window: (timestamp, opus, wall) sorted by timestamp.
    held: list[tuple[int, bytes, float]] = field(default_factory=list)
    released: int | None = None  # RTP timestamp of the last packet let through
    held_wall: float = 0.0  # wall time of the newest held packet
    last_arrival: float = 0.0  # monotonic time the last packet arrived


def _utc_stamp(epoch: float) -> str:
    return time.strftime("%Y%m%d-%H%M%SZ", time.gmtime(epoch))


class ChannelRecorder:
    def __init__(self, settings: RecorderSettings, guild_id: int, channel_id: int, *,
                 guild_name: str | None = None, channel_name: str | None = None) -> None:
        self.settings = settings
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.guild_name = guild_name
        self.channel_name = channel_name
        self.started_at = time.time()
        self.path = settings.root / str(guild_id) / f"{_utc_stamp(self.started_at)}-{channel_id}"
        self.counters: collections.Counter[str] = collections.Counter()
        self._queue: queue.SimpleQueue = queue.SimpleQueue()
        self._tracks: dict[tuple[int, int], _Track] = {}  # by (user, ssrc)
        self._next_index: dict[int, int] = {}  # file numbering is per user
        self._logged_packets = 0
        self._thread = threading.Thread(
            target=self._run, name=f"voice-recorder-{guild_id}", daemon=True
        )

    # -- called from other threads ------------------------------------

    def start(self) -> None:
        self.path.mkdir(parents=True, exist_ok=True)
        ACTIVE_SESSIONS.add(self.path)
        self._write_session(ended_at=None)
        self._thread.start()
        log.info("Recording started guild=%s channel=%s path=%s", self.guild_id, self.channel_id, self.path)

    def submit(self, user_id: int, timestamp: int, opus: bytes, wall: float | None = None, *,
               ssrc: int = 0) -> None:
        self._queue.put((user_id, timestamp, opus, time.time() if wall is None else wall, ssrc))

    def count(self, name: str, amount: int = 1) -> None:
        self.counters[name] += amount

    def note_name(self, user_id: int, name: str) -> None:
        """Remember a speaker's display name (written by the writer thread)."""
        self._to_writer(_NAME, user_id, name)

    def note_text(self, user_id: int, text: str, wall: float | None = None) -> None:
        """Record text the bot spoke for ``user_id`` (TTS) at ``wall``."""
        self._to_writer(_TEXT, user_id, text, time.time() if wall is None else wall)

    def _to_writer(self, *item: object) -> None:
        # Only the writer thread touches the files, so the socket thread
        # never waits for the disk and names.json has a single writer.
        if self._thread.is_alive():
            self._queue.put(item)
        else:
            self._dispatch(item)

    def stop(self, timeout: float = 10.0) -> None:
        if not self._thread.is_alive():
            return
        self._queue.put(_STOP)
        self._thread.join(timeout)
        self._write_session(ended_at=time.time())
        ACTIVE_SESSIONS.discard(self.path)
        self._log_stats(final=True)

    # -- writer thread -------------------------------------------------

    def _run(self) -> None:
        next_stats = time.monotonic() + _STATS_INTERVAL
        while True:
            try:
                item = self._queue.get(timeout=0.25)
            except queue.Empty:
                item = None
            if item is _STOP:
                break
            try:
                if item is not None:
                    self._dispatch(item)
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
                    self._dispatch(item)
                except Exception:
                    log.exception("Recording writer failed guild=%s", self.guild_id)
        for track in self._tracks.values():
            try:
                self._release(track, len(track.held))
            except Exception:
                log.exception("Recording writer failed guild=%s", self.guild_id)
            self._close_chunk(track, reason="stop")

    def _dispatch(self, item: tuple) -> None:
        if item[0] is _NAME:
            self._write_name(item[1], item[2])
        elif item[0] is _TEXT:
            self._write_text(item[1], item[2], item[3])
        else:
            self._handle(*item)

    def _write_name(self, user_id: int, name: str) -> None:
        path = self.path / "names.json"
        names = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        if names.get(str(user_id)) != name:
            names[str(user_id)] = name
            write_atomic(path, json.dumps(names, ensure_ascii=False, indent=1))

    def _write_text(self, user_id: int, text: str, wall: float) -> None:
        entry = {"user_id": user_id, "at": round(wall, 3), "text": text}
        with (self.path / "tts.jsonl").open("a", encoding="utf-8") as out:
            out.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def _handle(self, user_id: int, timestamp: int, opus: bytes, wall: float, ssrc: int = 0) -> None:
        key = (user_id, ssrc)
        track = self._tracks.get(key)
        if track is None:
            factory = self.settings.detector_factory
            track = self._tracks[key] = _Track(
                user_id=user_id, ssrc=ssrc, detector=factory() if factory else None, gate=SpeechGate()
            )
        track.last_arrival = time.monotonic()
        if wall - track.held_wall > _SENDER_PAUSE_SECONDS:
            # The sender paused: nothing older is coming, and its clock may
            # have restarted, so the next packet starts a fresh window.
            self._release(track, len(track.held))
            track.released = None
        self._hold(track, timestamp, opus, wall)
        if len(track.held) > REORDER_PACKETS:
            self._release(track, len(track.held) - REORDER_PACKETS)

    def _hold(self, track: _Track, timestamp: int, opus: bytes, wall: float) -> None:
        """Put a packet into the stream's reorder window, in timestamp order."""
        if track.released is not None:
            ref = track.released
        else:
            ref = track.held[0][0] if track.held else timestamp
        offset = _signed(timestamp - ref)
        offsets = [_signed(held[0] - ref) for held in track.held]
        if (track.released is not None and offset <= 0) or offset in offsets:
            self.count("late_packets")  # a duplicate, or older than what was written
            return
        track.held.insert(bisect.bisect(offsets, offset), (timestamp, opus, wall))
        track.held_wall = max(track.held_wall, wall)

    def _release(self, track: _Track, count: int) -> None:
        """Let the oldest *count* held packets through to the writer."""
        for _ in range(count):
            timestamp, opus, wall = track.held.pop(0)
            track.released = timestamp
            self._process(track, timestamp, opus, wall)

    def _process(self, track: _Track, timestamp: int, opus: bytes, wall: float) -> None:
        try:
            samples = opus_packet_samples(opus)
        except UnsupportedOpusStream:
            self.count("bad_opus")
            return
        self.count("packets")
        self.count("heard_ms", samples // 48)
        if track.detector is None:
            self._write(track, timestamp, opus, samples, wall)
            return
        if wall - track.last_received > track.gate.settings.hangover * 0.02:
            track.gate.reset()  # the sender stopped transmitting in between
        track.last_received = wall
        try:
            speech = track.detector(opus)
        except Exception:
            self.count("vad_errors")
            speech = True
        for item in track.gate.feed((timestamp, opus, samples, wall), speech):
            self._write(track, *item)

    def _write(self, track: _Track, timestamp: int, opus: bytes, samples: int, wall: float) -> None:
        settings = self.settings
        pause = settings.pause_ms * 48
        new_piece = track.writer is None
        gap = 0
        if not new_piece:
            delta = (timestamp - track.last_timestamp) & 0xFFFFFFFF
            # The wall clock also catches a sender whose RTP clock stood
            # still while it was not transmitting; 0.2 s absorbs jitter.
            wall_gap = wall - track.last_wall - (track.last_samples + pause) / 48000
            if delta == 0 or delta >= 0x80000000:
                if wall - track.last_wall <= _SENDER_PAUSE_SECONDS:
                    self.count("late_packets")  # a duplicate, or older than the last one written
                    return
                # After a pause an "older" timestamp is a restarted clock.
                gap, new_piece = 0, True
            else:
                gap = max(0, delta - track.last_samples)
                new_piece = gap > pause or wall_gap > 0.2
        if new_piece and track.writer is not None and track.writer.seconds >= settings.chunk_seconds:
            self._close_chunk(track, reason="full")
        if track.writer is None:
            self._open_chunk(track, wall)
        elif new_piece:
            track.writer.write_silence(pause)
            self._add_piece(track, round(track.writer.seconds, 3), round(wall, 3))
        elif gap > 0:
            track.writer.write_silence(gap)
            self.count("filled_ms", gap // 48)
        track.writer.write(opus)
        track.last_wall = wall
        track.last_seen = time.monotonic()
        track.last_timestamp = timestamp
        track.last_samples = samples
        track.packets += 1
        self.count("written_ms", samples // 48)

    def _open_chunk(self, track: _Track, wall: float) -> None:
        folder = self.path / str(track.user_id)
        folder.mkdir(exist_ok=True)
        track.index = self._next_index[track.user_id] = self._next_index.get(track.user_id, 0) + 1
        path = folder / f"{track.index:04d}.ogg"
        track.writer = OggOpusWriter(path, serial=(track.user_id ^ track.index))
        track.pieces = []
        track.started_at = wall
        track.packets = 0
        self._add_piece(track, 0.0, round(wall, 3))

    @staticmethod
    def _add_piece(track: _Track, offset: float, wall: float) -> None:
        track.pieces.append([offset, wall])
        with pieces_path(track.writer.path).open("a", encoding="utf-8") as out:
            out.write(json.dumps([offset, wall]) + "\n")

    def _close_idle(self, now: float) -> None:
        for track in self._tracks.values():
            if track.held and now - track.last_arrival > REORDER_FLUSH_SECONDS:
                self._release(track, len(track.held))
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
        pieces_path(writer.path).unlink(missing_ok=True)
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
        # Names as they were at the start, for readers without Discord access.
        if self.guild_name:
            data["guild_name"] = self.guild_name
        if self.channel_name:
            data["channel_name"] = self.channel_name
        write_atomic(self.path / "session.json", json.dumps(data, indent=2))

    def _log_stats(self, final: bool = False) -> None:
        counters = dict(self.counters)
        packets = counters.get("packets", 0)
        if not final and packets == self._logged_packets:
            return  # nothing heard since the last line
        self._logged_packets = packets
        log.info(
            "Recording %s guild=%s channel=%s users=%d %s",
            "finished" if final else "stats", self.guild_id, self.channel_id,
            len({track.user_id for track in self._tracks.values()}),
            " ".join(f"{name}={value}" for name, value in sorted(counters.items())) or "packets=0",
        )


def pieces_path(chunk: Path) -> Path:
    return chunk.with_name(chunk.stem + ".pieces.jsonl")


def recover_session(path: Path) -> int:
    """Close the books on a session a crash left open; return files recovered.

    Every ``.ogg`` without a ``chunks.jsonl`` line gets one from its
    ``.pieces.jsonl`` and its last Ogg page (the file itself stays as it
    is: players and Groq read it without the end-of-stream page), and
    ``session.json`` gets an end time.
    """
    from ttsbot.recording.ogg import ogg_duration

    manifest = path / "chunks.jsonl"
    listed = {entry["file"] for entry in read_jsonl(manifest, {"file": str})}
    recovered = 0
    last_mtime = 0.0
    for chunk in sorted(path.glob("*/*.ogg")):
        last_mtime = max(last_mtime, chunk.stat().st_mtime)
        name = str(chunk.relative_to(path))
        if name in listed:
            continue
        sidecar = pieces_path(chunk)
        pieces = []
        if sidecar.exists():
            pieces = [json.loads(line) for line in sidecar.read_text(encoding="utf-8").splitlines() if line]
        seconds = ogg_duration(chunk)
        if not pieces or seconds <= 0:
            log.warning("Recording recovery skipped file=%s/%s (no pieces or audio)", path.name, name)
            continue
        entry = {
            "user_id": int(chunk.parent.name),
            "file": name,
            "started_at": pieces[0][1],
            "ended_at": round(pieces[-1][1] + seconds - pieces[-1][0], 3),
            "audio_seconds": round(seconds, 3),
            "bytes": chunk.stat().st_size,
            "packets": None,
            "pieces": pieces,
            "reason": "recovered",
        }
        with manifest.open("a", encoding="utf-8") as out:
            out.write(json.dumps(entry) + "\n")
        sidecar.unlink(missing_ok=True)
        recovered += 1
        log.info("Recording chunk recovered file=%s/%s audio_s=%.1f pieces=%d", path.name, name, seconds, len(pieces))
    session_file = path / "session.json"
    session = json.loads(session_file.read_text(encoding="utf-8"))
    if session.get("ended_at") is None:
        session["ended_at"] = round(last_mtime or session["started_at"], 3)
        session["recovered"] = True
        write_atomic(session_file, json.dumps(session, indent=2))
    return recovered
