"""Cloud voices always stream; only Piper is synthesized to a file.

The dispatcher also synthesized cloud voices to files (TTS_STREAMING_ENABLED=0
or a voice without a stream) with its own copy of the cache, breaker and
fallback logic, and routed a voiceless request by TTS_PRIMARY_PROVIDER.
None of it ran in production.
"""

from __future__ import annotations

import asyncio
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from support import load_bot_module
from ttsbot.elevenlabs import ElevenLabsVoiceNotFoundError
from ttsbot.models import PreparedAudio, TTSJob
from ttsbot.providers import LocalProvider, TTSCacheConfig, TTSDispatcher, TTSPhraseCache
from ttsbot.voice_registry import ElevenLabsParams, FishParams, VoiceRecord

PIPER = VoiceRecord(name="piper-ruslan", label="P", description="", provider="piper")


class PiperSynthesisTests(unittest.IsolatedAsyncioTestCase):
    async def test_piper_is_synthesized_once_then_served_from_the_cache(self):
        async def piper(text, filename, profile):
            filename.write_bytes(b"RIFF" + b"\x00" * 40)

        local = LocalProvider(AsyncMock(side_effect=piper))
        with tempfile.TemporaryDirectory() as tmp:
            cache = TTSPhraseCache(TTSCacheConfig(enabled=True, cache_dir=Path(tmp)))
            d = TTSDispatcher(local=local, cache=cache)
            first, second = Path(tmp) / "a.wav", Path(tmp) / "b.wav"
            self.assertEqual(await d.synthesize_piper("привет", first, PIPER), "local")
            self.assertEqual(await d.synthesize_piper("привет", second, PIPER), "cache")
            self.assertEqual(second.read_bytes(), first.read_bytes())
        local._piper_synthesize.assert_awaited_once()

    async def test_cached_piper_audio_survives_a_restart(self):
        async def piper(text, filename, profile):
            filename.write_bytes(b"RIFF" + b"\x00" * 40)

        with tempfile.TemporaryDirectory() as tmp:
            config = TTSCacheConfig(enabled=True, cache_dir=Path(tmp) / "cache")
            d = TTSDispatcher(local=LocalProvider(AsyncMock(side_effect=piper)), cache=TTSPhraseCache(config))
            await d.synthesize_piper("привет", Path(tmp) / "a.wav", PIPER)
            restarted = TTSPhraseCache(config)  # a new process reads the directory
            self.assertEqual(restarted.size, 1)
            self.assertEqual(restarted.lookup("привет", "piper-ruslan").suffix, ".wav")

    def test_the_cloud_file_path_and_primary_provider_are_gone(self):
        d = TTSDispatcher(local=MagicMock())
        for name in ("synthesize", "synthesize_chain", "route", "_synthesize_fish", "_synthesize_minimax",
                     "_synthesize_gemini", "_synthesize_elevenlabs"):
            self.assertFalse(hasattr(d, name), name)
        self.assertFalse(hasattr(d.config, "primary"))


def _job(voice):
    channel = types.SimpleNamespace(id=10, guild=types.SimpleNamespace(id=1))
    return TTSJob("привет", channel, 0.0, 1, 1, 1000, voice, 0.0)


class FileFallbackTests(unittest.IsolatedAsyncioTestCase):
    def _bot(self, env=None):
        with patch.dict("os.environ", env or {}):
            bot_mod = load_bot_module()
        bot = bot_mod.TTSBot()
        bot._generate_file_into = AsyncMock()
        return bot

    async def test_a_voice_whose_provider_is_not_configured_falls_to_piper(self):
        bot = self._bot()
        bot.voice_registry.add(VoiceRecord(name="fishy", label="F", description="", provider="fish",
                                           fish=FishParams("ref")))
        bot.voice_registry.fallback_chain = ["piper-ruslan"]
        bot._stream_voice_into = AsyncMock()
        prepared = PreparedAudio(job=_job("fishy"), channel=asyncio.Queue())
        await bot._prepare_into(prepared)
        bot._stream_voice_into.assert_not_awaited()
        self.assertEqual(bot._generate_file_into.await_args.args[1].name, "piper-ruslan")

    async def test_a_request_error_tries_the_next_voice_of_the_same_provider(self):
        bot = self._bot()
        bot.tts_dispatcher.register("elevenlabs", MagicMock())
        for name in ("eleven-a", "eleven-b"):
            bot.voice_registry.add(VoiceRecord(name=name, label=name, description="", provider="elevenlabs",
                                               elevenlabs=ElevenLabsParams(voice_id=name)))
        bot.voice_registry.fallback_chain = ["eleven-b", "piper-ruslan"]
        spoken = []

        async def stream(prepared, voice):
            spoken.append(voice.name)
            if voice.name == "eleven-a":
                prepared.error = ElevenLabsVoiceNotFoundError("404", 404)
                return "pre_audio"
            return "ok"

        bot._stream_voice_into = AsyncMock(side_effect=stream)
        prepared = PreparedAudio(job=_job("eleven-a"), channel=asyncio.Queue())
        with self.assertLogs("tts_bot", "WARNING"):
            await bot._prepare_into(prepared)
        self.assertEqual(spoken, ["eleven-a", "eleven-b"])
        bot._generate_file_into.assert_not_awaited()

    async def test_the_old_streaming_switch_changes_nothing(self):
        bot = self._bot({"TTS_STREAMING_ENABLED": "0"})
        bot.tts_dispatcher.register("elevenlabs", MagicMock())
        bot.voice_registry.add(VoiceRecord(name="eleven-a", label="E", description="", provider="elevenlabs",
                                           elevenlabs=ElevenLabsParams(voice_id="a")))
        bot._stream_voice_into = AsyncMock(return_value="ok")
        prepared = PreparedAudio(job=_job("eleven-a"), channel=asyncio.Queue())
        await bot._prepare_into(prepared)
        bot._stream_voice_into.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
