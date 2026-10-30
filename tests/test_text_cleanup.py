"""One text cleanup for speech, applied the same way on every path.

normalize_for_tts and the message path's own token stripper differed
(www. links, the legacy emoji names), expanded emoji before removing
links, produced "Привет!. Как" and ". ." from newlines, spoke a lone "."
for a message of unaliased emoji, kept invisible characters and cut the
text in the middle of a word.
"""

from __future__ import annotations

import unittest

from ttsbot.textnorm import normalize_for_tts, speakable_text


class TextCleanupTests(unittest.TestCase):
    def test_an_emoji_inside_a_link_goes_with_the_link(self):
        self.assertEqual(normalize_for_tts("смотри https://x.com/😀abc ок"), "смотри ок")

    def test_a_newline_ends_a_sentence_only_once(self):
        self.assertEqual(normalize_for_tts("Привет!\nКак дела"), "Привет! Как дела")
        self.assertEqual(normalize_for_tts("раз\nдва"), "раз. два")
        self.assertEqual(normalize_for_tts("раз\n\n\nдва"), "раз. два")

    def test_nothing_speakable_is_nothing(self):
        for raw in ("<:x:1>\n<:y:2>", "...", "?!", " \u200b "):
            self.assertIsNone(normalize_for_tts(raw), repr(raw))
            self.assertEqual(speakable_text(raw), "", repr(raw))

    def test_invisible_characters_are_removed(self):
        self.assertEqual(normalize_for_tts("при\u200bвет\u2060"), "привет")

    def test_a_long_text_is_cut_between_words(self):
        self.assertEqual(normalize_for_tts("раз два три четыре", max_chars=13), "раз два три")

    def test_a_single_digit_is_still_spoken(self):
        self.assertEqual(normalize_for_tts("1"), "один")

    def test_every_path_cleans_the_same_way(self):
        aliases = {"1": "кекис"}
        mentions = {"5": "Вася"}
        for raw in ("<:kekw:9> www.site.ru <@5> привет", "<:Kekis:1>!\nну", "го 🎉\n<#7> https://a.b"):
            self.assertEqual(
                normalize_for_tts(raw, emoji_aliases=aliases, mentions=mentions) or "",
                speakable_text(raw, aliases, mentions), raw,
            )


if __name__ == "__main__":
    unittest.main()
