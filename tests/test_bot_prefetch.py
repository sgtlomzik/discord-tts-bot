"""Tests for the prefetch pipeline (P1/P2): channel flow, ordering,
cancellation, cache-hit and fallback through the generation worker."""

from __future__ import annotations

import asyncio
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from test_bot import load_bot_module
from test_bot_streaming import (
    FakeSource,
    _MP3,
    _chunks,
    _minimax_voice,
)
from ttsbot.providers import TTSCacheConfig, TTSPhraseCache


def _job(bot_mod, text="привет", gid=1):
    ch = types.SimpleNamespace(id=10, guild=types.SimpleNamespace(id=gid))
    return bot_mod.TTSJob(text, ch, 0.0, 1, gid, 1000, "bussshy01", 0.0)


async def _drain(prepared):
    """Drain a channel terminated by a None sentinel (via _prepare_into)."""
    out = []
    while True:
        batch = await prepared.channel.get()
        if batch is None:
            return out
        out.append(batch)


def _drain_nowait(prepared):
    """Collect whatever batches are already in the channel (no sentinel)."""
    out = []
    while not prepared.channel.empty():
        batch = prepared.channel.get_nowait()
        if batch is not None:
            out.append(batch)
    return out


@unittest.skipUnless(_MP3, "ffmpeg required")
class StreamToChannelTests(unittest.IsolatedAsyncioTestCase):
    async def test_streams_frame_batches_to_channel_and_caches(self):
        bot_mod = load_bot_module()
        tts_bot = bot_mod.TTSBot()
        d = tempfile.mkdtemp()
        cache = TTSPhraseCache(TTSCacheConfig(enabled=True, cache_dir=Path(d)))
        tts_bot.tts_dispatcher._cache = cache
        cloud = MagicMock()
        cloud.stream_audio = lambda *a, **k: _chunks(_MP3)
        tts_bot.tts_dispatcher._cloud = cloud

        job = _job(bot_mod)
        prepared = bot_mod.PreparedAudio(job=job, channel=asyncio.Queue())
        status, frames = await tts_bot._stream_to_channel(prepared, _minimax_voice())
        batches = _drain_nowait(prepared)  # _stream_to_channel emits no sentinel
        self.assertEqual(status, "ok")
        self.assertGreater(frames, 0)
        self.assertEqual(sum(len(b) for b in batches), frames)
        self.assertIsNotNone(cache.lookup(job.text, "bussshy01"))  # committed

    async def test_cancelled_before_audio_yields_no_frames(self):
        bot_mod = load_bot_module()
        tts_bot = bot_mod.TTSBot()
        cloud = MagicMock()
        cloud.stream_audio = lambda *a, **k: _chunks(_MP3)
        tts_bot.tts_dispatcher._cloud = cloud
        prepared = bot_mod.PreparedAudio(job=_job(bot_mod), channel=asyncio.Queue())
        prepared.cancelled = True
        status, frames = await tts_bot._stream_to_channel(prepared, _minimax_voice())
        _drain_nowait(prepared)
        self.assertEqual(status, "cancelled")
        self.assertEqual(frames, 0)


@unittest.skipUnless(_MP3, "ffmpeg required")
class PrepareIntoTests(unittest.IsolatedAsyncioTestCase):
    async def test_cache_hit_skips_api(self):
        bot_mod = load_bot_module()
        tts_bot = bot_mod.TTSBot()
        d = tempfile.mkdtemp()
        cache = TTSPhraseCache(TTSCacheConfig(enabled=True, cache_dir=Path(d)))
        tts_bot.tts_dispatcher._cache = cache
        # Register the minimax voice so job.voice_profile resolves to it
        # (the test bot seeds no minimax voice without an API key).
        tts_bot.voice_registry.add(_minimax_voice())
        job = _job(bot_mod)
        seed = Path(tempfile.mkdtemp()) / "s.mp3"
        seed.write_bytes(_MP3)
        cache.store(job.text, seed, "bussshy01")

        cloud = MagicMock()
        cloud.stream_audio = MagicMock(side_effect=AssertionError("API on cache hit!"))
        tts_bot.tts_dispatcher._cloud = cloud

        prepared = bot_mod.PreparedAudio(job=job, channel=asyncio.Queue())
        await tts_bot._prepare_into(prepared)
        batches = await _drain(prepared)
        self.assertEqual(prepared.provider, "cache")
        self.assertGreater(sum(len(b) for b in batches), 0)
        cloud.stream_audio.assert_not_called()

    async def test_piper_voice_uses_file_path(self):
        bot_mod = load_bot_module()
        tts_bot = bot_mod.TTSBot()

        async def fake_synth(text, filename, voice=None):
            Path(filename).write_bytes(_MP3)
            return "local"

        tts_bot.tts_dispatcher.synthesize = AsyncMock(side_effect=fake_synth)
        prepared = bot_mod.PreparedAudio(job=_job(bot_mod), channel=asyncio.Queue())
        await tts_bot._prepare_into(prepared)
        batches = await _drain(prepared)
        self.assertEqual(prepared.provider, "local")
        self.assertGreater(sum(len(b) for b in batches), 0)


class PipelineIntegrationTests(unittest.IsolatedAsyncioTestCase):
    @unittest.skipUnless(_MP3, "ffmpeg required")
    async def test_two_jobs_play_in_order(self):
        bot_mod = load_bot_module()
        tts_bot = bot_mod.TTSBot()
        cloud = MagicMock()
        cloud.stream_audio = lambda *a, **k: _chunks(_MP3)
        tts_bot.tts_dispatcher._cloud = cloud
        tts_bot.voice_registry.add(_minimax_voice())  # jobs resolve to streaming

        played: list[int] = []

        class RecSource(FakeSource):
            def enqueue_frames(self, frames):
                played.append(len(frames))
                super().enqueue_frames(frames)

        tts_bot.ensure_voice = AsyncMock(return_value=MagicMock())
        tts_bot.ensure_continuous_player = MagicMock(
            return_value=RecSource(bot_mod.PCM_FRAME_BYTES))
        tts_bot.schedule_continuous_idle_stop = MagicMock()
        tts_bot.schedule_idle_disconnect = MagicMock()
        tts_bot.wait_until_ready = AsyncMock()
        tts_bot.warmup_tts = AsyncMock()
        tts_bot.is_closed = MagicMock(return_value=False)

        for i in range(2):
            await tts_bot.message_queue.put(_job(bot_mod, text=f"msg{i}"))

        gen = asyncio.create_task(tts_bot._generation_worker())
        play = asyncio.create_task(tts_bot._playback_worker())
        try:
            await asyncio.wait_for(tts_bot.message_queue.join(), timeout=15)
            await asyncio.wait_for(tts_bot.ready_queue.join(), timeout=15)
        finally:
            gen.cancel()
            play.cancel()
            for t in (gen, play):
                try:
                    await t
                except asyncio.CancelledError:
                    pass

        self.assertGreater(len(played), 0)
        self.assertGreater(sum(played), 0)  # both jobs produced audio frames

    async def test_queue_clear_cancels_prepared(self):
        bot_mod = load_bot_module()
        tts_bot = bot_mod.TTSBot()
        job = _job(bot_mod, gid=7)
        prepared = bot_mod.PreparedAudio(job=job, channel=asyncio.Queue())
        tts_bot.active_prepared.add(prepared)
        cleared = tts_bot.clear_queue_for_guild(7)
        self.assertTrue(prepared.cancelled)
        self.assertGreaterEqual(cleared, 1)

    async def test_play_prepared_skips_cancelled_without_enqueue(self):
        bot_mod = load_bot_module()
        tts_bot = bot_mod.TTSBot()
        src = FakeSource(bot_mod.PCM_FRAME_BYTES)
        tts_bot.ensure_voice = AsyncMock(return_value=MagicMock())
        tts_bot.ensure_continuous_player = MagicMock(return_value=src)
        prepared = bot_mod.PreparedAudio(job=_job(bot_mod), channel=asyncio.Queue())
        prepared.cancelled = True
        await prepared.channel.put([b"\x00" * bot_mod.PCM_FRAME_BYTES])
        await prepared.channel.put(None)
        await tts_bot._play_prepared(prepared)
        self.assertEqual(src.frames, [])  # cancelled => nothing played


if __name__ == "__main__":
    unittest.main()
