"""Tests for registry-driven dispatch (piper/minimax routing + fallback)."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from ttsbot.providers import DispatcherConfig, PrimaryProvider, TTSDispatcher
from ttsbot.voice_registry import MiniMaxParams, PiperParams, VoiceRecord


def _piper_record(name="piper-ruslan"):
    return VoiceRecord(
        name=name, label="R", description="", provider="piper",
        piper=PiperParams(model_path="/m.onnx", length_scale=0.9),
    )


def _minimax_record(name="bussshy01"):
    return VoiceRecord(
        name=name, label="B", description="", provider="minimax",
        minimax=MiniMaxParams(voice_id="bussshy01", model="speech-2.8-turbo",
                              speed=1.1, vol=1.0, pitch=2, emotion="happy",
                              language_boost="Russian"),
    )


def _dispatcher(*, cloud=True, primary=PrimaryProvider.MINIMAX, fallback="piper-ruslan"):
    local = AsyncMock()
    local.name = "local"
    cloud_mock = None
    if cloud:
        cloud_mock = AsyncMock()
        cloud_mock.name = "minimax"
    disp = TTSDispatcher(
        local=local,
        cloud=cloud_mock,
        config=DispatcherConfig(primary=primary),
        fallback_profile=fallback,
    )
    return disp, local, cloud_mock


class DispatcherRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_piper_record_routes_to_local(self):
        disp, local, cloud = _dispatcher()
        used = await disp.synthesize("привет", Path("/tmp/x.wav"), voice=_piper_record())
        self.assertEqual(used, "local")
        local.synthesize.assert_awaited_once_with("привет", Path("/tmp/x.wav"), "piper-ruslan")
        cloud.synthesize.assert_not_awaited()

    async def test_minimax_record_routes_to_cloud_with_params(self):
        disp, local, cloud = _dispatcher()
        used = await disp.synthesize("привет", Path("/tmp/x.mp3"), voice=_minimax_record())
        self.assertEqual(used, "minimax")
        cloud.synthesize.assert_awaited_once_with(
            "привет", Path("/tmp/x.mp3"),
            voice_id="bussshy01", model="speech-2.8-turbo",
            speed=1.1, vol=1.0, pitch=2, emotion="happy", language_boost="Russian",
        )
        local.synthesize.assert_not_awaited()

    async def test_cloud_failure_falls_back_to_fallback_profile(self):
        disp, local, cloud = _dispatcher()
        cloud.synthesize.side_effect = RuntimeError("boom")
        used = await disp.synthesize("привет", Path("/tmp/x.mp3"), voice=_minimax_record())
        self.assertEqual(used, "local")
        # fell back to the registry fallback piper profile
        local.synthesize.assert_awaited_once_with("привет", Path("/tmp/x.mp3"), "piper-ruslan")
        self.assertEqual(disp.circuit_breaker.consecutive_failures, 1)

    async def test_minimax_record_with_no_cloud_uses_fallback_piper(self):
        disp, local, _ = _dispatcher(cloud=False)
        used = await disp.synthesize("привет", Path("/tmp/x.mp3"), voice=_minimax_record())
        self.assertEqual(used, "local")
        local.synthesize.assert_awaited_once_with("привет", Path("/tmp/x.mp3"), "piper-ruslan")

    async def test_no_voice_with_minimax_primary_uses_cloud(self):
        # back-compat: voice=None + primary=minimax still hits the cloud
        disp, local, cloud = _dispatcher()
        used = await disp.synthesize("привет", Path("/tmp/x.mp3"), voice=None)
        self.assertEqual(used, "minimax")
        cloud.synthesize.assert_awaited_once_with("привет", Path("/tmp/x.mp3"))

    async def test_no_voice_with_local_primary_uses_local_default(self):
        disp, local, cloud = _dispatcher(primary=PrimaryProvider.LOCAL)
        used = await disp.synthesize("привет", Path("/tmp/x.wav"), voice=None)
        self.assertEqual(used, "local")
        local.synthesize.assert_awaited_once_with("привет", Path("/tmp/x.wav"), None)
        cloud.synthesize.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
