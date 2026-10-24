"""Each guild has its own session: queue, prefetch and player loop.

One message queue and one generation and playback worker served every
guild, so a long message (or a stuck player) in one guild held up all the
others, and a guild's next message was only generated once the previous
one had finished downloading.
"""

from __future__ import annotations

import asyncio
import types
import unittest
from unittest.mock import AsyncMock

from support import load_bot_module


def _channel(guild_id):
    return types.SimpleNamespace(id=guild_id * 10, guild=types.SimpleNamespace(id=guild_id))


async def _until(condition, timeout=2.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not condition():
        if loop.time() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.01)


class GuildSessionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bot_mod = load_bot_module()
        self.bot = self.bot_mod.TTSBot()
        self.bot.wait_until_ready = AsyncMock()
        self.bot.warmup_tts = AsyncMock()
        self.played = []

    async def asyncTearDown(self):
        await self.bot.supervisor.stop_all()

    async def _drain_play(self, prepared):
        while await prepared.channel.get() is not None:
            pass
        self.played.append((prepared.job.guild_id, prepared.job.text))

    async def test_a_busy_guild_does_not_hold_up_another(self):
        release = asyncio.Event()

        async def prepare(prepared):
            await prepared.channel.put(None)

        async def play(prepared):
            await self._drain_play(prepared)
            if prepared.job.guild_id == 1:
                await release.wait()  # guild 1's player is stuck

        self.bot._prepare_into = prepare
        self.bot._play_prepared = play
        await self.bot.enqueue_tts("первый", _channel(1), 1, 1)
        await self.bot.enqueue_tts("второй", _channel(1), 1, 1)
        await self.bot.enqueue_tts("другой", _channel(2), 2, 2)
        await _until(lambda: (2, "другой") in self.played)
        self.assertNotIn((1, "второй"), self.played)
        release.set()
        await _until(lambda: (1, "второй") in self.played)

    async def test_the_next_message_is_generated_while_one_downloads_in_order(self):
        finished = []

        async def prepare(prepared):
            await asyncio.sleep(0.3 if prepared.job.text == "один" else 0.01)
            finished.append(prepared.job.text)
            await prepared.channel.put(None)

        self.bot._prepare_into = prepare
        self.bot._play_prepared = self._drain_play
        for text in ("один", "два", "три"):
            await self.bot.enqueue_tts(text, _channel(1), 1, 1)
        await _until(lambda: len(self.played) == 3)
        self.assertEqual([text for _, text in self.played], ["один", "два", "три"])
        self.assertLess(finished.index("два"), finished.index("один"))  # generated in parallel

    async def test_clearing_a_guild_leaves_the_others_alone(self):
        hold = asyncio.Event()

        async def prepare(prepared):
            await hold.wait()
            await prepared.channel.put(None)

        self.bot._prepare_into = prepare
        self.bot._play_prepared = self._drain_play
        for guild in (1, 2):
            for text in ("а", "б", "в"):
                await self.bot.enqueue_tts(text, _channel(guild), guild, guild)
        await asyncio.sleep(0.05)
        self.assertGreater(self.bot.clear_queue_for_guild(1), 0)
        self.assertTrue(all(p.cancelled for p in self.bot.session_for(1).active))
        self.assertFalse(any(p.cancelled for p in self.bot.session_for(2).active))
        self.assertTrue(self.bot.session_for(2).active)  # guild 2 still has its messages
        self.assertEqual(self.bot.queue_size(1), 0)
        hold.set()

    async def test_session_workers_are_supervised_and_stop_on_close(self):
        self.bot._prepare_into = AsyncMock()
        self.bot._play_prepared = AsyncMock()
        await self.bot.enqueue_tts("привет", _channel(7), 1, 1)
        status = self.bot.supervisor.status()
        self.assertIn("tts-generation:7", status)
        self.assertIn("tts-playback:7", status)
        await self.bot.supervisor.stop_all()
        self.assertFalse(self.bot.supervisor.status()["tts-playback:7"].running)

    def test_the_global_queues_are_gone(self):
        for name in ("message_queue", "ready_queue", "active_prepared"):
            self.assertFalse(hasattr(self.bot, name), name)


if __name__ == "__main__":
    unittest.main()
