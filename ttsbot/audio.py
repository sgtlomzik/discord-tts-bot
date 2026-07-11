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

log = logging.getLogger("tts_bot")

PCM_SAMPLE_RATE = 48000
PCM_CHANNELS = 2
PCM_SAMPLE_WIDTH = 2
PCM_FRAME_MS = 20
PCM_FRAME_BYTES = int(PCM_SAMPLE_RATE * PCM_FRAME_MS / 1000) * PCM_CHANNELS * PCM_SAMPLE_WIDTH


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



def seconds_from_ms(value_ms: int) -> str:
    return f"{max(value_ms, 0) / 1000:.3f}".rstrip("0").rstrip(".") or "0"


def build_preroll_lavfi_source(mode: str, duration: str) -> str:
    if mode == "sine":
        return f"sine=frequency=180:duration={duration}:sample_rate=48000"
    if mode == "silence":
        return f"anullsrc=r=48000:cl=stereo:d={duration}"
    return f"anoisesrc=d={duration}:c=pink:r=48000"


def build_playback_filter_complex(
    trim_silence: bool,
    preroll_volume_db: float,
) -> str:
    speech_filters = ["aformat=sample_rates=48000:channel_layouts=stereo"]
    if trim_silence:
        speech_filters.append(
            "silenceremove="
            "start_periods=1:start_silence=0.03:start_threshold=-50dB:"
            "stop_periods=-1:stop_duration=0.12:stop_threshold=-50dB"
        )

    return ";".join(
        [
            f"[0:a]{','.join(speech_filters)}[speech]",
            (
                "[1:a]"
                f"volume={preroll_volume_db:g}dB,"
                "aformat=sample_rates=48000:channel_layouts=stereo"
                "[primer]"
            ),
            "[2:a]aformat=sample_rates=48000:channel_layouts=stereo[tail]",
            "[primer][speech][tail]concat=n=3:v=0:a=1[out]",
        ]
    )


def build_playback_prepare_command(source: Path, prepared: Path) -> list[str]:
    preroll_seconds = seconds_from_ms(config.TTS_PREROLL_MS)
    tail_seconds = seconds_from_ms(config.TTS_SILENCE_TAIL_MS)
    filter_complex = build_playback_filter_complex(config.TTS_TRIM_SILENCE, config.TTS_PREROLL_VOLUME_DB)

    return [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-i",
        str(source),
        "-f",
        "lavfi",
        "-i",
        build_preroll_lavfi_source(config.TTS_PREROLL_MODE, preroll_seconds),
        "-f",
        "lavfi",
        "-i",
        f"anullsrc=r=48000:cl=stereo:d={tail_seconds}",
        "-filter_complex",
        filter_complex,
        "-map",
        "[out]",
        "-ar",
        "48000",
        "-ac",
        "2",
        "-c:a",
        "pcm_s16le",
        str(prepared),
    ]


def build_tts_pcm_command(source: Path) -> list[str]:
    audio_filters: list[str] = ["aformat=sample_rates=48000:channel_layouts=stereo"]
    if config.TTS_TRIM_SILENCE:
        audio_filters.append(
            "silenceremove="
            "start_periods=1:start_silence=0.03:start_threshold=-50dB:"
            "stop_periods=-1:stop_duration=0.12:stop_threshold=-50dB"
        )

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


def build_tts_stream_pcm_command() -> list[str]:
    """ffmpeg: decode an MP3 byte stream on stdin to s16le 48k stereo on stdout.

    Used by the streaming path: MiniMax MP3 chunks are written to stdin and
    decoded PCM is read from stdout incrementally. No silence trimming — that
    needs the whole clip, and the continuous player already handles idle.
    """
    return [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-f",
        "mp3",
        "-i",
        "pipe:0",
        "-vn",
        "-af",
        "aformat=sample_rates=48000:channel_layouts=stereo",
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
    def __init__(self, idle_frame: bytes) -> None:
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
            return self.idle_frame

        with self._lock:
            self._pending_frames = max(0, self._pending_frames - 1)
            if self._pending_frames == 0:
                self._drained.set()
        return frame

    def is_opus(self) -> bool:
        return False

    def enqueue_frames(self, frames: list[bytes]) -> None:
        if not frames:
            return
        with self._lock:
            self._pending_frames += len(frames)
            self._drained.clear()
        for frame in frames:
            if len(frame) != PCM_FRAME_BYTES:
                raise ValueError(f"PCM frame must be {PCM_FRAME_BYTES} bytes, got {len(frame)}")
            self.frames.put(frame)

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
