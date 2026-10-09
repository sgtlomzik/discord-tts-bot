"""Keep speech, drop breathing and silence, before packets reach the file.

Discord keeps sending while a microphone picks up breathing or room
noise, and some clients send long runs of digital silence: in the first
live recording 25 of 35 s were silence and 6 s were breathing. Each
packet is decoded (only to classify it; the file keeps the original Opus
packet) and passed through WebRTC's voice activity detector.

``SpeechGate`` turns the per-packet decisions into speech runs: a run
starts when ``start_needed`` of the last ``start_window`` packets are
speech and brings the ``preroll`` packets before it along (the start of
the first syllable), and it ends once no more than ``stop_max`` of the
last ``hangover`` packets are speech (the tail of the last one).
"""

from __future__ import annotations

import collections
from dataclasses import dataclass
from typing import Callable, Generic, TypeVar

import numpy as np

T = TypeVar("T")

_FRAME_BYTES = 960 * 2  # 20 ms of 48 kHz mono s16


@dataclass(frozen=True, slots=True)
class GateSettings:
    preroll: int = 10  # packets (20 ms each)
    start_window: int = 10
    start_needed: int = 6
    hangover: int = 15
    stop_max: int = 2


class SpeechGate(Generic[T]):
    def __init__(self, settings: GateSettings = GateSettings()) -> None:
        self.settings = settings
        self._ring: collections.deque[T] = collections.deque(maxlen=settings.preroll)
        self._recent: collections.deque[bool] = collections.deque(
            maxlen=max(settings.start_window, settings.hangover)
        )
        self.speaking = False

    def reset(self) -> None:
        """Forget the context: the sender stopped transmitting for a while."""
        self._ring.clear()
        self._recent.clear()
        self.speaking = False

    def feed(self, item: T, is_speech: bool) -> list[T]:
        """Return the items to keep, oldest first (none while not speaking)."""
        s = self.settings
        self._recent.append(is_speech)
        if self.speaking:
            if sum(list(self._recent)[-s.hangover:]) <= s.stop_max:
                self.speaking = False
                self._recent.clear()
            return [item]
        self._ring.append(item)
        if sum(list(self._recent)[-s.start_window:]) >= s.start_needed:
            self.speaking = True
            kept = list(self._ring)
            self._ring.clear()
            return kept
        return []


class OpusSpeechDetector:
    """Decode one sender's Opus packets in order and classify each as speech."""

    def __init__(self, aggressiveness: int) -> None:
        import discord.opus
        import webrtcvad

        self._decoder = discord.opus.Decoder()
        self._vad = webrtcvad.Vad(aggressiveness)

    def is_speech(self, packet: bytes) -> bool:
        pcm = self._decoder.decode(packet)
        mono = np.frombuffer(pcm, dtype=np.int16).reshape(-1, 2).mean(axis=1).astype(np.int16).tobytes()
        return any(
            self._vad.is_speech(mono[i:i + _FRAME_BYTES], 48000)
            for i in range(0, len(mono) - _FRAME_BYTES + 1, _FRAME_BYTES)
        )


DetectorFactory = Callable[[], Callable[[bytes], bool]]


def webrtc_detector_factory(aggressiveness: int) -> DetectorFactory:
    return lambda: OpusSpeechDetector(aggressiveness).is_speech
