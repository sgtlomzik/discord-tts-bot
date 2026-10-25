"""Parallel generation must not run more requests at a provider than it allows.

With one session per guild, each message is generated in its own task as
soon as it fits the prefetch window. Nothing bounded how many requests
ran at one provider at once: two messages in a row, or two guilds, opened
two Fish streams, Fish's plan answered 429 and the provider was paused for
everyone. A repeated phrase was also generated (and billed) twice, because
the second request started before the first one had been cached.
"""

from __future__ import annotations

import asyncio
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from support import load_bot_module
from ttsbot.models import PreparedAudio, TTSJob
from ttsbot.voice_registry import ElevenLabsParams, FishParams, VoiceRecord

FISH = VoiceRecord(name="fish-a", label="F", description="", provider="fish", fish=FishParams("ref"))
ELEVEN = VoiceRecord(name="eleven-a", label="E", description="", provider="elevenlabs",
                     elevenlabs=ElevenLabsParams(voice_id="a"))


def _prepared(job_id=1, guild_id=1):
    channel = types.SimpleNamespace(id=10, guild=types.SimpleNamespace(id=guild_id))
    return PreparedAudio(job=TTSJob("привет", channel, 0.0, 1, guild_id, 1000, "fish-a", 0.0, job_id=job_id),
                         channel=asyncio.Queue())


class ProviderSlotTests(unittest.IsolatedAsyncioTestCase):
    def _bot(self, env=None):
        with patch.dict("os.environ", env or {}):
            bot = load_bot_module().TTSBot()
        for name in ("fish", "elevenlabs"):
            bot.tts_dispatcher.register(name, MagicMock())
        bot._cached_frames = AsyncMock(return_value=[])
        self.running: dict[str, int] = {}
        self.peak: dict[str, int] = {}
        self.overall_peak = 0

        async def run(prepared, voice, spec):
            name = voice.provider
            self.running[name] = self.running.get(name, 0) + 1
            self.peak[name] = max(self.peak.get(name, 0), self.running[name])
            self.overall_peak = max(self.overall_peak, sum(self.running.values()))
            await asyncio.sleep(0.05)
            self.running[name] -= 1
            return "ok", 10

        bot._run_stream = AsyncMock(side_effect=run)
        return bot

    async def test_one_request_per_provider_at_a_time(self):
        bot = self._bot()
        statuses = await asyncio.gather(*(
            bot._stream_voice_into(_prepared(i, guild_id=i % 2), FISH) for i in range(3)
        ))
        self.assertEqual(statuses, ["ok"] * 3)
        self.assertEqual(self.peak, {"fish": 1})

    async def test_different_providers_still_run_in_parallel(self):
        bot = self._bot()
        await asyncio.gather(bot._stream_voice_into(_prepared(1), FISH),
                             bot._stream_voice_into(_prepared(2), ELEVEN))
        self.assertEqual(self.peak, {"fish": 1, "elevenlabs": 1})
        self.assertEqual(self.overall_peak, 2)

    async def test_the_limit_is_configurable_per_provider(self):
        bot = self._bot()
        with patch.dict("os.environ", {"FISH_MAX_CONCURRENCY": "2"}):  # read on first use
            await asyncio.gather(*(bot._stream_voice_into(_prepared(i), FISH) for i in range(4)))
        self.assertEqual(self.peak, {"fish": 2})

    async def test_a_repeated_phrase_waits_and_is_served_from_the_cache(self):
        bot = self._bot()
        cached: list[bytes] = []
        bot._cached_frames = AsyncMock(side_effect=lambda spec, text, voice: list(cached))
        first_run = bot._run_stream.side_effect

        async def run_and_cache(prepared, voice, spec):
            result = await first_run(prepared, voice, spec)
            cached.append(b"frame")  # committed when the stream ends
            return result

        bot._run_stream.side_effect = run_and_cache
        statuses = await asyncio.gather(bot._stream_voice_into(_prepared(1), FISH),
                                        bot._stream_voice_into(_prepared(2), FISH))
        self.assertEqual(statuses, ["ok", "cache"])
        bot._run_stream.assert_awaited_once()

    async def test_piper_synthesizes_one_message_at_a_time(self):
        bot = self._bot()
        running = peak = 0

        async def synthesize(text, filename, voice):
            nonlocal running, peak
            running += 1
            peak = max(peak, running)
            await asyncio.sleep(0.05)
            running -= 1
            return "local"

        bot.tts_dispatcher.synthesize_piper = synthesize
        bot._safe_pcm_frames = AsyncMock(return_value=[b"\x00" * 3840])
        piper = bot.voice_registry.get("piper-ruslan")
        results = await asyncio.gather(*(bot._generate_file_into(_prepared(i), piper) for i in range(3)))
        self.assertEqual(results, [True] * 3)
        self.assertEqual(peak, 1)


if __name__ == "__main__":
    unittest.main()
