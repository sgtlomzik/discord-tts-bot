"""Fish request contract, concurrent streaming, and Ogg/Opus cache playback."""

import asyncio
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from ttsbot.audio import PCM_FRAME_BYTES, build_tts_stream_pcm_command
from ttsbot import config
from ttsbot.fish import FishConfig, FishProvider
from ttsbot.models import PreparedAudio, TTSJob
from ttsbot.pipeline import SynthesisPipelineMixin
from ttsbot.playback import PlaybackMixin
from ttsbot.providers import CircuitBreaker, TTSCacheConfig, TTSPhraseCache
from ttsbot.voice_registry import FishParams, VoiceRecord, VoiceRegistry, registry_from_dict, registry_to_dict
from scripts.migrate_fish_default import migrate


class _TwoChunkStream(httpx.AsyncByteStream):
    def __init__(self, release: asyncio.Event):
        self.release = release

    async def __aiter__(self):
        yield b"OggS-first"
        await self.release.wait()
        yield b"-second"


class FishProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_clone_uploads_private_model_and_waits_for_training(self):
        requests = []

        def handle(request):
            requests.append(request)
            if request.method == "POST" and request.url.path == "/model":
                return httpx.Response(201, json={"_id": "new-fish-id", "state": "created"})
            if request.method == "GET" and request.url.path == "/model/new-fish-id":
                return httpx.Response(200, json={"_id": "new-fish-id", "state": "trained"})
            return httpx.Response(404)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handle), base_url="https://api.fish.audio")
        fish = FishProvider(FishConfig(api_key="test-key"), client)
        try:
            voice_id = await fish.clone_voice(b"sample-data", title="new-voice", filename="sample.wav", description="test")
            self.assertEqual(voice_id, "new-fish-id")
            self.assertEqual(len(requests), 2)
            create = requests[0]
            self.assertEqual(create.headers["Authorization"], "Bearer test-key")
            self.assertIn("multipart/form-data", create.headers["Content-Type"])
            for value in (b'name="voices"', b"sample-data", b'name="train_mode"', b"fast",
                          b'name="visibility"', b"private", b'name="title"', b"new-voice"):
                self.assertIn(value, create.content)
        finally:
            await fish.aclose()
            await client.aclose()

    async def test_clone_rejects_failed_training(self):
        def handle(request):
            return httpx.Response(201, json={"_id": "failed-id", "state": "failed"})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handle), base_url="https://api.fish.audio")
        fish = FishProvider(FishConfig(api_key="test-key"), client)
        try:
            with self.assertRaisesRegex(RuntimeError, "training failed"):
                await fish.clone_voice(b"sample-data", title="new-voice", filename="sample.wav")
        finally:
            await fish.aclose()
            await client.aclose()

    async def test_clone_registers_only_after_ogg_probe(self):
        calls = []

        def handle(request):
            calls.append(request.url.path)
            if request.url.path == "/model":
                return httpx.Response(201, json={"_id": "new-fish-id", "state": "trained"})
            if request.url.path == "/v1/tts":
                return httpx.Response(200, content=b"OggS-probe-audio")
            return httpx.Response(404)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handle), base_url="https://api.fish.audio")
        fish = FishProvider(FishConfig(api_key="test-key"), client)
        with tempfile.TemporaryDirectory() as tmp, patch.object(config, "TMP_DIR", Path(tmp)):
            pipeline = SynthesisPipelineMixin()
            pipeline.tts_dispatcher = SimpleNamespace(fish=fish)
            pipeline.voice_registry = VoiceRegistry(fallback_profile="piper-ruslan")
            saved = []
            pipeline.persist_voice_registry = lambda: saved.append(True)
            try:
                ok, reference_id = await pipeline.clone_fish_voice(
                    name="new-voice", sample=b"sample-data", filename="sample.wav", description="test",
                )
                self.assertTrue(ok)
                self.assertEqual(reference_id, "new-fish-id")
                self.assertEqual(pipeline.voice_registry.get("new-voice").fish.reference_id, reference_id)
                self.assertEqual(saved, [True])
                self.assertEqual(calls, ["/model", "/v1/tts"])
                self.assertEqual(list(Path(tmp).iterdir()), [])
            finally:
                await fish.aclose()
                await client.aclose()

    async def test_request_and_inflight_dedup(self):
        release = asyncio.Event()
        requests = []

        async def handle(request):
            requests.append(request)
            return httpx.Response(200, stream=_TwoChunkStream(release))

        client = httpx.AsyncClient(transport=httpx.MockTransport(handle), base_url="https://api.fish.audio")
        fish = FishProvider(FishConfig(api_key="test-key", reference_id="voice-id"), client)
        first_a = asyncio.Event()
        first_b = asyncio.Event()

        async def collect(first):
            chunks = []
            async for chunk in fish.stream_audio("Привет"):
                chunks.append(chunk)
                first.set()
            return b"".join(chunks)

        try:
            a = asyncio.create_task(collect(first_a))
            b = asyncio.create_task(collect(first_b))
            await asyncio.wait_for(asyncio.gather(first_a.wait(), first_b.wait()), 2)
            self.assertFalse(a.done())  # first audio arrives before the HTTP response ends
            self.assertFalse(b.done())
            release.set()
            self.assertEqual(await a, b"OggS-first-second")
            self.assertEqual(await b, b"OggS-first-second")
            self.assertEqual(len(requests), 1)
            request = requests[0]
            self.assertEqual(request.headers["model"], "s2.1-pro-free")
            self.assertEqual(request.headers["Authorization"], "Bearer test-key")
            body = json.loads(request.content)
            self.assertEqual(body["reference_id"], "voice-id")
            self.assertEqual(body["format"], "opus")
            self.assertEqual(body["latency"], "low")
            self.assertEqual(body["chunk_length"], 150)
            self.assertEqual(body["opus_bitrate"], 48000)
        finally:
            release.set()
            await fish.aclose()
            await client.aclose()

    async def test_http_failure_is_not_cached_in_flight(self):
        calls = 0

        def handle(request):
            nonlocal calls
            calls += 1
            return httpx.Response(503, json={"message": "unavailable", "status": 503})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handle), base_url="https://api.fish.audio")
        fish = FishProvider(FishConfig(api_key="test-key", reference_id="voice-id"), client)
        try:
            for _ in range(2):
                with self.assertRaisesRegex(RuntimeError, "Fish HTTP 503"):
                    async for _ in fish.stream_audio("Привет"):
                        pass
            self.assertEqual(calls, 2)
        finally:
            await fish.aclose()
            await client.aclose()


class FishCacheTests(unittest.TestCase):
    def test_migration_preserves_whitelist_and_other_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            original = {
                "version": 1,
                "guilds": {"123": {"enabled": True, "allowed_users": [1, 2],
                                    "default_voice": "bussshy01", "user_voices": {"1": "papich"},
                                    "user_fixed_phrases": {"2": "Привет"}}},
                "emoji_aliases": {"456": {"name": "smile", "say": "улыбка"}},
                "settings": {"tts_max_chars": 300},
            }
            path.write_text(json.dumps(original), encoding="utf-8")
            changed, cleared, backup = migrate(path)
            self.assertEqual((changed, cleared), (1, 1))
            self.assertEqual(json.loads(backup.read_text(encoding="utf-8")), original)
            updated = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(updated["guilds"]["123"]["default_voice"], "fish-default")
            self.assertEqual(updated["guilds"]["123"]["user_voices"], {})
            self.assertEqual(updated["guilds"]["123"]["allowed_users"], [1, 2])
            self.assertEqual(updated["guilds"]["123"]["user_fixed_phrases"], {"2": "Привет"})
            self.assertEqual(updated["emoji_aliases"], original["emoji_aliases"])

    def test_cache_key_changes_with_reference_model_and_tuning(self):
        cfg = FishConfig(api_key="ignored")
        self.assertNotEqual(cfg.cache_key("voice-a"), cfg.cache_key("voice-b"))
        self.assertNotEqual(cfg.cache_key("voice-a"), FishConfig(api_key="ignored", model="s2-pro").cache_key("voice-a"))
        self.assertNotEqual(cfg.cache_key("voice-a"), FishConfig(api_key="ignored", opus_bitrate=64000).cache_key("voice-a"))

    def test_opus_cache_rehydrates_and_evicts_by_lru(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp) / "cache"
            cache = TTSPhraseCache(TTSCacheConfig(enabled=True, max_entries=2, max_bytes=10000, cache_dir=cache_dir))
            for text in ("a", "b", "c"):
                source = Path(tmp) / f"{text}.opus"
                source.write_bytes(b"OggS" + text.encode())
                cache.store(text, source, "fish-config", suffix="opus")
            self.assertIsNone(cache.lookup("a", "fish-config"))
            self.assertEqual(cache.size, 2)
            self.assertEqual(cache.lookup("b", "fish-config").suffix, ".opus")
            restored = TTSPhraseCache(cache.config)
            self.assertEqual(restored.size, 2)
            self.assertEqual(restored.lookup("c", "fish-config").read_bytes(), b"OggSc")
            self.assertFalse(list(cache_dir.glob("*.tmp")))

    def test_fish_voice_round_trip(self):
        voice = VoiceRecord(name="fish-default", label="Fish", description="", provider="fish", fish=FishParams("voice-id"))
        reg = SimpleNamespace(version=1, fallback_profile="fish-default", voices={"fish-default": voice})
        restored = registry_from_dict(registry_to_dict(reg))
        self.assertEqual(restored.get("fish-default").fish.reference_id, "voice-id")


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg required")
class FishDecodeTests(unittest.IsolatedAsyncioTestCase):
    async def test_streamed_ogg_decodes_to_pcm_and_caches_opus(self):
        import subprocess

        ogg = subprocess.check_output([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
            "sine=frequency=440:duration=0.5", "-c:a", "libopus", "-f", "ogg", "pipe:1",
        ])
        with tempfile.TemporaryDirectory() as tmp:
            cache = TTSPhraseCache(TTSCacheConfig(enabled=True, max_entries=1000, max_bytes=1000000, cache_dir=Path(tmp)))
            pipeline = SynthesisPipelineMixin()
            pipeline.tts_dispatcher = SimpleNamespace(cache=cache)
            job = TTSJob(text="Привет", voice_channel=None, queued_at=0, author_id=1, guild_id=1, text_channel_id=1, voice_profile="fish-default")
            prepared = PreparedAudio(job=job, channel=asyncio.Queue())

            async def chunks():
                for offset in range(0, len(ogg), 200):
                    yield ogg[offset:offset + 200]
                    await asyncio.sleep(0)

            self.assertIn("ogg", build_tts_stream_pcm_command("ogg"))
            status, frames = await pipeline._decode_stream_to_channel(
                prepared, SimpleNamespace(fish=FishParams("voice-id")), chunks(),
                "ogg", "fish-config", "opus",
            )
            self.assertEqual(status, "ok")
            self.assertGreater(frames, 0)
            batch = await prepared.channel.get()
            self.assertTrue(all(len(frame) == PCM_FRAME_BYTES for frame in batch))
            hit = cache.lookup("Привет", "fish-config")
            self.assertEqual(hit.suffix, ".opus")
            self.assertEqual(hit.read_bytes(), ogg)

    async def test_pcm_arrives_before_final_ogg_page(self):
        import subprocess

        ogg = subprocess.check_output([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
            "sine=frequency=440:duration=3", "-c:a", "libopus", "-f", "ogg", "pipe:1",
        ])
        last_page = ogg.rfind(b"OggS")
        self.assertGreater(last_page, 0)
        release = asyncio.Event()
        with tempfile.TemporaryDirectory() as tmp:
            cache = TTSPhraseCache(TTSCacheConfig(enabled=True, max_entries=1000, max_bytes=1000000, cache_dir=Path(tmp)))
            pipeline = SynthesisPipelineMixin()
            pipeline.tts_dispatcher = SimpleNamespace(cache=cache)
            job = TTSJob(text="Длинная проверка", voice_channel=None, queued_at=0, author_id=1, guild_id=1, text_channel_id=1, voice_profile="fish-default")
            prepared = PreparedAudio(job=job, channel=asyncio.Queue())

            async def chunks():
                yield ogg[:last_page]
                await release.wait()
                yield ogg[last_page:]

            task = asyncio.create_task(pipeline._decode_stream_to_channel(
                prepared, SimpleNamespace(fish=FishParams("voice-id")), chunks(),
                "ogg", "fish-config", "opus",
            ))
            try:
                first_batch = await asyncio.wait_for(prepared.channel.get(), 3)
                self.assertGreater(len(first_batch), 0)
                self.assertFalse(task.done())
                self.assertIsNone(cache.lookup(job.text, "fish-config"))
            finally:
                release.set()
            self.assertEqual((await task)[0], "ok")

    async def test_fish_cache_hit_skips_second_http_request(self):
        import subprocess

        ogg = subprocess.check_output([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
            "sine=frequency=440:duration=0.5", "-c:a", "libopus", "-f", "ogg", "pipe:1",
        ])
        calls = 0

        def handle(request):
            nonlocal calls
            calls += 1
            return httpx.Response(200, content=ogg)

        class Pipeline(SynthesisPipelineMixin, PlaybackMixin):
            pass

        with tempfile.TemporaryDirectory() as tmp:
            client = httpx.AsyncClient(transport=httpx.MockTransport(handle), base_url="https://api.fish.audio")
            fish = FishProvider(FishConfig(api_key="test-key", reference_id="voice-id"), client)
            cache = TTSPhraseCache(TTSCacheConfig(enabled=True, max_entries=1000, max_bytes=1000000, cache_dir=Path(tmp)))
            pipeline = Pipeline()
            pipeline.tts_dispatcher = SimpleNamespace(fish=fish, cache=cache, fish_circuit_breaker=CircuitBreaker())
            voice = VoiceRecord(name="fish-default", label="Fish", description="", provider="fish", fish=FishParams("voice-id"))
            job = TTSJob(text="Привет", voice_channel=None, queued_at=0, author_id=1, guild_id=1, text_channel_id=1, voice_profile="fish-default")
            try:
                first = PreparedAudio(job=job, channel=asyncio.Queue())
                self.assertEqual(await pipeline._generate_fish_stream_into(first, voice), "ok")
                second = PreparedAudio(job=job, channel=asyncio.Queue())
                self.assertEqual(await pipeline._generate_fish_stream_into(second, voice), "cache")
                self.assertEqual(calls, 1)
                self.assertGreater(len(await second.channel.get()), 0)
            finally:
                await fish.aclose()
                await client.aclose()
