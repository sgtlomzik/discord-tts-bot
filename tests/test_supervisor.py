"""Long-running workers are restarted when they crash, and visible.

A crashed generation, playback or transcription worker used to die
silently: the bot stayed online but never spoke again.
"""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock

from test_bot import load_bot_module
from ttsbot.supervisor import TaskSupervisor


class TaskSupervisorTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_crashed_worker_is_restarted(self):
        sup = TaskSupervisor(base_delay=0.01)
        runs = []
        forever = asyncio.Event()

        async def worker():
            runs.append(1)
            if len(runs) == 1:
                raise RuntimeError("boom")
            await forever.wait()

        sup.start("w", worker)
        for _ in range(100):
            if len(runs) >= 2:
                break
            await asyncio.sleep(0.01)
        state = sup.status()["w"]
        self.assertEqual(len(runs), 2)
        self.assertTrue(state.running)
        self.assertEqual(state.restarts, 1)
        self.assertIn("boom", state.last_error)
        await sup.stop_all()

    async def test_stop_all_cancels_without_restarting(self):
        sup = TaskSupervisor(base_delay=0.01)
        runs = []

        async def worker():
            runs.append(1)
            await asyncio.Event().wait()

        sup.start("w", worker)
        await asyncio.sleep(0.02)
        await sup.stop_all()
        await asyncio.sleep(0.05)
        self.assertEqual(len(runs), 1)
        self.assertFalse(sup.status()["w"].running)

    async def test_a_worker_that_returns_is_not_restarted(self):
        sup = TaskSupervisor(base_delay=0.01)
        runs = []

        async def worker():
            runs.append(1)

        sup.start("w", worker)
        await asyncio.sleep(0.05)
        self.assertEqual(len(runs), 1)
        self.assertFalse(sup.status()["w"].running)
        self.assertEqual(sup.status()["w"].restarts, 0)

    async def test_restart_delay_grows_and_is_capped(self):
        sup = TaskSupervisor(base_delay=1.0, max_delay=4.0)
        self.assertEqual([sup.next_delay(n) for n in range(1, 6)], [1.0, 2.0, 4.0, 4.0, 4.0])


class BotWorkersTests(unittest.IsolatedAsyncioTestCase):
    async def test_setup_hook_runs_the_pipeline_workers_under_supervision(self):
        bot_mod = load_bot_module()
        tts_bot = bot_mod.TTSBot()
        tts_bot.tree.sync = AsyncMock(return_value=[])
        await tts_bot.setup_hook()
        try:
            names = set(tts_bot.supervisor.status())
            self.assertTrue({"tts-generation", "tts-playback"} <= names)
        finally:
            await tts_bot.supervisor.stop_all()


if __name__ == "__main__":
    unittest.main()
