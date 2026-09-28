"""Raw PCM to Discord frames without ffmpeg.

Providers that return raw s16le PCM (Gemini: 24 kHz mono) are resampled to
48 kHz, upmixed to stereo and cut into 20 ms frames here, in-process. An
ffmpeg pipe buffers raw input (~0.7 s before the first frame with the bot's
flags); this path costs well under a millisecond per chunk.
"""

from __future__ import annotations

import struct
from pathlib import Path

import numpy as np
import soxr

from ttsbot.audio import PCM_CHANNELS, PCM_FRAME_BYTES, PCM_SAMPLE_RATE

# Cache file layout: magic, sample rate, channels, bits per sample, then s16le.
PCM_CACHE_MAGIC = b"RPCM"
_HEADER = struct.Struct("<4sIHH")


class PcmFramer:
    """Incremental s16le PCM -> 48 kHz stereo 20 ms frames.

    ``feed`` accepts chunks of any length, including ones that split a
    sample; the odd byte, the resampler state and the partial frame carry
    over to the next call, so chunk boundaries never produce clicks.
    """

    def __init__(self, in_rate: int, in_channels: int = 1, volume_db: float = 0.0) -> None:
        if in_rate <= 0 or in_channels not in (1, 2):
            raise ValueError(f"Unsupported PCM format: {in_rate} Hz, {in_channels} ch")
        self.in_rate = in_rate
        self.in_channels = in_channels
        self._gain = 10.0 ** (volume_db / 20.0) if volume_db else 1.0
        self._resampler = (
            # float32, not int16: soxr dithers integer output, which turns
            # digital silence into +-1 noise and depends on chunk sizes.
            soxr.ResampleStream(in_rate, PCM_SAMPLE_RATE, in_channels, dtype="float32", quality="HQ")
            if in_rate != PCM_SAMPLE_RATE else None
        )
        self._in_rest = b""   # bytes of an incomplete input sample frame
        self._out_rest = b""  # output bytes short of a whole Discord frame
        self._flushed = False

    def feed(self, chunk: bytes) -> list[bytes]:
        if self._flushed:
            raise RuntimeError("PcmFramer already flushed")
        data = self._in_rest + chunk
        step = 2 * self.in_channels
        usable = len(data) - len(data) % step
        self._in_rest = data[usable:]
        if not usable:
            return []
        samples = np.frombuffer(data[:usable], dtype="<i2").reshape(-1, self.in_channels)
        return self._emit(self._resample(samples.astype(np.float32), last=False))

    def flush(self) -> list[bytes]:
        """Drain the resampler and pad the last frame with silence."""
        if self._flushed:
            return []
        self._flushed = True
        self._in_rest = b""  # half a sample cannot be played
        tail = self._resample(np.zeros((0, self.in_channels), dtype=np.float32), last=True)
        frames = self._emit(tail)
        if self._out_rest:
            frames.append(self._out_rest + b"\x00" * (PCM_FRAME_BYTES - len(self._out_rest)))
            self._out_rest = b""
        return frames

    def _resample(self, samples: np.ndarray, *, last: bool) -> np.ndarray:
        """float32 samples on the int16 scale in, the same at 48 kHz out."""
        if self._resampler is None:
            return samples
        return self._resampler.resample_chunk(samples, last=last).reshape(-1, self.in_channels)

    def _emit(self, samples: np.ndarray) -> list[bytes]:
        if samples.size:
            if self._gain != 1.0:
                samples = samples * self._gain
            samples = np.clip(np.rint(samples), -32768, 32767).astype("<i2")
            if self.in_channels != PCM_CHANNELS:
                samples = np.repeat(samples, PCM_CHANNELS, axis=1)
            data = self._out_rest + samples.tobytes()
        else:
            data = self._out_rest
        whole = len(data) - len(data) % PCM_FRAME_BYTES
        self._out_rest = data[whole:]
        return [data[i:i + PCM_FRAME_BYTES] for i in range(0, whole, PCM_FRAME_BYTES)]


def pcm_to_frames(pcm: bytes, rate: int, channels: int = 1, volume_db: float = 0.0) -> list[bytes]:
    """Convert a whole PCM clip in one go."""
    framer = PcmFramer(rate, channels, volume_db)
    return framer.feed(pcm) + framer.flush()


def apply_gain(pcm: bytes, volume_db: float) -> bytes:
    """Scale s16le samples by ``volume_db`` with saturation."""
    if not volume_db:
        return pcm
    samples = np.frombuffer(pcm[:len(pcm) - len(pcm) % 2], dtype="<i2").astype(np.float32)
    samples *= 10.0 ** (volume_db / 20.0)
    return np.clip(samples, -32768, 32767).astype("<i2").tobytes()


def pcm_cache_header(rate: int, channels: int) -> bytes:
    return _HEADER.pack(PCM_CACHE_MAGIC, rate, channels, 16)


def read_pcm_cache(path: Path) -> tuple[int, int, bytes]:
    """Return ``(rate, channels, pcm)`` from a cache file written with
    ``pcm_cache_header``; raises ValueError on a foreign or corrupt file."""
    data = path.read_bytes()
    if len(data) < _HEADER.size:
        raise ValueError(f"PCM cache file too short: {path}")
    magic, rate, channels, bits = _HEADER.unpack_from(data)
    if magic != PCM_CACHE_MAGIC or bits != 16 or channels not in (1, 2) or rate <= 0:
        raise ValueError(f"Not a PCM cache file: {path}")
    return rate, channels, data[_HEADER.size:]
