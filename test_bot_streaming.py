"""Tests for the streaming worker path (B2): ffmpeg pump + fallback semantics."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import voice_registry as vr
from test_bot import load_bot_module
from tts_providers import TTSCacheConfig, TTSPhraseCache


async def _chunks_then_raise(data: bytes):
    third = max(1, len(data) // 3)
    yield data[:third]
    raise RuntimeError("mid-stream boom")


def _minimax_voice(name="bussshy01"):
    return vr.VoiceRecord(
        name=name, label="B", description="", provider="minimax",
        minimax=vr.MiniMaxParams(voice_id="bussshy01"),
    )


def _piper_voice(name="piper-ruslan"):
    return vr.VoiceRecord(
        name=name, label="R", description="", provider="piper",
        piper=vr.PiperParams(model_path="/m.onnx"),
    )


def _job(bot_mod):
    ch = types.SimpleNamespace(id=10, guild=types.SimpleNamespace(id=1))
    return bot_mod.TTSJob("привет, это стрим", ch, 0.0, 1, 1, 1000, "bussshy01", 0.0)


def _make_mp3(seconds: float = 0.4) -> bytes:
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
        "-ac", "2", "-ar", "44100", "-f", "mp3", "pipe:1",
    ]
    return subprocess.run(cmd, capture_output=True, check=True).stdout


# Generate the sample MP3 once at import (sync) so the async tests do not
# block the event loop spawning ffmpeg.
_MP3: bytes | None = None
if shutil.which("ffmpeg"):
    try:
        _MP3 = _make_mp3(0.4)
    except Exception:  # pragma: no cover
        _MP3 = None


async def _chunks(data: bytes, n: int = 3):
    step = max(1, len(data) // n)
    for i in range(0, len(data), step):
        yield data[i:i + step]


async def _raise_before_first():
    if True:
        raise RuntimeError("boom before first chunk")
    yield b""  # pragma: no cover - makes this an async generator


class FakeSource:
    def __init__(self, frame_bytes: int):
        self.frame_bytes = frame_bytes
        self.frames: list[bytes] = []

    def enqueue_frames(self, frames):
        for f in frames:
            assert len(f) == self.frame_bytes, f"bad frame size {len(f)}"
        self.frames.extend(frames)

    async def wait_until_drained(self, timeout=None):
        return True


@unittest.skipUnless(_MP3, "ffmpeg required")
class StreamPumpTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_ffmpeg_pump_enqueues_frames_ok(self):
        bot_mod = load_bot_module()
        tts_bot = bot_mod.TTSBot()
        mp3 = _MP3
        self.assertGreater(len(mp3), 100)

        cloud = MagicMock()
        cloud.stream_audio = lambda *a, **k: _chunks(mp3)
        tts_bot.tts_dispatcher._cloud = cloud

        source = FakeSource(bot_mod.PCM_FRAME_BYTES)
        status, frames = await tts_bot._stream_tts_to_source(
            source, _minimax_voice(), _job(bot_mod)
        )
        self.assertEqual(status, "ok")
        self.assertGreater(frames, 0)
        self.assertEqual(len(source.frames), frames)
        # ~0.4s of 20ms frames => ~20 frames (allow slack for codec padding)
        self.assertGreater(frames, 10)

    async def test_long_stream_not_truncated_by_ttfa_timeout(self):
        # First chunk is immediate (under TTFA budget); the rest arrive
        # slowly, well past the budget. The TTFA timeout must apply ONLY to
        # the first chunk, so the long tail still streams to completion.
        bot_mod = load_bot_module()
        tts_bot = bot_mod.TTSBot()
        mp3 = _MP3

        async def _slow_tail(*a, **k):
            third = max(1, len(mp3) // 3)
            yield mp3[:third]                # immediate first chunk
            await asyncio.sleep(0.25)        # gap > TTFA budget below
            yield mp3[third:2 * third]
            await asyncio.sleep(0.25)
            yield mp3[2 * third:]

        cloud = MagicMock()
        cloud.stream_audio = lambda *a, **k: _slow_tail()
        tts_bot.tts_dispatcher._cloud = cloud

        original = bot_mod.config.TTS_STREAM_TTFA_TIMEOUT
        bot_mod.config.TTS_STREAM_TTFA_TIMEOUT = 0.1  # tiny: would kill the tail if misapplied
        try:
            source = FakeSource(bot_mod.PCM_FRAME_BYTES)
            status, frames = await tts_bot._stream_tts_to_source(
                source, _minimax_voice(), _job(bot_mod)
            )
        finally:
            bot_mod.config.TTS_STREAM_TTFA_TIMEOUT = original

        self.assertEqual(status, "ok")
        self.assertGreater(frames, 10)  # full ~0.4s clip decoded despite slow tail

    def _enable_cache(self, bot_mod, tts_bot):
        d = tempfile.mkdtemp()
        cache = TTSPhraseCache(TTSCacheConfig(enabled=True, cache_dir=Path(d)))
        tts_bot.tts_dispatcher._cache = cache
        return cache

    async def test_clean_stream_commits_to_cache(self):
        bot_mod = load_bot_module()
        tts_bot = bot_mod.TTSBot()
        cache = self._enable_cache(bot_mod, tts_bot)
        cloud = MagicMock()
        cloud.stream_audio = lambda *a, **k: _chunks(_MP3)
        tts_bot.tts_dispatcher._cloud = cloud

        job = _job(bot_mod)
        source = FakeSource(bot_mod.PCM_FRAME_BYTES)
        status, frames = await tts_bot._stream_tts_to_source(source, _minimax_voice(), job)
        self.assertEqual(status, "ok")
        # committed: a repeat would hit the cache
        self.assertIsNotNone(cache.lookup(job.text, "bussshy01"))
        self.assertGreater(cache.total_bytes, 0)

    async def test_partial_stream_not_cached(self):
        bot_mod = load_bot_module()
        tts_bot = bot_mod.TTSBot()
        cache = self._enable_cache(bot_mod, tts_bot)
        cloud = MagicMock()
        cloud.stream_audio = lambda *a, **k: _chunks_then_raise(_MP3)
        tts_bot.tts_dispatcher._cloud = cloud

        job = _job(bot_mod)
        source = FakeSource(bot_mod.PCM_FRAME_BYTES)
        status, frames = await tts_bot._stream_tts_to_source(source, _minimax_voice(), job)
        self.assertIn(status, ("truncated", "pre_audio"))
        # partial output must NOT be cached
        self.assertIsNone(cache.lookup(job.text, "bussshy01"))

    async def test_cache_hit_plays_from_file_no_api(self):
        bot_mod = load_bot_module()
        tts_bot = bot_mod.TTSBot()
        cache = self._enable_cache(bot_mod, tts_bot)
        job = _job(bot_mod)
        # Pre-store a real MP3 for this (text, voice).
        src = Path(tempfile.mkdtemp()) / "seed.mp3"
        src.write_bytes(_MP3)
        cache.store(job.text, src, "bussshy01")

        cloud = MagicMock()
        cloud.stream_audio = MagicMock(side_effect=AssertionError("API hit on cache!"))
        tts_bot.tts_dispatcher._cloud = cloud
        tts_bot.ensure_voice = AsyncMock(return_value=MagicMock())
        tts_bot.ensure_continuous_player = MagicMock(
            return_value=FakeSource(bot_mod.PCM_FRAME_BYTES)
        )
        tts_bot.schedule_continuous_idle_stop = MagicMock()
        tts_bot.schedule_idle_disconnect = MagicMock()

        out = await tts_bot._run_streaming_job(job, _minimax_voice(), 0.0)
        self.assertEqual(out, "done")
        cloud.stream_audio.assert_not_called()  # served from disk, no API
        self.assertEqual(cache.hits, 1)

    async def test_pre_audio_when_stream_raises_before_first_chunk(self):
        bot_mod = load_bot_module()
        tts_bot = bot_mod.TTSBot()
        cloud = MagicMock()
        cloud.stream_audio = lambda *a, **k: _raise_before_first()
        tts_bot.tts_dispatcher._cloud = cloud

        source = FakeSource(bot_mod.PCM_FRAME_BYTES)
        status, frames = await tts_bot._stream_tts_to_source(
            source, _minimax_voice(), _job(bot_mod)
        )
        self.assertEqual(status, "pre_audio")
        self.assertEqual(frames, 0)
        self.assertEqual(source.frames, [])


class StreamFallbackSemanticsTests(unittest.IsolatedAsyncioTestCase):
    def _bot_with_cloud(self):
        bot_mod = load_bot_module()
        tts_bot = bot_mod.TTSBot()
        tts_bot.tts_dispatcher._cloud = MagicMock()
        tts_bot.ensure_voice = AsyncMock(return_value=MagicMock())
        tts_bot.ensure_continuous_player = MagicMock(
            return_value=FakeSource(bot_mod.PCM_FRAME_BYTES)
        )
        tts_bot.schedule_continuous_idle_stop = MagicMock()
        tts_bot.schedule_idle_disconnect = MagicMock()
        return bot_mod, tts_bot

    def test_should_attempt_stream(self):
        bot_mod, tts_bot = self._bot_with_cloud()
        self.assertTrue(tts_bot._should_attempt_stream(_minimax_voice()))
        self.assertFalse(tts_bot._should_attempt_stream(_piper_voice()))
        self.assertFalse(tts_bot._should_attempt_stream(None))
        tts_bot.tts_dispatcher._cloud = None
        self.assertFalse(tts_bot._should_attempt_stream(_minimax_voice()))

    async def test_ok_records_success_and_done(self):
        bot_mod, tts_bot = self._bot_with_cloud()
        tts_bot._stream_tts_to_source = AsyncMock(return_value=("ok", 12))
        out = await tts_bot._run_streaming_job(_job(bot_mod), _minimax_voice(), 0.0)
        self.assertEqual(out, "done")
        self.assertEqual(tts_bot.tts_dispatcher.circuit_breaker.consecutive_failures, 0)

    async def test_pre_audio_records_failure_and_fallback(self):
        bot_mod, tts_bot = self._bot_with_cloud()
        tts_bot._stream_tts_to_source = AsyncMock(return_value=("pre_audio", 0))
        out = await tts_bot._run_streaming_job(_job(bot_mod), _minimax_voice(), 0.0)
        self.assertEqual(out, "fallback")
        self.assertEqual(tts_bot.tts_dispatcher.circuit_breaker.consecutive_failures, 1)

    async def test_truncated_records_failure_but_done(self):
        bot_mod, tts_bot = self._bot_with_cloud()
        tts_bot._stream_tts_to_source = AsyncMock(return_value=("truncated", 5))
        out = await tts_bot._run_streaming_job(_job(bot_mod), _minimax_voice(), 0.0)
        self.assertEqual(out, "done")  # do not overlay piper on live audio
        self.assertEqual(tts_bot.tts_dispatcher.circuit_breaker.consecutive_failures, 1)

    async def test_open_breaker_skips_stream_and_falls_back(self):
        bot_mod, tts_bot = self._bot_with_cloud()
        tts_bot.tts_dispatcher.circuit_breaker.allow_request = MagicMock(return_value=False)
        tts_bot._stream_tts_to_source = AsyncMock()
        out = await tts_bot._run_streaming_job(_job(bot_mod), _minimax_voice(), 0.0)
        self.assertEqual(out, "fallback")
        tts_bot._stream_tts_to_source.assert_not_called()


if __name__ == "__main__":
    unittest.main()
