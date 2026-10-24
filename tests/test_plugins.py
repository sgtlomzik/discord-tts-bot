"""Cloud providers are plugins: each module builds itself from the env.

The bot's constructor built every provider by hand (Fish config with its
error policy and saved latency, the ElevenLabs key ring, Gemini, MiniMax
through a pipeline method) and seeded their default voices, and close()
shut each down by name. MiniMax lived inside the generic provider module.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import ttsbot.providers
from support import load_bot_module
from ttsbot import plugins
from ttsbot.fish import FishProvider
from ttsbot.minimax import MiniMaxProvider
from ttsbot.models import VOICE_PROFILES
from ttsbot.store import BotConfigStore
from ttsbot.voice_registry import seed_registry
from support import state_from

NO_KEYS = {"MINIMAX_API_KEY": "", "FISH_API_KEY": "", "FISH_REFERENCE_ID": "", "OPENROUTER_API_KEY": "",
           "ELEVENLABS_API_KEY": "", "ELEVENLABS_API_KEYS": "", "ELEVENLABS_VOICE_ID": ""}


class PluginTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.store = BotConfigStore(state_from(Path(self.dir.name) / "config.json"), set())

    def _providers(self, **env):
        with patch.dict(os.environ, {**NO_KEYS, **env}):
            return plugins.create_providers(self.store)

    def test_every_plugin_answers_in_registration_order(self):
        self.assertEqual(list(self._providers()), ["minimax", "fish", "gemini", "elevenlabs"])

    def test_only_configured_providers_are_built(self):
        providers = self._providers(FISH_API_KEY="fk", FISH_REFERENCE_ID="ref")
        self.assertIsInstance(providers["fish"], FishProvider)
        self.assertEqual({name for name, p in providers.items() if p is not None}, {"fish"})

    def test_minimax_is_a_plugin_of_its_own(self):
        providers = self._providers(MINIMAX_API_KEY="mk")
        self.assertIsInstance(providers["minimax"], MiniMaxProvider)
        self.assertFalse(hasattr(ttsbot.providers, "MiniMaxProvider"))

    def test_the_saved_fish_latency_applies(self):
        self.store.set_fish_latency("balanced")
        providers = self._providers(FISH_API_KEY="fk", FISH_REFERENCE_ID="ref")
        self.assertEqual(providers["fish"].config.latency, "balanced")

    def test_env_voices_are_seeded(self):
        registry = seed_registry(VOICE_PROFILES, fallback_profile="piper-ruslan")
        with patch.dict(os.environ, {**NO_KEYS, "FISH_API_KEY": "fk", "FISH_REFERENCE_ID": "ref"}):
            changed = plugins.seed_voices(registry)
        self.assertTrue(changed)
        self.assertEqual(registry.get("fish-default").fish.reference_id, "ref")
        with patch.dict(os.environ, {**NO_KEYS, "FISH_API_KEY": "fk", "FISH_REFERENCE_ID": "ref"}):
            self.assertFalse(plugins.seed_voices(registry))  # nothing new


class BotProviderLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_close_shuts_every_registered_provider(self):
        bot_mod = load_bot_module()
        bot = bot_mod.TTSBot()
        acme = type("Acme", (), {"aclose": AsyncMock()})()
        bot.tts_dispatcher.register("acme", acme)
        await bot.close()
        acme.aclose.assert_awaited_once()

    def test_the_bot_keeps_no_provider_attributes(self):
        bot_mod = load_bot_module()
        bot = bot_mod.TTSBot()
        for name in ("fish_provider", "gemini_provider", "elevenlabs_provider", "fish_config"):
            self.assertFalse(hasattr(bot, name), name)


if __name__ == "__main__":
    unittest.main()
