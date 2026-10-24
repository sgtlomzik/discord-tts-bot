"""Fallback chain and provider health: failure kinds, pauses and checks,
the voice chain in the registry, the dispatcher and the prefetch pipeline."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx

from ttsbot.commands import _parse_chain
from ttsbot.elevenlabs import (
    ElevenLabsAuthError, ElevenLabsConfig, ElevenLabsError, ElevenLabsProvider, ElevenLabsQuotaExhaustedError,
    ElevenLabsRateLimitError, ElevenLabsVoiceNotFoundError,
)
from ttsbot.fish import FishConfig, FishError, _http_error as fish_http_error
from ttsbot.health import (
    CircuitBreaker, CircuitBreakerConfig, CircuitState, FailureKind, HealthStore, QuotaReport,
    classify_failure,
)
from ttsbot.models import PreparedAudio, TTSJob
from ttsbot.pipeline import SynthesisPipelineMixin
from ttsbot.providers import (
    DispatcherConfig, MiniMaxAuthError, MiniMaxQuotaError, MiniMaxQuotaExhaustedError,
    MiniMaxVoiceNotFoundError, NoVoiceAvailableError, TTSDispatcher,
)
from ttsbot.voice_registry import (
    ElevenLabsParams, FishParams, VoiceRecord, VoiceRegistry, registry_from_dict, registry_to_dict,
)


class Clock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _breaker(clock: Clock, **kwargs) -> CircuitBreaker:
    return CircuitBreaker(CircuitBreakerConfig(**kwargs), clock=clock)


def _el(name="eleven-george", fallback=None):
    return VoiceRecord(name=name, label=name, description="", provider="elevenlabs",
                       elevenlabs=ElevenLabsParams(voice_id="el-" + name), fallback=fallback)


def _fish(name="fish-default"):
    return VoiceRecord(name=name, label=name, description="", provider="fish",
                       fish=FishParams(reference_id="ref-" + name))


def _piper(name="piper-ruslan"):
    return VoiceRecord(name=name, label=name, description="", provider="piper")


def _registry(chain=("fish-default", "piper-ruslan"), *voices) -> VoiceRegistry:
    reg = VoiceRegistry(fallback_profile="piper-ruslan", fallback_chain=list(chain) if chain is not None else None)
    for voice in (_el(), _fish(), _fish("pirat"), _piper(), *voices):
        reg.add(voice)
    return reg


class ClassifyFailureTests(unittest.TestCase):
    def test_kinds_follow_the_marker_classes(self):
        cases = {
            None: FailureKind.TRANSIENT,
            RuntimeError("boom"): FailureKind.TRANSIENT,
            ElevenLabsError("HTTP 500", 500): FailureKind.TRANSIENT,
            ElevenLabsVoiceNotFoundError("404"): FailureKind.REQUEST,
            ElevenLabsQuotaExhaustedError("402", 402): FailureKind.QUOTA,
            ElevenLabsRateLimitError("429", 429, retry_after=5): FailureKind.RATE_LIMIT,
            MiniMaxQuotaExhaustedError("2056"): FailureKind.QUOTA,
            MiniMaxQuotaError("1039"): FailureKind.RATE_LIMIT,
            MiniMaxAuthError("1004"): FailureKind.AUTH,
            MiniMaxVoiceNotFoundError("2054"): FailureKind.REQUEST,
        }
        for exc, kind in cases.items():
            with self.subTest(exc=exc):
                self.assertIs(classify_failure(exc), kind)

    def test_fish_http_statuses(self):
        for status, kind in ((402, FailureKind.QUOTA), (401, FailureKind.AUTH), (403, FailureKind.AUTH),
                             (429, FailureKind.RATE_LIMIT), (404, FailureKind.REQUEST),
                             (400, FailureKind.REQUEST), (500, FailureKind.TRANSIENT)):
            with self.subTest(status=status):
                error = fish_http_error("Fish", status, "x", "7" if status == 429 else None)
                self.assertIsInstance(error, FishError)
                self.assertIs(classify_failure(error), kind)
        self.assertEqual(fish_http_error("Fish", 429, "x", "7").retry_after, 7.0)


class BreakerPolicyTests(unittest.TestCase):
    def test_transient_pauses_grow_and_reset_on_success(self):
        clock = Clock()
        cb = _breaker(clock, failure_threshold=2, cooldown_seconds=60, max_cooldown_seconds=200)
        cb.record_failure()
        self.assertIs(cb.state, CircuitState.CLOSED)
        cb.record_failure()
        pauses = [cb.cooldown_remaining]
        for _ in range(3):
            clock.now += cb.cooldown_remaining
            self.assertTrue(cb.allow_request())  # the probe
            cb.record_failure()
            pauses.append(cb.cooldown_remaining)
        self.assertEqual(pauses, [60, 120, 200, 200])
        clock.now += 200
        self.assertTrue(cb.allow_request())
        cb.record_success()
        cb.record_failure()
        cb.record_failure()
        self.assertEqual(cb.cooldown_remaining, 60)

    def test_quota_with_reset_time_pauses_until_it_capped_at_a_day(self):
        clock = Clock()
        cb = _breaker(clock)
        cb.record_failure(FailureKind.QUOTA, until=clock.now + 3600)
        self.assertEqual(cb.cooldown_remaining, 3600)
        cb.record_failure(FailureKind.QUOTA, until=clock.now + 30 * 86400)
        self.assertEqual(cb.cooldown_remaining, 86400)  # rechecked daily at least

    def test_quota_without_reset_time_backs_off_from_the_base(self):
        clock = Clock()
        cb = _breaker(clock)
        pauses = []
        for _ in range(8):
            cb.record_failure(FailureKind.QUOTA, base=1800)
            pauses.append(cb.cooldown_remaining)
        self.assertEqual(pauses, [1800, 3600, 7200, 14400, 28800, 57600, 86400, 86400])

    def test_reset_time_already_past_does_not_spin(self):
        clock = Clock()
        cb = _breaker(clock)
        cb.record_failure(FailureKind.QUOTA, until=clock.now - 5, base=600)
        self.assertEqual(cb.cooldown_remaining, 600)

    def test_transient_failure_does_not_shorten_a_quota_pause(self):
        clock = Clock()
        cb = _breaker(clock, failure_threshold=1)
        cb.record_failure(FailureKind.QUOTA, base=1800)
        cb.record_failure(FailureKind.TRANSIENT)
        self.assertEqual(cb.kind, FailureKind.QUOTA)
        self.assertEqual(cb.cooldown_remaining, 1800)

    def test_request_failures_are_not_counted(self):
        cb = _breaker(Clock(), failure_threshold=1)
        cb.record_failure(FailureKind.REQUEST)
        self.assertEqual((cb.state, cb.consecutive_failures), (CircuitState.CLOSED, 0))

    def test_await_check_holds_the_probe_until_grace(self):
        clock = Clock()
        cb = _breaker(clock, check_grace_seconds=300)
        cb.record_failure(FailureKind.QUOTA, base=60)
        cb.await_check = True
        clock.now += 61
        self.assertTrue(cb.check_due)
        self.assertFalse(cb.allow_request())  # the health worker checks first
        clock.now += 300
        self.assertTrue(cb.allow_request())   # worker stuck: a real request probes

    def test_lost_probe_frees_the_slot(self):
        clock = Clock()
        cb = _breaker(clock, failure_threshold=1, probe_timeout_seconds=120)
        cb.record_failure()
        clock.now += 60
        self.assertTrue(cb.allow_request())
        self.assertFalse(cb.allow_request())
        clock.now += 121
        self.assertTrue(cb.allow_request())


class ReviewFixTests(unittest.IsolatedAsyncioTestCase):
    def test_backoff_is_counted_per_kind(self):
        clock = Clock()
        cb = _breaker(clock, failure_threshold=1)
        cb.record_failure()                     # transient: 60
        for _ in range(5):
            cb.reopen("host unreachable")       # 120 ... 900
        cb.record_failure(FailureKind.QUOTA, base=1800)
        self.assertEqual(cb.cooldown_remaining, 1800)  # not 1800 * 2**6

    def test_retry_after_is_capped_and_does_not_shorten_a_quota_pause(self):
        clock = Clock()
        cb = _breaker(clock)
        cb.record_failure(FailureKind.RATE_LIMIT, retry_after=10 * 86400)
        self.assertEqual(cb.cooldown_remaining, 86400)
        cb = _breaker(clock)
        cb.record_failure(FailureKind.QUOTA, base=3600)
        cb.record_failure(FailureKind.RATE_LIMIT, retry_after=20)
        self.assertEqual((cb.kind, cb.cooldown_remaining), (FailureKind.QUOTA, 3600))

    def test_unreadable_snapshot_is_ignored(self):
        cb = _breaker(Clock())
        with self.assertLogs("tts_bot.health", "WARNING"):
            cb.restore({"state": "open", "kind": "quota", "opened_at": "yesterday", "cooldown": 60})
        self.assertIs(cb.state, CircuitState.CLOSED)

    async def test_auth_pause_is_not_closed_by_a_quota_report(self):
        provider = SimpleNamespace(config=ElevenLabsConfig(api_key="xk"),
                                   check_quota=AsyncMock(return_value=QuotaReport(True, None, "ok")))
        dispatcher = TTSDispatcher(local=MagicMock(), elevenlabs=provider)
        cb = dispatcher.elevenlabs_circuit_breaker
        clock = Clock()
        cb._clock = clock
        dispatcher.record_failure(cb, ElevenLabsAuthError("401 invalid_api_key", 401))
        self.assertFalse(cb.await_check)
        self.assertEqual(cb.cooldown_remaining, cb.config.auth_recheck_seconds)
        clock.now += cb.config.auth_recheck_seconds
        await dispatcher.check_health()
        provider.check_quota.assert_not_awaited()
        self.assertTrue(cb.allow_request())  # a real request probes the key

    async def test_check_that_was_wrong_is_not_trusted_again(self):
        provider = SimpleNamespace(config=ElevenLabsConfig(api_key="xk"),
                                   check_quota=AsyncMock(return_value=QuotaReport(True, None, "ok")))
        dispatcher = TTSDispatcher(local=MagicMock(), elevenlabs=provider,
                                   config=DispatcherConfig(quota_cooldown_seconds=1800))
        cb = dispatcher.elevenlabs_circuit_breaker
        clock = Clock()
        cb._clock = clock
        quota = ElevenLabsQuotaExhaustedError("402 billing", 402)
        dispatcher.record_failure(cb, quota)
        clock.now += 60
        await dispatcher.check_health()
        self.assertIs(cb.state, CircuitState.CLOSED)   # the check says it works...
        dispatcher.record_failure(cb, quota)           # ...but the next request fails
        self.assertFalse(cb.await_check)
        self.assertEqual(cb.cooldown_remaining, 1800)  # backoff, no 60 s check loop
        clock.now += 1800
        self.assertTrue(cb.allow_request())
        cb.record_success()                            # a real success trusts it again
        dispatcher.record_failure(cb, quota)
        self.assertTrue(cb.await_check)

    async def test_request_error_on_the_probe_releases_it(self):
        dispatcher = TTSDispatcher(local=MagicMock(), elevenlabs=SimpleNamespace(config=ElevenLabsConfig(api_key="xk")))
        cb = dispatcher.elevenlabs_circuit_breaker
        clock = Clock()
        cb._clock = clock
        cb.trip(60, FailureKind.TRANSIENT)
        clock.now += 60
        self.assertTrue(cb.allow_request())
        dispatcher.record_failure(cb, ElevenLabsVoiceNotFoundError("404"))
        self.assertIs(cb.state, CircuitState.CLOSED)

    async def test_no_check_while_waiting_for_a_real_probe(self):
        provider = SimpleNamespace(config=ElevenLabsConfig(api_key="xk"), check_quota=AsyncMock(return_value=None))
        dispatcher = TTSDispatcher(local=MagicMock(), elevenlabs=provider)
        cb = dispatcher.elevenlabs_circuit_breaker
        clock = Clock()
        cb._clock = clock
        dispatcher.record_failure(cb, ElevenLabsQuotaExhaustedError("402", 402))
        clock.now += 60
        await dispatcher.check_health()
        await dispatcher.check_health()
        self.assertEqual(provider.check_quota.await_count, 1)


class HealthStoreTests(unittest.TestCase):
    def test_pause_survives_restart_unless_the_key_changed(self):
        clock = Clock()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "provider_health.json"
            cb = _breaker(clock)
            HealthStore(path).bind("elevenlabs", cb, "fp-1")
            cb.record_failure(FailureKind.QUOTA, until=clock.now + 7200, error="no credits")
            cb.await_check = True  # saved too
            saved = json.loads(path.read_text(encoding="utf-8"))["breakers"]["elevenlabs"]
            self.assertEqual((saved["state"], saved["kind"], saved["fingerprint"]), ("open", "quota", "fp-1"))

            clock.now += 3600
            restored = _breaker(clock)
            HealthStore(path).bind("elevenlabs", restored, "fp-1")
            self.assertIs(restored.state, CircuitState.OPEN)
            self.assertEqual(restored.kind, FailureKind.QUOTA)
            self.assertAlmostEqual(restored.cooldown_remaining, 3600)
            self.assertTrue(restored.await_check)
            self.assertFalse(restored.allow_request())

            other_key = _breaker(clock)
            with self.assertLogs("tts_bot.health", "INFO"):
                HealthStore(path).bind("elevenlabs", other_key, "fp-2")
            self.assertIs(other_key.state, CircuitState.CLOSED)

    def test_success_is_saved_as_closed(self):
        clock = Clock()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "h.json"
            cb = _breaker(clock, failure_threshold=1)
            HealthStore(path).bind("fish", cb)
            cb.record_failure()
            clock.now += 61
            self.assertTrue(cb.allow_request())
            cb.record_success()
            restored = _breaker(clock)
            HealthStore(path).bind("fish", restored)
            self.assertIs(restored.state, CircuitState.CLOSED)


class RegistryChainTests(unittest.TestCase):
    def test_default_chain_is_the_fallback_profile(self):
        reg = _registry(chain=None)
        self.assertEqual([v.name for v in reg.chain_for(reg.get("eleven-george"))],
                         ["eleven-george", "piper-ruslan"])

    def test_global_chain_skips_unknown_and_repeated_voices(self):
        reg = _registry(chain=("ghost", "fish-default", "eleven-george", "piper-ruslan"))
        self.assertEqual([v.name for v in reg.chain_for(reg.get("eleven-george"))],
                         ["eleven-george", "fish-default", "piper-ruslan"])
        self.assertEqual([v.name for v in reg.chain_for(reg.get("piper-ruslan"))],
                         ["piper-ruslan", "fish-default", "eleven-george"])

    def test_voice_override_and_explicit_empty_chain(self):
        reg = _registry(("fish-default", "piper-ruslan"), _el("eleven-bella", fallback=("pirat", "piper-ruslan")),
                        _el("eleven-solo", fallback=()))
        self.assertEqual([v.name for v in reg.chain_for(reg.get("eleven-bella"))],
                         ["eleven-bella", "pirat", "piper-ruslan"])
        self.assertEqual([v.name for v in reg.chain_for(reg.get("eleven-solo"))], ["eleven-solo"])

    def test_round_trip_keeps_unset_and_empty_apart(self):
        reg = _registry(("fish-default",), _el("eleven-solo", fallback=()))
        data = registry_to_dict(reg)
        self.assertEqual(data["fallback_chain"], ["fish-default"])
        self.assertEqual(data["voices"]["eleven-solo"]["fallback"], [])
        self.assertNotIn("fallback", data["voices"]["eleven-george"])
        again = registry_from_dict(json.loads(json.dumps(data)))
        self.assertEqual(again.fallback_chain, ["fish-default"])
        self.assertEqual(again.get("eleven-solo").fallback, ())
        self.assertIsNone(again.get("eleven-george").fallback)
        legacy = registry_from_dict({"fallback_profile": "p", "voices": {}})
        self.assertIsNone(legacy.fallback_chain)

    def test_parse_chain_command_argument(self):
        self.assertEqual(_parse_chain("Fish-Default, piper-ruslan"), ["fish-default", "piper-ruslan"])
        self.assertEqual(_parse_chain("a → b → a"), ["a", "b"])
        self.assertEqual(_parse_chain("eleven-default -> fish-default -> piper-ruslan"),
                         ["eleven-default", "fish-default", "piper-ruslan"])
        self.assertEqual(_parse_chain("none"), [])
        self.assertIsNone(_parse_chain("default"))


def _fish_provider(side_effect=None):
    async def synthesize(text, filename, **kwargs):
        if side_effect is not None:
            raise side_effect
        Path(filename).write_bytes(b"OggS-fish")
    return SimpleNamespace(config=FishConfig(api_key="fk", reference_id="ref"),
                           synthesize=AsyncMock(side_effect=synthesize))


def _el_provider(side_effect):
    return SimpleNamespace(config=ElevenLabsConfig(api_key="xk"), fetch=AsyncMock(side_effect=side_effect))


class DispatcherChainTests(unittest.IsolatedAsyncioTestCase):
    def _dispatcher(self, elevenlabs=None, fish=None):
        local = SimpleNamespace(name="local", synthesize=AsyncMock())
        return TTSDispatcher(local=local, elevenlabs=elevenlabs, fish=fish,
                             config=DispatcherConfig(quota_cooldown_seconds=900))

    async def test_quota_moves_to_fish_then_skips_elevenlabs(self):
        eleven = _el_provider(ElevenLabsQuotaExhaustedError("no credits", 402))
        dispatcher = self._dispatcher(eleven, _fish_provider())
        reg = _registry()
        chain = reg.chain_for(reg.get("eleven-george"))
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "o.wav"
            used, voice = await dispatcher.synthesize_chain("x", out, chain[0], chain[1:])
            self.assertEqual((used, voice.name), ("fish", "fish-default"))
            used, voice = await dispatcher.synthesize_chain("y", out, chain[0], chain[1:])
            self.assertEqual(used, "fish")
        self.assertEqual(eleven.fetch.await_count, 1)  # paused: not asked again
        self.assertEqual(dispatcher.elevenlabs_circuit_breaker.kind, FailureKind.QUOTA)
        dispatcher._local.synthesize.assert_not_awaited()

    async def test_failed_provider_is_skipped_for_the_rest_of_the_chain(self):
        fish = _fish_provider(FishError("Fish HTTP 500", 500))
        dispatcher = self._dispatcher(fish=fish)
        reg = _registry(("pirat", "fish-default", "piper-ruslan"))
        chain = reg.chain_for(reg.get("eleven-george"))  # ElevenLabs not configured here
        with tempfile.TemporaryDirectory() as tmp:
            used, voice = await dispatcher.synthesize_chain("x", Path(tmp) / "o.wav", chain[0], chain[1:])
        self.assertEqual((used, voice.name), ("local", "piper-ruslan"))
        self.assertEqual(fish.synthesize.await_count, 1)  # fish-default was not tried
        dispatcher._local.synthesize.assert_awaited_once()
        self.assertEqual(dispatcher._local.synthesize.await_args.args[2], "piper-ruslan")

    async def test_request_error_tries_the_next_voice_of_the_same_provider(self):
        calls = []

        async def fetch(text, params):
            calls.append(params.voice_id)
            if params.voice_id == "el-eleven-bella":
                raise ElevenLabsVoiceNotFoundError("404")
            return "opus", 48000, b"OggS"

        dispatcher = self._dispatcher(SimpleNamespace(config=ElevenLabsConfig(api_key="xk"), fetch=fetch))
        reg = _registry(("eleven-george", "piper-ruslan"), _el("eleven-bella"))
        chain = reg.chain_for(reg.get("eleven-bella"))
        with tempfile.TemporaryDirectory() as tmp:
            used, voice = await dispatcher.synthesize_chain("x", Path(tmp) / "o.wav", chain[0], chain[1:])
        self.assertEqual((used, voice.name), ("elevenlabs", "eleven-george"))
        self.assertEqual(calls, ["el-eleven-bella", "el-eleven-george"])
        self.assertIs(dispatcher.elevenlabs_circuit_breaker.state, CircuitState.CLOSED)

    async def test_empty_chain_raises_when_the_voice_fails(self):
        dispatcher = self._dispatcher(_el_provider(ElevenLabsError("down", 500)))
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(NoVoiceAvailableError):
                await dispatcher.synthesize_chain("x", Path(tmp) / "o.wav", _el(), [])


class HealthCheckTests(unittest.IsolatedAsyncioTestCase):
    def _dispatcher(self, provider_name, provider):
        return TTSDispatcher(local=MagicMock(), config=DispatcherConfig(quota_cooldown_seconds=1800),
                             **{provider_name: provider})

    async def test_quota_report_closes_or_waits_for_the_reset(self):
        report = QuotaReport(False, None, "")
        provider = SimpleNamespace(config=ElevenLabsConfig(api_key="xk"),
                                   check_quota=AsyncMock(side_effect=lambda: report))
        dispatcher = self._dispatcher("elevenlabs", provider)
        cb = dispatcher.elevenlabs_circuit_breaker
        clock = Clock()
        cb._clock = clock
        dispatcher.record_failure(cb, ElevenLabsQuotaExhaustedError("402", 402))
        self.assertTrue(cb.await_check)
        self.assertEqual(cb.cooldown_remaining, 60)  # asked soon, to learn the reset time

        await dispatcher.check_health()
        provider.check_quota.assert_not_awaited()  # pause not over yet
        clock.now += 60
        report = QuotaReport(False, clock.now + 5 * 86400, "#1 0/10000")
        await dispatcher.check_health()
        self.assertEqual(cb.cooldown_remaining, 86400)  # daily recheck, reset is later
        self.assertFalse(cb.allow_request())

        clock.now += 86400
        report = QuotaReport(True, None, "#1 10000/10000")
        await dispatcher.check_health()
        self.assertIs(cb.state, CircuitState.CLOSED)

    async def test_unknown_quota_lets_a_real_request_probe(self):
        provider = SimpleNamespace(config=ElevenLabsConfig(api_key="xk"), check_quota=AsyncMock(return_value=None))
        dispatcher = self._dispatcher("elevenlabs", provider)
        cb = dispatcher.elevenlabs_circuit_breaker
        clock = Clock()
        cb._clock = clock
        dispatcher.record_failure(cb, ElevenLabsQuotaExhaustedError("402", 402))
        clock.now += 60
        await dispatcher.check_health()
        self.assertTrue(cb.allow_request())
        # The probe fails too: wait on the backoff, not on another useless check.
        dispatcher.record_failure(cb, ElevenLabsQuotaExhaustedError("402", 402))
        self.assertFalse(cb.await_check)
        self.assertEqual(cb.cooldown_remaining, 1800)

    async def test_transient_pause_pings_the_host_before_a_probe(self):
        warmer = SimpleNamespace(reachable=AsyncMock(return_value=False))
        fish = SimpleNamespace(config=FishConfig(api_key="fk"), warmer=warmer)
        dispatcher = self._dispatcher("fish", fish)
        cb = dispatcher.fish_circuit_breaker
        clock = Clock()
        cb._clock = clock
        for _ in range(cb.config.failure_threshold):
            dispatcher.record_failure(cb, httpx.ConnectError("down"))
        self.assertTrue(cb.await_check)
        first = cb.cooldown_remaining
        clock.now += first
        self.assertFalse(cb.allow_request())  # not before the ping
        await dispatcher.check_health()
        self.assertGreater(cb.cooldown_remaining, first)  # still down: longer pause
        clock.now += cb.cooldown_remaining
        warmer.reachable.return_value = True
        await dispatcher.check_health()
        self.assertTrue(cb.allow_request())  # host answers: one real request probes


def _subscription(remaining, limit=10000, reset=1_900_000_000):
    return httpx.Response(200, json={"character_count": limit - remaining, "character_limit": limit,
                                     "next_character_count_reset_unix": reset})


class ElevenLabsQuotaCheckTests(unittest.IsolatedAsyncioTestCase):
    def _provider(self, responses, keys=("k1", "k2", "k3")):
        def handle(request):
            assert request.url.path == "/v1/user/subscription"
            return responses[request.headers["xi-api-key"]]
        client = httpx.AsyncClient(transport=httpx.MockTransport(handle), base_url="https://el.test")
        self.addAsyncCleanup(client.aclose)
        switched = []
        cfg = ElevenLabsConfig(api_key=keys[0], api_keys=tuple(keys))
        return ElevenLabsProvider(cfg, client, on_key_switch=switched.append), switched

    async def test_moves_the_ring_to_a_key_with_credits(self):
        provider, switched = self._provider({"k1": _subscription(0), "k2": _subscription(10),
                                             "k3": _subscription(4000)})
        report = await provider.check_quota()
        self.assertTrue(report.available)
        self.assertEqual(provider.active_key_index, 2)
        self.assertEqual(len(switched), 1)
        self.assertIn("4000/10000", report.detail)

    async def test_all_empty_reports_the_earliest_reset(self):
        provider, _ = self._provider({"k1": _subscription(0, reset=1_900_000_500),
                                      "k2": _subscription(3, reset=1_900_000_100)}, keys=("k1", "k2"))
        report = await provider.check_quota()
        self.assertEqual((report.available, report.reset_at), (False, 1_900_000_100))

    async def test_unreadable_key_means_unknown(self):
        denied = httpx.Response(401, json={"detail": {"status": "missing_permissions", "message": "user_read"}})
        provider, _ = self._provider({"k1": _subscription(0), "k2": denied}, keys=("k1", "k2"))
        self.assertIsNone(await provider.check_quota())


def _job():
    return TTSJob(text="привет", voice_channel=None, queued_at=0, author_id=1,
                  guild_id=1, text_channel_id=1, voice_profile="eleven-george")


class PrefetchChainTests(unittest.IsolatedAsyncioTestCase):
    def _pipeline(self, registry):
        pipeline = SynthesisPipelineMixin()
        pipeline.voice_registry = registry
        pipeline.tts_dispatcher = TTSDispatcher(
            local=MagicMock(), elevenlabs=SimpleNamespace(config=ElevenLabsConfig(api_key="xk")),
            fish=SimpleNamespace(config=FishConfig(api_key="fk")),
        )
        pipeline._should_attempt_stream = lambda voice: voice.provider != "piper"
        pipeline._generate_file_into = AsyncMock()
        return pipeline

    async def test_elevenlabs_quota_streams_the_fish_fallback(self):
        pipeline = self._pipeline(_registry())

        async def eleven(prepared, voice):
            prepared.error = ElevenLabsQuotaExhaustedError("402", 402)
            return "pre_audio"

        pipeline._generate_elevenlabs_stream_into = AsyncMock(side_effect=eleven)
        pipeline._generate_fish_stream_into = AsyncMock(return_value="ok")
        prepared = PreparedAudio(job=_job(), channel=asyncio.Queue())
        await pipeline._prepare_into(prepared)
        self.assertEqual(pipeline._generate_fish_stream_into.await_args.args[1].name, "fish-default")
        pipeline._generate_file_into.assert_not_awaited()
        self.assertIsNone(await prepared.channel.get())

    async def test_fish_failure_skips_other_fish_voices_and_ends_with_piper(self):
        pipeline = self._pipeline(_registry(("fish-default", "pirat", "piper-ruslan")))
        pipeline._generate_elevenlabs_stream_into = AsyncMock(return_value="pre_audio")
        pipeline._generate_fish_stream_into = AsyncMock(return_value="pre_audio")
        prepared = PreparedAudio(job=_job(), channel=asyncio.Queue())
        with self.assertLogs("tts_bot", "WARNING") as logs:
            await pipeline._prepare_into(prepared)
        self.assertEqual(pipeline._generate_fish_stream_into.await_count, 1)
        voice, rest = pipeline._generate_file_into.await_args.args[1:]
        self.assertEqual((voice.name, rest), ("piper-ruslan", []))
        self.assertIn("voice=eleven-george -> fish-default", "\n".join(logs.output))


if __name__ == "__main__":
    unittest.main()
