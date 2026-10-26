"""WHITELIST_USERS is checked live, never copied into guild settings.

Every guild the bot saw got a copy of the env whitelist in config.json,
so removing someone from WHITELIST_USERS later changed nothing for the
guilds already saved. A guild can still opt out of an env user with
/voicebot deny: the deny is that guild's own and wins over the env.
"""

from __future__ import annotations

import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from test_bot import load_bot_module
from ttsbot.store import BotConfigStore
from support import state_from
from ttsbot.statedb import StateDB


class WhitelistOverlayTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "config.json"

    def tearDown(self):
        self.dir.cleanup()

    def test_env_users_are_allowed_everywhere_without_being_saved(self):
        store = BotConfigStore(state_from(self.path), {10})
        self.assertTrue(store.is_allowed(1, 10))
        store.add_user(1, 20)
        saved = StateDB(self.path.parent / "state.db").load_config()
        self.assertEqual(saved["guilds"]["1"]["allowed_users"], [20])

    def test_removing_someone_from_the_env_takes_effect(self):
        BotConfigStore(state_from(self.path), {10}).add_user(1, 20)
        store = BotConfigStore(state_from(self.path), set())
        self.assertFalse(store.is_allowed(1, 10))
        self.assertTrue(store.is_allowed(1, 20))

    def test_env_users_are_reported_as_such(self):
        store = BotConfigStore(state_from(self.path), {10})
        self.assertTrue(store.is_env_user(10))
        self.assertFalse(store.is_env_user(20))


class EnvUserDenyTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "config.json"

    def tearDown(self):
        self.dir.cleanup()

    def test_a_guild_can_deny_an_env_user_for_itself(self):
        store = BotConfigStore(state_from(self.path), {10})
        store.remove_user(1, 10)
        self.assertFalse(store.is_allowed(1, 10))
        self.assertTrue(store.is_allowed(2, 10))  # other guilds keep the env setting
        reopened = BotConfigStore(state_from(self.path), {10})
        self.assertFalse(reopened.is_allowed(1, 10))

    def test_allow_lifts_a_deny(self):
        store = BotConfigStore(state_from(self.path), {10})
        store.remove_user(1, 10)
        store.add_user(1, 10)
        self.assertTrue(BotConfigStore(state_from(self.path), {10}).is_allowed(1, 10))

    def test_env_copies_in_an_old_config_json_are_dropped_on_import(self):
        # The old bot copied WHITELIST_USERS into every guild it saw.
        self.path.write_text(json.dumps({"guilds": {"1": {"allowed_users": [10, 20]}}}), encoding="utf-8")
        db = state_from(self.path, env_users={10})
        self.assertEqual(db.load_config()["guilds"]["1"]["allowed_users"], [20])
        store = BotConfigStore(db, set())  # 10 removed from the env later
        self.assertFalse(store.is_allowed(1, 10))
        self.assertTrue(store.is_allowed(1, 20))


class DenyEnvUserCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_deny_removes_an_env_user_from_this_guild(self):
        bot_mod = load_bot_module()
        bot_mod.bot.config_store = BotConfigStore(state_from(Path(tempfile.mkdtemp()) / "config.json"), {10})
        user = types.SimpleNamespace(id=10, mention="@x")
        interaction = types.SimpleNamespace(
            guild=types.SimpleNamespace(id=1), user=types.SimpleNamespace(id=2),
            response=types.SimpleNamespace(send_message=AsyncMock()),
        )
        with patch.object(bot_mod.tts_commands, "require_guild_manager", AsyncMock(return_value=True)):
            await bot_mod.slash_tts_deny.callback(interaction, user)
        self.assertFalse(bot_mod.bot.config_store.is_allowed(1, 10))
        self.assertIn("Исключен", interaction.response.send_message.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
