"""Unit tests for MiniMaxProvider.clone_voice using httpx.MockTransport.

Exercises the two-call upload+clone flow without touching the network:
- happy path: upload returns a file_id, voice_clone returns status 0;
- the file_id is sent to /v1/voice_clone as an int (a string -> 2013);
- a 2054 on the clone surfaces as MiniMaxVoiceNotFoundError;
- an oversize sample is rejected before any HTTP call.
"""

from __future__ import annotations

import asyncio
import json
import unittest

import httpx

from ttsbot.providers import (
    MiniMaxConfig,
    MiniMaxError,
    MiniMaxProvider,
    MiniMaxVoiceNotFoundError,
)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _make_provider(handler, *, config=None) -> MiniMaxProvider:
    cfg = config or MiniMaxConfig(
        api_key="test-key",
        base_url="https://api.minimax.io",
        timeout_seconds=2.5,
    )
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(
        transport=transport, timeout=httpx.Timeout(cfg.timeout_seconds)
    )
    return MiniMaxProvider(cfg, http_client=client)


class CloneVoiceTests(unittest.TestCase):
    def test_upload_then_clone_sends_int_file_id(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path == "/v1/files/upload":
                captured["upload_auth"] = request.headers.get("Authorization")
                # multipart body carries the purpose field
                captured["upload_ct"] = request.headers.get("Content-Type", "")
                return httpx.Response(
                    200,
                    json={
                        "file": {"file_id": 998877},
                        "base_resp": {"status_code": 0, "status_msg": "success"},
                    },
                )
            if path == "/v1/voice_clone":
                captured["clone_body"] = json.loads(request.content.decode())
                return httpx.Response(
                    200, json={"base_resp": {"status_code": 0, "status_msg": "ok"}}
                )
            raise AssertionError(f"unexpected path {path}")

        provider = _make_provider(handler)
        _run(provider.clone_voice(b"fake-audio-bytes", voice_id="serega1234"))
        self.assertEqual(captured["upload_auth"], "Bearer test-key")
        self.assertIn("multipart/form-data", captured["upload_ct"])
        body = captured["clone_body"]
        self.assertEqual(body["voice_id"], "serega1234")
        self.assertEqual(body["file_id"], 998877)
        self.assertIsInstance(body["file_id"], int)  # string -> 2013
        self.assertEqual(body["model"], "speech-2.8-hd")

    def test_clone_voice_not_found_raises(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v1/files/upload":
                return httpx.Response(
                    200,
                    json={
                        "file": {"file_id": 1},
                        "base_resp": {"status_code": 0, "status_msg": "ok"},
                    },
                )
            return httpx.Response(
                200,
                json={"base_resp": {"status_code": 2054, "status_msg": "voice id not exist"}},
            )

        provider = _make_provider(handler)
        with self.assertRaises(MiniMaxVoiceNotFoundError):
            _run(provider.clone_voice(b"x", voice_id="bad12345"))

    def test_upload_missing_file_id_raises(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, json={"base_resp": {"status_code": 0, "status_msg": "ok"}}
            )

        provider = _make_provider(handler)
        with self.assertRaises(MiniMaxError):
            _run(provider.clone_voice(b"x", voice_id="abc12345"))

    def test_oversize_sample_rejected_without_http(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("must not hit the network for an oversize sample")

        provider = _make_provider(handler)
        with self.assertRaises(MiniMaxError):
            _run(provider.clone_voice(b"x" * (21 * 1024 * 1024), voice_id="abc12345"))


if __name__ == "__main__":
    unittest.main()
