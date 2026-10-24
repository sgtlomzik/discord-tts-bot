"""Every message is queued as it arrives, in order.

The legacy merge and selective-hold layers held short messages back to
glue them together. Production ran with TTS_MERGE_ALGORITHM=off, so they
were dead code with their own bugs; a leftover setting must not bring
them back.
"""

from __future__ import annotations

import types
import unittest
from unittest.mock import patch

from test_bot import load_bot_module, temp_config_store


class MessageIntakeTests(unittest.IsolatedAsyncioTestCase):
    async def _queue(self, env, texts):
        with patch.dict("os.environ", env):
            bot_mod = load_bot_module()
        tts_bot = bot_mod.TTSBot()
        tts_bot.config_store = temp_config_store(bot_mod, {300})
        channel = types.SimpleNamespace(id=200, guild=types.SimpleNamespace(id=100))
        for text in texts:
            await tts_bot.queue_message(text, channel, 300, 400)
        jobs = []
        while not tts_bot.message_queue.empty():
            jobs.append(tts_bot.message_queue.get_nowait().text)
        return jobs

    async def test_short_messages_are_queued_at_once_and_in_order(self):
        jobs = await self._queue({}, ["ну там", "просто", "дается хп"])
        self.assertEqual(jobs, ["ну там", "просто", "дается хп"])

    async def test_an_old_merge_setting_changes_nothing(self):
        for algorithm in ("legacy", "selective_hold_v2"):
            jobs = await self._queue({"TTS_MERGE_ALGORITHM": algorithm}, ["one", "two"])
            self.assertEqual(jobs, ["one", "two"], algorithm)

    async def test_nothing_to_say_is_not_queued(self):
        jobs = await self._queue({}, ["https://example.com/aaaa"])
        self.assertEqual(jobs, [])


if __name__ == "__main__":
    unittest.main()
