"""The bot rejoins allowed members already in voice when it (re)connects.

Auto-connect only reacts to a member joining or switching a channel, so
after a restart the bot stayed out, and nothing was recorded, until
someone moved.
"""

from __future__ import annotations

import types
import unittest
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import discord

from test_bot import load_bot_module, temp_config_store

ALLOWED = 441612025286885397
OTHER_ALLOWED = 441612025286885398
STRANGER = 1


def member(uid, *, bot=False):
    return types.SimpleNamespace(id=uid, bot=bot)


def channel(cid, *members):
    return types.SimpleNamespace(id=cid, members=list(members))


class RejoinTests(unittest.IsolatedAsyncioTestCase):
    def _bot(self, *voice_channels, connected=None, allowed=(ALLOWED, OTHER_ALLOWED)):
        bot_mod = load_bot_module()
        tts_bot = bot_mod.bot
        tts_bot.config_store = temp_config_store(bot_mod, set(allowed))
        tts_bot.ensure_voice = AsyncMock()
        guild = types.SimpleNamespace(id=123, voice_channels=list(voice_channels), voice_client=connected)
        for vch in voice_channels:
            vch.guild = guild
        patches = [
            patch.object(type(tts_bot), "guilds", new_callable=PropertyMock, return_value=[guild]),
            # auto_connect_for_member looks the voice client up this way.
            patch.object(bot_mod.discord.utils, "get", MagicMock(return_value=connected)),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return bot_mod, tts_bot, guild

    async def test_joins_the_channel_with_allowed_members(self):
        target = channel(10, member(ALLOWED))
        _, tts_bot, _ = self._bot(channel(9, member(STRANGER)), target)

        await tts_bot.rejoin_occupied_voice("ready")

        tts_bot.ensure_voice.assert_awaited_once_with(target)

    async def test_picks_the_channel_with_the_most_allowed_members(self):
        one = channel(10, member(ALLOWED))
        two = channel(11, member(ALLOWED), member(OTHER_ALLOWED))
        _, tts_bot, _ = self._bot(one, two)

        await tts_bot.rejoin_occupied_voice("ready")

        tts_bot.ensure_voice.assert_awaited_once_with(two)

    async def test_bots_and_strangers_do_not_count(self):
        _, tts_bot, _ = self._bot(channel(10, member(STRANGER), member(ALLOWED, bot=True)))

        await tts_bot.rejoin_occupied_voice("ready")

        tts_bot.ensure_voice.assert_not_awaited()

    async def test_a_denied_member_does_not_count(self):
        _, tts_bot, guild = self._bot(channel(10, member(ALLOWED)))
        tts_bot.config_store.remove_user(guild.id, ALLOWED)

        await tts_bot.rejoin_occupied_voice("ready")

        tts_bot.ensure_voice.assert_not_awaited()

    async def test_a_disabled_guild_is_left_alone(self):
        _, tts_bot, guild = self._bot(channel(10, member(ALLOWED)))
        tts_bot.config_store.set_enabled(guild.id, False)

        await tts_bot.rejoin_occupied_voice("ready")

        tts_bot.ensure_voice.assert_not_awaited()

    async def test_auto_connect_switched_off_leaves_it_alone(self):
        bot_mod, tts_bot, _ = self._bot(channel(10, member(ALLOWED)))

        with patch.object(bot_mod.config, "TTS_AUTO_CONNECT_ENABLED", False):
            await tts_bot.rejoin_occupied_voice("ready")

        tts_bot.ensure_voice.assert_not_awaited()

    async def test_an_already_connected_bot_does_not_move(self):
        vc = MagicMock(spec=discord.VoiceClient)
        vc.is_connected.return_value = True
        _, tts_bot, _ = self._bot(channel(10, member(ALLOWED)), connected=vc)

        await tts_bot.rejoin_occupied_voice("ready")

        tts_bot.ensure_voice.assert_not_awaited()

    async def test_a_failed_connect_is_logged_not_raised(self):
        _, tts_bot, _ = self._bot(channel(10, member(ALLOWED)))
        tts_bot.ensure_voice.side_effect = RuntimeError("boom")

        with self.assertLogs("tts_bot", "ERROR"):
            await tts_bot.rejoin_occupied_voice("ready")

    async def test_on_ready_schedules_one_rejoin(self):
        bot_mod, tts_bot, _ = self._bot(channel(10, member(ALLOWED)))
        tts_bot.rejoin_occupied_voice = AsyncMock()

        await bot_mod.on_ready()
        await bot_mod.on_ready()  # still running: not scheduled twice
        await tts_bot.rejoin_task

        tts_bot.rejoin_occupied_voice.assert_awaited_once_with("ready")


if __name__ == "__main__":
    unittest.main()
