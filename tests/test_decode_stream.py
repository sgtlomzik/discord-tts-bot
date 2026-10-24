"""The ffmpeg decoder path survives a full disk and a cancellation.

A failed write to the cache file inside the feeder aborted feeding
ffmpeg, cut the audio and counted against the provider; cancelling the
task left the feeder (and with it ffmpeg) waiting forever.
"""

from __future__ import annotations

import asyncio
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from test_bot_streaming import _MP3, _chunks, _minimax_voice
from ttsbot.models import PreparedAudio, TTSJob
from ttsbot.pipeline import SynthesisPipelineMixin
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


if __name__ == "__main__":
    unittest.main()
