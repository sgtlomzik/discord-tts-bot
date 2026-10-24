"""There is one pipeline: prefetching generation into the continuous player.

TTS_PREFETCH_ENABLED=0 used to switch to an older single worker and
TTS_CONTINUOUS_STREAM=0 to per-message FFmpeg playback. Neither ran in
production; a leftover setting must not bring them back.
"""

from __future__ import annotations

import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from test_bot import load_bot_module
from ttsbot import voice_registry


class OnePipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_old_switches_still_start_the_prefetch_workers(self):
        with patch.dict("os.environ", {"TTS_PREFETCH_ENABLED": "0", "TTS_CONTINUOUS_STREAM": "0"}):
            bot_mod = load_bot_module()
        tts_bot = bot_mod.TTSBot()
        tts_bot.tree.sync = AsyncMock(return_value=[])
        await tts_bot.setup_hook()
        try:
            self.assertNotIn("tts-worker", set(tts_bot.supervisor.status()))
            tts_bot.session_for(1)  # a guild's first message starts its workers
            names = set(tts_bot.supervisor.status())
            self.assertIn("tts-generation:1", names)
            self.assertIn("tts-playback:1", names)
        finally:
            await tts_bot.supervisor.stop_all()

    async def test_cloud_voices_stream_whatever_the_old_switch_says(self):
        with patch.dict("os.environ", {"TTS_CONTINUOUS_STREAM": "0"}):
            bot_mod = load_bot_module()
        tts_bot = bot_mod.TTSBot()
        tts_bot.tts_dispatcher.register("fish", MagicMock())
        fish = voice_registry.VoiceRecord(
            name="f", label="F", description="", provider=voice_registry.PROVIDER_FISH,
            fish=voice_registry.FishParams(reference_id="r"),
        )
        self.assertTrue(tts_bot._should_attempt_stream(fish))

    def test_the_legacy_entry_points_are_gone(self):
        bot_mod = load_bot_module()
        for name in ("tts_worker", "generate_tts_file", "play_file", "prepare_playback_file"):
            self.assertFalse(hasattr(bot_mod.TTSBot, name), name)


if __name__ == "__main__":
    unittest.main()
