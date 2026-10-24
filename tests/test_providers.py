"""Unit tests for the TTS provider abstraction layer.

These tests do not load bot.py — they exercise the dispatcher, the
local provider wrapper, and the circuit breaker skeleton in isolation.
Network-bound MiniMaxProvider tests live in ``test_providers_minimax.py``
(added with commit 3) and use ``httpx.MockTransport``.
"""

from __future__ import annotations

import asyncio
import os
import unittest
from pathlib import Path
from unittest import mock

from ttsbot.providers import (
    CircuitBreaker,
    CircuitBreakerConfig,
    CircuitState,
    DispatcherConfig,
    LocalProvider,
    TTSDispatcher,
    load_circuit_breaker_from_env,
    load_dispatcher_config_from_env,
)


def run(coro):
    """Tiny helper: run a coroutine to completion in tests.

    Uses a fresh event loop rather than ``asyncio.get_event_loop()`` so the
    helper keeps working after an ``IsolatedAsyncioTestCase`` (in another test
    module) has run and cleared the thread's current loop — otherwise Python
    3.11 raises ``RuntimeError: There is no current event loop`` under
    ``unittest discover``.
    """
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class LocalProviderTests(unittest.TestCase):
    def test_local_provider_delegates_to_piper_callable(self):
        captured = {}

        async def fake_piper(text, filename, voice_profile):
            captured["text"] = text
            captured["filename"] = filename
            captured["voice_profile"] = voice_profile
            # Simulate writing a tiny file so the caller sees something.
            filename.write_bytes(b"FAKE_WAV")

        provider = LocalProvider(fake_piper)
        target = Path("/tmp/local_test.wav")

        try:
            run(provider.synthesize("hello", target))
            self.assertEqual(captured["text"], "hello")
            self.assertEqual(captured["filename"], target)
            self.assertIsNone(captured["voice_profile"])
        finally:
            if target.exists():
                target.unlink()

    def test_local_provider_propagates_exceptions(self):
        async def boom(text, filename, voice_profile):
            raise RuntimeError("piper crashed")

        provider = LocalProvider(boom)
        with self.assertRaises(RuntimeError):
            run(provider.synthesize("x", Path("/tmp/nope.wav")))

    def test_local_provider_name_is_local(self):
        async def noop(text, filename, voice_profile):
            pass

        self.assertEqual(LocalProvider(noop).name, "local")


class DispatcherSkeletonTests(unittest.TestCase):
    """Tests for the dispatcher routing logic (post commit 3).

    These tests use a fake cloud provider so they can exercise success,
    failure, and fallback paths without any network.
    """

    def test_warm_local_does_not_touch_circuit_breaker(self):
        """Critical invariant: warmup must never increment CB failure
        counters even if the underlying provider were to fail."""

        async def fake_piper(text, filename, voice_profile):
            raise RuntimeError("simulated cold-start failure")

        local = LocalProvider(fake_piper)
        cb = CircuitBreaker()
        d = TTSDispatcher(local=local, breakers={"minimax": cb})

        with self.assertRaises(RuntimeError):
            run(d.warm_local("x", Path("/tmp/zzz.wav")))

        # CB must remain untouched even though the provider raised.
        self.assertEqual(cb.state, CircuitState.CLOSED)
        self.assertEqual(cb.consecutive_failures, 0)


class FakeProvider:
    """Minimal stand-in for MiniMaxProvider used in dispatcher tests.

    Mirrors the TTSProvider protocol exactly. Defined here (not in
    tts_providers) so the production module stays free of test-only
    helpers.
    """

    def __init__(self, name, synth_fn):
        self.name = name
        self._synth = synth_fn

    async def synthesize(self, text, filename):
        await self._synth(text, filename)


class CircuitBreakerStateMachineTests(unittest.TestCase):
    """Full state-machine tests for CircuitBreaker.

    Uses an injected fake clock so cooldown elapses instantly in tests
    without sleeping the real wall clock.
    """

    def _make(self, *, threshold=3, cooldown=60.0):
        """Helper: build a CircuitBreaker with a fake clock at t=0."""
        clock = {"now": 0.0}

        def fake():
            return clock["now"]

        cb = CircuitBreaker(
            CircuitBreakerConfig(failure_threshold=threshold, cooldown_seconds=cooldown),
            clock=fake,
        )
        return cb, clock

    def test_default_state_is_closed(self):
        cb, _ = self._make()
        self.assertEqual(cb.state, CircuitState.CLOSED)
        self.assertEqual(cb.consecutive_failures, 0)
        self.assertTrue(cb.allow_request())
        self.assertEqual(cb.cooldown_remaining, 0.0)

    def test_record_success_resets_failures(self):
        cb, _ = self._make()
        cb.record_failure()
        cb.record_failure()
        cb.record_success()
        self.assertEqual(cb.consecutive_failures, 0)
        self.assertEqual(cb.state, CircuitState.CLOSED)

    def test_below_threshold_keeps_closed_and_allows(self):
        cb, _ = self._make(threshold=3)
        cb.record_failure()
        cb.record_failure()
        self.assertEqual(cb.state, CircuitState.CLOSED)
        self.assertTrue(cb.allow_request())

    def test_at_threshold_opens_breaker_and_blocks(self):
        cb, clock = self._make(threshold=3, cooldown=60.0)
        cb.record_failure()
        cb.record_failure()
        cb.record_failure()
        self.assertEqual(cb.state, CircuitState.OPEN)
        # While cooldown is in the future, no requests allowed.
        self.assertFalse(cb.allow_request())
        self.assertAlmostEqual(cb.cooldown_remaining, 60.0, delta=0.001)

    def test_after_cooldown_one_probe_allowed_then_blocked(self):
        cb, clock = self._make(threshold=3, cooldown=60.0)
        for _ in range(3):
            cb.record_failure()
        # Move the clock past cooldown.
        clock["now"] = 60.5
        # First caller gets the probe.
        self.assertTrue(cb.allow_request())
        self.assertEqual(cb.state, CircuitState.HALF_OPEN)
        # Second caller in HALF_OPEN is blocked — only one probe at a time.
        self.assertFalse(cb.allow_request())
        # And subsequent ones too.
        self.assertFalse(cb.allow_request())

    def test_probe_success_closes_breaker(self):
        cb, clock = self._make(threshold=3, cooldown=60.0)
        for _ in range(3):
            cb.record_failure()
        clock["now"] = 60.5
        self.assertTrue(cb.allow_request())
        cb.record_success()
        self.assertEqual(cb.state, CircuitState.CLOSED)
        self.assertEqual(cb.consecutive_failures, 0)
        # Fresh requests allowed again.
        self.assertTrue(cb.allow_request())

    def test_probe_failure_reopens_with_fresh_cooldown(self):
        cb, clock = self._make(threshold=3, cooldown=60.0)
        for _ in range(3):
            cb.record_failure()
        clock["now"] = 60.5
        self.assertTrue(cb.allow_request())
        cb.record_failure()
        # Back to OPEN, anchored to the current clock, with the pause
        # doubled (CircuitBreakerConfig.backoff_factor) for a failed probe.
        self.assertEqual(cb.state, CircuitState.OPEN)
        self.assertAlmostEqual(cb.cooldown_remaining, 120.0, delta=0.001)
        # No probes again until another cooldown elapses.
        self.assertFalse(cb.allow_request())

def _write_bytes_provider(payload: bytes):
    async def fake_piper(text, filename, voice_profile):
        filename.write_bytes(payload)
    return fake_piper


class EnvConfigTests(unittest.TestCase):
    def test_circuit_breaker_config_from_env(self):
        with mock.patch.dict(
            os.environ,
            {"CB_FAILURE_THRESHOLD": "5", "CB_COOLDOWN_SECONDS": "30"},
        ):
            cb = load_circuit_breaker_from_env()
        self.assertEqual(cb._config.failure_threshold, 5)
        self.assertAlmostEqual(cb._config.cooldown_seconds, 30.0)


if __name__ == "__main__":
    unittest.main()