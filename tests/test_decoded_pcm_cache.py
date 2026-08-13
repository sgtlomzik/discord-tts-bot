"""Tests for the bounded second-level decoded PCM phrase cache."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from test_bot import load_bot_module
from ttsbot.audio import DecodedPCMCache, PCM_FRAME_BYTES


class DecodedPCMCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _source(self, name: str, payload: bytes = b"mp3") -> Path:
        path = self.root / name
        path.write_bytes(payload)
        return path

    def test_lru_is_bounded_and_hits_promote_entries(self):
        cache = DecodedPCMCache(max_bytes=8)
        settings = (200, False)
        first = self._source("first.mp3")
        second = self._source("second.mp3")
        third = self._source("third.mp3")

        cache.store(first, settings, [b"a" * 4])
        cache.store(second, settings, [b"b" * 4])
        self.assertEqual(cache.lookup(first, settings), [b"a" * 4])
        cache.store(third, settings, [b"c" * 4])

        self.assertIsNone(cache.lookup(second, settings))
        self.assertEqual(cache.lookup(first, settings), [b"a" * 4])
        self.assertEqual(cache.lookup(third, settings), [b"c" * 4])
        self.assertLessEqual(cache.total_bytes, 8)
        self.assertEqual(cache.size, 2)

    def test_source_replacement_invalidates_cached_pcm(self):
        cache = DecodedPCMCache(max_bytes=100)
        source = self._source("voice.mp3", b"old")
        settings = (200, True)
        cache.store(source, settings, [b"pcm"])
        self.assertEqual(cache.lookup(source, settings), [b"pcm"])

        source.write_bytes(b"new-and-a-different-size")

        self.assertIsNone(cache.lookup(source, settings))
        self.assertEqual(cache.size, 0)
        self.assertEqual(cache.total_bytes, 0)

    def test_disabled_or_oversized_entries_are_not_retained(self):
        source = self._source("voice.mp3")
        disabled = DecodedPCMCache(max_bytes=0)
        disabled.store(source, (), [b"pcm"])
        self.assertIsNone(disabled.lookup(source, ()))

        small = DecodedPCMCache(max_bytes=3)
        small.store(source, (), [b"four"])
        self.assertEqual(small.size, 0)


class DecodedPCMPlaybackIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_second_persistent_cache_decode_skips_ffmpeg(self):
        bot_mod = load_bot_module()
        tts_bot = bot_mod.TTSBot()
        tts_bot.decoded_pcm_cache = DecodedPCMCache(PCM_FRAME_BYTES * 4)

        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "cached.mp3"
            source.write_bytes(b"fake-mp3")
            proc = MagicMock(returncode=0)
            proc.communicate = AsyncMock(
                return_value=(b"\x01" * PCM_FRAME_BYTES, b"")
            )
            create_proc = AsyncMock(return_value=proc)

            original_tail = bot_mod.config.TTS_STREAM_TAIL_MS
            original_trim = bot_mod.config.TTS_TRIM_SILENCE
            bot_mod.config.TTS_STREAM_TAIL_MS = 0
            bot_mod.config.TTS_TRIM_SILENCE = False
            try:
                with patch(
                    "ttsbot.playback.asyncio.create_subprocess_exec", create_proc
                ):
                    first = await tts_bot.prepare_tts_pcm_frames(
                        source, cache_decoded=True
                    )
                    second = await tts_bot.prepare_tts_pcm_frames(
                        source, cache_decoded=True
                    )
            finally:
                bot_mod.config.TTS_STREAM_TAIL_MS = original_tail
                bot_mod.config.TTS_TRIM_SILENCE = original_trim

        self.assertEqual(first, second)
        self.assertEqual(len(first), 1)
        self.assertEqual(create_proc.await_count, 1)
        self.assertEqual(tts_bot.decoded_pcm_cache.hits, 1)


if __name__ == "__main__":
    unittest.main()
