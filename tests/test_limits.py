"""Tests for the runtime text-length limit and the audio playback guard."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from ttsbot import voice_registry as vr
from test_bot import load_bot_module, temp_config_store


def _minimax_voice(speed: float = 1.0):
    return vr.VoiceRecord(
        name="bussshy01", label="B", description="", provider="minimax",
        minimax=vr.MiniMaxParams(voice_id="bussshy01", speed=speed),
    )


def _piper_voice(length_scale: float = 1.0):
    return vr.VoiceRecord(
        name="piper-ruslan", label="R", description="", provider="piper",
        piper=vr.PiperParams(model_path="/m.onnx", length_scale=length_scale),
    )


class FrameLimitMathTests(unittest.TestCase):
    def setUp(self):
        self.bot_mod = load_bot_module()  # reload() -> clean config
        from ttsbot import pipeline, config
        self.pipeline = pipeline
        self.config = config
        config.TTS_AUDIO_CHARS_PER_SECOND = 10.0
        config.TTS_AUDIO_LIMIT_SAFETY = 2.0
        config.TTS_AUDIO_LIMIT_MIN_SECONDS = 5.0

    def tearDown(self):
        self.config.reload()

    def test_disabled_returns_none(self):
        self.config.TTS_AUDIO_LIMIT_ENABLED = False
        self.assertIsNone(self.pipeline.playback_frame_limit("привет" * 20))

    def test_basic_estimate_with_safety(self):
        # 100 chars / 10 cps * 2.0 safety = 20s = 1000 frames of 20ms
        limit = self.pipeline.playback_frame_limit("а" * 100, _minimax_voice())
        self.assertEqual(limit, 1000)

    def test_min_seconds_floor_for_short_text(self):
        # 5 chars would be 1s; the 5s floor wins => 250 frames
        limit = self.pipeline.playback_frame_limit("абвгд", _minimax_voice())
        self.assertEqual(limit, 250)

    def test_minimax_speed_shrinks_the_budget(self):
        # Same text, speed 2.0 => half the duration allowed
        slow = self.pipeline.playback_frame_limit("а" * 100, _minimax_voice(speed=1.0))
        fast = self.pipeline.playback_frame_limit("а" * 100, _minimax_voice(speed=2.0))
        self.assertEqual(fast * 2, slow)

    def test_piper_length_scale_grows_the_budget(self):
        # length_scale 2.0 = twice slower speech = twice the budget
        normal = self.pipeline.playback_frame_limit("а" * 100, _piper_voice())
        slow = self.pipeline.playback_frame_limit("а" * 100, _piper_voice(length_scale=2.0))
        self.assertEqual(slow, normal * 2)

    def test_limit_pcm_frames_truncates(self):
        frames = [b"x"] * 2000
        out = self.pipeline.limit_pcm_frames(frames, "а" * 100, _minimax_voice())
        self.assertEqual(len(out), 1000)

    def test_limit_pcm_frames_keeps_short_audio(self):
        frames = [b"x"] * 100
        out = self.pipeline.limit_pcm_frames(frames, "а" * 100, _minimax_voice())
        self.assertEqual(len(out), 100)


class MaxCharsSettingTests(unittest.TestCase):
    def test_set_tts_max_chars_applies_and_persists(self):
        bot_mod = load_bot_module()
        store = temp_config_store(bot_mod)
        store.set_tts_max_chars(800)
        self.assertEqual(bot_mod.config.TTS_MAX_CHARS, 800)
        # the hard normalization ceiling must not silently undercut the limit
        self.assertGreaterEqual(bot_mod.config.MAX_TEXT_LENGTH, 800)

        # a fresh store from the same file re-applies the override
        bot_mod.config.reload()
        self.assertNotEqual(bot_mod.config.TTS_MAX_CHARS, 800)
        reloaded = bot_mod.BotConfigStore(store.path, set())
        self.assertEqual(reloaded.settings.get("tts_max_chars"), 800)
        self.assertEqual(bot_mod.config.TTS_MAX_CHARS, 800)
        bot_mod.config.reload()

    def test_lowering_limit_does_not_lower_hard_ceiling(self):
        bot_mod = load_bot_module()
        store = temp_config_store(bot_mod)
        original_ceiling = bot_mod.config.MAX_TEXT_LENGTH
        store.set_tts_max_chars(100)
        self.assertEqual(bot_mod.config.TTS_MAX_CHARS, 100)
        self.assertEqual(bot_mod.config.MAX_TEXT_LENGTH, original_ceiling)
        bot_mod.config.reload()


class SlashLimitCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_slash_limit_sets_and_reports(self):
        bot_mod = load_bot_module()
        bot_mod.bot.config_store = temp_config_store(bot_mod)
        interaction = types.SimpleNamespace(
            guild=types.SimpleNamespace(id=100),
            user=types.SimpleNamespace(id=1),
            response=types.SimpleNamespace(send_message=AsyncMock()),
        )
        with patch.object(
            bot_mod.tts_commands, "require_guild_manager", AsyncMock(return_value=True)
        ):
            await bot_mod.slash_tts_limit.callback(interaction, 700)
        self.assertEqual(bot_mod.config.TTS_MAX_CHARS, 700)
        msg = interaction.response.send_message.await_args.args[0]
        self.assertIn("700", msg)

        with patch.object(
            bot_mod.tts_commands, "require_guild_manager", AsyncMock(return_value=True)
        ):
            await bot_mod.slash_tts_limit.callback(interaction, None)
        msg = interaction.response.send_message.await_args.args[0]
        self.assertIn("700", msg)
        bot_mod.config.reload()


def _make_mp3(seconds: float = 0.4) -> bytes:
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
        "-ac", "2", "-ar", "44100", "-f", "mp3", "pipe:1",
    ]
    return subprocess.run(cmd, capture_output=True, check=True).stdout


_MP3: bytes | None = None
if shutil.which("ffmpeg"):
    try:
        _MP3 = _make_mp3(0.4)
    except Exception:  # pragma: no cover
        _MP3 = None


async def _stutter_forever(data: bytes):
    """Simulate the MiniMax stutter loop: the API never stops sending audio."""
    while True:
        yield data
        await asyncio.sleep(0)


class FakeSource:
    def __init__(self, frame_bytes: int):
        self.frame_bytes = frame_bytes
        self.frames: list[bytes] = []

    def enqueue_frames(self, frames):
        self.frames.extend(frames)

    async def wait_until_drained(self, timeout=None):
        return True


@unittest.skipUnless(_MP3, "ffmpeg required")
class StutterGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_infinite_stream_is_cut_at_the_frame_limit(self):
        bot_mod = load_bot_module()
        tts_bot = bot_mod.TTSBot()
        cloud = MagicMock()
        cloud.stream_audio = lambda *a, **k: _stutter_forever(_MP3)
        tts_bot.tts_dispatcher._cloud = cloud

        cfg = bot_mod.config
        cfg.TTS_AUDIO_CHARS_PER_SECOND = 12.0
        cfg.TTS_AUDIO_LIMIT_SAFETY = 1.0
        cfg.TTS_AUDIO_LIMIT_MIN_SECONDS = 0.2

        ch = types.SimpleNamespace(id=10, guild=types.SimpleNamespace(id=1))
        job = bot_mod.TTSJob("привет, это стрим", ch, 0.0, 1, 1, 1000, "bussshy01", 0.0)
        from ttsbot.pipeline import playback_frame_limit
        limit = playback_frame_limit(job.text, _minimax_voice())

        source = FakeSource(bot_mod.PCM_FRAME_BYTES)
        try:
            status, frames = await asyncio.wait_for(
                tts_bot._stream_tts_to_source(source, _minimax_voice(), job),
                timeout=15,
            )
        finally:
            cfg.reload()
        self.assertEqual(status, "truncated")
        self.assertLessEqual(frames, limit)
        self.assertEqual(len(source.frames), frames)

    async def test_prefetch_infinite_stream_is_cut_at_the_frame_limit(self):
        bot_mod = load_bot_module()
        tts_bot = bot_mod.TTSBot()
        cloud = MagicMock()
        cloud.stream_audio = lambda *a, **k: _stutter_forever(_MP3)
        tts_bot.tts_dispatcher._cloud = cloud

        cfg = bot_mod.config
        cfg.TTS_AUDIO_CHARS_PER_SECOND = 12.0
        cfg.TTS_AUDIO_LIMIT_SAFETY = 1.0
        cfg.TTS_AUDIO_LIMIT_MIN_SECONDS = 0.2

        ch = types.SimpleNamespace(id=10, guild=types.SimpleNamespace(id=1))
        job = bot_mod.TTSJob("привет, это стрим", ch, 0.0, 1, 1, 1000, "bussshy01", 0.0)
        from ttsbot.pipeline import playback_frame_limit
        limit = playback_frame_limit(job.text, _minimax_voice())

        prepared = bot_mod.PreparedAudio(job=job, channel=asyncio.Queue())
        try:
            status, frames = await asyncio.wait_for(
                tts_bot._stream_to_channel(prepared, _minimax_voice()),
                timeout=15,
            )
        finally:
            cfg.reload()
        self.assertEqual(status, "truncated")
        self.assertLessEqual(frames, limit)


if __name__ == "__main__":
    unittest.main()
