"""Text is cleaned once at the enqueue boundary; status fits one guild.

A Discord message was cleaned twice (speakable_text, then
normalize_for_tts in enqueue_tts), so a resolved mention such as a display
name ``www.foo`` was removed as a URL by the second pass. /voicebot test
cut its text at TTS_MAX_TEXT_LENGTH before /voicebot limit applied.
/voicebot status listed two workers for every guild that had ever spoken:
past ~20 guilds the reply was over Discord's 2000 characters and failed,
and it showed every other server's id to anyone.
"""

from __future__ import annotations

import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from support import load_bot_module
from ttsbot.supervisor import WorkerState


def _channel(guild_id):
    return types.SimpleNamespace(id=guild_id * 10, guild=types.SimpleNamespace(id=guild_id))


class IntakeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bot_mod = load_bot_module()
        self.bot = self.bot_mod.TTSBot()
        self.bot.start_session = self.bot.session_for  # queue only, no workers

    async def test_a_message_is_cleaned_once(self):
        await self.bot.queue_message("<@1> привет", _channel(1), 5, 5, mentions={"1": "www.foo"})
        job = self.bot.session_for(1).queue.get_nowait()
        self.assertEqual(job.text, "www.foo привет")

    async def test_an_unspeakable_message_is_not_queued(self):
        await self.bot.queue_message("https://example.com/x", _channel(1), 5, 5)
        self.assertEqual(self.bot.queue_size(1), 0)

    async def test_the_test_command_uses_the_runtime_limit(self):
        class FakeMember:
            id = 400

        self.bot_mod.bot.config_store.set_tts_max_chars(1000)
        self.addCleanup(self.bot_mod.config.reload)
        target = _channel(100)
        interaction = types.SimpleNamespace(
            guild=types.SimpleNamespace(id=100, members=[]), user=FakeMember(), channel_id=300,
            response=types.SimpleNamespace(send_message=AsyncMock()),
        )
        self.bot_mod.bot.config_store.is_allowed = MagicMock(return_value=True)
        self.bot_mod.bot.start_session = self.bot_mod.bot.session_for
        text = "слово " * 150  # 900 characters, over the 500 default
        with (
            patch.object(self.bot_mod.discord, "Member", FakeMember),
            patch.object(self.bot_mod.tts_commands, "resolve_tts_command_voice_channel",
                         MagicMock(return_value=target)),
        ):
            await self.bot_mod.slash_tts_test.callback(interaction, text)
        job = self.bot_mod.bot.session_for(100).queue.get_nowait()
        self.assertGreater(len(job.text), 800)


class StatusCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_status_shows_this_guilds_workers_and_fits_a_message(self):
        bot_mod = load_bot_module()
        states = {"tts-warmup": WorkerState(running=True), "stt-worker": WorkerState(running=True)}
        for guild_id in range(1000, 1060):
            for kind in ("generation", "playback"):
                states[f"tts-{kind}:{guild_id}"] = WorkerState(running=True, restarts=3, last_error="x" * 200)
        bot_mod.bot.supervisor.status = MagicMock(return_value=states)
        interaction = types.SimpleNamespace(
            guild=types.SimpleNamespace(id=1005),
            response=types.SimpleNamespace(send_message=AsyncMock()),
        )
        await bot_mod.slash_tts_status.callback(interaction)
        text = interaction.response.send_message.await_args.args[0]
        self.assertLessEqual(len(text), 2000)
        self.assertIn("tts-playback:1005", text)
        self.assertIn("stt-worker", text)
        self.assertNotIn("1006", text)  # other servers' ids stay private


if __name__ == "__main__":
    unittest.main()
