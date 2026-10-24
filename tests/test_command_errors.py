"""A failing command answers the user and leaves memory equal to disk.

Without a tree error handler a command that raised left the user with
"the application did not respond"; store and registry mutators changed
memory first and kept the change when the save to disk failed.
"""

from __future__ import annotations

import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from discord import app_commands

from test_bot import load_bot_module
from ttsbot import voice_registry
from ttsbot.store import BotConfigStore
from support import state_from


def _interaction(done: bool):
    return types.SimpleNamespace(
        response=types.SimpleNamespace(is_done=MagicMock(return_value=done), send_message=AsyncMock()),
        followup=types.SimpleNamespace(send=AsyncMock()),
        command=types.SimpleNamespace(qualified_name="voicebot test"),
        guild=None, user=types.SimpleNamespace(id=1),
    )


class TreeErrorHandlerTests(unittest.IsolatedAsyncioTestCase):
    def _handler(self):
        bot_mod = load_bot_module()
        return bot_mod.on_app_command_error

    async def test_unanswered_interaction_gets_an_ephemeral_reply(self):
        handler = self._handler()
        interaction = _interaction(done=False)
        with self.assertLogs("tts_bot", level="ERROR"):
            await handler(interaction, app_commands.CommandInvokeError(MagicMock(), OSError("disk full")))
        interaction.response.send_message.assert_awaited_once()
        args, kwargs = interaction.response.send_message.call_args
        self.assertTrue(kwargs.get("ephemeral"))
        self.assertIn("OSError", args[0])

    async def test_deferred_interaction_gets_a_followup(self):
        handler = self._handler()
        interaction = _interaction(done=True)
        with self.assertLogs("tts_bot", level="ERROR"):
            await handler(interaction, app_commands.CommandInvokeError(MagicMock(), RuntimeError("x")))
        interaction.followup.send.assert_awaited_once()

    async def test_check_failures_are_not_logged_as_errors(self):
        handler = self._handler()
        interaction = _interaction(done=False)
        await handler(interaction, app_commands.CheckFailure("no"))
        interaction.response.send_message.assert_awaited_once()


class StoreRollbackTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.store = BotConfigStore(state_from(Path(self.dir.name) / "config.json"), set())
        self.store.set_enabled(1, True)

    def tearDown(self):
        self.dir.cleanup()

    def _failing_save(self):
        return patch.object(self.store.db, "save_config", side_effect=OSError("disk full"))

    def test_failed_save_reverts_the_change(self):
        with self._failing_save(), self.assertRaises(OSError):
            self.store.set_enabled(1, False)
        self.assertTrue(self.store.is_enabled(1))

    def test_failed_save_reverts_added_user_and_alias(self):
        with self._failing_save():
            with self.assertRaises(OSError):
                self.store.add_user(1, 42)
            with self.assertRaises(OSError):
                self.store.set_emoji_alias("9", "kek", "кек")
        self.assertFalse(self.store.is_allowed(1, 42))
        self.assertNotIn("9", self.store.emoji_aliases)


class RegistryRollbackTests(unittest.TestCase):
    def test_failed_persist_reverts_the_registry(self):
        bot_mod = load_bot_module()
        tts_bot = bot_mod.TTSBot()
        before = set(tts_bot.voice_registry.names())
        tts_bot.voice_registry.add(voice_registry.VoiceRecord(
            name="new-voice", label="New", description="", provider=voice_registry.PROVIDER_FISH,
            fish=voice_registry.FishParams(reference_id="abc"),
        ))
        with patch("ttsbot.voice_registry.save_registry", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                tts_bot.persist_voice_registry()
        self.assertEqual(set(tts_bot.voice_registry.names()), before)


if __name__ == "__main__":
    unittest.main()
