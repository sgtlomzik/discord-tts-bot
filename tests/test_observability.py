"""Connection keep-alive, per-request HTTP log, job ids and cache logs."""

import importlib.util
import logging
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, PropertyMock, patch

import discord
import httpx

from ttsbot import config
from ttsbot.httpclient import attach_warmer
from ttsbot.models import TTSJob
from ttsbot.providers import TTSCacheConfig, TTSPhraseCache
from ttsbot.voice_registry import ElevenLabsParams, VoiceRecord, VoiceRegistry


def load_bot_module():
    path = Path(__file__).resolve().parent.parent / "bot.py"
    spec = importlib.util.spec_from_file_location("tts_bot_module_obs", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def ok(request):
    return httpx.Response(200, content=b"x")


class HttpLogTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(ok), base_url="https://el.test")
        self.addAsyncCleanup(self.client.aclose)
        self.warmer = attach_warmer(self.client, "Test")

    async def test_one_line_per_request_with_reuse_and_idle(self):
        with self.assertLogs("tts_bot", "INFO") as logs:
            await self.client.post("/v1/tts", params={"key": "secret"})
            await self.client.post("/v1/tts")
        lines = [line for line in logs.output if "Test HTTP" in line]
        self.assertEqual(len(lines), 2)
        self.assertIn("Test HTTP POST /v1/tts status=200 conn=reused idle_before_s=- headers_s=", lines[0])
        self.assertIn("idle_before_s=0.0", lines[1])
        self.assertNotIn("secret", "\n".join(logs.output))  # no query string

    async def test_new_connection_is_reported(self):
        request = self.client.build_request("POST", "/v1/tts")
        await self.client.event_hooks["request"][-1](request)
        await request.extensions["trace"]("connection.connect_tcp.started", {})
        with self.assertLogs("tts_bot", "INFO") as logs:
            await self.client.event_hooks["response"][-1](httpx.Response(200, request=request))
        self.assertIn("conn=new", logs.output[0])

    async def test_keepalive_ping_waits_for_its_own_idle_time(self):
        await self.client.post("/v1/tts")
        self.assertIsNone(self.warmer.maybe_warm("keepalive", idle=60))
        with self.assertLogs("tts_bot", "DEBUG") as logs:
            await self.warmer.maybe_warm("keepalive", idle=0)
        line = next(l for l in logs.output if "connection warmed" in l)
        # A ping that found the connection open is logged at DEBUG only.
        self.assertTrue(line.startswith("DEBUG"), line)
        self.assertIn("reason=keepalive conn=reused", line)
        self.assertFalse(any("Test HTTP" in l for l in logs.output))  # no second line


class KeepAliveWorkerTests(unittest.TestCase):
    def _bot(self):
        bot = load_bot_module().bot
        bot.voice_registry = VoiceRegistry(fallback_profile="piper-ruslan", voices={
            "eleven-river": VoiceRecord(name="eleven-river", label="R", description="", provider="elevenlabs",
                                        elevenlabs=ElevenLabsParams(voice_id="v")),
            "piper-ruslan": VoiceRecord(name="piper-ruslan", label="P", description="", provider="piper"),
        })
        warmer = types.SimpleNamespace(name="ElevenLabs")
        providers = {"elevenlabs": types.SimpleNamespace(warmer=warmer)}
        bot.tts_dispatcher = types.SimpleNamespace(provider_for=lambda name: providers.get(name or ""))
        bot.config_store = MagicMock()
        bot.config_store.is_enabled.return_value = True
        bot.config_store.is_allowed.side_effect = lambda guild, user: user != 3
        voices = {1: "eleven-river", 2: "piper-ruslan", 3: "eleven-river"}
        bot.config_store.voice_for_user.side_effect = lambda guild, user: voices[user]
        return bot, warmer

    def _vc(self, *member_ids, bot_member=False):
        channel = MagicMock(spec=discord.VoiceChannel)
        channel.guild = types.SimpleNamespace(id=10)
        channel.members = [types.SimpleNamespace(id=i, bot=bot_member) for i in member_ids]
        vc = MagicMock()
        vc.channel = channel
        vc.is_connected.return_value = True
        return vc

    def test_only_cloud_voices_of_allowed_present_users(self):
        bot, warmer = self._bot()
        with patch.object(type(bot), "voice_clients", new_callable=PropertyMock) as clients:
            clients.return_value = [self._vc(1, 2)]
            self.assertEqual(bot._keepalive_warmers(), {"ElevenLabs": warmer})
            clients.return_value = [self._vc(2, 3)]  # Piper user + not allowed user
            self.assertEqual(bot._keepalive_warmers(), {})
            clients.return_value = [self._vc(1, bot_member=True)]
            self.assertEqual(bot._keepalive_warmers(), {})
            clients.return_value = []  # not in a voice channel
            self.assertEqual(bot._keepalive_warmers(), {})

    def test_disabled_guild_is_skipped(self):
        bot, _ = self._bot()
        bot.config_store.is_enabled.return_value = False
        with patch.object(type(bot), "voice_clients", new_callable=PropertyMock, return_value=[self._vc(1)]):
            self.assertEqual(bot._keepalive_warmers(), {})

    def test_interval_is_configurable(self):
        self.assertEqual(config.TTS_CONNECTION_KEEPALIVE_SECONDS, 60.0)


class JobIdTests(unittest.TestCase):
    def test_ids_are_unique_and_increasing(self):
        make = lambda: TTSJob(text="a", voice_channel=None, queued_at=0.0, author_id=1, guild_id=1,
                              text_channel_id=1, voice_profile="p")
        first, second = make(), make()
        self.assertEqual(second.job_id, first.job_id + 1)


class CacheLogTests(unittest.TestCase):
    def test_store_hit_and_evict_are_logged(self):
        with tempfile.TemporaryDirectory() as d:
            cache = TTSPhraseCache(TTSCacheConfig(enabled=True, cache_dir=Path(d), max_entries=1))
            with self.assertLogs("tts_bot", "INFO") as logs:
                cache.store_bytes("да", b"12345", "voice")
                self.assertIsNotNone(cache.lookup("да", "voice"))
                cache.store_bytes("нет", b"123", "voice")
            text = "\n".join(logs.output)
            self.assertIn("TTS cache stored file=", text)
            self.assertIn("bytes=5", text)
            self.assertIn("TTS cache hit file=", text)
            self.assertIn("hits=1 misses=0", text)
            self.assertIn("TTS cache evicted file=", text)

    def test_miss_is_debug_only(self):
        with tempfile.TemporaryDirectory() as d:
            cache = TTSPhraseCache(TTSCacheConfig(enabled=True, cache_dir=Path(d)))
            with self.assertLogs("tts_bot", "DEBUG") as logs:
                self.assertIsNone(cache.lookup("да", "voice"))
            self.assertTrue(all(line.startswith("DEBUG") for line in logs.output), logs.output)


if __name__ == "__main__":
    unittest.main()
