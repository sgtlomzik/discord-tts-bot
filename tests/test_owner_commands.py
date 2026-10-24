"""Settings shared by every guild belong to the bot's owner.

Voices, tuning, the fallback chain, the length limit, Fish latency and
provider resets apply to all guilds, but any member with Manage Server in
any guild the bot is in could change them.
"""

from __future__ import annotations

import types
import unittest
from unittest.mock import AsyncMock, MagicMock

import discord

from test_bot import load_bot_module, temp_config_store


def _manager(user_id=7):
    member = MagicMock(spec=discord.Member)
    member.id = user_id
    member.guild_permissions = types.SimpleNamespace(manage_guild=True, administrator=False)
    return member


def _interaction(user):
    return types.SimpleNamespace(
        guild=types.SimpleNamespace(id=1),
        user=user,
        response=types.SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock(),
                                       is_done=MagicMock(return_value=False)),
        followup=types.SimpleNamespace(send=AsyncMock()),
    )


class OwnerOnlyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bot_mod = load_bot_module()
        self.bot = self.bot_mod.bot
        self.bot.config_store = temp_config_store(self.bot_mod)
        self.bot.is_owner = AsyncMock(return_value=False)
        self.limit_before = self.bot.config_store.max_chars()

    async def test_a_guild_manager_cannot_change_the_global_limit(self):
        interaction = _interaction(_manager())
        await self.bot_mod.slash_tts_limit.callback(interaction, 777)
        self.assertEqual(self.bot.config_store.max_chars(), self.limit_before)
        text = interaction.response.send_message.call_args.args[0]
        self.assertIn("владелец", text)

    async def test_the_owner_can(self):
        self.bot.is_owner = AsyncMock(return_value=True)
        interaction = _interaction(_manager())
        await self.bot_mod.slash_tts_limit.callback(interaction, 777)
        self.assertEqual(self.bot.config_store.max_chars(), 777)

    async def test_owner_ids_from_the_environment_count_too(self):
        self.bot_mod.config.OWNER_IDS = {7}
        interaction = _interaction(_manager(7))
        await self.bot_mod.slash_tts_limit.callback(interaction, 555)
        self.assertEqual(self.bot.config_store.max_chars(), 555)

    async def test_global_voice_commands_are_owner_only(self):
        for name, args in [
            ("slash_tts_voice_describe", ("fish-default", "x")),
            ("slash_tts_fish_latency", ()),
        ]:
            interaction = _interaction(_manager())
            await getattr(self.bot_mod, name).callback(interaction, *args)
            text = interaction.response.send_message.call_args.args[0]
            self.assertIn("владелец", text, name)

    async def test_provider_reset_is_owner_only_but_status_is_not(self):
        reset = types.SimpleNamespace(value="fish", name="Fish")
        interaction = _interaction(_manager())
        await self.bot_mod.slash_tts_providers.callback(interaction, reset)
        self.assertIn("владелец", interaction.response.send_message.call_args.args[0])

        interaction = _interaction(_manager())
        await self.bot_mod.slash_tts_providers.callback(interaction, None)
        interaction.response.defer.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
