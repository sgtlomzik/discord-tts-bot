"""Only joining or switching a channel pulls the bot in.

Muting, deafening or starting a stream also arrives as a voice state
update. It used to auto-connect the bot, so after every idle disconnect
the next mute toggle of an allowed member brought it straight back.
"""

from __future__ import annotations

import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from test_bot import load_bot_module, temp_config_store


class FakeVoiceChannel:
    def __init__(self, cid):
        self.id = cid
        self.members = []


class VoiceFollowTests(unittest.IsolatedAsyncioTestCase):
    async def _update(self, before_channel, after_channel):
        bot_mod = load_bot_module()
        guild = types.SimpleNamespace(id=123)
        member = types.SimpleNamespace(id=441612025286885397, bot=False, guild=guild)
        bot_mod.bot.config_store = temp_config_store(bot_mod, {member.id})
        bot_mod.bot.auto_connect_for_member = AsyncMock()
        with (
            patch.object(bot_mod.discord, "VoiceChannel", FakeVoiceChannel),
            patch.object(bot_mod.discord.utils, "get", MagicMock(return_value=None)),
        ):
            await bot_mod.on_voice_state_update(
                member,
                types.SimpleNamespace(channel=before_channel),
                types.SimpleNamespace(channel=after_channel),
            )
        return bot_mod.bot.auto_connect_for_member

    async def test_a_mute_toggle_does_not_pull_the_bot_in(self):
        channel = FakeVoiceChannel(1)
        auto = await self._update(channel, channel)
        auto.assert_not_awaited()

    async def test_joining_a_channel_does(self):
        channel = FakeVoiceChannel(1)
        auto = await self._update(None, channel)
        auto.assert_awaited_once()

    async def test_switching_channels_does(self):
        auto = await self._update(FakeVoiceChannel(1), FakeVoiceChannel(2))
        auto.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
