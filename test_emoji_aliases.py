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
from unittest.mock import AsyncMock, MagicMock, patch


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


class NormalizeIntegrationTests(unittest.TestCase):
    def test_aliased_emoji_becomes_word(self):
        out = bot.normalize_for_tts("да<:Kekis:1>", emoji_aliases={"1": "кекис"})
        self.assertEqual(out, "да кекис")

    def test_unaliased_emoji_still_stripped(self):
        out = bot.normalize_for_tts("привет <:Other:2>", emoji_aliases={"1": "кекис"})
        self.assertEqual(out, "привет")

    def test_mixed_aliased_and_unaliased(self):
        out = bot.normalize_for_tts(
            "<:K:1> и <:Z:2>", emoji_aliases={"1": "кек"}
        )
        self.assertEqual(out, "кек и")

    def test_no_aliases_strips_everything(self):
        self.assertIsNone(bot.normalize_for_tts("<:K:1>"))

    def test_multiple_occurrences_all_replaced(self):
        out = bot.normalize_for_tts(
            "<:K:1><:K:1>", emoji_aliases={"1": "кек"}
        )
        self.assertEqual(out, "кек кек")


class StripForSpeechIntegrationTests(unittest.TestCase):
    def test_alias_wins_over_emoji_map(self):
        # "kekw" is in the legacy EMOJI_MAP -> "Кек"; an id alias overrides it.
        out = bot._strip_discord_tokens_for_speech("<:kekw:1>", {"1": "кекис"})
        self.assertEqual(out, "кекис")

    def test_emoji_map_fallback_when_no_alias(self):
        out = bot._strip_discord_tokens_for_speech("<:kekw:1>", {})
        self.assertEqual(out, "Кек")

    def test_unknown_unaliased_stripped(self):
        out = bot._strip_discord_tokens_for_speech("hi <:Nope:1> there", {})
        self.assertEqual(out, "hi there")


class MergeAnalysisTests(unittest.TestCase):
    def test_aliased_emoji_only_message_is_spoken(self):
        parsed = bot.analyze_message_for_merge("<:Kekis:1>", {"1": "кекис"})
        # The aliased word survives, so the message is not dropped as empty.
        self.assertEqual(parsed.spoken_text, "кекис")
        self.assertTrue(parsed.is_custom_emoji_only)

    def test_unaliased_emoji_only_message_is_empty(self):
        parsed = bot.analyze_message_for_merge("<:Kekis:1>", {})
        self.assertEqual(parsed.spoken_text, "")
        self.assertTrue(parsed.is_custom_emoji_only)


class CacheKeyTests(unittest.TestCase):
    def test_changing_alias_changes_cache_key(self):
        from tts_providers import TTSPhraseCache

        raw = "смотри <:Kekis:1>"
        text_a = bot.normalize_for_tts(raw, emoji_aliases={"1": "кекис"})
        text_b = bot.normalize_for_tts(raw, emoji_aliases={"1": "лол"})
        self.assertNotEqual(text_a, text_b)
        key_a = TTSPhraseCache.hash_text(text_a, "bussshy01")
        key_b = TTSPhraseCache.hash_text(text_b, "bussshy01")
        # Different normalized text -> different key -> no stale audio served.
        self.assertNotEqual(key_a, key_b)
        # Same alias is stable (a repeat hits the same cache entry).
        self.assertEqual(
            key_a, TTSPhraseCache.hash_text(text_a, "bussshy01")
        )


class _FakeMember:
    def __init__(self, user_id=400):
        self.id = user_id


def _make_interaction(bot_mod, guild_emojis=()):
    sent = []
    guild = types.SimpleNamespace(
        id=100,
        emojis=[types.SimpleNamespace(id=eid, name=name) for eid, name in guild_emojis],
    )
    interaction = types.SimpleNamespace(
        guild=guild,
        user=_FakeMember(),
        channel_id=300,
        response=types.SimpleNamespace(
            send_message=AsyncMock(side_effect=lambda *a, **k: sent.append(a[0] if a else "")),
        ),
    )
    return interaction, sent


class EmojiAliasCommandTests(unittest.IsolatedAsyncioTestCase):
    def _setup(self):
        bot_mod = load_bot_module()
        tmp = tempfile.TemporaryDirectory()
        store = bot_mod.BotConfigStore(Path(tmp.name) / "config.json", set())
        store._tmp_dir = tmp
        bot_mod.bot.config_store = store
        return bot_mod, store

    async def _call(self, bot_mod, cmd, *args):
        with patch.object(bot_mod.discord, "Member", _FakeMember), patch.object(
            bot_mod, "is_guild_manager", MagicMock(return_value=True)
        ):
            await cmd.callback(*args)

    async def test_add_via_token(self):
        bot_mod, store = self._setup()
        interaction, sent = _make_interaction(bot_mod)
        await self._call(
            bot_mod, bot_mod.slash_emoji_alias, interaction, "<:Kekis:1035>", "кекис"
        )
        self.assertEqual(store.emoji_aliases, {"1035": {"name": "Kekis", "say": "кекис"}})
        self.assertIn("кекис", sent[0])

    async def test_add_via_shortcode(self):
        bot_mod, store = self._setup()
        interaction, sent = _make_interaction(bot_mod, guild_emojis=[(77, "Pepe")])
        await self._call(
            bot_mod, bot_mod.slash_emoji_alias, interaction, ":Pepe:", "пепе"
        )
        self.assertEqual(store.emoji_aliases, {"77": {"name": "Pepe", "say": "пепе"}})

    async def test_update_overwrites(self):
        bot_mod, store = self._setup()
        interaction, _ = _make_interaction(bot_mod)
        await self._call(bot_mod, bot_mod.slash_emoji_alias, interaction, "<:K:1>", "кек")
        interaction2, _ = _make_interaction(bot_mod)
        await self._call(bot_mod, bot_mod.slash_emoji_alias, interaction2, "<:K:1>", "лол")
        self.assertEqual(store.emoji_aliases["1"]["say"], "лол")

    async def test_reject_unicode_emoji(self):
        bot_mod, store = self._setup()
        interaction, sent = _make_interaction(bot_mod)
        await self._call(bot_mod, bot_mod.slash_emoji_alias, interaction, "😀", "смайл")
        self.assertEqual(store.emoji_aliases, {})
        self.assertIn("не поддерживаются", sent[0])

    async def test_reject_plain_text(self):
        bot_mod, store = self._setup()
        interaction, sent = _make_interaction(bot_mod)
        await self._call(bot_mod, bot_mod.slash_emoji_alias, interaction, "просто слова", "x")
        self.assertEqual(store.emoji_aliases, {})

    async def test_reject_empty_pronunciation(self):
        bot_mod, store = self._setup()
        interaction, sent = _make_interaction(bot_mod)
        # A pronunciation that sanitizes to nothing must be rejected.
        await self._call(bot_mod, bot_mod.slash_emoji_alias, interaction, "<:K:1>", "<@123>")
        self.assertEqual(store.emoji_aliases, {})

    async def test_remove_via_token(self):
        bot_mod, store = self._setup()
        store.set_emoji_alias("1", "K", "кек")
        interaction, sent = _make_interaction(bot_mod)
        await self._call(
            bot_mod, bot_mod.slash_emoji_alias_remove, interaction, "<:K:1>"
        )
        self.assertEqual(store.emoji_aliases, {})
        self.assertIn("удалён", sent[0])

    async def test_remove_via_raw_id(self):
        bot_mod, store = self._setup()
        store.set_emoji_alias("1", "K", "кек")
        interaction, _ = _make_interaction(bot_mod)
        await self._call(bot_mod, bot_mod.slash_emoji_alias_remove, interaction, "1")
        self.assertEqual(store.emoji_aliases, {})

    async def test_remove_unknown(self):
        bot_mod, store = self._setup()
        interaction, sent = _make_interaction(bot_mod)
        await self._call(bot_mod, bot_mod.slash_emoji_alias_remove, interaction, "999")
        self.assertIn("нет", sent[0])

    async def test_list_empty(self):
        bot_mod, store = self._setup()
        interaction, sent = _make_interaction(bot_mod)
        await self._call(bot_mod, bot_mod.slash_emoji_aliases, interaction)
        self.assertIn("не заданы", sent[0])

    async def test_list_shows_entries(self):
        bot_mod, store = self._setup()
        store.set_emoji_alias("1", "Kekis", "кекис")
        interaction, sent = _make_interaction(bot_mod)
        await self._call(bot_mod, bot_mod.slash_emoji_aliases, interaction)
        self.assertIn("кекис", sent[0])


if __name__ == "__main__":
    unittest.main()
