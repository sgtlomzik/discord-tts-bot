"""A provider config never prints its keys.

The configs are dataclasses whose generated repr included api_key (and
the ElevenLabs key list), so any log line or traceback that showed one
leaked the secrets.
"""

from __future__ import annotations

import unittest

from ttsbot.elevenlabs import ElevenLabsConfig
from ttsbot.fish import FishConfig
from ttsbot.gemini import GeminiConfig
from ttsbot.minimax import MiniMaxConfig

SECRET = "sk-live-0123456789abcdef"


class SecretReprTests(unittest.TestCase):
    def test_no_config_shows_its_key(self):
        configs = [
            FishConfig(api_key=SECRET),
            GeminiConfig(api_key=SECRET),
            ElevenLabsConfig(api_key=SECRET, api_keys=(SECRET + "-2",)),
            MiniMaxConfig(api_key=SECRET),
        ]
        for cfg in configs:
            self.assertNotIn(SECRET, repr(cfg), type(cfg).__name__)
            self.assertNotIn(SECRET, str(cfg), type(cfg).__name__)


if __name__ == "__main__":
    unittest.main()
