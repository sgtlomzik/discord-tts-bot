"""Connection warm-up on typing and the Discord delivery log."""

import asyncio
import datetime as dt
import importlib.util
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import discord
import httpx

from ttsbot import httpclient
from ttsbot.elevenlabs import ElevenLabsConfig, ElevenLabsProvider
from ttsbot.fish import FishConfig, FishProvider
from ttsbot.gemini import GeminiConfig, GeminiProvider
from ttsbot.httpclient import ConnectionWarmer, attach_warmer
from ttsbot.providers import MiniMaxConfig, MiniMaxProvider, TTSDispatcher
from ttsbot.voice_registry import ElevenLabsParams, VoiceRecord, VoiceRegistry


def load_bot_module():
    path = Path(__file__).resolve().parent.parent / "bot.py"
    spec = importlib.util.spec_from_file_location("tts_bot_module_warm", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class WarmerTests(unittest.IsolatedAsyncioTestCase):
    async def test_warms_once_then_waits_for_idle(self):
        seen = []

        def handle(request):
            seen.append((request.method, request.url.path, "xi-api-key" in request.headers))
            return httpx.Response(401)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handle), base_url="https://el.test")
        self.addAsyncCleanup(client.aclose)
        warmer = attach_warmer(client, "Test")
        with self.assertLogs("tts_bot", "INFO") as logs:
            await warmer.maybe_warm()
        self.assertIn("Test connection warmed status=401", "\n".join(logs.output))
        self.assertIsNone(warmer.maybe_warm())  # just warmed
        self.assertEqual(seen, [("GET", "/", False)])  # no key: nothing billed
        with patch.object(httpclient, "WARM_IDLE_SECONDS", 0.0):
            await warmer.maybe_warm()
        self.assertEqual(len(seen), 2)

    async def test_real_request_counts_as_activity(self):
        def handle(request):
            if request.url.path == "/":
                self.fail("warm-up sent right after a request")
            return httpx.Response(200, headers={"content-type": "audio/opus"}, content=b"OggS")

        client = httpx.AsyncClient(transport=httpx.MockTransport(handle), base_url="https://el.test")
        self.addAsyncCleanup(client.aclose)
        provider = ElevenLabsProvider(ElevenLabsConfig(api_key="k", voice_id="v"), client)
        await provider.fetch("x")
        self.assertIsNone(provider.warmer.maybe_warm())

    async def test_every_cloud_provider_has_a_warmer_on_its_host(self):
        providers = {
            "elevenlabs": ElevenLabsProvider(ElevenLabsConfig(api_key="k")),
            "fish": FishProvider(FishConfig(api_key="k")),
            "gemini": GeminiProvider(GeminiConfig(api_key="k")),
            "minimax": MiniMaxProvider(MiniMaxConfig(api_key="k", base_url="https://api.minimax.io")),
        }
        dispatcher = TTSDispatcher(
            local=MagicMock(), cloud=providers["minimax"], fish=providers["fish"],
            gemini=providers["gemini"], elevenlabs=providers["elevenlabs"],
        )
        hosts = {
            "elevenlabs": "api.elevenlabs.io", "fish": "api.fish.audio",
            "gemini": "openrouter.ai", "minimax": "api.minimax.io",
        }
        for name, provider in providers.items():
            self.assertIs(dispatcher.provider_for(name), provider)
            warmer = provider.warmer
            url = provider._client.build_request("GET", warmer._url).url
            self.assertEqual(url.host, hosts[name], name)
            # A request through the client counts as activity (event hook).
            await provider._client.event_hooks["request"][-1](None)
            self.assertIsNone(warmer.maybe_warm(), name)
            await provider.aclose()
        self.assertIsNone(dispatcher.provider_for("piper"))
        self.assertIsNone(dispatcher.provider_for(None))

    async def test_failed_warm_up_is_only_logged(self):
        def handle(request):
            raise httpx.ConnectError("down")

        client = httpx.AsyncClient(transport=httpx.MockTransport(handle), base_url="https://el.test")
        self.addAsyncCleanup(client.aclose)
        with self.assertLogs("tts_bot", "INFO") as logs:
            await ConnectionWarmer(client, "Test").maybe_warm()
        self.assertIn("warm-up failed", "\n".join(logs.output))


class TypingEventTests(unittest.IsolatedAsyncioTestCase):
    def _bot(self):
        bot_mod = load_bot_module()
        bot = bot_mod.bot
        bot.voice_registry = VoiceRegistry(fallback_profile="piper-ruslan", voices={
            "eleven-river": VoiceRecord(name="eleven-river", label="R", description="", provider="elevenlabs",
                                        elevenlabs=ElevenLabsParams(voice_id="v")),
            "piper-ruslan": VoiceRecord(name="piper-ruslan", label="P", description="", provider="piper"),
        })
        warmer = MagicMock()
        providers = {"elevenlabs": types.SimpleNamespace(warmer=warmer)}
        bot.tts_dispatcher = types.SimpleNamespace(provider_for=lambda name: providers.get(name or ""))
        bot.config_store = MagicMock()
        bot.config_store.is_enabled.return_value = True
        bot.config_store.is_allowed.return_value = True
        bot.config_store.voice_for_user.return_value = "eleven-river"
        return bot_mod, bot, warmer

    def _user(self, in_voice=True, is_bot=False):
        voice = types.SimpleNamespace(channel=MagicMock(spec=discord.VoiceChannel)) if in_voice else None
        return types.SimpleNamespace(id=7, bot=is_bot, voice=voice)

    async def test_allowed_user_in_voice_warms_their_provider(self):
        bot_mod, bot, warmer = self._bot()
        channel = types.SimpleNamespace(guild=types.SimpleNamespace(id=1))
        await bot_mod.on_typing(channel, self._user(), None)
        warmer.maybe_warm.assert_called_once()

    async def test_other_cases_do_not_warm(self):
        bot_mod, bot, warmer = self._bot()
        guild_channel = types.SimpleNamespace(guild=types.SimpleNamespace(id=1))
        await bot_mod.on_typing(types.SimpleNamespace(guild=None), self._user(), None)  # DM
        await bot_mod.on_typing(guild_channel, self._user(in_voice=False), None)
        await bot_mod.on_typing(guild_channel, self._user(is_bot=True), None)
        bot.config_store.voice_for_user.return_value = "piper-ruslan"
        await bot_mod.on_typing(guild_channel, self._user(), None)  # local voice: nothing to warm
        bot.config_store.voice_for_user.return_value = "eleven-river"
        bot.config_store.is_allowed.return_value = False
        await bot_mod.on_typing(guild_channel, self._user(), None)
        warmer.maybe_warm.assert_not_called()


class DeliveryLogTests(unittest.IsolatedAsyncioTestCase):
    async def test_logs_gateway_delay_for_voiced_messages(self):
        bot_mod = load_bot_module()
        bot = bot_mod.bot
        bot.config_store = MagicMock()
        bot.config_store.fixed_phrase_for_user.return_value = None

        async def noop(*args, **kwargs):
            return None

        bot.queue_or_merge_message = noop
        bot.process_commands = noop
        created = discord.utils.utcnow() - dt.timedelta(milliseconds=120)
        message = types.SimpleNamespace(
            id=5, content="привет", created_at=created, mentions=[], role_mentions=[],
            guild=types.SimpleNamespace(id=1),
            channel=types.SimpleNamespace(id=2),
            author=types.SimpleNamespace(id=7, bot=False, voice=types.SimpleNamespace(
                channel=MagicMock(spec=discord.VoiceChannel))),
        )
        with patch.object(bot_mod.tts_events, "build_mention_say_map", return_value={}), \
                self.assertLogs("tts_bot", "INFO") as logs:
            await bot_mod.on_message(message)
        line = next(l for l in logs.output if "Discord delivery" in l)
        delay = int(line.rsplit("delay_ms=", 1)[1])
        self.assertTrue(100 <= delay < 2000, line)


if __name__ == "__main__":
    unittest.main()
