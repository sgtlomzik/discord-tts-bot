"""ElevenLabs: request contract, errors, registry, dispatcher and both stream paths."""

import asyncio
import json
import os
import subprocess
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
from ttsbot.elevenlabs import (
    ElevenLabsAuthError, ElevenLabsConfig, ElevenLabsError, ElevenLabsProvider,
    ElevenLabsQuotaExhaustedError, ElevenLabsRateLimitError, ElevenLabsVoiceNotFoundError,
    format_kind,
)
from ttsbot.errors import QuotaExhaustedError
from ttsbot.models import PreparedAudio, TTSJob
from ttsbot.ogg_opus import opus_packet_samples
from ttsbot.pipeline import DISCORD_OPUS_CACHE_SUFFIX, SynthesisPipelineMixin
from ttsbot.providers import (
    CircuitState, DispatcherConfig, PrimaryProvider, TTSCacheConfig, TTSDispatcher,
    TTSPhraseCache, load_dispatcher_config_from_env,
)
from ttsbot.voice_registry import (
    ElevenLabsParams, VoiceRecord, VoiceRegistry, registry_from_dict, registry_to_dict,
)

VOICE_ID = "JBFqnCBsd6RMkjVDRZzb"


def _ogg(duration=0.5) -> bytes:
    """Mono 48 kHz Ogg/Opus with 20 ms packets: the shape ElevenLabs sends."""
    return subprocess.check_output([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
        f"sine=frequency=440:duration={duration}", "-ac", "1", "-c:a", "libopus",
        "-b:a", "64k", "-frame_duration", "20", "-f", "ogg", "pipe:1",
    ])


def _pcm(seconds=0.5, rate=24000) -> bytes:
    t = np.arange(int(rate * seconds)) / rate
    return (np.sin(2 * np.pi * 440 * t) * 8000).astype("<i2").tobytes()


def _config(fmt="opus_48000_64", **kwargs) -> ElevenLabsConfig:
    return ElevenLabsConfig(api_key="xi-key", format=fmt, **kwargs)


def _provider(handler, fmt="opus_48000_64", **kwargs):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://el.test")
    return ElevenLabsProvider(_config(fmt, **kwargs), client), client


def _voice(**kwargs):
    return VoiceRecord(name="eleven-george", label="George", description="", provider="elevenlabs",
                       elevenlabs=ElevenLabsParams(voice_id=VOICE_ID, **kwargs))


def _job(text="Кто со мной?"):
    return TTSJob(text=text, voice_channel=None, queued_at=0, author_id=1,
                  guild_id=1, text_channel_id=1, voice_profile="eleven-george")


def _audio(content=b"", fmt="opus", cost="6", stream=None):
    ct = "audio/opus" if fmt == "opus" else "audio/pcm"
    headers = {"content-type": ct, "character-cost": cost}
    if stream is not None:
        return httpx.Response(200, headers=headers, stream=stream)
    return httpx.Response(200, headers=headers, content=content)


class _Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks, fail_after=False):
        self.chunks = chunks
        self.fail_after = fail_after

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk
        if self.fail_after:
            raise httpx.ReadError("connection reset")


def _error(status, reason, message="nope", headers=None):
    return httpx.Response(status, headers=headers or {},
                          json={"detail": {"status": reason, "message": message}})


class ElevenLabsProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_request_contract_and_counters(self):
        seen = []

        def handle(request):
            seen.append((request.url, dict(request.headers), json.loads(request.content)))
            return _audio(b"OggS-audio", cost="7")

        provider, client = _provider(handle, language_code="ru")
        try:
            kind, rate, audio = await provider.fetch(
                "Привет", ElevenLabsParams(voice_id="v1", stability=0.4, similarity_boost=0.9),
            )
            await provider.fetch("Пока", ElevenLabsParams(voice_id="v1", model="eleven_v4"))
        finally:
            await client.aclose()
        url, headers, body = seen[0]
        self.assertEqual(url.path, "/v1/text-to-speech/v1/stream")
        self.assertEqual(url.params["output_format"], "opus_48000_64")
        self.assertEqual(headers["xi-api-key"], "xi-key")
        self.assertEqual(body, {"text": "Привет", "model_id": "eleven_v4_turbo", "language_code": "ru",
                                "voice_settings": {"stability": 0.4, "similarity_boost": 0.9}})
        # Unset settings are not sent, so the voice's own defaults apply.
        self.assertEqual(seen[1][2], {"text": "Пока", "model_id": "eleven_v4", "language_code": "ru"})
        self.assertEqual((kind, rate, audio), ("opus", 48000, b"OggS-audio"))
        self.assertEqual(
            (provider.session_requests, provider.session_chars, provider.session_credits), (2, 10, 14),
        )

    async def test_errors_map_to_typed_exceptions(self):
        cases = [
            (_error(401, "quota_exceeded"), ElevenLabsError),
            (_error(402, "payment_required"), ElevenLabsQuotaExhaustedError),
            (_error(401, "invalid_api_key"), ElevenLabsAuthError),
            (_error(401, "missing_permissions"), ElevenLabsAuthError),
            (_error(400, "invalid_api_key_length"), ElevenLabsError),
            (_error(404, "voice_not_found"), ElevenLabsVoiceNotFoundError),
            (httpx.Response(404, text="Not Found"), ElevenLabsError),
            (_error(429, "too_many_concurrent_requests", headers={"retry-after": "7"}), ElevenLabsRateLimitError),
            (httpx.Response(500, text="boom"), ElevenLabsError),
            (httpx.Response(200, headers={"content-type": "application/json"}, content=b"{}"), ElevenLabsError),
        ]
        for response, exc_type in cases:
            provider, client = _provider(lambda r, resp=response: resp)
            try:
                with self.assertRaises(exc_type) as ctx:
                    await provider.fetch("x", ElevenLabsParams(voice_id="v"))
            finally:
                await client.aclose()
            self.assertIs(type(ctx.exception), exc_type)
            if exc_type is ElevenLabsRateLimitError:
                self.assertEqual(ctx.exception.retry_after, 7.0)
        self.assertTrue(issubclass(ElevenLabsQuotaExhaustedError, QuotaExhaustedError))
        self.assertEqual(provider.session_requests, 0)

    async def test_error_message_names_the_reason(self):
        provider, client = _provider(lambda r: _error(404, "voice_not_found", "A voice was not found."))
        try:
            with self.assertRaisesRegex(ElevenLabsVoiceNotFoundError, "404 voice_not_found: A voice"):
                await provider.fetch("x", ElevenLabsParams(voice_id="v"))
        finally:
            await client.aclose()

    async def test_missing_key_or_voice_fails_without_a_request(self):
        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: self.fail("request sent")))
        with self.assertRaises(ElevenLabsAuthError):
            await ElevenLabsProvider(ElevenLabsConfig(api_key=""), client).fetch("x", ElevenLabsParams("v"))
        with self.assertRaises(ElevenLabsVoiceNotFoundError):
            await ElevenLabsProvider(_config(), client).fetch("x")
        await client.aclose()

    async def test_empty_body_is_an_error(self):
        provider, client = _provider(lambda r: _audio(b""))
        try:
            with self.assertRaisesRegex(ElevenLabsError, "no audio"):
                await provider.fetch("x", ElevenLabsParams(voice_id="v"))
        finally:
            await client.aclose()

    async def test_synthesize_writes_ogg_or_wav(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "a.wav"
            provider, client = _provider(lambda r: _audio(b"OggS-audio"))
            self.assertEqual(await provider.synthesize("x", out, ElevenLabsParams("v")), "opus")
            self.assertEqual(out.read_bytes(), b"OggS-audio")
            await client.aclose()
            provider, client = _provider(
                lambda r: _audio(stream=_Chunks([b"\x01\x02\x03", b"\x04\x05"]), fmt="pcm"), fmt="pcm_24000",
            )
            self.assertEqual(await provider.synthesize("x", out, ElevenLabsParams("v")), "pcm")
            await client.aclose()
            with wave.open(str(out)) as w:
                self.assertEqual((w.getframerate(), w.getnchannels(), w.getnframes()), (24000, 1, 2))

    async def test_list_voices_is_cached_and_tolerates_failures(self):
        calls = 0

        def handle(request):
            nonlocal calls
            calls += 1
            return httpx.Response(200, json={"voices": [{"voice_id": "a1", "name": "George"}, {"x": 1}]})

        provider, client = _provider(handle)
        self.assertEqual(await provider.list_voices(), [("a1", "George")])
        self.assertEqual(await provider.list_voices(), [("a1", "George")])
        self.assertEqual(calls, 1)
        await client.aclose()
        failures = 0

        def deny(request):
            nonlocal failures
            failures += 1
            return _error(401, "missing_permissions")

        provider, client = _provider(deny)
        self.assertEqual(await provider.list_voices(), [])
        self.assertEqual(await provider.list_voices(), [])
        self.assertEqual(failures, 1)  # a failure is cached too
        await client.aclose()

    def test_format_and_config(self):
        self.assertEqual(format_kind("opus_48000_128"), ("opus", 48000))
        self.assertEqual(format_kind("pcm_24000"), ("pcm", 24000))
        for bad in ("mp3_44100_128", "opus_24000_64", "pcm_12345", "pcm_"):
            with self.assertRaises(ValueError):
                format_kind(bad)
        with patch.dict(os.environ, {"ELEVENLABS_API_KEY": " k ", "ELEVENLABS_MODEL": "",
                                     "ELEVENLABS_FORMAT": "PCM_48000", "ELEVENLABS_VOICE_ID": "v"}):
            cfg = ElevenLabsConfig.from_env()
        self.assertEqual((cfg.api_key, cfg.model, cfg.format, cfg.voice_id, cfg.kind),
                         ("k", "eleven_v4_turbo", "pcm_48000", "v", "pcm"))
        with patch.dict(os.environ, {"ELEVENLABS_FORMAT": "mp3_44100_128"}):
            with self.assertRaises(ValueError):
                ElevenLabsConfig.from_env()

    def test_cache_key_covers_every_audio_setting(self):
        cfg = _config()
        base = cfg.cache_key(ElevenLabsParams("v"))
        self.assertEqual(base, cfg.cache_key(ElevenLabsParams("v")))
        for other in (
            cfg.cache_key(ElevenLabsParams("w")),
            cfg.cache_key(ElevenLabsParams("v", model="eleven_v4")),
            cfg.cache_key(ElevenLabsParams("v", stability=0.3)),
            cfg.cache_key(ElevenLabsParams("v", similarity_boost=0.3)),
            _config("opus_48000_128").cache_key(ElevenLabsParams("v")),
            _config(language_code="ru").cache_key(ElevenLabsParams("v")),
        ):
            self.assertNotEqual(base, other)


class ElevenLabsRegistryTests(unittest.TestCase):
    def test_roundtrip_and_invalid_record_is_skipped(self):
        reg = VoiceRegistry(fallback_profile="piper-ruslan")
        reg.add(_voice(model="eleven_v4", stability=0.3))
        data = registry_to_dict(reg)
        self.assertEqual(data["voices"]["eleven-george"]["elevenlabs"], {
            "voice_id": VOICE_ID, "model": "eleven_v4", "stability": 0.3, "similarity_boost": None,
        })
        loaded = registry_from_dict(data)
        self.assertEqual(loaded.get("eleven-george"), reg.get("eleven-george"))
        self.assertTrue(loaded.get("eleven-george").is_elevenlabs)
        data["voices"]["odd"] = {"provider": "elevenlabs",
                                 "elevenlabs": {"voice_id": "v", "stability": "high", "similarity_boost": 7}}
        odd = registry_from_dict(data).get("odd").elevenlabs
        self.assertEqual((odd.stability, odd.similarity_boost), (None, None))
        data["voices"]["bad"] = {"provider": "elevenlabs", "elevenlabs": {}}
        with self.assertLogs("tts_bot.registry", "WARNING"):
            self.assertNotIn("bad", registry_from_dict(data))


class ElevenLabsDispatcherTests(unittest.IsolatedAsyncioTestCase):
    def _dispatcher(self, elevenlabs, cache=None, local=None):
        return TTSDispatcher(
            local=local or SimpleNamespace(name="local", synthesize=AsyncMock()),
            elevenlabs=elevenlabs, cache=cache, fallback_profile="piper-ruslan",
            config=DispatcherConfig(quota_cooldown_seconds=900),
        )

    async def test_routes_voice_and_caches_ogg(self):
        eleven = SimpleNamespace(config=_config(), fetch=AsyncMock(return_value=("opus", 48000, b"OggS-x")))
        with tempfile.TemporaryDirectory() as tmp:
            cache = TTSPhraseCache(TTSCacheConfig(enabled=True, cache_dir=Path(tmp)))
            dispatcher = self._dispatcher(eleven, cache)
            out = Path(tmp) / "out.wav"
            self.assertEqual(await dispatcher.synthesize("Привет", out, voice=_voice()), "elevenlabs")
            out.unlink()
            self.assertEqual(await dispatcher.synthesize("Привет", out, voice=_voice()), "cache")
            self.assertEqual(out.read_bytes(), b"OggS-x")
            self.assertEqual(eleven.fetch.await_count, 1)
            self.assertEqual([p.suffix for p in Path(tmp).glob("*.opus")], [".opus"])

    async def test_pcm_format_caches_pcm_and_writes_wav(self):
        eleven = SimpleNamespace(config=_config("pcm_24000"),
                                 fetch=AsyncMock(return_value=("pcm", 24000, _pcm())))
        with tempfile.TemporaryDirectory() as tmp:
            cache = TTSPhraseCache(TTSCacheConfig(enabled=True, cache_dir=Path(tmp)))
            dispatcher = self._dispatcher(eleven, cache)
            out = Path(tmp) / "out.wav"
            self.assertEqual(await dispatcher.synthesize("x", out, voice=_voice()), "elevenlabs")
            self.assertEqual(await dispatcher.synthesize("x", out, voice=_voice()), "cache")
            with wave.open(str(out)) as w:
                self.assertEqual((w.getframerate(), w.getnframes()), (24000, 12000))

    async def test_failure_falls_back_to_piper_without_caching_it(self):
        eleven = SimpleNamespace(config=_config(), fetch=AsyncMock(side_effect=ElevenLabsError("boom", 500)))
        local = SimpleNamespace(name="local", synthesize=AsyncMock())
        with tempfile.TemporaryDirectory() as tmp:
            cache = TTSPhraseCache(TTSCacheConfig(enabled=True, cache_dir=Path(tmp)))
            dispatcher = self._dispatcher(eleven, cache, local)
            out = Path(tmp) / "out.wav"
            self.assertEqual(await dispatcher.synthesize("x", out, voice=_voice()), "local")
        local.synthesize.assert_awaited_once_with("x", out, "piper-ruslan")
        self.assertEqual(cache.size, 0)
        self.assertEqual(dispatcher.elevenlabs_circuit_breaker.consecutive_failures, 1)

    async def test_quota_and_retry_after_set_breaker_cooldown(self):
        eleven = SimpleNamespace(config=_config(), fetch=AsyncMock())
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "o.wav"
            dispatcher = self._dispatcher(eleven)
            eleven.fetch.side_effect = ElevenLabsQuotaExhaustedError("quota", 401)
            await dispatcher.synthesize("x", out, voice=_voice())
            self.assertGreater(dispatcher.elevenlabs_circuit_breaker.cooldown_remaining, 800)
            dispatcher = self._dispatcher(eleven)
            eleven.fetch.side_effect = ElevenLabsRateLimitError("429", 429, retry_after=20)
            await dispatcher.synthesize("x", out, voice=_voice())
            breaker = dispatcher.elevenlabs_circuit_breaker
            self.assertEqual(breaker.state, CircuitState.OPEN)
            calls = eleven.fetch.await_count
            await dispatcher.synthesize("x", out, voice=_voice())
            self.assertEqual(eleven.fetch.await_count, calls)  # breaker open: no request

    async def test_voice_not_found_and_voiceless_primary_do_not_trip_the_breaker(self):
        eleven = SimpleNamespace(config=_config(), fetch=AsyncMock(side_effect=ElevenLabsVoiceNotFoundError("404")))
        dispatcher = self._dispatcher(eleven)
        with tempfile.TemporaryDirectory() as tmp:
            for _ in range(5):
                self.assertEqual(await dispatcher.synthesize("x", Path(tmp) / "o.wav", voice=_voice()), "local")
            self.assertEqual(dispatcher.elevenlabs_circuit_breaker.consecutive_failures, 0)
            dispatcher = TTSDispatcher(
                local=SimpleNamespace(name="local", synthesize=AsyncMock()), elevenlabs=eleven,
                config=DispatcherConfig(primary=PrimaryProvider.ELEVENLABS),
            )
            eleven.fetch.reset_mock()
            self.assertEqual(await dispatcher.synthesize("x", Path(tmp) / "o.wav"), "local")
            eleven.fetch.assert_not_awaited()  # no ELEVENLABS_VOICE_ID: not even tried

    def test_primary_provider_from_env(self):
        with patch.dict(os.environ, {"TTS_PRIMARY_PROVIDER": "elevenlabs"}):
            self.assertIs(load_dispatcher_config_from_env().primary, PrimaryProvider.ELEVENLABS)


class ElevenLabsPipelineTests(unittest.IsolatedAsyncioTestCase):
    def _pipeline(self, handler, cache=None, fmt="opus_48000_64"):
        provider, client = _provider(handler, fmt)
        pipeline = SynthesisPipelineMixin()
        pipeline.tts_dispatcher = TTSDispatcher(
            local=MagicMock(), elevenlabs=provider, cache=cache,
            config=DispatcherConfig(quota_cooldown_seconds=900),
        )
        self.addAsyncCleanup(client.aclose)
        return pipeline

    async def _drain(self, prepared) -> list[bytes]:
        frames = []
        while not prepared.channel.empty():
            frames.extend(await prepared.channel.get())
        return frames

    async def test_opus_packets_go_direct_and_cache_hit_skips_the_api(self):
        ogg = _ogg()
        calls = 0

        def handle(request):
            nonlocal calls
            calls += 1
            return _audio(stream=_Chunks([ogg[:700], ogg[700:]]))

        with tempfile.TemporaryDirectory() as tmp:
            cache = TTSPhraseCache(TTSCacheConfig(enabled=True, cache_dir=Path(tmp)))
            pipeline = self._pipeline(handle, cache)
            first = PreparedAudio(job=_job(), channel=asyncio.Queue())
            self.assertEqual(await pipeline._generate_elevenlabs_stream_into(first, _voice()), "ok")
            self.assertEqual(first.provider, "elevenlabs")
            live = await self._drain(first)
            self.assertGreaterEqual(len(live), 25)
            # Opus packets, not 3840-byte PCM frames: nothing was decoded.
            self.assertTrue(all(len(p) < PCM_FRAME_BYTES and opus_packet_samples(p) == 960 for p in live))
            key = pipeline.tts_dispatcher.elevenlabs.config.cache_key(_voice().elevenlabs)
            self.assertEqual(cache.lookup(_job().text, key + DISCORD_OPUS_CACHE_SUFFIX).suffix, ".dopus")
            second = PreparedAudio(job=_job(), channel=asyncio.Queue())
            self.assertEqual(await pipeline._generate_elevenlabs_stream_into(second, _voice()), "cache")
            self.assertEqual(calls, 1)
            self.assertEqual(await self._drain(second), live)
            self.assertEqual(list(Path(tmp).glob("*.tmp")), [])

    async def test_first_packet_is_sent_before_the_stream_ends(self):
        ogg = _ogg(3)
        last_page = ogg.rfind(b"OggS")
        release = asyncio.Event()

        class Gated(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield ogg[:last_page]
                await release.wait()
                yield ogg[last_page:]

        pipeline = self._pipeline(lambda r: _audio(stream=Gated()))
        prepared = PreparedAudio(job=_job("Длинная фраза для проверки стрима"), channel=asyncio.Queue())
        task = asyncio.create_task(pipeline._generate_elevenlabs_stream_into(prepared, _voice()))
        try:
            first = await asyncio.wait_for(prepared.channel.get(), 3)
            self.assertTrue(first)
            self.assertFalse(task.done())
        finally:
            release.set()
        self.assertEqual(await task, "ok")

    async def test_pcm_format_is_framed_in_process_and_cached(self):
        pcm = _pcm(rate=48000)
        with tempfile.TemporaryDirectory() as tmp:
            cache = TTSPhraseCache(TTSCacheConfig(enabled=True, cache_dir=Path(tmp)))
            pipeline = self._pipeline(
                lambda r: _audio(stream=_Chunks([pcm[:4801], pcm[4801:]]), fmt="pcm"), cache, "pcm_48000",
            )
            first = PreparedAudio(job=_job(), channel=asyncio.Queue())
            self.assertEqual(await pipeline._generate_elevenlabs_stream_into(first, _voice()), "ok")
            live = await self._drain(first)
            self.assertEqual(len(live), 25)
            self.assertTrue(all(len(f) == PCM_FRAME_BYTES for f in live))
            second = PreparedAudio(job=_job(), channel=asyncio.Queue())
            self.assertEqual(await pipeline._generate_elevenlabs_stream_into(second, _voice()), "cache")
            self.assertEqual(b"".join(await self._drain(second)), b"".join(live))

    async def test_mid_stream_error_is_truncated_and_not_cached(self):
        ogg = _ogg(1)
        with tempfile.TemporaryDirectory() as tmp:
            cache = TTSPhraseCache(TTSCacheConfig(enabled=True, cache_dir=Path(tmp)))
            pipeline = self._pipeline(
                lambda r: _audio(stream=_Chunks([ogg[:ogg.rfind(b"OggS")]], fail_after=True)), cache,
            )
            prepared = PreparedAudio(job=_job(), channel=asyncio.Queue())
            self.assertEqual(await pipeline._generate_elevenlabs_stream_into(prepared, _voice()), "truncated")
            self.assertEqual(cache.size, 0)
            self.assertEqual(list(Path(tmp).iterdir()), [])

    async def test_pre_audio_failures_fall_back_and_quota_trips_the_breaker(self):
        for response in (_audio(b""), _error(402, "payment_required")):
            pipeline = self._pipeline(lambda r, resp=response: resp)
            prepared = PreparedAudio(job=_job(), channel=asyncio.Queue())
            self.assertEqual(await pipeline._generate_elevenlabs_stream_into(prepared, _voice()), "pre_audio")
            self.assertTrue(prepared.channel.empty())
        self.assertIsInstance(prepared.error, ElevenLabsQuotaExhaustedError)
        self.assertGreater(pipeline.tts_dispatcher.elevenlabs_circuit_breaker.cooldown_remaining, 800)

    async def test_billing_is_counted_when_the_stream_stops_early(self):
        pipeline = self._pipeline(lambda r: _audio(stream=_Chunks([_ogg(1)[:900]], fail_after=True), cost="6.5"))
        prepared = PreparedAudio(job=_job(), channel=asyncio.Queue())
        await pipeline._generate_elevenlabs_stream_into(prepared, _voice())
        provider = pipeline.tts_dispatcher.elevenlabs
        self.assertEqual((provider.session_requests, provider.session_credits), (1, 6))

    async def test_missing_voice_or_short_quota_does_not_pause_other_voices(self):
        for response in (_error(404, "voice_not_found"), _error(401, "quota_exceeded")):
            pipeline = self._pipeline(lambda r, resp=response: resp)
            for _ in range(5):
                prepared = PreparedAudio(job=_job(), channel=asyncio.Queue())
                self.assertEqual(await pipeline._generate_elevenlabs_stream_into(prepared, _voice()), "pre_audio")
            breaker = pipeline.tts_dispatcher.elevenlabs_circuit_breaker
            if isinstance(prepared.error, ElevenLabsVoiceNotFoundError):
                self.assertEqual(breaker.state, CircuitState.CLOSED)
            else:  # an ordinary failure: the short breaker, not the quota pause
                self.assertLess(breaker.cooldown_remaining, 100)

    async def test_slow_first_audio_times_out_to_piper(self):
        async def slow(request):
            await asyncio.sleep(1)
            return _audio(_ogg())

        pipeline = self._pipeline(slow)
        prepared = PreparedAudio(job=_job("x"), channel=asyncio.Queue())
        with patch.object(config, "ELEVENLABS_TTFA_TIMEOUT", 0.05):
            with self.assertLogs("tts_bot", "WARNING") as logs:
                status = await pipeline._generate_elevenlabs_stream_into(prepared, _voice())
        self.assertEqual(status, "pre_audio")
        self.assertIn("ElevenLabs first audio exceeded", "\n".join(logs.output))

    async def test_cancelled_job_and_open_breaker_skip_the_api(self):
        pipeline = self._pipeline(lambda r: self.fail("request sent"))
        cancelled = PreparedAudio(job=_job(), channel=asyncio.Queue(), cancelled=True)
        self.assertEqual(await pipeline._generate_elevenlabs_stream_into(cancelled, _voice()), "cancelled")
        pipeline.tts_dispatcher.elevenlabs_circuit_breaker.trip(60)
        prepared = PreparedAudio(job=_job(), channel=asyncio.Queue())
        self.assertEqual(await pipeline._generate_elevenlabs_stream_into(prepared, _voice()), "pre_audio")

    async def test_prepare_into_falls_back_to_piper_file_path(self):
        pipeline = self._pipeline(lambda r: httpx.Response(500, text="down"))
        piper = VoiceRecord(name="piper-ruslan", label="P", description="", provider="piper")
        pipeline.voice_registry = VoiceRegistry(fallback_profile="piper-ruslan",
                                                voices={"eleven-george": _voice(), "piper-ruslan": piper})
        pipeline._generate_file_into = AsyncMock()
        prepared = PreparedAudio(job=_job(), channel=asyncio.Queue())
        with patch.object(config, "TTS_STREAMING_ENABLED", True), patch.object(config, "TTS_CONTINUOUS_STREAM", True):
            await pipeline._prepare_into(prepared)
        pipeline._generate_file_into.assert_awaited_once_with(prepared, piper)
        self.assertIsNone(await prepared.channel.get())

    async def test_register_voice_probes_and_saves(self):
        pipeline = self._pipeline(lambda r: _audio(b"OggS-probe"))
        pipeline.voice_registry = VoiceRegistry(fallback_profile="piper-ruslan")
        pipeline.persist_voice_registry = MagicMock()
        self.assertEqual(await pipeline.register_elevenlabs_voice(
            name="eleven-george", voice_id=VOICE_ID, label="G", description=""), "")
        self.assertEqual(pipeline.voice_registry.get("eleven-george").elevenlabs.voice_id, VOICE_ID)
        pipeline.persist_voice_registry.assert_called_once()

        bad = self._pipeline(lambda r: _error(404, "voice_not_found"))
        bad.voice_registry = VoiceRegistry(fallback_profile="piper-ruslan")
        error = await bad.register_elevenlabs_voice(name="x", voice_id="nope", label="x", description="")
        self.assertIn("проверка озвучки не прошла", error)
        self.assertNotIn("x", bad.voice_registry)


if __name__ == "__main__":
    unittest.main()
