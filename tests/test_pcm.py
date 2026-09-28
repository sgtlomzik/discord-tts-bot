"""PcmFramer: resampling, framing, chunk-boundary continuity, gain, speed."""

import random
import tempfile
import time
import unittest
from pathlib import Path

import numpy as np

from ttsbot.audio import PCM_FRAME_BYTES
from ttsbot.pcm import (
    PcmFramer, apply_gain, pcm_cache_header, pcm_to_frames, read_pcm_cache,
)


def _sine(freq=1000.0, seconds=1.0, rate=24000, amplitude=0.5) -> bytes:
    t = np.arange(int(rate * seconds)) / rate
    return (np.sin(2 * np.pi * freq * t) * amplitude * 32767).astype("<i2").tobytes()


def _left(frames: list[bytes]) -> np.ndarray:
    return np.frombuffer(b"".join(frames), dtype="<i2").reshape(-1, 2)[:, 0].astype(np.float64)


class PcmFramerTests(unittest.TestCase):
    def test_every_frame_is_one_discord_frame_and_length_doubles(self):
        pcm = _sine(seconds=1.0)
        frames = pcm_to_frames(pcm, 24000)
        self.assertTrue(all(len(frame) == PCM_FRAME_BYTES for frame in frames))
        # 24k -> 48k: 24000 input samples become 48000 output samples = 50 frames.
        self.assertEqual(len(frames), 50)

    def test_arbitrary_chunking_matches_one_shot(self):
        pcm = _sine(freq=440, seconds=0.7)
        whole = pcm_to_frames(pcm, 24000)
        rng = random.Random(7)
        framer = PcmFramer(24000)
        pieces, offset = [], 0
        while offset < len(pcm):
            size = rng.randint(1, 3001)  # odd sizes split samples
            pieces.extend(framer.feed(pcm[offset:offset + size]))
            offset += size
        pieces.extend(framer.flush())
        self.assertEqual(b"".join(pieces), b"".join(whole))

    def test_silence_stays_silent(self):
        frames = pcm_to_frames(b"\x00" * 9600, 24000)
        self.assertEqual(set(b"".join(frames)), {0})

    def test_sine_keeps_frequency_and_amplitude(self):
        left = _left(pcm_to_frames(_sine(freq=1000, seconds=1.0, amplitude=0.5), 24000))
        body = left[4800:-4800]  # skip resampler edges
        spectrum = np.abs(np.fft.rfft(body * np.hanning(len(body))))
        peak_hz = np.argmax(spectrum) * 48000 / len(body)
        self.assertAlmostEqual(peak_hz, 1000, delta=5)
        rms_db = 20 * np.log10(np.sqrt(np.mean(body ** 2)) / (0.5 * 32767 / np.sqrt(2)))
        self.assertLess(abs(rms_db), 1.0)

    def test_stereo_channels_are_identical(self):
        data = np.frombuffer(b"".join(pcm_to_frames(_sine(), 24000)), dtype="<i2").reshape(-1, 2)
        self.assertTrue(np.array_equal(data[:, 0], data[:, 1]))

    def test_gain_scales_and_saturates(self):
        quiet = _left(pcm_to_frames(_sine(amplitude=0.25), 24000, volume_db=6.0))
        reference = _left(pcm_to_frames(_sine(amplitude=0.25), 24000))
        ratio_db = 20 * np.log10(np.sqrt(np.mean(quiet ** 2)) / np.sqrt(np.mean(reference ** 2)))
        self.assertAlmostEqual(ratio_db, 6.0, delta=0.2)
        loud = _left(pcm_to_frames(_sine(amplitude=0.9), 24000, volume_db=20.0))
        self.assertLessEqual(loud.max(), 32767)
        self.assertGreaterEqual(loud.min(), -32768)
        self.assertEqual(loud.max(), 32767)  # clipped, not wrapped

    def test_apply_gain_on_raw_pcm(self):
        pcm = np.array([1000, -1000, 30000], dtype="<i2").tobytes()
        out = np.frombuffer(apply_gain(pcm, 6.0), dtype="<i2")
        self.assertEqual(out[0], 1995)
        self.assertEqual(out[2], 32767)
        self.assertEqual(apply_gain(pcm, 0.0), pcm)

    def test_48k_input_skips_resampling(self):
        pcm = _sine(rate=48000, seconds=0.1)
        frames = pcm_to_frames(pcm, 48000)
        left = np.frombuffer(b"".join(frames), dtype="<i2").reshape(-1, 2)[:, 0]
        self.assertTrue(np.array_equal(left[:4800], np.frombuffer(pcm, dtype="<i2")))

    def test_other_input_rates_are_accepted(self):
        for rate in (16000, 44100):
            frames = pcm_to_frames(_sine(rate=rate, seconds=0.5), rate)
            self.assertEqual(len(frames), 25)

    def test_feed_after_flush_is_an_error(self):
        framer = PcmFramer(24000)
        framer.flush()
        with self.assertRaises(RuntimeError):
            framer.feed(b"\x00\x00")

    def test_under_one_millisecond_per_90ms_chunk(self):
        chunk = _sine(seconds=0.09)
        framer = PcmFramer(24000)
        for _ in range(5):
            framer.feed(chunk)
        started = time.perf_counter()
        runs = 200
        for _ in range(runs):
            framer.feed(chunk)
        self.assertLess((time.perf_counter() - started) / runs, 0.001)

    def test_cache_file_roundtrip_and_rejects_foreign_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.pcm"
            path.write_bytes(pcm_cache_header(24000, 1) + b"\x01\x02")
            self.assertEqual(read_pcm_cache(path), (24000, 1, b"\x01\x02"))
            path.write_bytes(b"OggS-not-pcm-at-all")
            with self.assertRaises(ValueError):
                read_pcm_cache(path)


if __name__ == "__main__":
    unittest.main()
