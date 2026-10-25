"""The ffmpeg decoder path survives a full disk and a cancellation.

A failed write to the cache file inside the feeder aborted feeding
ffmpeg, cut the audio and counted against the provider; cancelling the
task left the feeder (and with it ffmpeg) waiting forever.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from test_bot_streaming import _MP3, _chunks, _minimax_voice
from ttsbot.audio import load_opus
from ttsbot.models import PreparedAudio, TTSJob
from ttsbot.pipeline import DISCORD_OPUS_CACHE_SUFFIX, SynthesisPipelineMixin
from ttsbot.providers import TTSCacheConfig, TTSDispatcher, TTSPhraseCache


def _prepared():
    channel = types.SimpleNamespace(id=10, guild=types.SimpleNamespace(id=1))
    return PreparedAudio(job=TTSJob("привет", channel, 0.0, 1, 1, 1000, "bussshy01", 0.0), channel=asyncio.Queue())


class FullDisk:
    """A cache part file whose writes fail like a full disk."""

    def write(self, data):
        raise OSError(28, "No space left on device")

    def close(self):
        pass


@unittest.skipUnless(_MP3, "ffmpeg required")
class DecodeStreamTests(unittest.IsolatedAsyncioTestCase):
    def _pipeline(self, cache=None):
        pipeline = SynthesisPipelineMixin()
        pipeline.tts_dispatcher = TTSDispatcher(local=None, cache=cache)
        return pipeline

    async def test_a_full_disk_only_stops_caching(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = TTSPhraseCache(TTSCacheConfig(enabled=True, cache_dir=Path(tmp)))
            pipeline = self._pipeline(cache)
            prepared = _prepared()
            real_open = Path.open

            def open_(path, mode="r", *args, **kwargs):
                return FullDisk() if path.suffix == ".tmp" else real_open(path, mode, *args, **kwargs)

            with patch.object(Path, "open", open_):
                status, frames = await pipeline._decode_stream_to_channel(
                    prepared, _minimax_voice(), _chunks(_MP3), "mp3", "key", "mp3", ttfa_timeout=5,
                )
            self.assertEqual(status, "ok")
            self.assertGreater(frames, 15)  # the whole 0.4 s clip
            self.assertIsNone(prepared.error)
            self.assertIsNone(cache.lookup("привет", "key"))
            self.assertEqual(list(Path(tmp).glob("*.tmp")), [])

    async def test_cancelling_does_not_hang_on_a_stalled_stream(self):
        stalled = asyncio.Event()

        async def first_then_stall():
            yield _MP3[: len(_MP3) // 3]
            await stalled.wait()
            yield b""

        pipeline = self._pipeline()
        prepared = _prepared()
        task = asyncio.create_task(pipeline._decode_stream_to_channel(
            prepared, _minimax_voice(), first_then_stall(), "mp3", "key", "mp3", ttfa_timeout=5,
        ))
        await asyncio.sleep(0.5)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 3)

    async def test_a_cache_file_that_fails_to_close_is_not_committed(self):
        # close() flushes the buffered tail; if that fails the file on disk
        # is cut short and must never be served from the cache.
        with tempfile.TemporaryDirectory() as tmp:
            cache = TTSPhraseCache(TTSCacheConfig(enabled=True, cache_dir=Path(tmp)))
            prepared = _prepared()
            with _cache_files_on(BrokenDisk.failing_close):
                status, _ = await self._pipeline(cache)._decode_stream_to_channel(
                    prepared, _minimax_voice(), _chunks(_MP3), "mp3", "key", "mp3", ttfa_timeout=5,
                )
            self.assertEqual(status, "ok")
            self.assertIsNone(cache.lookup("привет", "key"))
            self.assertEqual(list(Path(tmp).glob("*.tmp")), [])


class BrokenDisk:
    """A real cache part file on a disk that fills up: writes after the
    first ``ok`` fail, and close() fails too once a write has failed (it
    flushes the buffered data again), or always with ``fail_close``."""

    def __init__(self, file, ok=10**9, fail_close=False):
        self._file, self._ok, self._failed = file, ok, fail_close

    @classmethod
    def after(cls, ok):
        return lambda file: cls(file, ok=ok)

    @classmethod
    def failing_close(cls, file):
        return cls(file, fail_close=True)

    def write(self, data):
        if self._ok <= 0:
            self._failed = True
            raise OSError(28, "No space left on device")
        self._ok -= 1
        return self._file.write(data)

    def close(self):
        self._file.close()
        if self._failed:
            raise OSError(28, "No space left on device")


def _cache_files_on(disk):
    real_open = Path.open

    def open_(path, mode="r", *args, **kwargs):
        file = real_open(path, mode, *args, **kwargs)
        return disk(file) if path.suffix == ".tmp" else file

    return patch.object(Path, "open", open_)


def _ogg(duration=0.5) -> bytes:
    return subprocess.check_output([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
        f"sine=frequency=440:duration={duration}", "-ac", "1", "-c:a", "libopus",
        "-b:a", "64k", "-frame_duration", "20", "-f", "ogg", "pipe:1",
    ])


class FullCacheDiskTests(unittest.IsolatedAsyncioTestCase):
    """A full cache disk stops caching the message, on every stream path;
    the audio plays on and the provider is not blamed."""

    def _pipeline(self, cache):
        pipeline = SynthesisPipelineMixin()
        pipeline.tts_dispatcher = TTSDispatcher(local=None, cache=cache)
        return pipeline

    @unittest.skipUnless(_MP3 and load_opus(), "ffmpeg and libopus required")
    async def test_the_direct_opus_path(self):
        ogg = _ogg()
        with tempfile.TemporaryDirectory() as tmp:
            cache = TTSPhraseCache(TTSCacheConfig(enabled=True, cache_dir=Path(tmp)))
            prepared = _prepared()
            with _cache_files_on(BrokenDisk.after(3)):
                status, frames = await self._pipeline(cache)._stream_ogg_opus_to_channel(
                    prepared, _minimax_voice(), _chunks(ogg, 5), "key", ttfa_timeout=5, label="Test",
                )
            self.assertEqual(status, "ok")
            self.assertGreater(frames, 20)
            self.assertIsNone(prepared.error)
            self.assertIsNone(cache.lookup("привет", "key" + DISCORD_OPUS_CACHE_SUFFIX))
            self.assertEqual(list(Path(tmp).glob("*.tmp")), [])

    async def test_the_pcm_path(self):
        pcm = b"\x10\x00" * 24000  # 1 s of 24 kHz mono

        class Stream:
            rate, channels = 24000, 1

            async def chunks(self):
                for offset in range(0, len(pcm), 4800):
                    yield pcm[offset:offset + 4800]

            async def aclose(self):
                pass

        async def open_stream():
            return Stream()

        with tempfile.TemporaryDirectory() as tmp:
            cache = TTSPhraseCache(TTSCacheConfig(enabled=True, cache_dir=Path(tmp)))
            prepared = _prepared()
            with _cache_files_on(BrokenDisk.after(3)):
                status, frames = await self._pipeline(cache)._stream_pcm_to_channel(
                    prepared, _minimax_voice(), open_stream, "key", budget=5, label="Test",
                )
            self.assertEqual(status, "ok")
            self.assertEqual(frames, 50)
            self.assertIsNone(prepared.error)
            self.assertIsNone(cache.lookup("привет", "key"))
            self.assertEqual(list(Path(tmp).glob("*.tmp")), [])


class FfmpegFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_decoder_that_fails_mid_stream_counts_against_the_provider(self):
        # Audio that turns undecodable part-way: ffmpeg gave some frames,
        # then exited with an error. That is a failed stream, not a success.
        decoder = [sys.executable, "-c", (
            "import sys; sys.stdout.buffer.write(bytes(3840 * 10)); sys.stdout.flush();"
            " sys.stdin.buffer.read(); sys.exit(1)"
        )]
        pipeline = SynthesisPipelineMixin()
        pipeline.tts_dispatcher = TTSDispatcher(local=None, cache=None)
        prepared = _prepared()
        with patch("ttsbot.pipeline.build_tts_stream_pcm_command", return_value=decoder):
            with self.assertLogs("tts_bot", "WARNING"):
                status, frames = await pipeline._decode_stream_to_channel(
                    prepared, _minimax_voice(), _chunks(b"\xff" * 3000), "mp3", "key", "mp3", ttfa_timeout=5,
                )
        self.assertEqual((status, frames), ("truncated", 10))
        self.assertIsNotNone(prepared.error)


if __name__ == "__main__":
    unittest.main()
