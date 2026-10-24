"""Settings changed at runtime live in the store, not in ``config``.

``/voicebot limit`` used to write its value into ``config.TTS_MAX_CHARS``
(and lift ``config.MAX_TEXT_LENGTH``), so the env-derived config module
doubled as a data bus: which value was in effect depended on whether the
store had been loaded since the last ``config.reload()``.
"""

from __future__ import annotations

import re
import types
import unittest
from pathlib import Path

from ttsbot import config
from test_bot import load_bot_module, temp_config_store

PACKAGE = Path(__file__).resolve().parent.parent / "ttsbot"


class StoredLimitTests(unittest.TestCase):
    def setUp(self):
        self.bot_mod = load_bot_module()
        self.store = temp_config_store(self.bot_mod)

    def tearDown(self):
        config.reload()

    def test_the_limit_defaults_to_the_env_setting(self):
        self.assertEqual(self.store.max_chars(), config.TTS_MAX_CHARS)

    def test_setting_the_limit_leaves_config_alone(self):
        before = (config.TTS_MAX_CHARS, config.MAX_TEXT_LENGTH)
        self.store.set_tts_max_chars(800)
        self.assertEqual(self.store.max_chars(), 800)
        self.assertEqual((config.TTS_MAX_CHARS, config.MAX_TEXT_LENGTH), before)

    def test_the_limit_survives_a_restart(self):
        self.store.set_tts_max_chars(800)
        reloaded = self.bot_mod.BotConfigStore(self.store.db, set())
        self.assertEqual(reloaded.max_chars(), 800)


class IntakeLimitTests(unittest.IsolatedAsyncioTestCase):
    async def test_messages_are_capped_at_the_stored_limit(self):
        bot_mod = load_bot_module()
        tts_bot = bot_mod.TTSBot()
        tts_bot.config_store = temp_config_store(bot_mod)
        tts_bot.start_session = tts_bot.session_for  # no workers: inspect the queue
        tts_bot.config_store.set_tts_max_chars(12)
        channel = types.SimpleNamespace(id=10, guild=types.SimpleNamespace(id=1))
        self.assertTrue(await tts_bot.enqueue_tts("раз два три четыре пять", channel, 1, 1))
        job = tts_bot.session_for(1).queue.get_nowait()
        self.assertLessEqual(len(job.text), 12)
        config.reload()


class ConfigIsReadOnlyTests(unittest.TestCase):
    def test_no_runtime_module_assigns_to_config(self):
        assignment = re.compile(r"\bconfig\.[A-Z][A-Z0-9_]*\s*=(?!=)")
        offenders = [
            f"{path.relative_to(PACKAGE.parent)}:{number}"
            for path in PACKAGE.rglob("*.py") if path.name != "config.py"
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
            if assignment.search(line)
        ]
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
