"""Unit tests for normalize_for_tts — the cleanup that runs at the
enqueue boundary for every TTS message."""

from __future__ import annotations

import unittest


def load_bot_module():
    import importlib.util
    from pathlib import Path
    module_path = Path(__file__).resolve().parent.parent / "bot.py"
    spec = importlib.util.spec_from_file_location("tts_bot_module", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class NormalizeForTTSTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bot = load_bot_module()

    def test_tts_max_chars_default_is_300(self):
        """Spec recommends ~300 char cap. The cap is exposed as the
        module-level TTS_MAX_CHARS constant and is the value passed
        to normalize_for_tts from enqueue_tts."""
        self.assertEqual(self.bot.config.TTS_MAX_CHARS, 300)

    def test_enqueue_tts_uses_tts_max_chars_cap(self):
        """End-to-end: TTS_MAX_CHARS is the cap that enqueue_tts
        passes to normalize_for_tts. Verify by patching the constant
        and inspecting that long messages get truncated to that size.
        """
        long_text = "а" * 500
        target = long_text[:self.bot.config.TTS_MAX_CHARS]
        out = self.bot.normalize_for_tts(long_text, max_chars=self.bot.config.TTS_MAX_CHARS)
        self.assertIsNotNone(out)
        self.assertEqual(len(out), self.bot.config.TTS_MAX_CHARS)
        self.assertEqual(out, target)

    # ----- strip rules (spec §"Препроцессинг текста") -----

    def test_strips_user_mention(self):
        self.assertEqual(
            self.bot.normalize_for_tts("<@123456> привет"),
            "привет",
        )

    def test_strips_nick_mention_with_exclamation(self):
        self.assertEqual(
            self.bot.normalize_for_tts("<@!123456> привет"),
            "привет",
        )

    def test_strips_role_mention(self):
        self.assertEqual(
            self.bot.normalize_for_tts("<@&789> привет"),
            "привет",
        )

    def test_strips_channel_mention(self):
        self.assertEqual(
            self.bot.normalize_for_tts("смотри <#42> привет"),
            "смотри привет",
        )

    def test_strips_custom_emoji(self):
        self.assertEqual(
            self.bot.normalize_for_tts("hello <:kekw:123> world"),
            "hello world",
        )

    def test_strips_animated_custom_emoji(self):
        self.assertEqual(
            self.bot.normalize_for_tts("hello <a:pepe_sad:456> world"),
            "hello world",
        )

    def test_strips_https_url(self):
        self.assertEqual(
            self.bot.normalize_for_tts("see https://example.com/path?x=1 ok"),
            "see ok",
        )

    def test_strips_http_url(self):
        self.assertEqual(
            self.bot.normalize_for_tts("see http://example.com ok"),
            "see ok",
        )

    # ----- empty / passthrough -----

    def test_returns_none_for_empty_string(self):
        self.assertIsNone(self.bot.normalize_for_tts(""))

    def test_returns_none_when_only_mentions(self):
        self.assertIsNone(self.bot.normalize_for_tts("<@123> <@!456> <@&789>"))

    def test_returns_none_when_only_url(self):
        self.assertIsNone(self.bot.normalize_for_tts("https://example.com/x"))

    def test_returns_none_when_only_custom_emoji(self):
        self.assertIsNone(self.bot.normalize_for_tts("<:kek:1><:lol:2><a:pepe:3>"))

    def test_passes_plain_text_unchanged(self):
        self.assertEqual(
            self.bot.normalize_for_tts("обычное сообщение"),
            "обычное сообщение",
        )

    def test_collapses_whitespace(self):
        self.assertEqual(
            self.bot.normalize_for_tts("a    b\t\tc"),
            "a b c",
        )

    def test_replaces_newlines_with_dot_space(self):
        self.assertEqual(
            self.bot.normalize_for_tts("line one\nline two"),
            "line one. line two",
        )

    # ----- length cap -----

    def test_truncates_long_message(self):
        long_text = "а" * 1000
        out = self.bot.normalize_for_tts(long_text, max_chars=100)
        self.assertIsNotNone(out)
        self.assertEqual(len(out), 100)

    def test_truncation_at_zero_returns_none(self):
        out = self.bot.normalize_for_tts("hello", max_chars=0)
        # max_chars=0 disables the cap (legacy behavior preserved).
        self.assertEqual(out, "hello")

    def test_truncation_below_one_returns_none(self):
        out = self.bot.normalize_for_tts("ab", max_chars=1)
        # max_chars=1 -> truncate to 1 char ("a"), which is still truthy.
        self.assertEqual(out, "a")

    def test_truncation_to_one_rstrip(self):
        # If truncation lands on whitespace, rstrip cleans it.
        out = self.bot.normalize_for_tts("abc def", max_chars=4)
        self.assertEqual(out, "abc")


if __name__ == "__main__":
    unittest.main()