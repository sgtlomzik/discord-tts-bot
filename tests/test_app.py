"""The composition root builds the bot on demand, not on import.

bot.py used to construct the bot (reading data files) as a side effect
of being imported and re-exported about forty names so tests could reach
them through it.
"""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

from ttsbot import app
from ttsbot.core import TTSBot


class AppTests(unittest.TestCase):
    def test_importing_the_entrypoint_builds_nothing(self):
        path = Path(__file__).resolve().parent.parent / "bot.py"
        spec = importlib.util.spec_from_file_location("bot_entrypoint", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertTrue(callable(module.main))
        self.assertFalse(hasattr(module, "bot"))
        self.assertFalse(hasattr(module, "TTSBot"))

    def test_create_bot_wires_commands_and_events(self):
        bot, objects = app.create_bot()
        self.assertIsInstance(bot, TTSBot)
        self.assertEqual(bot.tts_group.name, "voicebot")
        self.assertEqual([group.name for group in bot.extra_groups], ["fallback"])
        for name in ("on_message", "on_voice_state_update", "slash_tts_status", "on_app_command_error"):
            self.assertIn(name, objects)


if __name__ == "__main__":
    unittest.main()
