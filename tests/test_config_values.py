"""Environment settings: a bad value never stops the bot.

A typo in one numeric setting (``TTS_QUEUE_MAXSIZE=5O``) crashed startup
with a bare ValueError; an unknown idle frame mode fell back to a mode that
is not the default; ``TTS_MAX_CHARS=0`` turned the length cap off although
the docs say the 500-character ceiling still applies; and a cloud voice in
``TTS_DEFAULT_VOICE_PROFILE`` was silently replaced by Piper.
"""

from __future__ import annotations

import os
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from ttsbot import config, models
from ttsbot.pipeline import SynthesisPipelineMixin
from ttsbot.store import BotConfigStore
from ttsbot.textnorm import normalize_for_tts
from support import state_from

NUMERIC_SETTINGS = (
    "TTS_MAX_TEXT_LENGTH", "TTS_MAX_CHARS", "TTS_QUEUE_MAXSIZE", "TTS_STREAM_TTFA_TIMEOUT",
    "TTS_REQUEST_TIMEOUT", "FISH_TTFA_TIMEOUT", "GEMINI_TTFA_TIMEOUT", "GEMINI_TTFA_PER_CHAR",
    "ELEVENLABS_TTFA_TIMEOUT", "TTS_CONNECTION_KEEPALIVE_SECONDS", "TTS_HEALTH_CHECK_INTERVAL",
    "TTS_PREFETCH_LOOKAHEAD", "TTS_IDLE_VOLUME_DB", "TTS_STREAM_TAIL_MS",
    "TTS_MAX_CONTINUOUS_IDLE_SECONDS", "TTS_IDLE_DISCONNECT_SECONDS",
    "TTS_AUTO_CONNECT_SUPPRESS_SECONDS", "TTS_QUEUE_PUT_TIMEOUT_MS", "TTS_AUDIO_CHARS_PER_SECOND",
    "TTS_AUDIO_LIMIT_SAFETY", "TTS_AUDIO_LIMIT_MIN_SECONDS", "RECORDING_CHUNK_SECONDS",
    "RECORDING_IDLE_CLOSE_SECONDS", "RECORDING_PAUSE_MS", "RECORDING_VAD_AGGRESSIVENESS",
    "RECORDING_AUDIO_KEEP_DAYS", "RECORDING_TRANSCRIPT_KEEP_DAYS", "RECORDING_PRUNE_INTERVAL_HOURS",
    "STT_AUDIO_SECONDS_PER_HOUR", "STT_AUDIO_SECONDS_PER_DAY", "STT_MIN_BILLED_SECONDS",
    "STT_REQUESTS_PER_MINUTE", "VOICE_CONNECT_COOLDOWN_SECONDS", "PIPER_SPEAKER",
    "PIPER_LENGTH_SCALE", "WEB_PORT", "WEB_CACHE_MAX_MB", "WEB_MIX_GAP_MS",
    "WEB_MAINTENANCE_SECONDS", "WEB_PREBUILD_RECENT",
)


def _snapshot() -> dict:
    return {name: getattr(config, name) for name in dir(config) if name.isupper()}


class _Registry:
    """The bits of VoiceRegistry the config store looks at."""

    def __init__(self, names, fallback):
        self.names = set(names)
        self.fallback_profile = fallback

    def __contains__(self, name):
        return name in self.names


class EnvSettingTests(unittest.TestCase):
    def tearDown(self):
        config.reload()
        models.reload_voice_profiles()

    def _reload(self, **env):
        clean = {k: v for k, v in os.environ.items() if k not in NUMERIC_SETTINGS}
        clean.update(env)
        with patch.dict(os.environ, clean, clear=True):
            config.reload()
            models.reload_voice_profiles()

    def test_a_malformed_number_falls_back_to_its_default(self):
        self._reload()
        defaults = _snapshot()
        for name in NUMERIC_SETTINGS:
            with self.subTest(name=name):
                with self.assertLogs("tts_bot", "WARNING"):
                    self._reload(**{name: "5O"})  # a typo: letter O for zero
                self.assertEqual(_snapshot(), defaults)

    def test_numbers_keep_their_bounds(self):
        self._reload(TTS_PREFETCH_LOOKAHEAD="0", RECORDING_VAD_AGGRESSIVENESS="9", FISH_TTFA_TIMEOUT="0.1")
        self.assertEqual(config.TTS_PREFETCH_LOOKAHEAD, 1)
        self.assertEqual(config.RECORDING_VAD_AGGRESSIVENESS, 3)
        self.assertEqual(config.FISH_TTFA_TIMEOUT, 0.5)

    def test_an_unknown_idle_frame_mode_uses_the_default(self):
        with patch.dict(os.environ, {"TTS_IDLE_FRAME_MODE": "loud"}):
            config.reload()
        self.assertEqual(config.TTS_IDLE_FRAME_MODE, "silence")

    def test_zero_max_chars_keeps_the_text_length_ceiling(self):
        with patch.dict(os.environ, {"TTS_MAX_CHARS": "0", "TTS_MAX_TEXT_LENGTH": "20"}):
            config.reload()
        self.assertEqual(config.TTS_MAX_CHARS, 20)
        text = normalize_for_tts("слово " * 20, max_chars=config.TTS_MAX_CHARS)
        self.assertLessEqual(len(text), 20)


class CloudDefaultVoiceTests(unittest.TestCase):
    def setUp(self):
        with patch.dict(os.environ, {"TTS_DEFAULT_VOICE_PROFILE": "fish-default"}):
            config.reload()
        models.reload_voice_profiles()

    def tearDown(self):
        config.reload()
        models.reload_voice_profiles()

    def test_a_cloud_default_voice_survives_startup(self):
        self.assertEqual(config.DEFAULT_VOICE_PROFILE, "fish-default")

    def test_new_guilds_get_the_cloud_default_voice(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = _Registry({"piper-ruslan", "fish-default"}, "piper-ruslan")
            store = BotConfigStore(state_from(Path(tmp) / "config.json"), set(), voice_registry=registry)
            self.assertEqual(store.get_guild(1).default_voice, "fish-default")

    def test_the_piper_fallback_stays_a_piper_voice(self):
        self.assertEqual(models.piper_default_profile(), "piper-ruslan")
        bot = types.SimpleNamespace(voice_registry=types.SimpleNamespace(fallback_profile="", get=lambda name: None))
        profile = SynthesisPipelineMixin._resolve_piper_profile(bot, None)
        self.assertEqual(profile.name, "piper-ruslan")


if __name__ == "__main__":
    unittest.main()
