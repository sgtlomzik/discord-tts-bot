"""Fish request contract, concurrent streaming, and Ogg/Opus cache playback."""

import asyncio
import json
import math
import os
import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from ttsbot.audio import (
    PCM_FRAME_BYTES, OPUS_SILENCE_FRAME, ContinuousTTSAudioSource, load_opus,
    build_playback_filter_complex, build_tts_stream_pcm_command,
)
from ttsbot.ogg_opus import OggOpusDemuxer, UnsupportedOpusStream, opus_packet_samples, read_frame_cache
from ttsbot import config
from ttsbot.fish import FishConfig, FishProvider, fish_tts_text
from ttsbot.models import PreparedAudio, TTSJob
from ttsbot.pipeline import SynthesisPipelineMixin
from ttsbot.playback import PlaybackMixin
from ttsbot.providers import CircuitBreaker, TTSCacheConfig, TTSPhraseCache
from ttsbot.store import BotConfigStore
from ttsbot.voice_registry import FishParams, VoiceRecord, VoiceRegistry, registry_from_dict, registry_to_dict, save_registry
from scripts.migrate_fish_default import migrate
from test_bot import load_bot_module


class _TwoChunkStream(httpx.AsyncByteStream):
    def __init__(self, release: asyncio.Event):
        self.release = release

    async def __aiter__(self):
        yield b"OggS-first"
        await self.release.wait()
        yield b"-second"


class FishProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_voice_tuning_survives_bot_restart(self):
        from ttsbot.core import TTSBot

        with tempfile.TemporaryDirectory() as tmp, patch.object(config, "VOICES_REGISTRY_PATH", Path(tmp) / "voices.json"), \
                patch.object(config, "BOT_CONFIG_PATH", Path(tmp) / "config.json"), \
                patch.dict(os.environ, {"FISH_API_KEY": "test-key", "FISH_REFERENCE_ID": "voice-id", "TTS_CACHE_ENABLED": "0"}):
            first = TTSBot()
            try:
                original = first.voice_registry.get("fish-default")
                first.voice_registry.add(replace(original, fish=replace(original.fish, speed=1.4)))
                save_registry(config.VOICES_REGISTRY_PATH, first.voice_registry)
                first.set_fish_latency("balanced")
                second = TTSBot()
                try:
                    self.assertEqual(second.voice_registry.get("fish-default").fish.speed, 1.4)
                    self.assertEqual(second.voice_registry.get("fish-default").fish.reference_id, "voice-id")
                    self.assertEqual(second.fish_latency, "balanced")
                    self.assertEqual(second.fish_provider.config.latency, "balanced")
                finally:
                    await second.fish_provider.aclose()
            finally:
                await first.fish_provider.aclose()

    async def test_tuned_voice_request_and_cache_identity(self):
        requests = []

        def handle(request):
            requests.append(request)
            return httpx.Response(200, content=b"OggS-audio")

        client = httpx.AsyncClient(transport=httpx.MockTransport(handle), base_url="https://api.fish.audio")
        fish = FishProvider(FishConfig(api_key="test-key", reference_id="voice-id", latency="balanced"), client)
        tuned = FishParams("voice-id", model="s2-pro", speed=1.25, volume_db=3.0,
                           pitch=4, emotion="auto", temperature=0.5, top_p=0.8)
        try:
            self.assertNotEqual(fish.config.cache_key("voice-id"), fish.config.cache_key("voice-id", tuned))
            self.assertEqual(fish_tts_text("ПРИВЕТ!", "auto"), "[angry] ПРИВЕТ!")
            self.assertEqual(b"".join([chunk async for chunk in fish.stream_audio(
                "ПРИВЕТ!", reference_id="voice-id", params=tuned,
            )]), b"OggS-audio")
            request = requests[0]
            self.assertEqual(request.headers["model"], "s2-pro")
            body = json.loads(request.content)
            self.assertEqual(body["text"], "[angry] ПРИВЕТ!")
            self.assertEqual(body["prosody"]["speed"], 1.25)
            self.assertEqual(body["prosody"]["volume"], 3.0)
            self.assertEqual(body["temperature"], 0.5)
            self.assertEqual(body["top_p"], 0.8)
            self.assertEqual(body["latency"], "balanced")
        finally:
            await fish.aclose()
            await client.aclose()

    async def test_global_latency_snapshot_keeps_inflight_request_consistent(self):
        requests = []

        def handle(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, content=b"OggS-audio")

        client = httpx.AsyncClient(transport=httpx.MockTransport(handle), base_url="https://api.fish.audio")
        fish = FishProvider(FishConfig(api_key="test-key", reference_id="voice-id"), client)
        old_config = fish.config
        fish.config = replace(old_config, latency="normal")
        try:
            self.assertNotEqual(old_config.cache_key("voice-id"), fish.config.cache_key("voice-id"))
            self.assertEqual(b"".join([chunk async for chunk in fish.stream_audio(
                "Привет", request_config=old_config,
            )]), b"OggS-audio")
            self.assertEqual(b"".join([chunk async for chunk in fish.stream_audio(
                "Привет",
            )]), b"OggS-audio")
            self.assertEqual([request["latency"] for request in requests], ["low", "normal"])
        finally:
            await fish.aclose()
            await client.aclose()

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
            self.assertEqual(fish.session_requests, 1)
            self.assertEqual(fish.session_chars, len("Привет"))
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
        self.assertEqual(cfg.latency, "low")
        self.assertNotEqual(cfg.cache_key("voice-a"), cfg.cache_key("voice-b"))
        self.assertNotEqual(cfg.cache_key("voice-a"), FishConfig(api_key="ignored", model="s2-pro").cache_key("voice-a"))
        self.assertNotEqual(cfg.cache_key("voice-a"), FishConfig(api_key="ignored", opus_bitrate=64000).cache_key("voice-a"))
        self.assertNotEqual(cfg.cache_key("voice-a"), replace(cfg, latency="balanced").cache_key("voice-a"))

    def test_cache_key_ignores_local_pitch_shift(self):
        cfg = FishConfig(api_key="ignored")
        self.assertEqual(
            cfg.cache_key("voice-a", FishParams("voice-a")),
            cfg.cache_key("voice-a", FishParams("voice-a", pitch=5)),
        )

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
        voice = VoiceRecord(name="fish-default", label="Fish", description="", provider="fish",
                            fish=FishParams("voice-id", speed=1.25, pitch=-3, emotion="sad"))
        reg = SimpleNamespace(version=1, fallback_profile="fish-default", voices={"fish-default": voice})
        restored = registry_from_dict(registry_to_dict(reg))
        self.assertEqual(restored.get("fish-default").fish.reference_id, "voice-id")
        self.assertEqual(restored.get("fish-default").fish.speed, 1.25)
        self.assertEqual(restored.get("fish-default").fish.pitch, -3)
        self.assertEqual(restored.get("fish-default").fish.emotion, "sad")


class FishLatencyCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_store_rejects_invalid_mode_and_rolls_back_failed_save(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = BotConfigStore(Path(tmp) / "config.json", set())
            store.set_fish_latency("normal")
            self.assertEqual(BotConfigStore(store.path, set()).settings["fish_latency"], "normal")
            with self.assertRaises(ValueError):
                store.set_fish_latency("fastest")
            with patch.object(store, "save", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    store.set_fish_latency("balanced")
            self.assertEqual(store.settings["fish_latency"], "normal")

    async def test_global_discord_switch_persists_and_applies_to_next_requests(self):
        from ttsbot.core import TTSBot

        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
            "BOT_CONFIG_PATH": str(Path(tmp) / "config.json"),
            "VOICES_REGISTRY_PATH": str(Path(tmp) / "voices.json"),
            "FISH_API_KEY": "test-key", "FISH_REFERENCE_ID": "voice-id",
            "FISH_LATENCY": "low", "TTS_CACHE_ENABLED": "0",
        }):
            bot_mod = load_bot_module()
            sent = []
            interaction = SimpleNamespace(
                guild=SimpleNamespace(id=1), user=SimpleNamespace(id=2),
                response=SimpleNamespace(send_message=AsyncMock(
                    side_effect=lambda message, **kwargs: sent.append(message),
                )),
            )
            voice = bot_mod.bot.voice_registry.get("fish-default")
            original_key = bot_mod.bot.fish_provider.config.cache_key(voice.fish.reference_id, voice.fish)
            with patch.object(bot_mod.tts_commands, "is_guild_manager", return_value=True), \
                    patch.object(bot_mod.discord, "Member", type(interaction.user)):
                await bot_mod.slash_tts_fish_latency.callback(
                    interaction, SimpleNamespace(value="balanced"),
                )
            self.assertEqual(bot_mod.bot.fish_latency, "balanced")
            self.assertEqual(bot_mod.bot.fish_provider.config.latency, "balanced")
            self.assertEqual(bot_mod.bot.voice_registry.get("fish-default"), voice)
            self.assertNotEqual(
                original_key,
                bot_mod.bot.fish_provider.config.cache_key(voice.fish.reference_id, voice.fish),
            )
            self.assertEqual(json.loads(Path(tmp, "config.json").read_text())["settings"]["fish_latency"], "balanced")
            self.assertIn("balanced", sent[0])
            restarted = TTSBot()
            try:
                self.assertEqual(restarted.fish_latency, "balanced")
            finally:
                await restarted.fish_provider.aclose()
                await bot_mod.bot.fish_provider.aclose()


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg required")
class FishDirectOpusTests(unittest.TestCase):
    def test_player_mixes_opus_and_pcm_without_restart(self):
        player = SimpleNamespace(
            continuous_sources={}, cancel_continuous_idle_stop=MagicMock(),
        )
        playing = MagicMock(return_value=False)
        vc = SimpleNamespace(
            guild=SimpleNamespace(id=123), is_playing=playing,
            is_paused=MagicMock(return_value=False), stop=MagicMock(), play=MagicMock(),
        )
        source = PlaybackMixin.ensure_continuous_player(
            player, vc, initial_frames=[OPUS_SILENCE_FRAME],
        )
        playing.return_value = True
        self.assertTrue(source.is_opus())
        again = PlaybackMixin.ensure_continuous_player(
            player, vc, initial_frames=[b"\x00" * PCM_FRAME_BYTES],
        )
        self.assertIs(again, source)
        self.assertFalse(source.stopped)
        vc.stop.assert_not_called()
        vc.play.assert_called_once_with(source)
        self.assertEqual(source.read(), OPUS_SILENCE_FRAME)
        source.stop()

    def test_player_replaces_legacy_pcm_source(self):
        player = SimpleNamespace(
            continuous_sources={}, cancel_continuous_idle_stop=MagicMock(),
        )
        vc = SimpleNamespace(
            guild=SimpleNamespace(id=123), is_playing=MagicMock(return_value=True),
            is_paused=MagicMock(return_value=False), stop=MagicMock(), play=MagicMock(),
        )
        legacy = ContinuousTTSAudioSource(b"\x00" * PCM_FRAME_BYTES)
        player.continuous_sources[123] = legacy
        source = PlaybackMixin.ensure_continuous_player(player, vc)
        self.assertTrue(legacy.stopped)
        self.assertTrue(source.is_opus())
        vc.stop.assert_called_once()

    @unittest.skipUnless(load_opus(), "libopus required")
    def test_opus_player_encodes_pcm_frames(self):
        source = ContinuousTTSAudioSource(OPUS_SILENCE_FRAME, opus=True)
        tone = b"".join(
            int(8000 * math.sin(2 * math.pi * 440 * i / 48000)).to_bytes(2, "little", signed=True) * 2
            for i in range(960)
        )
        source.enqueue_frames([tone, OPUS_SILENCE_FRAME])
        packet = source.read()
        self.assertLess(len(packet), PCM_FRAME_BYTES)
        self.assertEqual(opus_packet_samples(packet), 960)
        self.assertEqual(source.read(), OPUS_SILENCE_FRAME)
        self.assertEqual(source.read(), OPUS_SILENCE_FRAME)  # idle
        self.assertTrue(source.is_drained)
        with self.assertRaises(ValueError):
            source.enqueue_frames([b"\x00" * 100])

    def test_incremental_ogg_demux_emits_discord_frames(self):
        import subprocess

        ogg = subprocess.check_output([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
            "sine=frequency=440:duration=0.5", "-c:a", "libopus",
            "-frame_duration", "20", "-f", "ogg", "pipe:1",
        ])
        demuxer = OggOpusDemuxer()
        frames = []
        for offset in range(0, len(ogg), 17):
            frames.extend(demuxer.feed(ogg[offset:offset + 17]))
        demuxer.finish()
        self.assertGreater(len(frames), 20)
        self.assertTrue(all(opus_packet_samples(packet) == 960 for packet in frames))

        source = ContinuousTTSAudioSource(OPUS_SILENCE_FRAME, opus=True)
        self.assertTrue(source.is_opus())
        source.enqueue_frames(frames)
        self.assertEqual(source.read(), frames[0])
        source.stop()

    def test_rejects_non_20_ms_packets(self):
        import subprocess

        ogg = subprocess.check_output([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
            "sine=frequency=440:duration=0.5", "-c:a", "libopus",
            "-frame_duration", "40", "-f", "ogg", "pipe:1",
        ])
        demuxer = OggOpusDemuxer()
        with self.assertRaises(UnsupportedOpusStream):
            demuxer.feed(ogg)


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg required")
class FishDecodeTests(unittest.IsolatedAsyncioTestCase):
    async def test_direct_opus_arrives_before_final_ogg_page(self):
        import subprocess

        ogg = subprocess.check_output([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
            "sine=frequency=440:duration=3", "-c:a", "libopus", "-f", "ogg", "pipe:1",
        ])
        last_page = ogg.rfind(b"OggS")
        self.assertGreater(last_page, 0)
        release = asyncio.Event()

        async def chunks(*args, **kwargs):
            yield ogg[:last_page]
            await release.wait()
            yield ogg[last_page:]

        with tempfile.TemporaryDirectory() as tmp:
            cache = TTSPhraseCache(TTSCacheConfig(
                enabled=True, max_entries=1000, max_bytes=1000000, cache_dir=Path(tmp),
            ))
            pipeline = SynthesisPipelineMixin()
            pipeline.tts_dispatcher = SimpleNamespace(
                fish=SimpleNamespace(stream_audio=chunks), cache=cache,
            )
            job = TTSJob(text="Длинная проверка", voice_channel=None, queued_at=0,
                         author_id=1, guild_id=1, text_channel_id=1, voice_profile="fish-default")
            prepared = PreparedAudio(job=job, channel=asyncio.Queue())
            voice = SimpleNamespace(fish=FishParams("voice-id"))
            task = asyncio.create_task(pipeline._stream_fish_opus_to_channel(
                prepared, voice, "direct-key",
            ))
            try:
                first = await asyncio.wait_for(prepared.channel.get(), 3)
                self.assertTrue(first)
                self.assertTrue(all(opus_packet_samples(packet) == 960 for packet in first))
                self.assertEqual(prepared.codec, "opus")
                self.assertFalse(task.done())
                self.assertIsNone(cache.lookup(job.text, "direct-key"))
            finally:
                release.set()
            self.assertEqual((await task)[0], "ok")
            self.assertEqual(cache.lookup(job.text, "direct-key").suffix, ".dopus")

    async def test_pitch_shift_raises_frequency_without_changing_duration(self):
        import struct
        import subprocess

        ogg = subprocess.check_output([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
            "sine=frequency=440:duration=0.5", "-c:a", "libopus", "-f", "ogg", "pipe:1",
        ])
        decoded = subprocess.run(
            build_tts_stream_pcm_command("ogg", 12), input=ogg,
            capture_output=True, check=True,
        ).stdout
        self.assertGreater(len(decoded), PCM_FRAME_BYTES * 18)
        self.assertLess(len(decoded), PCM_FRAME_BYTES * 32)
        mono = [struct.unpack_from("<h", decoded, i)[0] for i in range(0, len(decoded), 4)]
        crossings = sum(a <= 0 < b for a, b in zip(mono, mono[1:]))
        frequency = crossings / (len(mono) / 48000)
        self.assertGreater(frequency, 800)
        self.assertLess(frequency, 960)
        self.assertIn("asetrate=96000", build_playback_filter_complex(False, -40.0, 12))

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
                self.assertEqual(first.codec, "opus")
                second = PreparedAudio(job=job, channel=asyncio.Queue())
                with patch.object(pipeline, "_safe_pcm_frames", side_effect=AssertionError("ffmpeg used")):
                    self.assertEqual(await pipeline._generate_fish_stream_into(second, voice), "cache")
                self.assertEqual(calls, 1)
                self.assertEqual(second.codec, "opus")
                cached = cache.lookup(job.text, f"{fish.config.cache_key('voice-id', voice.fish)}:discord-opus-v1")
                self.assertEqual(cached.suffix, ".dopus")
                self.assertGreater(len(list(read_frame_cache(cached))), 0)
                self.assertGreater(len(await second.channel.get()), 0)
            finally:
                await fish.aclose()
                await client.aclose()

    async def test_legacy_ogg_cache_converts_without_http_or_ffmpeg(self):
        import subprocess

        ogg = subprocess.check_output([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
            "sine=frequency=440:duration=0.5", "-c:a", "libopus", "-f", "ogg", "pipe:1",
        ])
        with tempfile.TemporaryDirectory() as tmp:
            fish = FishProvider(FishConfig(api_key="test-key", reference_id="voice-id"))
            cache = TTSPhraseCache(TTSCacheConfig(
                enabled=True, max_entries=1000, max_bytes=1000000, cache_dir=Path(tmp),
            ))
            pipeline = SynthesisPipelineMixin()
            pipeline.tts_dispatcher = SimpleNamespace(
                fish=fish, cache=cache, fish_circuit_breaker=CircuitBreaker(),
            )
            voice = VoiceRecord(name="fish-default", label="Fish", description="",
                                provider="fish", fish=FishParams("voice-id"))
            job = TTSJob(text="Привет", voice_channel=None, queued_at=0, author_id=1,
                         guild_id=1, text_channel_id=1, voice_profile="fish-default")
            key = fish.config.cache_key("voice-id", voice.fish)
            old_path = cache.cache_path_for(job.text, key, "opus")
            old_path.parent.mkdir(parents=True, exist_ok=True)
            old_path.write_bytes(ogg)
            cache.commit_file(job.text, old_path, key)
            prepared = PreparedAudio(job=job, channel=asyncio.Queue())
            try:
                with patch.object(fish, "stream_audio", side_effect=AssertionError("HTTP used")), \
                        patch.object(pipeline, "_safe_pcm_frames", side_effect=AssertionError("ffmpeg used")):
                    self.assertEqual(await pipeline._generate_fish_stream_into(prepared, voice), "cache")
                self.assertEqual(prepared.codec, "opus")
                new_path = cache.lookup(job.text, f"{key}:discord-opus-v1")
                self.assertEqual(new_path.suffix, ".dopus")
                self.assertGreater(len(list(read_frame_cache(new_path))), 0)
            finally:
                await fish.aclose()
