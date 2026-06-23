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
from unittest.mock import AsyncMock

from tts_providers import (
    CircuitBreaker,
    CircuitBreakerConfig,
    CircuitState,
    DispatcherConfig,
    LocalProvider,
    PrimaryProvider,
    TTSDispatcher,
    load_circuit_breaker_from_env,
    load_dispatcher_config_from_env,
)


def run(coro):
    """Tiny helper: run a coroutine to completion in tests."""
    return asyncio.get_event_loop().run_until_complete(coro)


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

    def _make_dispatcher(self, *, primary, cloud=None, cb=None, local_writes=b"LOCAL"):
        async def fake_piper(text, filename, voice_profile):
            filename.write_bytes(local_writes)

        local = LocalProvider(fake_piper)
        return TTSDispatcher(
            local=local,
            cloud=cloud,
            circuit_breaker=cb or CircuitBreaker(),
            config=DispatcherConfig(primary=primary),
        )

    def test_primary_local_routes_through_local_even_if_cloud_present(self):
        async def fake_cloud(text, filename):
            filename.write_bytes(b"CLOUD")

        cloud = FakeProvider("cloud", fake_cloud)
        d = self._make_dispatcher(
            primary=PrimaryProvider.LOCAL, cloud=cloud,
        )
        target = Path("/tmp/disp_local.bin")
        try:
            used = run(d.synthesize("hi", target))
            self.assertEqual(used, "local")
            self.assertEqual(target.read_bytes(), b"LOCAL")
        finally:
            if target.exists():
                target.unlink()

    def test_primary_minimax_with_cloud_uses_cloud_on_success(self):
        async def fake_cloud(text, filename):
            filename.write_bytes(b"CLOUD")

        cloud = FakeProvider("cloud", fake_cloud)
        d = self._make_dispatcher(
            primary=PrimaryProvider.MINIMAX, cloud=cloud,
        )
        target = Path("/tmp/disp_cloud_ok.bin")
        try:
            used = run(d.synthesize("hi", target))
            self.assertEqual(used, "cloud")
            self.assertEqual(target.read_bytes(), b"CLOUD")
        finally:
            if target.exists():
                target.unlink()

    def test_primary_minimax_falls_back_to_local_when_cloud_fails(self):
        async def fake_cloud(text, filename):
            raise RuntimeError("cloud is down")

        cloud = FakeProvider("cloud", fake_cloud)
        d = self._make_dispatcher(
            primary=PrimaryProvider.MINIMAX, cloud=cloud,
        )
        target = Path("/tmp/disp_fallback.bin")
        try:
            used = run(d.synthesize("hi", target))
            self.assertEqual(used, "local")
            self.assertEqual(target.read_bytes(), b"LOCAL")
        finally:
            if target.exists():
                target.unlink()

    def test_primary_minimax_records_circuit_breaker_failure_on_cloud_error(self):
        async def fake_cloud(text, filename):
            raise RuntimeError("boom")

        cloud = FakeProvider("cloud", fake_cloud)
        cb = CircuitBreaker()
        d = self._make_dispatcher(
            primary=PrimaryProvider.MINIMAX, cloud=cloud, cb=cb,
        )
        run(d.synthesize("hi", Path("/tmp/cb1.bin")))
        self.assertEqual(cb.consecutive_failures, 1)
        self.assertEqual(cb.state, CircuitState.CLOSED)

    def test_primary_minimax_uses_local_when_cloud_is_none(self):
        d = self._make_dispatcher(primary=PrimaryProvider.MINIMAX, cloud=None)
        target = Path("/tmp/disp_no_cloud.bin")
        try:
            used = run(d.synthesize("hi", target))
            self.assertEqual(used, "local")
        finally:
            if target.exists():
                target.unlink()

    def test_warm_local_uses_local_provider_regardless_of_primary(self):
        async def fake_cloud(text, filename):
            raise AssertionError("warmup must not call cloud")

        cloud = FakeProvider("cloud", fake_cloud)
        d = self._make_dispatcher(
            primary=PrimaryProvider.MINIMAX, cloud=cloud,
        )
        target = Path("/tmp/warm_test.bin")
        try:
            run(d.warm_local("Привет", target))
            self.assertTrue(target.exists())
        finally:
            if target.exists():
                target.unlink()

    def test_warm_local_does_not_touch_circuit_breaker(self):
        """Critical invariant: warmup must never increment CB failure
        counters even if the underlying provider were to fail."""

        async def fake_piper(text, filename, voice_profile):
            raise RuntimeError("simulated cold-start failure")

        local = LocalProvider(fake_piper)
        cb = CircuitBreaker()
        d = TTSDispatcher(local=local, circuit_breaker=cb)

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


class CircuitBreakerSkeletonTests(unittest.TestCase):
    def test_default_state_is_closed(self):
        cb = CircuitBreaker()
        self.assertEqual(cb.state, CircuitState.CLOSED)
        self.assertEqual(cb.consecutive_failures, 0)
        self.assertTrue(cb.allow_request())

    def test_record_success_resets_failures(self):
        cb = CircuitBreaker()
        cb.record_failure()
        cb.record_failure()
        cb.record_success()
        self.assertEqual(cb.consecutive_failures, 0)
        self.assertEqual(cb.state, CircuitState.CLOSED)

    def test_record_failure_below_threshold_keeps_closed(self):
        cb = CircuitBreaker(CircuitBreakerConfig(failure_threshold=3, cooldown_seconds=60.0))
        cb.record_failure()
        cb.record_failure()
        self.assertEqual(cb.state, CircuitState.CLOSED)

    def test_record_failure_at_threshold_opens_breaker(self):
        cb = CircuitBreaker(CircuitBreakerConfig(failure_threshold=3, cooldown_seconds=60.0))
        cb.record_failure()
        cb.record_failure()
        cb.record_failure()
        self.assertEqual(cb.state, CircuitState.OPEN)

    # NOTE: HALF_OPEN transition is intentionally NOT exercised here —
    # the full state machine (cooldown timer, single probe) lands in
    # commit 6 of the rollout plan. The skeleton only tracks state.

    def test_skeleton_allow_request_always_true(self):
        """Documented invariant for commit 2: allow_request() is
        a no-op until commit 6 wires the cooldown timer."""
        cb = CircuitBreaker(CircuitBreakerConfig(failure_threshold=1, cooldown_seconds=60.0))
        cb.record_failure()
        self.assertEqual(cb.state, CircuitState.OPEN)
        # Even in OPEN state, skeleton still allows requests. Commit 6
        # changes this so OPEN short-circuits to fallback.
        self.assertTrue(cb.allow_request())


class EnvConfigTests(unittest.TestCase):
    def test_dispatcher_config_default_is_local_and_2_5s(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            # Drop any TTS_* overrides so we observe the defaults.
            for key in ("TTS_PRIMARY_PROVIDER", "TTS_REQUEST_TIMEOUT"):
                os.environ.pop(key, None)
            cfg = load_dispatcher_config_from_env()
        self.assertEqual(cfg.primary, PrimaryProvider.LOCAL)
        self.assertAlmostEqual(cfg.request_timeout_seconds, 2.5)

    def test_dispatcher_config_reads_primary_provider(self):
        with mock.patch.dict(os.environ, {"TTS_PRIMARY_PROVIDER": "minimax"}):
            cfg = load_dispatcher_config_from_env()
        self.assertEqual(cfg.primary, PrimaryProvider.MINIMAX)

    def test_dispatcher_config_unknown_provider_falls_back_to_local(self):
        with mock.patch.dict(os.environ, {"TTS_PRIMARY_PROVIDER": "gibberish"}):
            cfg = load_dispatcher_config_from_env()
        self.assertEqual(cfg.primary, PrimaryProvider.LOCAL)

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