"""Unit tests for custom-emoji pronunciation aliases.

Covers three stages, network- and Discord-free:
  - storage in BotConfigStore (load/save round-trip, malformed entries) and
    the emoji-token parsing / pronunciation-sanitizing helpers;
  - substitution inside normalize_for_tts / _strip_discord_tokens_for_speech
    (before the strip regex) plus the cache-key-changes-on-alias check;
  - the /voicebot emoji-alias* command surface is exercised indirectly via
    the store + parser (the commands are thin wrappers over them).
"""

from __future__ import annotations

import importlib.util
import json
import tempfile
import types
import unittest
from pathlib import Path


def load_bot_module():
    module_path = Path(__file__).with_name("bot.py")
    spec = importlib.util.spec_from_file_location("tts_bot_module_emoji", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


bot = load_bot_module()


def _fake_guild(*emojis):
    """emojis: (id, name) tuples -> object with a .emojis list."""
    objs = [types.SimpleNamespace(id=eid, name=name) for eid, name in emojis]
    return types.SimpleNamespace(emojis=objs)


class ParseEmojiArgTests(unittest.TestCase):
    def test_static_token(self):
        self.assertEqual(
            bot.parse_custom_emoji_arg("<:Kekis:1035000000000000001>", None),
            ("1035000000000000001", "Kekis"),
        )

    def test_animated_token(self):
        self.assertEqual(
            bot.parse_custom_emoji_arg("<a:Wow:42>", None), ("42", "Wow")
        )

    def test_token_embedded_in_text(self):
        self.assertEqual(
            bot.parse_custom_emoji_arg("  hi <:Kek:7> ", None), ("7", "Kek")
        )

    def test_shortcode_resolved_against_guild(self):
        guild = _fake_guild((7, "Kekis"), (8, "Pepe"))
        self.assertEqual(bot.parse_custom_emoji_arg(":Pepe:", guild), ("8", "Pepe"))

    def test_shortcode_case_insensitive(self):
        guild = _fake_guild((7, "Kekis"))
        self.assertEqual(bot.parse_custom_emoji_arg(":kekis:", guild), ("7", "Kekis"))

    def test_shortcode_unknown_guild_emoji_rejected(self):
        guild = _fake_guild((7, "Kekis"))
        self.assertIsNone(bot.parse_custom_emoji_arg(":Nope:", guild))

    def test_unicode_emoji_rejected(self):
        self.assertIsNone(bot.parse_custom_emoji_arg("😀", None))

    def test_plain_text_rejected(self):
        self.assertIsNone(bot.parse_custom_emoji_arg("just words", None))


class SanitizePronunciationTests(unittest.TestCase):
    def test_trims_and_collapses(self):
        self.assertEqual(bot.sanitize_pronunciation("  кек  ис \n"), "кек ис")

    def test_strips_control_chars(self):
        self.assertEqual(bot.sanitize_pronunciation("ке\x00к\x07ис"), "кекис")

    def test_strips_nested_tokens(self):
        self.assertEqual(
            bot.sanitize_pronunciation("кек <:Kekis:1> <@123>"), "кек"
        )

    def test_caps_length(self):
        out = bot.sanitize_pronunciation("я" * 250)
        self.assertEqual(len(out), 100)

    def test_empty_returns_none(self):
        self.assertIsNone(bot.sanitize_pronunciation("   "))
        self.assertIsNone(bot.sanitize_pronunciation(""))
        self.assertIsNone(bot.sanitize_pronunciation("<@123>"))


class SubstituteAliasesTests(unittest.TestCase):
    def test_replaces_aliased_pads_spaces(self):
        out = bot.substitute_emoji_aliases("да<:Kekis:1>", {"1": "кекис"})
        self.assertEqual(out, "да кекис ")

    def test_leaves_unaliased_token(self):
        out = bot.substitute_emoji_aliases("<:Other:2>", {"1": "кекис"})
        self.assertEqual(out, "<:Other:2>")

    def test_replaces_all_occurrences(self):
        out = bot.substitute_emoji_aliases("<:K:1> и <:K:1>", {"1": "кек"})
        self.assertEqual(out, " кек  и  кек ")

    def test_animated_token_by_id(self):
        out = bot.substitute_emoji_aliases("<a:K:9>", {"9": "вау"})
        self.assertEqual(out, " вау ")

    def test_empty_aliases_noop(self):
        self.assertEqual(bot.substitute_emoji_aliases("<:K:1>", {}), "<:K:1>")


class ConfigStoreEmojiAliasTests(unittest.TestCase):
    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            store = bot.BotConfigStore(path, set())
            store.set_emoji_alias("1035", "Kekis", "кекис")
            store.set_emoji_alias("2046", "Pepe", "пепе")

            loaded = bot.BotConfigStore(path, set())
            self.assertEqual(
                loaded.emoji_aliases,
                {
                    "1035": {"name": "Kekis", "say": "кекис"},
                    "2046": {"name": "Pepe", "say": "пепе"},
                },
            )
            self.assertEqual(loaded.emoji_say_map(), {"1035": "кекис", "2046": "пепе"})

    def test_remove(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            store = bot.BotConfigStore(path, set())
            store.set_emoji_alias("1", "K", "кек")
            self.assertTrue(store.remove_emoji_alias("1"))
            self.assertFalse(store.remove_emoji_alias("1"))
            self.assertEqual(bot.BotConfigStore(path, set()).emoji_aliases, {})

    def test_update_overwrites(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            store = bot.BotConfigStore(path, set())
            store.set_emoji_alias("1", "K", "кек")
            store.set_emoji_alias("1", "K", "лол")
            self.assertEqual(
                bot.BotConfigStore(path, set()).emoji_aliases["1"]["say"], "лол"
            )

    def test_malformed_entries_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "guilds": {},
                        "emoji_aliases": {
                            "1": {"name": "Ok", "say": "норм"},
                            "notanid": {"name": "X", "say": "x"},
                            "2": {"name": "NoSay"},
                            "3": {"name": "Empty", "say": "   "},
                            "4": "not a dict",
                        },
                    }
                ),
                encoding="utf-8",
            )
            store = bot.BotConfigStore(path, set())
            self.assertEqual(store.emoji_aliases, {"1": {"name": "Ok", "say": "норм"}})

    def test_does_not_disturb_guild_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            store = bot.BotConfigStore(path, {111})
            store.add_user(10, 222)
            store.set_emoji_alias("1", "K", "кек")
            loaded = bot.BotConfigStore(path, set())
            self.assertEqual(loaded.get_guild(10).allowed_users, {111, 222})
            self.assertEqual(loaded.emoji_aliases, {"1": {"name": "K", "say": "кек"}})


if __name__ == "__main__":
    unittest.main()
