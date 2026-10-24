"""A recording failure must never take the voice connection with it.

_start_recording() ran unguarded inside connect(): a full disk or a bad
RECORDING_DIR raised after the voice connection was up, ensure_voice set
a cooldown and TTS stopped working although only recording had failed.
"""

from __future__ import annotations

import types
import unittest
from unittest.mock import MagicMock, patch

from ttsbot.recording.client import RecordingVoiceClient


def _client():
    client = RecordingVoiceClient.__new__(RecordingVoiceClient)
    client._ssrc_users = {}
    client._ignored_ssrcs = set()
    client._pending = {}
    client._dave_warned = set()
    client._dave_held = {}
    client._named = set()
    client._recorder = None
    client._listening = False
    client._connection = types.SimpleNamespace(add_socket_listener=MagicMock())
    guild = types.SimpleNamespace(id=1, name="g")
    client.channel = types.SimpleNamespace(id=2, name="c", guild=guild)
    return client


class RecordingStartFailureTests(unittest.TestCase):
    def test_a_recorder_that_cannot_start_is_logged_not_raised(self):
        client = _client()
        recorder = MagicMock()
        recorder.start.side_effect = OSError("No space left on device")
        with patch("ttsbot.recording.client.ChannelRecorder", return_value=recorder):
            with self.assertLogs("tts_bot", level="ERROR"):
                client._start_recording()
        self.assertIsNone(client._recorder)
        client._connection.add_socket_listener.assert_not_called()

    def test_a_recorder_that_cannot_be_built_is_logged_not_raised(self):
        client = _client()
        with patch("ttsbot.recording.client.ChannelRecorder", side_effect=PermissionError("RECORDING_DIR")):
            with self.assertLogs("tts_bot", level="ERROR"):
                client._start_recording()
        self.assertIsNone(client._recorder)

    def test_a_working_recorder_starts_listening(self):
        client = _client()
        recorder = MagicMock()
        with patch("ttsbot.recording.client.ChannelRecorder", return_value=recorder):
            client._start_recording()
        self.assertIs(client._recorder, recorder)
        client._connection.add_socket_listener.assert_called_once()


if __name__ == "__main__":
    unittest.main()
