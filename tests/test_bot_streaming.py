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

from ttsbot import voice_registry as vr
from test_bot import load_bot_module
from ttsbot.providers import TTSCacheConfig, TTSPhraseCache, voice_cache_key
from ttsbot.providers import MiniMaxProvider
from types import MethodType


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
    stopped = False

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
    def _enable_cache(self, bot_mod, tts_bot):
        d = tempfile.mkdtemp()
        cache = TTSPhraseCache(TTSCacheConfig(enabled=True, cache_dir=Path(d)))
        tts_bot.tts_dispatcher._cache = cache
        return cache

    async def test_prefetch_cache_key_follows_voice_tuning(self):
        from dataclasses import replace
        from ttsbot.models import PreparedAudio

        bot_mod = load_bot_module()
        tts_bot = bot_mod.TTSBot()
        cache = self._enable_cache(bot_mod, tts_bot)
        job = _job(bot_mod)
        voice = _minimax_voice()
        src = Path(tempfile.mkdtemp()) / "seed.mp3"
        src.write_bytes(_MP3)
        cache.store(job.text, src, voice_cache_key(voice))
        cloud = MagicMock()
        cloud.stream_audio = MagicMock(side_effect=AssertionError("API hit"))
        cloud.stream_spec = MethodType(MiniMaxProvider.stream_spec, cloud)
        tts_bot.tts_dispatcher.register("minimax", cloud)

        hit = PreparedAudio(job=job, channel=asyncio.Queue())
        self.assertEqual(await tts_bot._stream_voice_into(hit, voice), "cache")
        # A re-tuned voice must not be served the old audio.
        tuned = replace(voice, minimax=replace(voice.minimax, speed=1.3))
        tts_bot.tts_dispatcher.breaker_for("minimax").trip(60)  # the miss must not call the API
        miss = PreparedAudio(job=job, channel=asyncio.Queue())
        self.assertEqual(await tts_bot._stream_voice_into(miss, tuned), "pre_audio")
        cloud.stream_audio.assert_not_called()

class StreamFallbackSemanticsTests(unittest.IsolatedAsyncioTestCase):
    def _bot_with_cloud(self):
        bot_mod = load_bot_module()
        tts_bot = bot_mod.TTSBot()
        tts_bot.tts_dispatcher.register("minimax", MagicMock())
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
        # A bot without MiniMax credentials has no MiniMax provider to stream.
        self.assertFalse(bot_mod.TTSBot()._should_attempt_stream(_minimax_voice()))

if __name__ == "__main__":
    unittest.main()
