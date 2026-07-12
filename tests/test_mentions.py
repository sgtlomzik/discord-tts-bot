"""Unit tests for reading Discord mentions aloud by name.

Network- and Discord-free: mentions are resolved from a map (built from a
message's resolved mention lists, or from a guild on the test path) and
substituted before the stripping step, mirroring the emoji-alias design.
"""

from __future__ import annotations

import importlib.util
import types
import unittest
from pathlib import Path


def load_bot_module():
    module_path = Path(__file__).resolve().parent.parent / "bot.py"
    spec = importlib.util.spec_from_file_location("tts_bot_module_mentions", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


bot = load_bot_module()


def _fake_message(users=(), roles=(), channels=()):
    return types.SimpleNamespace(
        mentions=[types.SimpleNamespace(id=i, display_name=d, name=n) for i, d, n in users],
        role_mentions=[types.SimpleNamespace(id=i, name=n) for i, n in roles],
        channel_mentions=[types.SimpleNamespace(id=i, name=n) for i, n in channels],
    )


def _fake_guild(members=(), roles=(), channels=()):
    md = {i: types.SimpleNamespace(id=i, display_name=d, name=n) for i, d, n in members}
    rd = {i: types.SimpleNamespace(id=i, name=n) for i, n in roles}
    cd = {i: types.SimpleNamespace(id=i, name=n) for i, n in channels}
    return types.SimpleNamespace(
        get_member=lambda i: md.get(i),
        get_role=lambda i: rd.get(i),
        get_channel=lambda i: cd.get(i),
    )


class ResolveMentionsTests(unittest.TestCase):
    def test_user_role_channel(self):
        names = {"1": "Вася", "2": "Админы", "3": "канал общий"}
        out = bot.resolve_mentions("<@1> <@&2> <#3>", names)
        self.assertEqual(out, " Вася   Админы   канал общий ")

    def test_nickname_form(self):
        out = bot.resolve_mentions("привет <@!1>", {"1": "Вася"})
        self.assertEqual(out, "привет  Вася ")

    def test_unresolved_left_untouched(self):
        out = bot.resolve_mentions("<@9>", {"1": "Вася"})
        self.assertEqual(out, "<@9>")

    def test_multiple_same_user(self):
        out = bot.resolve_mentions("<@1> и <@1>", {"1": "Вася"})
        self.assertEqual(out, " Вася  и  Вася ")

    def test_empty_map_noop(self):
        self.assertEqual(bot.resolve_mentions("<@1>", {}), "<@1>")


class BuildMapTests(unittest.TestCase):
    def test_from_message(self):
        msg = _fake_message(
            users=[(1, "Вася", "vasya")],
            roles=[(2, "Админы")],
            channels=[(3, "общий")],
        )
        self.assertEqual(
            bot.build_mention_say_map(msg),
            {"1": "Вася", "2": "Админы", "3": "канал общий"},
        )

    def test_from_guild_only_present_tokens(self):
        guild = _fake_guild(
            members=[(1, "Вася", "vasya"), (5, "Петя", "petya")],
            roles=[(2, "Админы")],
            channels=[(3, "общий")],
        )
        # only ids present in the text are resolved
        out = bot.build_mention_say_map_from_guild("<@1> <#3>", guild)
        self.assertEqual(out, {"1": "Вася", "3": "канал общий"})

    def test_from_guild_unknown_id_skipped(self):
        guild = _fake_guild(members=[(1, "Вася", "vasya")])
        self.assertEqual(bot.build_mention_say_map_from_guild("<@9>", guild), {})


class NormalizeIntegrationTests(unittest.TestCase):
    def test_mention_becomes_name(self):
        out = bot.normalize_for_tts("<@1> привет", mentions={"1": "Вася"})
        self.assertEqual(out, "Вася привет")

    def test_without_map_stripped_as_before(self):
        self.assertEqual(bot.normalize_for_tts("<@1> привет"), "привет")

    def test_strip_for_speech_resolves(self):
        out = bot._strip_discord_tokens_for_speech("<@1> ку", None, {"1": "Вася"})
        self.assertEqual(out, "Вася ку")

    def test_mention_only_message_is_spoken(self):
        parsed = bot.analyze_message_for_merge("<@1>", None, {"1": "Вася"})
        self.assertEqual(parsed.spoken_text, "Вася")
        self.assertTrue(parsed.is_mention_only)

    def test_unresolved_mention_only_stays_empty(self):
        parsed = bot.analyze_message_for_merge("<@1>", None, {})
        self.assertEqual(parsed.spoken_text, "")
        self.assertTrue(parsed.is_mention_only)


if __name__ == "__main__":
    unittest.main()
