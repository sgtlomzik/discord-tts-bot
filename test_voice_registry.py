"""Unit tests for the unified voice registry (catalog) — no net/Discord."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import voice_registry as vr


def _piper_profiles() -> dict:
    return {
        "piper-ruslan": SimpleNamespace(
            name="piper-ruslan",
            label="Piper Ruslan",
            piper_model_path="/app/models/ru_RU-ruslan-medium.onnx",
            piper_config_path="/app/models/ru_RU-ruslan-medium.onnx.json",
            piper_speaker=-1,
            piper_length_scale=0.9,
        ),
        "piper-irina": SimpleNamespace(
            name="piper-irina",
            label="Piper Irina",
            piper_model_path="/app/models/ru_RU-irina-medium.onnx",
            piper_config_path="/app/models/ru_RU-irina-medium.onnx.json",
            piper_speaker=-1,
            piper_length_scale=1.0,
        ),
    }


class VoiceRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_valid_voice_name(self):
        self.assertTrue(vr.valid_voice_name("bussshy01"))
        self.assertTrue(vr.valid_voice_name("mm-qingse"))
        self.assertFalse(vr.valid_voice_name("Bad_Name"))
        self.assertFalse(vr.valid_voice_name("-leading"))
        self.assertFalse(vr.valid_voice_name(""))

    def test_seed_includes_piper_and_minimax(self):
        reg = vr.seed_registry(
            _piper_profiles(),
            fallback_profile="piper-ruslan",
            minimax_voice_id="bussshy01",
        )
        self.assertEqual(set(reg.names()), {"piper-ruslan", "piper-irina", "bussshy01"})
        self.assertEqual(reg.fallback_profile, "piper-ruslan")

        ruslan = reg.get("piper-ruslan")
        self.assertTrue(ruslan.is_piper)
        self.assertAlmostEqual(ruslan.piper.length_scale, 0.9)

        clone = reg.get("bussshy01")
        self.assertTrue(clone.is_minimax)
        self.assertEqual(clone.minimax.voice_id, "bussshy01")
        self.assertEqual(clone.minimax.model, "speech-2.8-turbo")
        self.assertEqual(clone.minimax.language_boost, "Russian")

    def test_seed_without_minimax(self):
        reg = vr.seed_registry(_piper_profiles(), fallback_profile="piper-ruslan")
        self.assertEqual(set(reg.names()), {"piper-ruslan", "piper-irina"})
        self.assertTrue(all(r.is_piper for r in reg.voices.values()))

    def test_seed_fallback_falls_back_to_first_when_missing(self):
        reg = vr.seed_registry(_piper_profiles(), fallback_profile="does-not-exist")
        self.assertIn(reg.fallback_profile, reg.voices)

    def test_save_load_roundtrip(self):
        path = self.tmp / "voices.json"
        reg = vr.seed_registry(
            _piper_profiles(), fallback_profile="piper-ruslan", minimax_voice_id="bussshy01"
        )
        vr.save_registry(path, reg)
        self.assertTrue(path.exists())

        loaded = vr.load_registry(path)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.fallback_profile, "piper-ruslan")
        self.assertEqual(set(loaded.names()), set(reg.names()))
        self.assertEqual(loaded.get("bussshy01").minimax.voice_id, "bussshy01")
        self.assertAlmostEqual(loaded.get("piper-ruslan").piper.length_scale, 0.9)

    def test_load_missing_returns_none(self):
        self.assertIsNone(vr.load_registry(self.tmp / "nope.json"))

    def test_load_or_seed_creates_file(self):
        path = self.tmp / "voices.json"
        self.assertFalse(path.exists())
        reg = vr.load_or_seed(
            path, _piper_profiles(), fallback_profile="piper-ruslan", minimax_voice_id="bussshy01"
        )
        self.assertTrue(path.exists())
        self.assertIn("bussshy01", reg)

        reg2 = vr.load_or_seed(path, _piper_profiles(), fallback_profile="piper-ruslan")
        self.assertEqual(set(reg2.names()), set(reg.names()))

    def test_partial_minimax_block_uses_defaults(self):
        path = self.tmp / "voices.json"
        path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "fallback_profile": "piper-ruslan",
                    "voices": {
                        "piper-ruslan": {
                            "label": "R",
                            "description": "",
                            "provider": "piper",
                            "piper": {"model_path": "/m.onnx"},
                        },
                        "mm-x": {
                            "label": "X",
                            "description": "",
                            "provider": "minimax",
                            "minimax": {"voice_id": "male-qn-qingse"},
                        },
                    },
                }
            ),
            encoding="utf-8",
        )
        reg = vr.load_registry(path)
        mm = reg.get("mm-x")
        self.assertEqual(mm.minimax.voice_id, "male-qn-qingse")
        self.assertEqual(mm.minimax.speed, 1.0)
        self.assertEqual(mm.minimax.pitch, 0)
        self.assertEqual(reg.get("piper-ruslan").piper.config_path, "")

    def test_minimax_without_voice_id_is_skipped(self):
        path = self.tmp / "voices.json"
        path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "fallback_profile": "p",
                    "voices": {
                        "p": {"label": "P", "description": "", "provider": "piper", "piper": {}},
                        "broken": {"label": "B", "description": "", "provider": "minimax", "minimax": {}},
                        "unknown": {"label": "U", "description": "", "provider": "espeak"},
                    },
                }
            ),
            encoding="utf-8",
        )
        reg = vr.load_registry(path)
        self.assertEqual(set(reg.names()), {"p"})


if __name__ == "__main__":
    unittest.main()
