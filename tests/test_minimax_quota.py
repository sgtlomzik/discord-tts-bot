"""MiniMax quota errors: plain-JSON stream errors and the long breaker trip."""

from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx

from ttsbot.voice_registry import FishParams
from ttsbot.models import PreparedAudio, TTSJob
from ttsbot.pipeline import SynthesisPipelineMixin
from ttsbot.providers import (
    CircuitBreaker,
    CircuitBreakerConfig,
    CircuitState,
    DispatcherConfig,
    MiniMaxConfig,
    MiniMaxProvider,
    MiniMaxQuotaError,
    MiniMaxQuotaExhaustedError,
    TTSDispatcher,
)

QUOTA_BODY = (
    b'{"base_resp":{"status_code":2056,"status_msg":"Token Plan usage limit reached"}}'
)


def _provider(handler) -> MiniMaxProvider:
    cfg = MiniMaxConfig(api_key="k", voice_id="v", base_url="https://api.minimax.io")
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return MiniMaxProvider(cfg, http_client=client)


class MiniMaxQuotaTests(unittest.IsolatedAsyncioTestCase):
    async def test_stream_raises_quota_error_on_plain_json_body(self):
        provider = _provider(lambda request: httpx.Response(
            200, content=QUOTA_BODY, headers={"content-type": "application/json"},
        ))
        with self.assertRaises(MiniMaxQuotaExhaustedError):
            async for _ in provider.stream_audio("привет"):
                pass

    async def test_rate_limit_is_not_exhaustion(self):
        body = b'{"base_resp":{"status_code":1039,"status_msg":"token limit"}}'
        provider = _provider(lambda request: httpx.Response(
            200, content=body, headers={"content-type": "application/json"},
        ))
        with self.assertRaises(MiniMaxQuotaError) as ctx:
            async for _ in provider.stream_audio("привет"):
                pass
        self.assertNotIsInstance(ctx.exception, MiniMaxQuotaExhaustedError)

    async def test_synthesize_raises_quota_error(self):
        provider = _provider(lambda request: httpx.Response(200, content=QUOTA_BODY))
        with self.assertRaises(MiniMaxQuotaExhaustedError):
            await provider.synthesize("привет", Path("/tmp/unused.mp3"))


class QuotaBreakerTests(unittest.TestCase):
    def test_trip_holds_long_cooldown_until_success(self):
        now = [0.0]
        cb = CircuitBreaker(
            CircuitBreakerConfig(failure_threshold=3, cooldown_seconds=60),
            clock=lambda: now[0],
        )
        cb.trip(1800)
        for _ in range(5):
            cb.record_failure()  # ordinary failures must not shorten the trip
        now[0] = 120
        self.assertFalse(cb.allow_request())
        self.assertAlmostEqual(cb.cooldown_remaining, 1680)
        now[0] = 1800
        self.assertTrue(cb.allow_request())  # half-open probe
        cb.record_success()
        self.assertIs(cb.state, CircuitState.CLOSED)
        for _ in range(3):
            cb.record_failure()
        self.assertAlmostEqual(cb.cooldown_remaining, 60)

    def test_dispatcher_trips_on_quota_error(self):
        cb = CircuitBreaker(CircuitBreakerConfig(failure_threshold=3, cooldown_seconds=60))
        dispatcher = TTSDispatcher(
            local=MagicMock(), circuit_breaker=cb,
            config=DispatcherConfig(quota_cooldown_seconds=900),
        )
        dispatcher.record_cloud_failure(MiniMaxQuotaError("HTTP 429"))
        self.assertIs(cb.state, CircuitState.CLOSED)  # rate limit: ordinary failure
        dispatcher.record_cloud_failure(MiniMaxQuotaExhaustedError("2056"))
        self.assertIs(cb.state, CircuitState.OPEN)
        self.assertGreater(cb.cooldown_remaining, 800)
        dispatcher.record_cloud_failure(RuntimeError("boom"))
        self.assertGreater(cb.cooldown_remaining, 800)

    def test_zero_quota_cooldown_disables_the_pause(self):
        cb = CircuitBreaker(CircuitBreakerConfig(failure_threshold=3, cooldown_seconds=60))
        cb.trip(0)
        self.assertEqual(cb.cooldown_remaining, 0.0)
        self.assertTrue(cb.allow_request())


class FishTtfaTests(unittest.IsolatedAsyncioTestCase):
    async def test_fish_uses_its_own_first_byte_budget(self):
        from ttsbot import config

        async def slow_stream(*args, **kwargs):
            await asyncio.sleep(0.2)
            yield b"never"

        fish = SimpleNamespace(stream_audio=slow_stream)
        pipeline = SynthesisPipelineMixin()
        pipeline.tts_dispatcher = SimpleNamespace(fish=fish, cache=None)
        job = TTSJob(
            text="проверка", voice_channel=None, queued_at=0, author_id=1,
            guild_id=1, text_channel_id=1, voice_profile="fish-default",
        )
        prepared = PreparedAudio(job=job, channel=asyncio.Queue())
        voice = SimpleNamespace(fish=FishParams("voice-id"), name="fish-default")
        saved = config.TTS_STREAM_TTFA_TIMEOUT, config.FISH_TTFA_TIMEOUT
        config.TTS_STREAM_TTFA_TIMEOUT, config.FISH_TTFA_TIMEOUT = 5.0, 0.05
        try:
            with self.assertLogs("tts_bot", "WARNING") as logs:
                status, frames = await pipeline._stream_fish_opus_to_channel(
                    prepared, voice, "key",
                )
        finally:
            config.TTS_STREAM_TTFA_TIMEOUT, config.FISH_TTFA_TIMEOUT = saved
        self.assertEqual((status, frames), ("pre_audio", 0))
        self.assertIn("Fish TTFA exceeded", "\n".join(logs.output))


if __name__ == "__main__":
    unittest.main()
