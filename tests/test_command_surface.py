"""The command surface Discord sees: names, order and autocompletes.

commands.py was split into the ttsbot/commands package without changing
a single command; this pins the result so a later move cannot drop or
reorder one unnoticed.
"""

from __future__ import annotations

import unittest

from test_bot import load_bot_module

VOICEBOT = [
    "off", "on", "allow", "deny", "voice-set", "voice-user", "voice-clear", "voice-say-set",
    "voice-say-clear", "voices", "voice-add", "voice-fish-add", "voice-clone", "voice-describe",
    "voice-fish-tune", "fish-latency", "voice-tune", "emoji-alias", "emoji-alias-remove",
    "emoji-aliases", "stats", "status", "test", "limit", "queue-clear",
]


class CommandSurfaceTests(unittest.TestCase):
    def setUp(self):
        self.bot = load_bot_module().bot

    def test_voicebot_subcommands_in_order(self):
        names = [cmd.name for cmd in self.bot.tts_group.commands]
        self.assertEqual(names, VOICEBOT)
        self.assertLessEqual(len(names), 25)  # Discord's limit per group

    def test_fallback_group(self):
        (group,) = self.bot.extra_groups
        self.assertEqual(group.name, "fallback")
        self.assertEqual([cmd.name for cmd in group.commands], ["set", "status"])

    def test_prefix_commands(self):
        self.assertEqual(sorted(cmd.name for cmd in self.bot.commands), ["help", "join", "stop"])

    def test_voice_parameters_keep_their_autocomplete(self):
        commands = {cmd.name: cmd for cmd in self.bot.tts_group.commands}
        (fallback,) = self.bot.extra_groups
        commands["fallback set"] = next(cmd for cmd in fallback.commands if cmd.name == "set")
        for name in ("voice-set", "voice-user", "fallback set"):
            callback = commands[name]._params["voice"].autocomplete
            self.assertTrue(callable(callback), name)
            self.assertEqual(callback.__name__, "voice_profile_autocomplete", name)
        voice_id = commands["voice-add"]._params["voice_id"].autocomplete
        self.assertEqual(getattr(voice_id, "__name__", None), "voice_id_autocomplete")

    def test_a_failed_slash_command_is_answered(self):
        self.assertIsNotNone(self.bot.tree.on_error)


if __name__ == "__main__":
    unittest.main()
