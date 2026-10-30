"""docker stop closes the bot cleanly and its recordings are finished.

The bot is PID 1 in its container and had no SIGTERM handler, so docker
stop killed it after 10 s; even a clean close left the recorder's stop to
a daemon thread the exiting process did not wait for. The open session
was only recovered on the next start.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

from test_bot import load_bot_module

from ttsbot import app
from ttsbot.recording.client import RecordingVoiceClient


class FakeBot:
    def __init__(self):
        self.stopped = asyncio.Event()
        self.close = AsyncMock(side_effect=self.stopped.set)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def start(self, token):
        await self.stopped.wait()


@unittest.skipIf(sys.platform == "win32", "asyncio has no signal handlers on Windows")
class SigtermTests(unittest.IsolatedAsyncioTestCase):
    async def test_sigterm_closes_the_bot_once(self):
        bot = FakeBot()
        served = asyncio.create_task(app.serve(bot, "token"))
        await asyncio.sleep(0)  # let serve() install its handlers

        with self.assertLogs("tts_bot", "INFO") as logs:
            os.kill(os.getpid(), signal.SIGTERM)
            os.kill(os.getpid(), signal.SIGTERM)  # a second signal changes nothing
            await asyncio.wait_for(served, 5)

        bot.close.assert_awaited_once()
        self.assertTrue(any("SIGTERM" in line for line in logs.output))


class StopRecordingTests(unittest.IsolatedAsyncioTestCase):
    async def test_stop_recording_waits_for_the_recorder(self):
        client = RecordingVoiceClient.__new__(RecordingVoiceClient)
        recorder = MagicMock()
        client._recorder = recorder

        await client.stop_recording()

        recorder.stop.assert_called_once_with()
        self.assertIsNone(client._recorder)
        await client.stop_recording()  # nothing left to stop
        recorder.stop.assert_called_once_with()


class CloseFinishesRecordingsTests(unittest.IsolatedAsyncioTestCase):
    def _bot(self, *voice_clients):
        bot = load_bot_module().TTSBot()
        p = patch.object(type(bot), "voice_clients", new_callable=PropertyMock,
                         return_value=list(voice_clients))
        p.start()
        self.addCleanup(p.stop)
        return bot

    async def test_close_finishes_every_recording(self):
        recording = MagicMock(stop_recording=AsyncMock())
        plain = MagicMock(spec=["disconnect"])  # a voice client without recording
        bot = self._bot(recording, plain)

        await bot.close()

        recording.stop_recording.assert_awaited_once_with()

    async def test_a_failed_recording_stop_does_not_stop_the_close(self):
        broken = MagicMock(stop_recording=AsyncMock(side_effect=OSError("disk")))
        other = MagicMock(stop_recording=AsyncMock())
        bot = self._bot(broken, other)

        with self.assertLogs("tts_bot", "ERROR"):
            await bot.close()

        other.stop_recording.assert_awaited_once_with()
        self.assertTrue(bot.is_closed())


if __name__ == "__main__":
    unittest.main()
