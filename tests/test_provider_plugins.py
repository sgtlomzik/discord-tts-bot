"""A provider plugs in by describing its stream; the pipeline is generic.

The pipeline had one cache/breaker/stream method per provider
(_generate_fish_stream_into, ..._gemini_..., ..._elevenlabs_...,
_generate_stream_into for MiniMax) and a wrapper per transport. A
provider now returns a StreamSpec (format, cache key, first-audio
budget, how to open the stream) and one method plays any of them.
"""

from __future__ import annotations

import asyncio
import tempfile
import types
import unittest
from pathlib import Path

from support import load_bot_module
from ttsbot.models import PreparedAudio, TTSJob
from ttsbot.providers import TTSCacheConfig, TTSPhraseCache
from ttsbot.streams import StreamSpec
from ttsbot.voice_registry import VoiceRecord

ONE_SECOND_24K_MONO = b"\x10\x00" * 24000


class AcmeStream:
    rate, channels = 24000, 1

    def __init__(self, pcm: bytes, fail_after: Exception | None = None) -> None:
        self.pcm = pcm
        self.fail_after = fail_after

    async def chunks(self):
        half = len(self.pcm) // 2
        yield self.pcm[:half]
        if self.fail_after is not None:
            raise self.fail_after
        yield self.pcm[half:]

    async def aclose(self) -> None:
        pass


class AcmeProvider:
    """A provider the pipeline has never heard of."""

    name = "acme"

    def __init__(self, fail: Exception | None = None, pcm: bytes = ONE_SECOND_24K_MONO,
                 fail_after: Exception | None = None) -> None:
        self.fail = fail
        self.pcm = pcm
        self.fail_after = fail_after
        self.requests = 0

    def stream_spec(self, text: str, voice) -> StreamSpec:
        async def open_stream():
            self.requests += 1
            if self.fail is not None:
                raise self.fail
            return AcmeStream(self.pcm, self.fail_after)

        return StreamSpec(kind="pcm", cache_key=f"acme:{voice.name}", ttfa=2.0, label="Acme", open=open_stream)


async def _drain(prepared):
    frames = []
    while (batch := await prepared.channel.get()) is not None:
        frames.extend(batch)
    return frames


class ProviderPluginTests(unittest.IsolatedAsyncioTestCase):
    def _bot(self, provider):
        bot_mod = load_bot_module()
        bot = bot_mod.TTSBot()
        bot.tts_dispatcher.register("acme", provider)
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        bot.tts_dispatcher._cache = TTSPhraseCache(TTSCacheConfig(enabled=True, cache_dir=Path(self.dir.name)))
        bot.voice_registry.add(VoiceRecord(name="acme-voice", label="A", description="", provider="acme",
                                           fallback=()))
        return bot

    def _prepared(self):
        channel = types.SimpleNamespace(id=10, guild=types.SimpleNamespace(id=1))
        job = TTSJob("привет", channel, 0.0, 1, 1, 1000, "acme-voice", 0.0)
        return PreparedAudio(job=job, channel=asyncio.Queue())

    async def test_a_new_provider_speaks_through_the_generic_path(self):
        acme = AcmeProvider()
        bot = self._bot(acme)
        prepared = self._prepared()
        await bot._prepare_into(prepared)
        frames = await _drain(prepared)
        self.assertEqual(len(frames), 50)  # 1 s in 20 ms frames
        self.assertEqual(prepared.provider, "acme")
        self.assertEqual(bot.tts_dispatcher.breaker_for("acme").consecutive_failures, 0)

    async def test_its_audio_is_cached_by_its_key(self):
        acme = AcmeProvider()
        bot = self._bot(acme)
        first = self._prepared()
        await bot._prepare_into(first)
        await _drain(first)
        again = self._prepared()
        await bot._prepare_into(again)
        self.assertEqual(len(await _drain(again)), 50)
        self.assertEqual(again.provider, "cache")
        self.assertEqual(acme.requests, 1)

    async def test_its_failures_count_against_its_own_breaker(self):
        acme = AcmeProvider(fail=RuntimeError("acme is down"))
        bot = self._bot(acme)
        prepared = self._prepared()
        await bot._prepare_into(prepared)
        self.assertEqual(await _drain(prepared), [])
        self.assertEqual(bot.tts_dispatcher.breaker_for("acme").consecutive_failures, 1)

    async def test_a_stream_cut_at_the_length_limit_does_not_pause_the_provider(self):
        # A stutter loop is cut by the frame limit: the provider answered,
        # so three of them in a row must not pause it.
        bot = self._bot(AcmeProvider(pcm=ONE_SECOND_24K_MONO * 30))
        for _ in range(3):
            prepared = self._prepared()
            await bot._prepare_into(prepared)
            await _drain(prepared)
        breaker = bot.tts_dispatcher.breaker_for("acme")
        self.assertEqual(breaker.consecutive_failures, 0)
        self.assertTrue(breaker.allow_request())

    async def test_a_stream_broken_after_audio_still_counts(self):
        bot = self._bot(AcmeProvider(fail_after=ConnectionResetError("dropped")))
        prepared = self._prepared()
        await bot._prepare_into(prepared)
        self.assertGreater(len(await _drain(prepared)), 0)
        self.assertEqual(bot.tts_dispatcher.breaker_for("acme").consecutive_failures, 1)

    def test_the_per_provider_methods_are_gone(self):
        bot_mod = load_bot_module()
        for name in ("_generate_stream_into", "_generate_fish_stream_into", "_generate_gemini_stream_into",
                     "_generate_elevenlabs_stream_into", "_stream_gemini_to_channel", "_stream_to_channel",
                     "_stream_fish_opus_to_channel", "_stream_fish_to_channel"):
            self.assertFalse(hasattr(bot_mod.TTSBot, name), name)


if __name__ == "__main__":
    unittest.main()
