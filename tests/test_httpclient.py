"""Every cloud TTS client keeps idle connections open long enough to reuse."""

import unittest
from unittest.mock import patch

import httpx

from ttsbot.elevenlabs import ElevenLabsConfig, ElevenLabsProvider
from ttsbot.fish import FishConfig, FishProvider
from ttsbot.gemini import GeminiConfig, GeminiProvider
from ttsbot.httpclient import KEEPALIVE_EXPIRY, provider_limits
from ttsbot.providers import MiniMaxConfig, MiniMaxProvider


class KeepAliveTests(unittest.TestCase):
    def test_limits(self):
        limits = provider_limits()
        self.assertEqual(
            (limits.max_connections, limits.max_keepalive_connections, limits.keepalive_expiry),
            (10, 5, KEEPALIVE_EXPIRY),
        )
        self.assertGreaterEqual(KEEPALIVE_EXPIRY, 60)

    def test_every_provider_uses_the_long_keepalive(self):
        seen = []
        real = httpx.AsyncClient

        def capture(*args, **kwargs):
            seen.append(kwargs.get("limits"))
            return real(*args, **kwargs)

        with patch("httpx.AsyncClient", side_effect=capture):
            ElevenLabsProvider(ElevenLabsConfig(api_key="k"))
            FishProvider(FishConfig(api_key="k"))
            GeminiProvider(GeminiConfig(api_key="k"))
            MiniMaxProvider(MiniMaxConfig(api_key="k"))
        self.assertEqual(len(seen), 4)
        self.assertTrue(all(limits.keepalive_expiry == KEEPALIVE_EXPIRY for limits in seen))


if __name__ == "__main__":
    unittest.main()
