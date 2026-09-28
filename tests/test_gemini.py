"""Gemini via OpenRouter: request contract, errors, dispatcher and pipeline paths."""

import asyncio
import json
import os
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import numpy as np

from ttsbot import config
from ttsbot.audio import PCM_FRAME_BYTES
from ttsbot.errors import QuotaExhaustedError
from ttsbot.gemini import (
    GeminiAuthError, GeminiConfig, GeminiError, GeminiProvider, GeminiQuotaExhaustedError,
    GeminiRateLimitError, parse_pcm_content_type,
)
from ttsbot.models import PreparedAudio, TTSJob
from ttsbot.pipeline import SynthesisPipelineMixin
from ttsbot.providers import (
    CircuitBreaker, CircuitState, DispatcherConfig, PrimaryProvider, TTSCacheConfig,
    TTSDispatcher, TTSPhraseCache, load_dispatcher_config_from_env,
)
from ttsbot.voice_registry import (
    GeminiParams, VoiceRecord, VoiceRegistry, registry_from_dict, registry_to_dict,
)

PCM_CT = "audio/pcm;rate=24000;channels=1"


def _pcm(seconds=0.5, rate=24000) -> bytes:
    t = np.arange(int(rate * seconds)) / rate
    return (np.sin(2 * np.pi * 440 * t) * 8000).astype("<i2").tobytes()


def _provider(handler) -> tuple[GeminiProvider, httpx.AsyncClient]:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://openrouter.test/api/v1")
    return GeminiProvider(GeminiConfig(api_key="or-key", app_title="bot"), client), client


def _voice(volume_db=0.0):
    return VoiceRecord(name="gemini-kore", label="Gemini", description="", provider="gemini",
                       gemini=GeminiParams(voice="Kore", volume_db=volume_db))


def _job(text="Кто со мной?"):
    return TTSJob(text=text, voice_channel=None, queued_at=0, author_id=1,
                  guild_id=1, text_channel_id=1, voice_profile="gemini-kore")


class _Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks, fail_after=False):
        self.chunks = chunks
        self.fail_after = fail_after

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk
        if self.fail_after:
            raise httpx.ReadError("connection reset")


class GeminiProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_request_contract_and_counters(self):
        seen = []

        def handle(request):
            seen.append((request.url.path, dict(request.headers), json.loads(request.content)))
            return httpx.Response(200, headers={"content-type": PCM_CT}, content=_pcm())

        provider, client = _provider(handle)
        try:
            rate, channels, pcm = await provider.fetch_pcm("Привет", GeminiParams(voice="Puck"))
        finally:
            await client.aclose()
        path, headers, body = seen[0]
        self.assertEqual(path, "/api/v1/audio/speech")
        self.assertEqual(headers["authorization"], "Bearer or-key")
        self.assertEqual(headers["x-title"], "bot")
        self.assertEqual(body, {"model": "google/gemini-3.8-flash-lite-tts", "input": "Привет",
                                "voice": "Puck", "response_format": "pcm"})
        self.assertEqual((rate, channels, len(pcm)), (24000, 1, len(_pcm())))
        self.assertEqual((provider.session_requests, provider.session_chars), (1, 6))

    async def test_odd_chunks_are_trimmed_to_whole_samples(self):
        provider, client = _provider(lambda r: httpx.Response(
            200, headers={"content-type": PCM_CT}, stream=_Chunks([b"\x01\x02\x03", b"\x04\x05"])))
        try:
            _, _, pcm = await provider.fetch_pcm("x")
        finally:
            await client.aclose()
        self.assertEqual(pcm, b"\x01\x02\x03\x04")

    async def test_http_errors_map_to_typed_exceptions(self):
        cases = [
            (401, {}, GeminiAuthError, None),
            (402, {}, GeminiQuotaExhaustedError, None),
            (429, {"retry-after": "12"}, GeminiRateLimitError, 12.0),
            (429, {}, GeminiRateLimitError, None),
            (500, {}, GeminiError, None),
        ]
        for status, headers, exc_type, retry_after in cases:
            provider, client = _provider(lambda r, s=status, h=headers: httpx.Response(
                s, headers=h, json={"error": {"message": "nope", "code": s}}))
            try:
                with self.assertRaises(exc_type) as ctx:
                    await provider.fetch_pcm("x")
            finally:
                await client.aclose()
            self.assertEqual(ctx.exception.status_code, status)
            self.assertIn("nope", str(ctx.exception))
            if exc_type is GeminiRateLimitError:
                self.assertEqual(ctx.exception.retry_after, retry_after)
        self.assertTrue(issubclass(GeminiQuotaExhaustedError, QuotaExhaustedError))
        self.assertEqual(provider.session_requests, 0)

    async def test_empty_body_and_non_pcm_are_errors(self):
        for response in (
            httpx.Response(200, headers={"content-type": PCM_CT}, content=b""),
            httpx.Response(200, headers={"content-type": "application/json"}, content=b"{}"),
        ):
            provider, client = _provider(lambda r, resp=response: resp)
            try:
                with self.assertRaises(GeminiError):
                    await provider.fetch_pcm("ну")
            finally:
                await client.aclose()

    async def test_missing_key_fails_without_a_request(self):
        provider = GeminiProvider(GeminiConfig(api_key=""), httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: self.fail("request sent"))))
        with self.assertRaises(GeminiAuthError):
            await provider.fetch_pcm("x")

    async def test_synthesize_writes_a_wav(self):
        provider, client = _provider(lambda r: httpx.Response(200, headers={"content-type": PCM_CT}, content=_pcm()))
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "a.wav"
            try:
                await provider.synthesize("x", out)
            finally:
                await client.aclose()
            with wave.open(str(out)) as w:
                self.assertEqual((w.getframerate(), w.getnchannels(), w.getnframes()), (24000, 1, 12000))

    def test_content_type_parsing(self):
        self.assertEqual(parse_pcm_content_type("audio/pcm; rate=16000; channels=2"), (16000, 2))
        self.assertEqual(parse_pcm_content_type("audio/pcm"), (24000, 1))
        for bad in ("audio/mpeg", "", "audio/pcm;rate=abc", "audio/pcm;channels=6"):
            with self.assertRaises(GeminiError):
                parse_pcm_content_type(bad)

    def test_config_from_env_and_cache_key(self):
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": " k ", "GEMINI_TTS_MODEL": "",
                                     "GEMINI_TTS_VOICE": "Puck"}):
            cfg = GeminiConfig.from_env()
        self.assertEqual((cfg.api_key, cfg.model, cfg.voice), ("k", "google/gemini-3.8-flash-lite-tts", "Puck"))
        base = cfg.cache_key(GeminiParams(voice="Kore"))
        self.assertEqual(base, cfg.cache_key(GeminiParams(voice="Kore", volume_db=6.0)))
        self.assertNotEqual(base, cfg.cache_key(GeminiParams(voice="Zephyr")))
        self.assertNotEqual(base, cfg.cache_key(GeminiParams(voice="Kore", model="other")))


class GeminiRegistryTests(unittest.TestCase):
    def test_roundtrip_and_invalid_record_is_skipped(self):
        reg = VoiceRegistry(fallback_profile="piper-ruslan")
        reg.add(VoiceRecord(name="g", label="G", description="d", provider="gemini",
                            gemini=GeminiParams(voice="Kore", model="m", volume_db=-3.0)))
        data = registry_to_dict(reg)
        self.assertEqual(data["voices"]["g"]["gemini"], {"voice": "Kore", "model": "m", "volume_db": -3.0})
        loaded = registry_from_dict(data)
        self.assertEqual(loaded.get("g"), reg.get("g"))
        self.assertTrue(loaded.get("g").is_gemini)
        data["voices"]["bad"] = {"provider": "gemini", "gemini": {"volume_db": "loud"}}
        with self.assertLogs("tts_bot.registry", "WARNING"):
            self.assertNotIn("bad", registry_from_dict(data))


class GeminiDispatcherTests(unittest.IsolatedAsyncioTestCase):
    def _dispatcher(self, gemini, cache=None, local=None):
        return TTSDispatcher(
            local=local or SimpleNamespace(name="local", synthesize=AsyncMock()),
            gemini=gemini, cache=cache, fallback_profile="piper-ruslan",
            config=DispatcherConfig(quota_cooldown_seconds=900),
        )

    async def test_routes_gemini_voice_and_caches_raw_pcm(self):
        gemini = SimpleNamespace(config=GeminiConfig(api_key="k"),
                                 fetch_pcm=AsyncMock(return_value=(24000, 1, _pcm())))
        with tempfile.TemporaryDirectory() as tmp:
            cache = TTSPhraseCache(TTSCacheConfig(enabled=True, cache_dir=Path(tmp)))
            dispatcher = self._dispatcher(gemini, cache)
            out = Path(tmp) / "out.wav"
            self.assertEqual(await dispatcher.synthesize("Привет", out, voice=_voice()), "gemini")
            self.assertEqual(await dispatcher.synthesize("Привет", out, voice=_voice(volume_db=6)), "cache")
            self.assertEqual(gemini.fetch_pcm.await_count, 1)
            with wave.open(str(out)) as w:
                self.assertEqual(w.getframerate(), 24000)
            self.assertEqual([p.suffix for p in Path(tmp).glob("*.pcm")], [".pcm"])
            # A new process rehydrates .pcm entries.
            self.assertEqual(TTSPhraseCache(TTSCacheConfig(enabled=True, cache_dir=Path(tmp))).size, 1)

    async def test_failure_falls_back_to_piper_without_caching_it(self):
        gemini = SimpleNamespace(config=GeminiConfig(api_key="k"),
                                 fetch_pcm=AsyncMock(side_effect=GeminiError("boom", 500)))
        local = SimpleNamespace(name="local", synthesize=AsyncMock())
        with tempfile.TemporaryDirectory() as tmp:
            cache = TTSPhraseCache(TTSCacheConfig(enabled=True, cache_dir=Path(tmp)))
            dispatcher = self._dispatcher(gemini, cache, local)
            out = Path(tmp) / "out.wav"
            out.write_bytes(b"RIFF-piper")
            self.assertEqual(await dispatcher.synthesize("x", out, voice=_voice()), "local")
        local.synthesize.assert_awaited_once_with("x", out, "piper-ruslan")
        self.assertEqual(cache.size, 0)
        self.assertEqual(dispatcher.gemini_circuit_breaker.consecutive_failures, 1)

    async def test_quota_and_retry_after_set_breaker_cooldown(self):
        gemini = SimpleNamespace(config=GeminiConfig(api_key="k"), fetch_pcm=AsyncMock())
        dispatcher = self._dispatcher(gemini)
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "o.wav"
            gemini.fetch_pcm.side_effect = GeminiQuotaExhaustedError("402", 402)
            await dispatcher.synthesize("x", out, voice=_voice())
            self.assertGreater(dispatcher.gemini_circuit_breaker.cooldown_remaining, 800)
            dispatcher = self._dispatcher(gemini)
            gemini.fetch_pcm.side_effect = GeminiRateLimitError("429", 429, retry_after=20)
            await dispatcher.synthesize("x", out, voice=_voice())
            breaker = dispatcher.gemini_circuit_breaker
            self.assertEqual(breaker.state, CircuitState.OPEN)
            self.assertTrue(10 < breaker.cooldown_remaining <= 20)
            calls = gemini.fetch_pcm.await_count
            await dispatcher.synthesize("x", out, voice=_voice())
            self.assertEqual(gemini.fetch_pcm.await_count, calls)  # breaker open: no request

    def test_primary_provider_gemini_from_env(self):
        with patch.dict(os.environ, {"TTS_PRIMARY_PROVIDER": "gemini"}):
            self.assertIs(load_dispatcher_config_from_env().primary, PrimaryProvider.GEMINI)


class GeminiPipelineTests(unittest.IsolatedAsyncioTestCase):
    def _pipeline(self, handler, cache=None):
        provider, client = _provider(handler)
        pipeline = SynthesisPipelineMixin()
        pipeline.tts_dispatcher = TTSDispatcher(
            local=MagicMock(), gemini=provider, cache=cache,
            config=DispatcherConfig(quota_cooldown_seconds=900),
        )
        self.addAsyncCleanup(client.aclose)
        return pipeline

    async def _drain(self, prepared) -> list[bytes]:
        frames = []
        while not prepared.channel.empty():
            frames.extend(await prepared.channel.get())
        return frames

    async def test_stream_frames_cache_and_cache_hit_without_request(self):
        calls = 0

        def handle(request):
            nonlocal calls
            calls += 1
            return httpx.Response(200, headers={"content-type": PCM_CT},
                                  stream=_Chunks([_pcm()[:4801], _pcm()[4801:]]))

        with tempfile.TemporaryDirectory() as tmp:
            cache = TTSPhraseCache(TTSCacheConfig(enabled=True, cache_dir=Path(tmp)))
            pipeline = self._pipeline(handle, cache)
            first = PreparedAudio(job=_job(), channel=asyncio.Queue())
            self.assertEqual(await pipeline._generate_gemini_stream_into(first, _voice()), "ok")
            self.assertEqual(first.provider, "gemini")
            live = await self._drain(first)
            self.assertEqual(len(live), 25)  # 0.5 s of audio
            self.assertTrue(all(len(f) == PCM_FRAME_BYTES for f in live))
            second = PreparedAudio(job=_job(), channel=asyncio.Queue())
            self.assertEqual(await pipeline._generate_gemini_stream_into(second, _voice()), "cache")
            self.assertEqual(calls, 1)
            self.assertEqual(b"".join(await self._drain(second)), b"".join(live))
            self.assertEqual(list(Path(tmp).glob("*.tmp")), [])

    async def test_first_frame_is_sent_before_the_stream_ends(self):
        release = asyncio.Event()

        class Gated(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield _pcm()[:9600]
                await release.wait()
                yield _pcm()[9600:]

        pipeline = self._pipeline(lambda r: httpx.Response(200, headers={"content-type": PCM_CT}, stream=Gated()))
        prepared = PreparedAudio(job=_job(), channel=asyncio.Queue())
        task = asyncio.create_task(pipeline._generate_gemini_stream_into(prepared, _voice()))
        first = await asyncio.wait_for(prepared.channel.get(), 2)
        self.assertFalse(task.done())
        self.assertGreater(len(first), 0)
        release.set()
        self.assertEqual(await task, "ok")

    async def test_mid_stream_error_is_truncated_and_not_cached(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = TTSPhraseCache(TTSCacheConfig(enabled=True, cache_dir=Path(tmp)))
            pipeline = self._pipeline(lambda r: httpx.Response(
                200, headers={"content-type": PCM_CT}, stream=_Chunks([_pcm()], fail_after=True)), cache)
            prepared = PreparedAudio(job=_job(), channel=asyncio.Queue())
            self.assertEqual(await pipeline._generate_gemini_stream_into(prepared, _voice()), "truncated")
            self.assertEqual(cache.size, 0)
            self.assertEqual(list(Path(tmp).iterdir()), [])

    async def test_pre_audio_failures_fall_back(self):
        for response in (
            httpx.Response(200, headers={"content-type": PCM_CT}, content=b""),
            httpx.Response(402, json={"error": {"message": "credits"}}),
        ):
            pipeline = self._pipeline(lambda r, resp=response: resp)
            prepared = PreparedAudio(job=_job(), channel=asyncio.Queue())
            self.assertEqual(await pipeline._generate_gemini_stream_into(prepared, _voice()), "pre_audio")
            self.assertTrue(prepared.channel.empty())
        self.assertIsInstance(prepared.error, GeminiQuotaExhaustedError)
        self.assertGreater(pipeline.tts_dispatcher.gemini_circuit_breaker.cooldown_remaining, 800)

    async def test_slow_first_audio_times_out_to_piper(self):
        async def slow(request):
            await asyncio.sleep(1)
            return httpx.Response(200, headers={"content-type": PCM_CT}, content=_pcm())

        pipeline = self._pipeline(slow)
        prepared = PreparedAudio(job=_job("x"), channel=asyncio.Queue())
        with patch.object(config, "GEMINI_TTFA_TIMEOUT", 0.05), patch.object(config, "GEMINI_TTFA_PER_CHAR", 0.0):
            self.assertEqual(await pipeline._generate_gemini_stream_into(prepared, _voice()), "pre_audio")

    async def test_cancelled_job_stops(self):
        pipeline = self._pipeline(lambda r: self.fail("request sent"))
        prepared = PreparedAudio(job=_job(), channel=asyncio.Queue(), cancelled=True)
        self.assertEqual(await pipeline._generate_gemini_stream_into(prepared, _voice()), "cancelled")

    async def test_open_breaker_skips_the_api(self):
        pipeline = self._pipeline(lambda r: self.fail("request sent"))
        pipeline.tts_dispatcher.gemini_circuit_breaker.trip(60)
        prepared = PreparedAudio(job=_job(), channel=asyncio.Queue())
        self.assertEqual(await pipeline._generate_gemini_stream_into(prepared, _voice()), "pre_audio")

    async def test_prepare_into_falls_back_to_piper_file_path(self):
        pipeline = self._pipeline(lambda r: httpx.Response(500, text="down"))
        piper = VoiceRecord(name="piper-ruslan", label="P", description="", provider="piper")
        pipeline.voice_registry = VoiceRegistry(fallback_profile="piper-ruslan",
                                                voices={"gemini-kore": _voice(), "piper-ruslan": piper})
        pipeline._generate_file_into = AsyncMock()
        prepared = PreparedAudio(job=_job(), channel=asyncio.Queue())
        with patch.object(config, "TTS_STREAMING_ENABLED", True), patch.object(config, "TTS_CONTINUOUS_STREAM", True):
            await pipeline._prepare_into(prepared)
        pipeline._generate_file_into.assert_awaited_once_with(prepared, piper)
        self.assertIsNone(await prepared.channel.get())

    async def test_register_voice_probes_and_saves(self):
        pipeline = self._pipeline(lambda r: httpx.Response(200, headers={"content-type": PCM_CT}, content=_pcm()))
        pipeline.voice_registry = VoiceRegistry(fallback_profile="piper-ruslan")
        pipeline.persist_voice_registry = MagicMock()
        self.assertEqual(await pipeline.register_gemini_voice(
            name="gemini-kore", voice="Kore", label="K", description=""), "")
        self.assertEqual(pipeline.voice_registry.get("gemini-kore").gemini.voice, "Kore")
        pipeline.persist_voice_registry.assert_called_once()

        bad = self._pipeline(lambda r: httpx.Response(400, text="Provider returned 400"))
        bad.voice_registry = VoiceRegistry(fallback_profile="piper-ruslan")
        error = await bad.register_gemini_voice(name="x", voice="Nope", label="x", description="")
        self.assertIn("проверка озвучки не прошла", error)
        self.assertNotIn("x", bad.voice_registry)


if __name__ == "__main__":
    unittest.main()
