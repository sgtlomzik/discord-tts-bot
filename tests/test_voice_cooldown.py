"""The connect cooldown only holds back new connects and moves.

A failed move used to arm a cooldown that also refused the channel the
bot was already playing in, and the playback worker then tore that live
connection down: a minute of silence for the whole guild.
"""

from __future__ import annotations

import asyncio
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

from test_bot import load_bot_module
from ttsbot.voice_lifecycle import VoiceCooldown


def _connected_vc(channel):
    return types.SimpleNamespace(
        guild=channel.guild, channel=channel,
        is_connected=MagicMock(return_value=True), move_to=AsyncMock(),
    )


class EnsureVoiceCooldownTests(unittest.IsolatedAsyncioTestCase):
    async def test_cooldown_does_not_refuse_the_channel_already_joined(self):
        bot_mod = load_bot_module()
        tts_bot = bot_mod.TTSBot()
        guild = types.SimpleNamespace(id=5)
        channel = types.SimpleNamespace(id=50, guild=guild)
        vc = _connected_vc(channel)
        tts_bot.session_for(guild.id).connect_cooldown_until = bot_mod.time.monotonic() + 60

        with patch.object(type(tts_bot), "voice_clients", new_callable=PropertyMock, return_value=[vc]):
            result = await tts_bot.ensure_voice(channel)

        self.assertIs(result, vc)

    async def test_cooldown_refuses_a_move_with_voice_cooldown(self):
        bot_mod = load_bot_module()
        tts_bot = bot_mod.TTSBot()
        guild = types.SimpleNamespace(id=5)
        here = types.SimpleNamespace(id=50, guild=guild)
        there = types.SimpleNamespace(id=51, guild=guild)
        vc = _connected_vc(here)
        tts_bot.session_for(guild.id).connect_cooldown_until = bot_mod.time.monotonic() + 60

        with patch.object(type(tts_bot), "voice_clients", new_callable=PropertyMock, return_value=[vc]):
            with self.assertRaises(VoiceCooldown):
                await tts_bot.ensure_voice(there)
        vc.move_to.assert_not_awaited()

    async def test_voice_cooldown_is_a_runtime_error(self):
        self.assertTrue(issubclass(VoiceCooldown, RuntimeError))


class PlaybackCooldownTests(unittest.IsolatedAsyncioTestCase):
    async def test_playback_keeps_the_connection_during_a_cooldown(self):
        bot_mod = load_bot_module()
        tts_bot = bot_mod.TTSBot()
        guild = types.SimpleNamespace(id=5)
        channel = types.SimpleNamespace(id=51, guild=guild)
        job = bot_mod.TTSJob("привет", channel, 0.0, 1, guild.id, 1000, "fish-default", 0.0)
        prepared = bot_mod.PreparedAudio(job=job, channel=asyncio.Queue())
        await prepared.channel.put([b"\x00" * bot_mod.PCM_FRAME_BYTES])
        await prepared.channel.put(None)
        tts_bot.ensure_voice = AsyncMock(side_effect=VoiceCooldown("cooldown 30.0s"))
        tts_bot.disconnect_guild_voice = AsyncMock()

        await asyncio.wait_for(tts_bot._play_prepared(prepared), 2.0)

        tts_bot.disconnect_guild_voice.assert_not_awaited()
        self.assertTrue(prepared.channel.empty())  # drained to the sentinel


if __name__ == "__main__":
    unittest.main()
