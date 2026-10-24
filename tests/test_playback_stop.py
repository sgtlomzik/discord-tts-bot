"""A stopped player must never hang the playback worker.

The player is stopped by `!tts stop`, `/voicebot off`, a kick or a failed
voice prepare while a streamed message may still be producing batches.
"""

from __future__ import annotations

import asyncio
import types
import unittest
from unittest.mock import AsyncMock, MagicMock

from test_bot import load_bot_module
from ttsbot import playback
from ttsbot.audio import OPUS_SILENCE_FRAME, ContinuousTTSAudioSource


def _job(bot_mod, gid=1):
    ch = types.SimpleNamespace(id=10, guild=types.SimpleNamespace(id=gid))
    return bot_mod.TTSJob("привет", ch, 0.0, 1, gid, 1000, "fish-default", 0.0)


class StoppedSourceTests(unittest.IsolatedAsyncioTestCase):
    async def test_enqueue_after_stop_is_ignored(self):
        source = ContinuousTTSAudioSource(OPUS_SILENCE_FRAME, opus=True)
        source.stop()
        source.enqueue_frames([OPUS_SILENCE_FRAME] * 3)
        self.assertTrue(source.frames.empty())
        drained = await asyncio.wait_for(source.wait_until_drained(1.0), 2.0)
        self.assertTrue(drained)


class PlayPreparedStopTests(unittest.IsolatedAsyncioTestCase):
    async def test_returns_when_source_is_stopped_mid_stream(self):
        bot_mod = load_bot_module()
        tts_bot = bot_mod.TTSBot()
        source = ContinuousTTSAudioSource(OPUS_SILENCE_FRAME, opus=True)
        tts_bot.ensure_voice = AsyncMock(return_value=MagicMock())
        tts_bot.ensure_continuous_player = MagicMock(return_value=source)
        prepared = bot_mod.PreparedAudio(job=_job(bot_mod), channel=asyncio.Queue())

        await prepared.channel.put([OPUS_SILENCE_FRAME] * 2)
        play = asyncio.create_task(tts_bot._play_prepared(prepared))
        await asyncio.sleep(0.05)
        source.stop()  # e.g. `!tts stop` while the stream is still arriving
        await prepared.channel.put([OPUS_SILENCE_FRAME] * 2)
        await prepared.channel.put(None)

        await asyncio.wait_for(play, 2.0)
        self.assertLessEqual(source.frames.qsize(), 2)

    async def test_drain_wait_is_bounded_when_nothing_reads(self):
        bot_mod = load_bot_module()
        tts_bot = bot_mod.TTSBot()
        source = ContinuousTTSAudioSource(OPUS_SILENCE_FRAME, opus=True)  # never read
        tts_bot.ensure_voice = AsyncMock(return_value=MagicMock())
        tts_bot.ensure_continuous_player = MagicMock(return_value=source)
        prepared = bot_mod.PreparedAudio(job=_job(bot_mod), channel=asyncio.Queue())
        await prepared.channel.put([OPUS_SILENCE_FRAME] * 5)
        await prepared.channel.put(None)

        old = playback.DRAIN_SLACK_SECONDS
        playback.DRAIN_SLACK_SECONDS = 0.2
        try:
            await asyncio.wait_for(tts_bot._play_prepared(prepared), 2.0)
        finally:
            playback.DRAIN_SLACK_SECONDS = old


class DisconnectCancelsTests(unittest.IsolatedAsyncioTestCase):
    async def test_disconnect_cancels_the_guilds_prepared_jobs(self):
        bot_mod = load_bot_module()
        tts_bot = bot_mod.TTSBot()
        mine = bot_mod.PreparedAudio(job=_job(bot_mod, gid=7), channel=asyncio.Queue())
        other = bot_mod.PreparedAudio(job=_job(bot_mod, gid=8), channel=asyncio.Queue())
        tts_bot.active_prepared.update({mine, other})
        guild = types.SimpleNamespace(id=7)

        await tts_bot.disconnect_guild_voice(guild)

        self.assertTrue(mine.cancelled)
        self.assertFalse(other.cancelled)


if __name__ == "__main__":
    unittest.main()
