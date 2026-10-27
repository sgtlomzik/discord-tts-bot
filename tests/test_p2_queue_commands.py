"""Small queue and command fixes from the October audit (P2).

- queue-clear also cuts the phrase being spoken;
- /voicebot off answers before it disconnects;
- disconnects wait for a connect in progress (the per-guild voice lock);
- !tts commands are not spoken;
- !tts join checks the guild switch and reports a failed connect;
- a malformed numeric env setting no longer crashes provider setup.
"""

from __future__ import annotations

import asyncio
import os
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from test_bot import load_bot_module, temp_config_store
from ttsbot.audio import OPUS_SILENCE_FRAME, ContinuousTTSAudioSource
from ttsbot.voice_lifecycle import VoiceCooldown

ALLOWED = 441612025286885397


class SourceClearTests(unittest.IsolatedAsyncioTestCase):
    async def test_clear_drops_pending_frames_and_drains(self):
        source = ContinuousTTSAudioSource(OPUS_SILENCE_FRAME, opus=True)
        source.enqueue_frames([b"\x01" * 10] * 5)
        self.assertFalse(source.is_drained)

        self.assertEqual(source.clear(), 5)

        self.assertTrue(source.is_drained)
        self.assertEqual(source.read(), OPUS_SILENCE_FRAME)  # idle again, still playing
        self.assertFalse(source.stopped)
        self.assertTrue(await asyncio.wait_for(source.wait_until_drained(1.0), 2.0))

    async def test_clear_queue_for_guild_cuts_the_current_phrase(self):
        bot_mod = load_bot_module()
        tts_bot = bot_mod.TTSBot()
        source = ContinuousTTSAudioSource(OPUS_SILENCE_FRAME, opus=True)
        source.enqueue_frames([b"\x01" * 10] * 50)
        tts_bot.session_for(1).player = source

        tts_bot.clear_queue_for_guild(1)

        self.assertTrue(source.frames.empty())
        self.assertTrue(source.is_drained)


class VoicebotOffTests(unittest.IsolatedAsyncioTestCase):
    async def test_off_defers_before_disconnecting(self):
        bot_mod = load_bot_module()
        tts_bot = bot_mod.bot
        tts_bot.config_store = temp_config_store(bot_mod)
        order = []
        tts_bot.disconnect_guild_voice = AsyncMock(side_effect=lambda *a, **k: order.append("disconnect"))
        guild = types.SimpleNamespace(id=5)
        perms = types.SimpleNamespace(manage_guild=True, administrator=False)
        user = MagicMock(spec=discord.Member)
        user.guild_permissions = perms
        interaction = MagicMock()
        interaction.guild = guild
        interaction.user = user
        interaction.response.defer = AsyncMock(side_effect=lambda **k: order.append("defer"))
        interaction.followup.send = AsyncMock(side_effect=lambda *a, **k: order.append("reply"))

        await bot_mod.slash_tts_off.callback(interaction)

        self.assertEqual(order, ["defer", "disconnect", "reply"])
        self.assertFalse(tts_bot.config_store.is_enabled(5))


class DisconnectLockTests(unittest.IsolatedAsyncioTestCase):
    async def test_disconnect_waits_for_a_connect_in_progress(self):
        bot_mod = load_bot_module()
        tts_bot = bot_mod.TTSBot()
        guild = types.SimpleNamespace(id=7)
        vc = MagicMock()
        vc.disconnect = AsyncMock()
        lock = tts_bot.get_voice_connect_lock(7)
        with patch.object(bot_mod.discord.utils, "get", MagicMock(return_value=vc)):
            await lock.acquire()  # a connect is running
            task = asyncio.create_task(tts_bot.disconnect_guild_voice(guild))
            await asyncio.sleep(0.05)
            vc.disconnect.assert_not_awaited()
            lock.release()
            await asyncio.wait_for(task, 2.0)
        vc.disconnect.assert_awaited_once()


class PrefixCommandTests(unittest.IsolatedAsyncioTestCase):
    def _message(self, bot_mod, content):
        voice = types.SimpleNamespace(channel=FakeVoiceChannel())
        author = types.SimpleNamespace(id=ALLOWED, bot=False, voice=voice)
        return types.SimpleNamespace(
            author=author, content=content, guild=types.SimpleNamespace(id=9),
            channel=types.SimpleNamespace(id=10), id=1, created_at=None,
            mentions=[], role_mentions=[], channel_mentions=[],
        )

    async def _on_message(self, content):
        bot_mod = load_bot_module()
        tts_bot = bot_mod.bot
        tts_bot.config_store = temp_config_store(bot_mod, {ALLOWED})
        tts_bot.queue_message = AsyncMock()
        tts_bot.process_commands = AsyncMock()
        with patch.object(bot_mod.discord, "VoiceChannel", FakeVoiceChannel):
            await bot_mod.on_message(self._message(bot_mod, content))
        return tts_bot

    async def test_a_tts_command_is_not_spoken(self):
        tts_bot = await self._on_message("!tts join")
        tts_bot.queue_message.assert_not_awaited()
        tts_bot.process_commands.assert_awaited_once()

    async def test_a_normal_message_is_spoken(self):
        tts_bot = await self._on_message("привет всем")
        tts_bot.queue_message.assert_awaited_once()


class FakeVoiceChannel:
    id = 20
    members: list = []


class JoinCommandTests(unittest.IsolatedAsyncioTestCase):
    def _ctx(self):
        author = MagicMock(spec=discord.Member)
        author.id = ALLOWED
        author.voice = types.SimpleNamespace(channel=FakeVoiceChannel())
        ctx = MagicMock()
        ctx.guild = types.SimpleNamespace(id=9)
        ctx.author = author
        ctx.reply = AsyncMock()
        return ctx

    def _bot(self):
        bot_mod = load_bot_module()
        tts_bot = bot_mod.bot
        tts_bot.config_store = temp_config_store(bot_mod, {ALLOWED})
        tts_bot.ensure_voice = AsyncMock()
        return bot_mod, tts_bot

    async def _join(self, bot_mod, ctx):
        with patch.object(bot_mod.discord, "VoiceChannel", FakeVoiceChannel):
            await bot_mod.join.callback(ctx)

    async def test_a_disabled_guild_does_not_connect(self):
        bot_mod, tts_bot = self._bot()
        tts_bot.config_store.set_enabled(9, False)
        ctx = self._ctx()

        await self._join(bot_mod, ctx)

        tts_bot.ensure_voice.assert_not_awaited()
        self.assertIn("turned off", ctx.reply.await_args.args[0])

    async def test_a_cooldown_is_reported(self):
        bot_mod, tts_bot = self._bot()
        tts_bot.ensure_voice.side_effect = VoiceCooldown("Voice connect cooldown active for 9.0s")
        ctx = self._ctx()

        await self._join(bot_mod, ctx)

        self.assertIn("try again", ctx.reply.await_args.args[0])

    async def test_a_failed_connect_is_reported(self):
        bot_mod, tts_bot = self._bot()
        tts_bot.ensure_voice.side_effect = RuntimeError("boom")
        ctx = self._ctx()

        with self.assertLogs("tts_bot", "ERROR"):
            await self._join(bot_mod, ctx)

        self.assertIn("Could not connect", ctx.reply.await_args.args[0])


class EnvNumberTests(unittest.TestCase):
    def test_malformed_cache_and_breaker_settings_fall_back(self):
        from ttsbot import providers

        env = {"TTS_CACHE_MAX_BYTES": "2GB", "TTS_CACHE_MAX_ENTRIES": "lots",
               "CB_FAILURE_THRESHOLD": "three", "TTS_QUOTA_COOLDOWN_SECONDS": "half an hour"}
        with patch.dict(os.environ, env), self.assertLogs("tts_bot", "WARNING"):
            cache = providers.load_cache_config_from_env()
            providers.load_circuit_breaker_from_env()
            dispatcher = providers.load_dispatcher_config_from_env()
        self.assertEqual(cache.max_bytes, providers._DEFAULT_CACHE_MAX_BYTES)
        self.assertEqual(cache.max_entries, 0)
        self.assertEqual(dispatcher.quota_cooldown_seconds, 1800.0)

    def test_malformed_minimax_settings_fall_back(self):
        from ttsbot.minimax import load_minimax_config_from_env

        with patch.dict(os.environ, {"MINIMAX_SAMPLE_RATE": "fast", "TTS_REQUEST_TIMEOUT": "x"}), \
                self.assertLogs("tts_bot", "WARNING"):
            cfg = load_minimax_config_from_env()
        self.assertEqual(cfg.sample_rate, 32000)
        self.assertEqual(cfg.timeout_seconds, 2.5)


if __name__ == "__main__":
    unittest.main()
