"""Unit tests for the /voicebot stats menu (embed builders + view).

Network- and Discord-runtime-free: the embed builders read live bot state
(cache/cloud may be None when MiniMax/cache are disabled, as in CI) and must
not raise; the view must expose the three tab buttons.
"""

from __future__ import annotations

import importlib.util
import types
import unittest
from pathlib import Path

import discord


def load_bot_module():
    module_path = Path(__file__).with_name("bot.py")
    spec = importlib.util.spec_from_file_location("tts_bot_module_stats", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


bot = load_bot_module()


class FormatTests(unittest.TestCase):
    def test_uptime(self):
        self.assertEqual(bot._fmt_uptime(0), "0м")
        self.assertEqual(bot._fmt_uptime(90), "1м")
        self.assertEqual(bot._fmt_uptime(3 * 3600 + 12 * 60), "3ч 12м")
        self.assertEqual(bot._fmt_uptime(86400 + 3600), "1д 1ч 0м")

    def test_int_grouping(self):
        self.assertEqual(bot._fmt_int(12480), "12 480")
        self.assertEqual(bot._fmt_int(5), "5")


class EmbedBuilderTests(unittest.TestCase):
    def test_stats_embed_no_cache_no_cloud(self):
        embed = bot._build_stats_embed()
        self.assertIsInstance(embed, discord.Embed)
        names = [f.name for f in embed.fields]
        self.assertIn("Очередь", names)
        self.assertIn("Аптайм", names)

    def test_settings_embed(self):
        embed = bot._build_settings_embed()
        self.assertIsInstance(embed, discord.Embed)
        names = [f.name for f in embed.fields]
        self.assertIn("Склейка сообщений", names)
        self.assertIn("Лимит символов", names)

    def test_voices_embed_without_guild(self):
        embed = bot._build_voices_embed(None)
        self.assertIsInstance(embed, discord.Embed)
        names = [f.name for f in embed.fields]
        self.assertIn("Всего голосов", names)
        self.assertIn("Fallback", names)

    def test_voices_embed_with_guild(self):
        guild = types.SimpleNamespace(id=42)
        embed = bot._build_voices_embed(guild)
        names = [f.name for f in embed.fields]
        self.assertIn("По умолчанию", names)
        self.assertIn("Алиасов эмодзи", names)


class StatsViewTests(unittest.IsolatedAsyncioTestCase):
    async def test_three_tab_buttons(self):
        # discord.ui.View.__init__ needs a running loop (creates a future)
        view = bot.StatsView(None, author_id=7)
        buttons = [c for c in view.children if isinstance(c, discord.ui.Button)]
        self.assertEqual(len(buttons), 3)
        self.assertEqual(view.author_id, 7)
        self.assertEqual(view.timeout, 120)


if __name__ == "__main__":
    unittest.main()
