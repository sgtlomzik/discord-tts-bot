"""Unit tests for voice expressiveness: per-voice tuning + auto-emotion.

Network-free. Covers the auto-emotion heuristic, its translation inside
_build_body, the cache-key fingerprint that keeps re-tuning from serving
stale audio, and the /voicebot voice-tune command surface.
"""

from __future__ import annotations

import importlib.util
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from tts_providers import (
    MiniMaxConfig,
    MiniMaxProvider,
    derive_auto_emotion,
    resolve_emotion,
    voice_cache_key,
)


def load_bot_module():
    module_path = Path(__file__).with_name("bot.py")
    spec = importlib.util.spec_from_file_location("tts_bot_module_expr", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class AutoEmotionTests(unittest.TestCase):
    def test_caps_is_angry(self):
        self.assertEqual(derive_auto_emotion("ПРЕКРАТИ"), "angry")

    def test_interrobang_is_surprised(self):
        self.assertEqual(derive_auto_emotion("что это?!"), "surprised")

    def test_exclamation_is_happy(self):
        self.assertEqual(derive_auto_emotion("ура победа!"), "happy")

    def test_plain_is_neutral(self):
        self.assertEqual(derive_auto_emotion("просто текст"), "")

    def test_empty(self):
        self.assertEqual(derive_auto_emotion(""), "")

    def test_short_caps_not_angry(self):
        # fewer than 4 letters shouldn't trip the shout heuristic
        self.assertEqual(derive_auto_emotion("ОК"), "")


class ResolveEmotionTests(unittest.TestCase):
    def test_auto_resolves_from_text(self):
        self.assertEqual(resolve_emotion("auto", "ура!"), "happy")

    def test_fixed_passthrough(self):
        self.assertEqual(resolve_emotion("sad", "ура!"), "sad")

    def test_empty_passthrough(self):
        self.assertEqual(resolve_emotion("", "ура!"), "")


class BuildBodyEmotionTests(unittest.TestCase):
    def _provider(self):
        cfg = MiniMaxConfig(
            api_key="k", base_url="https://api.minimax.io", timeout_seconds=2.5
        )
        return MiniMaxProvider(cfg)

    def test_auto_translated_in_body(self):
        body = self._provider()._build_body(
            "ура победа!",
            voice_id="v12345678",
            model="speech-2.8-hd",
            speed=1.0,
            vol=1.0,
            pitch=0,
            emotion="auto",
            language_boost="Russian",
        )
        self.assertEqual(body["voice_setting"]["emotion"], "happy")

    def test_auto_neutral_omits_emotion(self):
        body = self._provider()._build_body(
            "просто текст",
            voice_id="v12345678",
            model="speech-2.8-hd",
            speed=1.0,
            vol=1.0,
            pitch=0,
            emotion="auto",
            language_boost="Russian",
        )
        self.assertNotIn("emotion", body["voice_setting"])


class VoiceCacheKeyTests(unittest.TestCase):
    def _mm_voice(self, **overrides):
        params = dict(speed=1.0, vol=1.0, pitch=0, emotion="", model="speech-2.8-hd")
        params.update(overrides)
        return types.SimpleNamespace(
            name="bussshy", provider="minimax", minimax=types.SimpleNamespace(**params)
        )

    def test_none_voice(self):
        self.assertEqual(voice_cache_key(None), "")

    def test_piper_is_just_name(self):
        v = types.SimpleNamespace(name="piper-ruslan", provider="piper", minimax=None)
        self.assertEqual(voice_cache_key(v), "piper-ruslan")

    def test_minimax_includes_params(self):
        self.assertNotEqual(
            voice_cache_key(self._mm_voice(emotion="happy")),
            voice_cache_key(self._mm_voice(emotion="sad")),
        )

    def test_minimax_speed_change_changes_key(self):
        self.assertNotEqual(
            voice_cache_key(self._mm_voice(speed=1.0)),
            voice_cache_key(self._mm_voice(speed=1.5)),
        )

    def test_minimax_same_params_stable(self):
        self.assertEqual(
            voice_cache_key(self._mm_voice()), voice_cache_key(self._mm_voice())
        )


class VoiceTuneCommandTests(unittest.IsolatedAsyncioTestCase):
    def _setup(self):
        bot_mod = load_bot_module()
        vr = bot_mod.voice_registry
        rec = vr.VoiceRecord(
            name="bussshy",
            label="Bussshy",
            description="",
            provider=vr.PROVIDER_MINIMAX,
            minimax=vr.MiniMaxParams(voice_id="bussshy01"),
        )
        bot_mod.bot.voice_registry.add(rec)
        bot_mod.bot.persist_voice_registry = MagicMock()
        return bot_mod

    def _interaction(self):
        sent = []
        interaction = types.SimpleNamespace(
            guild=types.SimpleNamespace(id=1),
            user=types.SimpleNamespace(id=2),
            response=types.SimpleNamespace(
                send_message=AsyncMock(side_effect=lambda *a, **k: sent.append(a[0] if a else "")),
            ),
        )
        return interaction, sent

    async def _call(self, bot_mod, *args, **kwargs):
        with patch.object(bot_mod, "is_guild_manager", MagicMock(return_value=True)), patch.object(
            bot_mod.discord, "Member", type(args[0].user)
        ):
            await bot_mod.slash_tts_voice_tune.callback(*args, **kwargs)

    async def test_sets_emotion_and_speed(self):
        bot_mod = self._setup()
        interaction, sent = self._interaction()
        await self._call(
            bot_mod,
            interaction,
            "bussshy",
            emotion=types.SimpleNamespace(value="happy"),
            speed=1.5,
        )
        mm = bot_mod.bot.voice_registry.get("bussshy").minimax
        self.assertEqual(mm.emotion, "happy")
        self.assertEqual(mm.speed, 1.5)
        self.assertEqual(mm.pitch, 0)  # untouched
        bot_mod.bot.persist_voice_registry.assert_called_once()

    async def test_emotion_none_clears(self):
        bot_mod = self._setup()
        # start from happy
        vr = bot_mod.voice_registry
        rec = bot_mod.bot.voice_registry.get("bussshy")
        bot_mod.bot.voice_registry.add(
            vr.VoiceRecord(
                name=rec.name, label=rec.label, description=rec.description,
                provider=rec.provider,
                minimax=vr.MiniMaxParams(voice_id="bussshy01", emotion="happy"),
            )
        )
        interaction, _ = self._interaction()
        await self._call(
            bot_mod, interaction, "bussshy",
            emotion=types.SimpleNamespace(value="none"),
        )
        self.assertEqual(bot_mod.bot.voice_registry.get("bussshy").minimax.emotion, "")

    async def test_auto_emotion_value_stored(self):
        bot_mod = self._setup()
        interaction, _ = self._interaction()
        await self._call(
            bot_mod, interaction, "bussshy",
            emotion=types.SimpleNamespace(value="auto"),
        )
        self.assertEqual(bot_mod.bot.voice_registry.get("bussshy").minimax.emotion, "auto")

    async def test_rejects_piper_voice(self):
        bot_mod = self._setup()
        interaction, sent = self._interaction()
        await self._call(bot_mod, interaction, "piper-ruslan", speed=1.2)
        self.assertIn("MiniMax", sent[0])

    async def test_requires_a_param(self):
        bot_mod = self._setup()
        interaction, sent = self._interaction()
        await self._call(bot_mod, interaction, "bussshy")
        self.assertIn("хотя бы один", sent[0])


if __name__ == "__main__":
    unittest.main()
