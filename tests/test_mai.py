"""MAI-Voice through OpenRouter: request contract, voice ids, plugin and pipeline paths."""

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import numpy as np

from ttsbot import config, mai, plugins
from ttsbot.audio import PCM_FRAME_BYTES
from ttsbot.errors import AuthError, QuotaExhaustedError, RateLimitError, RequestError
from ttsbot.models import PreparedAudio, TTSJob
from ttsbot.openrouter import OpenRouterError
from ttsbot.pipeline import SynthesisPipelineMixin
from ttsbot.providers import (
    PROVIDER_LABELS, DispatcherConfig, TTSCacheConfig, TTSDispatcher, TTSPhraseCache,
)
from ttsbot.voice_registry import (
    MaiParams, VoiceRecord, VoiceRegistry, registry_from_dict, registry_to_dict,
)

PCM_CT = "audio/pcm;rate=24000;channels=1"


def _pcm(seconds=0.5, rate=24000) -> bytes:
    t = np.arange(int(rate * seconds)) / rate
    return (np.sin(2 * np.pi * 440 * t) * 8000).astype("<i2").tobytes()


def _provider(handler, **cfg) -> tuple[mai.MaiProvider, httpx.AsyncClient]:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://openrouter.test/api/v1")
    return mai.MaiProvider(mai.MaiConfig(api_key="or-key", **cfg), client), client


def _voice(voice="ru-RU-Lev", model="", volume_db=0.0):
    return VoiceRecord(name="mai-lev", label="MAI Лев", description="", provider="mai",
                       mai=MaiParams(voice=voice, model=model, volume_db=volume_db))


def _job(text="Кто со мной?"):
    return TTSJob(text=text, voice_channel=None, queued_at=0, author_id=1,
                  guild_id=1, text_channel_id=1, voice_profile="mai-lev")


class _Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk


class MaiProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_request_contract_and_counters(self):
        seen = []

        def handle(request):
            seen.append((request.url.path, dict(request.headers), json.loads(request.content)))
            return httpx.Response(200, headers={"content-type": PCM_CT}, content=_pcm())

        provider, client = _provider(handle)
        try:
            rate, channels, pcm = await provider.fetch_pcm("Привет", MaiParams(voice="ru-RU-Masha"))
        finally:
            await client.aclose()
        path, headers, body = seen[0]
        self.assertEqual(path, "/api/v1/audio/speech")
        self.assertEqual(headers["authorization"], "Bearer or-key")
        self.assertEqual(body, {"model": "microsoft/mai-voice-2.1-flash", "input": "Привет",
                                "voice": "ru-RU-Masha:MAI-Voice-2.1-Flash", "response_format": "pcm"})
        self.assertEqual((rate, channels, len(pcm)), (24000, 1, len(_pcm())))
        self.assertEqual((provider.session_requests, provider.session_chars), (1, 6))

    def test_voice_id_follows_the_model(self):
        self.assertEqual(mai.voice_id("ru-RU-Lev", "microsoft/mai-voice-2.1-flash"),
                         "ru-RU-Lev:MAI-Voice-2.1-Flash")
        self.assertEqual(mai.voice_id("ru-RU-Lev", "microsoft/mai-voice-2.1"), "ru-RU-Lev:MAI-Voice-2.1")
        # A full id is sent as it is.
        self.assertEqual(mai.voice_id("en-US-Iris:MAI-Voice-2.1", "microsoft/mai-voice-2.1-flash"),
                         "en-US-Iris:MAI-Voice-2.1")

    async def test_the_voice_model_overrides_the_configured_one(self):
        bodies = []

        def handle(request):
            bodies.append(json.loads(request.content))
            return httpx.Response(200, headers={"content-type": PCM_CT}, content=_pcm())

        provider, client = _provider(handle)
        try:
            await provider.fetch_pcm("x", MaiParams(voice="ru-RU-Lev", model="microsoft/mai-voice-2.1"))
        finally:
            await client.aclose()
        self.assertEqual((bodies[0]["model"], bodies[0]["voice"]),
                         ("microsoft/mai-voice-2.1", "ru-RU-Lev:MAI-Voice-2.1"))

    async def test_http_errors_map_to_the_marker_classes(self):
        cases = [(401, AuthError), (402, QuotaExhaustedError), (429, RateLimitError), (400, RequestError),
                 (500, OpenRouterError)]
        for status, marker in cases:
            provider, client = _provider(lambda r, s=status: httpx.Response(
                s, json={"error": {"message": "nope", "code": s}}))
            try:
                with self.assertRaises(marker) as ctx:
                    await provider.fetch_pcm("x")
            finally:
                await client.aclose()
            self.assertEqual(ctx.exception.status_code, status)
        self.assertEqual(provider.session_requests, 0)

    async def test_empty_audio_is_an_error_named_after_mai(self):
        provider, client = _provider(lambda r: httpx.Response(200, headers={"content-type": PCM_CT}, content=b""))
        try:
            with self.assertRaisesRegex(OpenRouterError, "MAI returned no audio"):
                await provider.fetch_pcm("ну")
        finally:
            await client.aclose()

    def test_config_from_env_and_cache_key(self):
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": " k ", "MAI_TTS_MODEL": "", "MAI_TTS_VOICE": "ru-RU-Masha"}):
            cfg = mai.MaiConfig.from_env()
        self.assertEqual((cfg.api_key, cfg.model, cfg.voice), ("k", "microsoft/mai-voice-2.1-flash", "ru-RU-Masha"))
        base = cfg.cache_key(MaiParams(voice="ru-RU-Lev"))
        self.assertTrue(base.startswith("mai:"))
        self.assertEqual(base, cfg.cache_key(MaiParams(voice="ru-RU-Lev", volume_db=6.0)))
        self.assertNotEqual(base, cfg.cache_key(MaiParams(voice="ru-RU-Grant")))
        self.assertNotEqual(base, cfg.cache_key(MaiParams(voice="ru-RU-Lev", model="microsoft/mai-voice-2.1")))

    def test_stream_spec(self):
        provider = mai.MaiProvider(mai.MaiConfig(api_key="k"), httpx.AsyncClient())
        with patch.object(config, "MAI_TTFA_TIMEOUT", 2.5):
            spec = provider.stream_spec("Привет", _voice(volume_db=-3.0))
        self.assertEqual((spec.kind, spec.label, spec.ttfa, spec.volume_db), ("pcm", "MAI", 2.5, -3.0))
        self.assertEqual(spec.cache_key, provider.config.cache_key(MaiParams(voice="ru-RU-Lev")))
        self.assertEqual(PROVIDER_LABELS["mai"], "MAI")


class MaiRegistryTests(unittest.TestCase):
    def test_roundtrip_and_invalid_record_is_skipped(self):
        reg = VoiceRegistry(fallback_profile="piper-ruslan")
        reg.add(VoiceRecord(name="m", label="M", description="d", provider="mai",
                            mai=MaiParams(voice="ru-RU-Lev", model="x", volume_db=-3.0)))
        data = registry_to_dict(reg)
        self.assertEqual(data["voices"]["m"]["mai"], {"voice": "ru-RU-Lev", "model": "x", "volume_db": -3.0})
        loaded = registry_from_dict(data)
        self.assertEqual(loaded.get("m"), reg.get("m"))
        self.assertTrue(loaded.get("m").is_mai)
        data["voices"]["bad"] = {"provider": "mai", "mai": {"volume_db": 1}}
        with self.assertLogs("tts_bot.registry", "WARNING"):
            self.assertNotIn("bad", registry_from_dict(data))


class MaiPluginTests(unittest.TestCase):
    def test_built_from_the_openrouter_key(self):
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": ""}):
            self.assertIsNone(mai.create())
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "k"}):
            self.assertIsInstance(mai.create(), mai.MaiProvider)
        self.assertIn(mai, plugins.PLUGINS)

    def test_only_lev_is_seeded_once(self):
        registry = VoiceRegistry(fallback_profile="piper-ruslan")
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": ""}):
            self.assertFalse(mai.seed_voices(registry))
        self.assertEqual(registry.names(), [])
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "k"}):
            self.assertTrue(mai.seed_voices(registry))
            self.assertFalse(mai.seed_voices(registry))
        self.assertEqual(registry.names(), ["mai-lev"])
        self.assertEqual(registry.get("mai-lev").mai.voice, "ru-RU-Lev")

    def test_a_retuned_seeded_voice_is_kept(self):
        registry = VoiceRegistry(fallback_profile="piper-ruslan")
        registry.add(_voice(volume_db=4.0))
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "k"}):
            mai.seed_voices(registry)
        self.assertEqual(registry.get("mai-lev").mai.volume_db, 4.0)


class MaiPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_stream_frames_and_cache_hit_without_request(self):
        calls = 0

        def handle(request):
            nonlocal calls
            calls += 1
            return httpx.Response(200, headers={"content-type": PCM_CT},
                                  stream=_Chunks([_pcm()[:4801], _pcm()[4801:]]))

        provider, client = _provider(handle)
        self.addAsyncCleanup(client.aclose)
        with tempfile.TemporaryDirectory() as tmp:
            cache = TTSPhraseCache(TTSCacheConfig(enabled=True, cache_dir=Path(tmp)))
            pipeline = SynthesisPipelineMixin()
            pipeline.tts_dispatcher = TTSDispatcher(local=MagicMock(), cache=cache,
                                                    config=DispatcherConfig(), providers={"mai": provider})
            first = PreparedAudio(job=_job(), channel=asyncio.Queue())
            self.assertEqual(await pipeline._stream_voice_into(first, _voice()), "ok")
            self.assertEqual(first.provider, "mai")
            frames = []
            while not first.channel.empty():
                frames.extend(await first.channel.get())
            self.assertEqual(len(frames), 25)  # 0.5 s of audio
            self.assertTrue(all(len(f) == PCM_FRAME_BYTES for f in frames))
            second = PreparedAudio(job=_job(), channel=asyncio.Queue())
            self.assertEqual(await pipeline._stream_voice_into(second, _voice()), "cache")
            self.assertEqual(calls, 1)


if __name__ == "__main__":
    unittest.main()
