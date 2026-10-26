"""A stopped player must never hang the playback worker.

The player is stopped by `!tts stop`, `/voicebot off`, a kick or a failed
voice prepare while a streamed message may still be producing batches.
"""

from __future__ import annotations

import asyncio
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

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


class IdleTimerTests(unittest.IsolatedAsyncioTestCase):
    """Every played message ends with the idle timers armed again
    (ensure_voice cancelled the idle disconnect), or the bot keeps its
    comfort noise going and stays in an empty channel."""

    def _bot(self):
        bot_mod = load_bot_module()
        tts_bot = bot_mod.TTSBot()
        tts_bot.ensure_voice = AsyncMock(return_value=MagicMock())
        tts_bot.schedule_continuous_idle_stop = MagicMock()
        tts_bot.schedule_idle_disconnect = MagicMock()
        return bot_mod, tts_bot

    async def test_timers_are_armed_once_a_stalled_player_drains(self):
        bot_mod, tts_bot = self._bot()
        source = ContinuousTTSAudioSource(OPUS_SILENCE_FRAME, opus=True)  # stalled: nobody reads

        def player(vc, initial_frames=None):
            source.enqueue_frames(initial_frames)
            return source

        tts_bot.ensure_continuous_player = MagicMock(side_effect=player)
        prepared = bot_mod.PreparedAudio(job=_job(bot_mod), channel=asyncio.Queue())
        await prepared.channel.put([OPUS_SILENCE_FRAME] * 3)
        await prepared.channel.put(None)

        old = playback.DRAIN_SLACK_SECONDS
        playback.DRAIN_SLACK_SECONDS = 0.1
        try:
            with self.assertLogs("tts_bot", "WARNING"):
                await asyncio.wait_for(tts_bot._play_prepared(prepared), 2.0)
        finally:
            playback.DRAIN_SLACK_SECONDS = old
        tts_bot.schedule_idle_disconnect.assert_not_called()  # audio still pending

        for _ in range(4):  # the voice connection recovers and plays the rest
            source.read()
        for _ in range(100):
            if tts_bot.schedule_idle_disconnect.called:
                break
            await asyncio.sleep(0.02)
        tts_bot.schedule_continuous_idle_stop.assert_called_once()
        tts_bot.schedule_idle_disconnect.assert_called_once()

    async def test_timers_are_armed_after_a_stop(self):
        bot_mod, tts_bot = self._bot()
        source = ContinuousTTSAudioSource(OPUS_SILENCE_FRAME, opus=True)
        tts_bot.ensure_continuous_player = MagicMock(return_value=source)
        prepared = bot_mod.PreparedAudio(job=_job(bot_mod), channel=asyncio.Queue())
        await prepared.channel.put([OPUS_SILENCE_FRAME] * 2)
        play = asyncio.create_task(tts_bot._play_prepared(prepared))
        await asyncio.sleep(0.05)
        source.stop()  # `!tts stop`: the bot stays in the channel
        await prepared.channel.put(None)
        await asyncio.wait_for(play, 2.0)
        tts_bot.schedule_idle_disconnect.assert_called_once()


class PlaybackFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_failed_job_keeps_the_rest_of_the_guilds_queue(self):
        bot_mod = load_bot_module()
        tts_bot = bot_mod.TTSBot()
        tts_bot.wait_until_ready = AsyncMock()
        session = tts_bot.session_for(7)
        first = bot_mod.PreparedAudio(job=_job(bot_mod, gid=7), channel=asyncio.Queue())
        second = bot_mod.PreparedAudio(job=_job(bot_mod, gid=7), channel=asyncio.Queue())
        session.active.update({first, second})
        played = []

        async def play(prepared):
            played.append(prepared)
            if prepared is first:
                raise RuntimeError("Not connected to voice.")

        tts_bot._play_prepared = AsyncMock(side_effect=play)
        worker = asyncio.create_task(tts_bot._playback_worker(session))
        with self.assertLogs("tts_bot", "ERROR"):
            await session.ready.put(first)
            await session.ready.put(second)
            await asyncio.wait_for(session.ready.join(), 2.0)
        worker.cancel()
        self.assertEqual(played, [first, second])
        self.assertTrue(first.cancelled)  # its generation stops
        self.assertFalse(second.cancelled)

    async def test_a_failed_voice_prepare_keeps_the_rest_of_the_guilds_queue(self):
        bot_mod = load_bot_module()
        tts_bot = bot_mod.TTSBot()
        failing = bot_mod.PreparedAudio(job=_job(bot_mod, gid=7), channel=asyncio.Queue())
        waiting = bot_mod.PreparedAudio(job=_job(bot_mod, gid=7), channel=asyncio.Queue())
        tts_bot.session_for(7).active.update({failing, waiting})
        tts_bot.ensure_voice = AsyncMock(side_effect=RuntimeError("voice handshake timed out"))
        connected = MagicMock(guild=types.SimpleNamespace(id=7))
        connected.is_connected.return_value = True
        connected.disconnect = AsyncMock()
        await failing.channel.put(None)
        with patch.object(type(tts_bot), "voice_clients", new_callable=PropertyMock, return_value=[connected]):
            with self.assertLogs("tts_bot", "ERROR"):
                await tts_bot._play_prepared(failing)
        connected.disconnect.assert_awaited()  # the broken connection is dropped
        self.assertTrue(failing.cancelled)  # its generation stops instead of running on
        self.assertFalse(waiting.cancelled)  # and the next message reconnects


class DisconnectCancelsTests(unittest.IsolatedAsyncioTestCase):
    async def test_disconnect_cancels_the_guilds_prepared_jobs(self):
        bot_mod = load_bot_module()
        tts_bot = bot_mod.TTSBot()
        mine = bot_mod.PreparedAudio(job=_job(bot_mod, gid=7), channel=asyncio.Queue())
        other = bot_mod.PreparedAudio(job=_job(bot_mod, gid=8), channel=asyncio.Queue())
        tts_bot.session_for(7).active.add(mine)
        tts_bot.session_for(8).active.add(other)
        guild = types.SimpleNamespace(id=7)

        await tts_bot.disconnect_guild_voice(guild)

        self.assertTrue(mine.cancelled)
        self.assertFalse(other.cancelled)


if __name__ == "__main__":
    unittest.main()
