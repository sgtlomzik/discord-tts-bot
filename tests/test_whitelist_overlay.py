"""WHITELIST_USERS is checked live, never copied into guild settings.

Every guild the bot saw got a copy of the env whitelist in config.json,
so removing someone from WHITELIST_USERS later changed nothing for the
guilds already saved.
"""

from __future__ import annotations

import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from test_bot import load_bot_module
from ttsbot.store import BotConfigStore


class WhitelistOverlayTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "config.json"

    def tearDown(self):
        self.dir.cleanup()

    def test_env_users_are_allowed_everywhere_without_being_saved(self):
        store = BotConfigStore(self.path, {10})
        self.assertTrue(store.is_allowed(1, 10))
        store.add_user(1, 20)
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(saved["guilds"]["1"]["allowed_users"], [20])

    def test_removing_someone_from_the_env_takes_effect(self):
        BotConfigStore(self.path, {10}).add_user(1, 20)
        store = BotConfigStore(self.path, set())
        self.assertFalse(store.is_allowed(1, 10))
        self.assertTrue(store.is_allowed(1, 20))

    def test_env_users_are_reported_as_such(self):
        store = BotConfigStore(self.path, {10})
        self.assertTrue(store.is_env_user(10))
        self.assertFalse(store.is_env_user(20))


class DenyEnvUserTests(unittest.IsolatedAsyncioTestCase):
    async def test_deny_explains_that_env_users_are_set_in_env(self):
        bot_mod = load_bot_module()
        bot_mod.bot.config_store = BotConfigStore(Path(tempfile.mkdtemp()) / "config.json", {10})
        user = types.SimpleNamespace(id=10, mention="@x")
        interaction = types.SimpleNamespace(
            guild=types.SimpleNamespace(id=1), user=types.SimpleNamespace(id=2),
            response=types.SimpleNamespace(send_message=AsyncMock()),
        )
        with patch.object(bot_mod.tts_commands, "require_guild_manager", AsyncMock(return_value=True)):
            await bot_mod.slash_tts_deny.callback(interaction, user)
        self.assertIn("WHITELIST_USERS", interaction.response.send_message.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
