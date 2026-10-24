"""Settings and Retry-After parsing shared by every cloud provider.

Fish, Gemini and ElevenLabs each carried their own copy of these.
"""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from ttsbot import elevenlabs, fish, gemini
from ttsbot.httpclient import env_setting, retry_after_seconds


class HttpHelperTests(unittest.TestCase):
    def test_retry_after_takes_positive_seconds_only(self):
        self.assertEqual(retry_after_seconds("20"), 20.0)
        self.assertEqual(retry_after_seconds("1.5"), 1.5)
        for value in (None, "", "0", "-3", "Wed, 21 Oct 2026 07:28:00 GMT", "soon"):
            self.assertIsNone(retry_after_seconds(value), value)

    def test_an_empty_setting_counts_as_unset(self):
        with patch.dict(os.environ, {"TTS_X": "  ", "TTS_Y": " v "}):
            self.assertEqual(env_setting("TTS_X", "default"), "default")
            self.assertEqual(env_setting("TTS_Y", "default"), "v")
            self.assertEqual(env_setting("TTS_UNSET_Z", "default"), "default")

    def test_the_providers_use_the_shared_helpers(self):
        for module in (fish, gemini, elevenlabs):
            self.assertFalse(hasattr(module, "_retry_after"), module.__name__)
            self.assertFalse(hasattr(module, "_env"), module.__name__)


if __name__ == "__main__":
    unittest.main()
