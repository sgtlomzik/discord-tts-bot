"""Audio plumbing: PCM framing, ffmpeg command builders, Opus loading and
the continuous Discord audio source.

The continuous source plays an endless stream of 20ms PCM frames — real
speech when frames are queued, idle silence/comfort noise otherwise — so
the voice connection stays warm between messages.
"""

import asyncio
import ctypes.util
import logging
import queue as thread_queue
import struct
import threading
from pathlib import Path

import discord

from ttsbot import config
from ttsbot.ogg_opus import require_discord_frame

log = logging.getLogger("tts_bot")

PCM_SAMPLE_RATE = 48000
PCM_CHANNELS = 2
PCM_SAMPLE_WIDTH = 2
PCM_FRAME_MS = 20
PCM_FRAME_BYTES = int(PCM_SAMPLE_RATE * PCM_FRAME_MS / 1000) * PCM_CHANNELS * PCM_SAMPLE_WIDTH
OPUS_SILENCE_FRAME = bytes((0xF8, 0xFF, 0xFE))  # 20 ms, 960 samples at 48 kHz


def load_opus() -> bool:
    if discord.opus.is_loaded():
        return True

    opus_path = ctypes.util.find_library("opus")
    if not opus_path:
        log.error("Opus library not found")
        return False

    discord.opus.load_opus(opus_path)
    log.info("Opus loaded: %s", opus_path)
    return discord.opus.is_loaded()



def _pitch_filters(pitch: int) -> list[str]:
    if not pitch:
        return []
    factor = 2 ** (pitch / 12)
    # Input is already 48 kHz (aformat runs first): asetrate shifts pitch and
    # speed, then resample back and undo the speed change with atempo.
    return [
        f"asetrate={round(48000 * factor)}",
        "aresample=48000",
        f"atempo={1 / factor:.8f}",
    ]


def build_tts_pcm_command(source: Path, pitch: int = 0) -> list[str]:
    audio_filters: list[str] = ["aformat=sample_rates=48000:channel_layouts=stereo"]
    if config.TTS_TRIM_SILENCE:
        audio_filters.append(
            "silenceremove="
            "start_periods=1:start_silence=0.03:start_threshold=-50dB:"
            "stop_periods=-1:stop_duration=0.12:stop_threshold=-50dB"
        )
    audio_filters.extend(_pitch_filters(pitch))

    return [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-i",
        str(source),
        "-vn",
        "-af",
        ",".join(audio_filters),
        "-f",
        "s16le",
        "-ar",
        str(PCM_SAMPLE_RATE),
        "-ac",
        str(PCM_CHANNELS),
        "pipe:1",
    ]


def build_tts_stream_pcm_command(input_format: str = "mp3", pitch: int = 0) -> list[str]:
    """Decode streaming MP3 or Ogg/Opus on stdin to s16le 48k stereo.

    Used by the streaming path: compressed chunks are written to stdin and
    decoded PCM is read from stdout incrementally. No silence trimming — that
    needs the whole clip, and the continuous player already handles idle.
    """
    if input_format not in {"mp3", "ogg"}:
        raise ValueError(f"Unsupported streaming input format: {input_format}")
    return [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-f",
        input_format,
        "-i",
        "pipe:0",
        "-vn",
        "-af",
        ",".join(["aformat=sample_rates=48000:channel_layouts=stereo", *_pitch_filters(pitch)]),
        "-f",
        "s16le",
        "-ar",
        str(PCM_SAMPLE_RATE),
        "-ac",
        str(PCM_CHANNELS),
        "pipe:1",
    ]


def split_pcm_frames(pcm_data: bytes, tail_ms: int = 0) -> list[bytes]:
    frames: list[bytes] = []
    for offset in range(0, len(pcm_data), PCM_FRAME_BYTES):
        frame = pcm_data[offset : offset + PCM_FRAME_BYTES]
        if len(frame) < PCM_FRAME_BYTES:
            frame = frame + (b"\x00" * (PCM_FRAME_BYTES - len(frame)))
        frames.append(frame)

    tail_frames = max(tail_ms, 0) // PCM_FRAME_MS
    frames.extend([b"\x00" * PCM_FRAME_BYTES for _ in range(tail_frames)])
    return frames


def build_idle_pcm_frame(mode: str, volume_db: float) -> bytes:
    if mode == "silence":
        return b"\x00" * PCM_FRAME_BYTES

    amplitude = max(1, min(32767, int(32767 * (10 ** (volume_db / 20)))))
    samples: list[int] = []
    seed = 0x1234ABCD
    for _ in range(PCM_FRAME_BYTES // PCM_SAMPLE_WIDTH):
        seed = (1103515245 * seed + 12345) & 0x7FFFFFFF
        samples.append((seed % (amplitude * 2 + 1)) - amplitude)
    return struct.pack("<" + "h" * len(samples), *samples)


class ContinuousTTSAudioSource(discord.AudioSource):
    """Endless 20 ms frame stream: queued speech, idle frames in between.

    In ``opus`` mode discord.py receives raw Opus packets. The source still
    accepts 20 ms PCM frames (Piper, MiniMax, PCM cache; a PCM frame is 3840
    bytes, an Opus packet at most 1275) and encodes them itself, so Fish and
    PCM voices share one player instead of restarting it on every switch.
    """

    def __init__(self, idle_frame: bytes, *, opus: bool = False) -> None:
        self._opus = opus
        self._encoder: discord.opus.Encoder | None = None
        self._check_frame(idle_frame)
        self.idle_frame = idle_frame
        self.frames: thread_queue.Queue[bytes] = thread_queue.Queue()
        self._stopped = threading.Event()
        self._drained = threading.Event()
        self._drained.set()
        self._lock = threading.Lock()
        self._pending_frames = 0

    def read(self) -> bytes:
        if self._stopped.is_set():
            return b""

        try:
            frame = self.frames.get_nowait()
        except thread_queue.Empty:
            if self._opus and len(self.idle_frame) == PCM_FRAME_BYTES:
                self.idle_frame = self._encode(self.idle_frame)  # encode once, reuse
            return self.idle_frame

        with self._lock:
            self._pending_frames = max(0, self._pending_frames - 1)
            if self._pending_frames == 0:
                self._drained.set()
        if self._opus and len(frame) == PCM_FRAME_BYTES:
            return self._encode(frame)
        return frame

    def _encode(self, pcm: bytes) -> bytes:
        # Created lazily on the player thread; Fish-only playback never needs it.
        if self._encoder is None:
            self._encoder = discord.opus.Encoder()
        return self._encoder.encode(pcm, discord.opus.Encoder.SAMPLES_PER_FRAME)

    def _check_frame(self, frame: bytes) -> None:
        if len(frame) == PCM_FRAME_BYTES:
            return
        if not self._opus:
            raise ValueError(f"PCM frame must be {PCM_FRAME_BYTES} bytes, got {len(frame)}")
        require_discord_frame(frame)

    def is_opus(self) -> bool:
        return self._opus

    def enqueue_frames(self, frames: list[bytes]) -> None:
        # A stopped source is never read again, so frames queued now would
        # never drain and wait_until_drained() would never return.
        if not frames or self._stopped.is_set():
            return
        for frame in frames:
            self._check_frame(frame)
        with self._lock:
            self._pending_frames += len(frames)
            self._drained.clear()
        for frame in frames:
            self.frames.put(frame)

    def clear(self) -> int:
        """Drop the frames not played yet, so a cleared queue also cuts the
        phrase being spoken; the player goes on with idle frames. Returns
        how many frames were dropped."""
        dropped = 0
        with self._lock:
            while True:
                try:
                    self.frames.get_nowait()
                except thread_queue.Empty:
                    break
                dropped += 1
            self._pending_frames = 0
            self._drained.set()
        return dropped

    async def wait_until_drained(self, timeout: float | None = None) -> bool:
        return await asyncio.to_thread(self._drained.wait, timeout)

    def stop(self) -> None:
        self._stopped.set()
        self._drained.set()

    def cleanup(self) -> None:
        self.stop()

    @property
    def stopped(self) -> bool:
        return self._stopped.is_set()

    @property
    def is_drained(self) -> bool:
        return self._drained.is_set()
