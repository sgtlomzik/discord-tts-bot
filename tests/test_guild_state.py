"""A guild's voice state lives in its session, not in parallel dicts.

The bot kept six dicts keyed by guild id (player, connect lock, connect
cooldown, auto-connect suppression, two idle timers) next to the queue
state, each with its own cleanup.
"""

from __future__ import annotations

import time
import types
import unittest
from unittest.mock import MagicMock

from support import load_bot_module


class GuildStateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bot = load_bot_module().TTSBot()

    def test_the_parallel_dicts_are_gone(self):
        for name in ("idle_disconnect_tasks", "continuous_idle_stop_tasks", "voice_connect_locks",
                     "voice_connect_cooldown_until", "suppress_auto_connect_until", "continuous_sources"):
            self.assertFalse(hasattr(self.bot, name), name)

    async def test_cooldowns_and_lock_are_the_sessions(self):
        self.bot.set_voice_connect_cooldown(5, "test")
        self.bot.suppress_auto_connect(5, "test")
        session = self.bot.session_for(5)
        self.assertGreater(session.connect_cooldown_until, time.monotonic())
        self.assertGreater(session.auto_connect_suppressed_until, time.monotonic())
        self.assertIs(self.bot.get_voice_connect_lock(5), session.voice_lock)
        self.assertGreater(self.bot.voice_connect_cooldown_remaining(5), 0)
        self.assertEqual(self.bot.voice_connect_cooldown_remaining(6), 0)

    async def test_looking_at_a_guild_starts_no_workers(self):
        self.bot.session_for(9)
        self.assertEqual(self.bot.supervisor.status(), {})
        self.bot.start_session(9)
        self.assertIn("tts-playback:9", self.bot.supervisor.status())
        await self.bot.supervisor.stop_all()

    async def test_disconnect_stops_the_sessions_player(self):
        player = MagicMock()
        self.bot.session_for(3).player = player
        await self.bot.disconnect_guild_voice(types.SimpleNamespace(id=3))
        player.stop.assert_called_once()
        self.assertIsNone(self.bot.session_for(3).player)


if __name__ == "__main__":
    unittest.main()
