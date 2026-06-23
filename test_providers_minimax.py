"""Unit tests for MiniMaxProvider using httpx.MockTransport.

These tests never touch the network — every HTTP call is intercepted
by a MockTransport so we can exercise success, quota, auth, timeout,
and network-failure paths deterministically.
"""

from __future__ import annotations

import asyncio
import json
import unittest
from pathlib import Path

import httpx

from tts_providers import (
    MiniMaxAuthError,
    MiniMaxConfig,
    MiniMaxError,
    MiniMaxProvider,
    MiniMaxQuotaError,
    MiniMaxTimeoutError,
)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _mp3_hex_response(text_chars: int = 13) -> httpx.Response:
    """Build a fake MiniMax success response with a tiny MP3 in hex.

    The bytes don't have to be a valid MP3 — the provider only checks
    the hex decodes and the file is written. The downstream ffmpeg
    call is not exercised in these unit tests.
    """
    payload_bytes = b"\x00\x01\x02\x03\x04"  # 5 arbitrary bytes
    body = {
        "data": {
            "audio": payload_bytes.hex(),
            "status": 2,
        },
        "extra_info": {
            "audio_length": 12345,
            "audio_sample_rate": 32000,
            "audio_size": len(payload_bytes),
            "bitrate": 128000,
            "word_count": text_chars,
            "usage_characters": text_chars,
            "audio_format": "mp3",
        },
        "trace_id": "test-trace-id",
        "base_resp": {"status_code": 0, "status_msg": "success"},
    }
    return httpx.Response(200, json=body)


def _make_provider(handler, *, config=None) -> MiniMaxProvider:
    """Build a provider that uses a MockTransport with ``handler``."""
    cfg = config or MiniMaxConfig(
        api_key="test-key",
        voice_id="test-voice",
        base_url="https://api.minimax.io",
        timeout_seconds=2.5,
    )
    transport = httpx.MockTransport(handler)
    # We pass our own client so the provider does not own it (tests
    # can reuse a single client across multiple synthesizes).
    client = httpx.AsyncClient(
        transport=transport,
        timeout=httpx.Timeout(cfg.timeout_seconds),
    )
    return MiniMaxProvider(cfg, http_client=client)


class MiniMaxProviderSuccessTests(unittest.TestCase):
    def test_successful_synthesis_writes_decoded_bytes_to_file(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["method"] = request.method
            captured["url"] = str(request.url)
            captured["auth"] = request.headers.get("Authorization")
            captured["body"] = json.loads(request.content.decode("utf-8"))
            return _mp3_hex_response()

        provider = _make_provider(handler)
        target = Path("/tmp/minimax_success_test.mp3")
        try:
            _run(provider.synthesize("hello world", target))
            self.assertEqual(captured["method"], "POST")
            self.assertEqual(captured["url"], "https://api.minimax.io/v1/t2a_v2")
            self.assertEqual(captured["auth"], "Bearer test-key")
            self.assertEqual(captured["body"]["model"], "speech-2.8-turbo")
            self.assertEqual(captured["body"]["text"], "hello world")
            self.assertEqual(captured["body"]["output_format"], "hex")
            self.assertEqual(captured["body"]["voice_setting"]["voice_id"], "test-voice")
            self.assertEqual(captured["body"]["audio_setting"]["format"], "mp3")
            # File written with decoded bytes
            self.assertTrue(target.exists())
            self.assertEqual(target.read_bytes(), b"\x00\x01\x02\x03\x04")
        finally:
            if target.exists():
                target.unlink()

    def test_group_id_added_to_query_when_configured(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            return _mp3_hex_response()

        cfg = MiniMaxConfig(
            api_key="k", voice_id="v",
            base_url="https://api.minimax.io",
            group_id="G123",
        )
        provider = _make_provider(handler, config=cfg)
        try:
            _run(provider.synthesize("hi", Path("/tmp/g.mp3")))
            self.assertIn("GroupId=G123", captured["url"])
        finally:
            p = Path("/tmp/g.mp3")
            if p.exists():
                p.unlink()


class MiniMaxProviderErrorTests(unittest.TestCase):
    def test_http_429_raises_quota_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, text="rate limited")

        provider = _make_provider(handler)
        with self.assertRaises(MiniMaxQuotaError):
            _run(provider.synthesize("x", Path("/tmp/x.mp3")))

    def test_http_500_raises_generic_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="boom")

        provider = _make_provider(handler)
        with self.assertRaises(MiniMaxError) as ctx:
            _run(provider.synthesize("x", Path("/tmp/x.mp3")))
        # Not an auth/quota classification
        self.assertNotIsInstance(ctx.exception, MiniMaxAuthError)
        self.assertNotIsInstance(ctx.exception, MiniMaxQuotaError)

    def test_status_code_nonzero_invalid_api_key_raises_auth_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            body = {"base_resp": {"status_code": 1002, "status_msg": "invalid api key"}}
            return httpx.Response(200, json=body)

        provider = _make_provider(handler)
        with self.assertRaises(MiniMaxAuthError):
            _run(provider.synthesize("x", Path("/tmp/x.mp3")))

    def test_status_code_nonzero_other_raises_generic_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            body = {"base_resp": {"status_code": 1, "status_msg": "internal error"}}
            return httpx.Response(200, json=body)

        provider = _make_provider(handler)
        with self.assertRaises(MiniMaxError) as ctx:
            _run(provider.synthesize("x", Path("/tmp/x.mp3")))
        self.assertNotIsInstance(ctx.exception, MiniMaxAuthError)

    def test_missing_audio_field_raises_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            body = {"base_resp": {"status_code": 0}, "data": {}}
            return httpx.Response(200, json=body)

        provider = _make_provider(handler)
        with self.assertRaises(MiniMaxError):
            _run(provider.synthesize("x", Path("/tmp/x.mp3")))

    def test_invalid_hex_payload_raises_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            body = {"base_resp": {"status_code": 0}, "data": {"audio": "not-hex-zzz"}}
            return httpx.Response(200, json=body)

        provider = _make_provider(handler)
        with self.assertRaises(MiniMaxError):
            _run(provider.synthesize("x", Path("/tmp/x.mp3")))

    def test_missing_api_key_raises_auth_error_without_http_call(self):
        cfg = MiniMaxConfig(api_key="", voice_id="v")
        # No handler needed — the provider must short-circuit before HTTP
        provider = _make_provider(lambda r: httpx.Response(200), config=cfg)
        with self.assertRaises(MiniMaxAuthError):
            _run(provider.synthesize("x", Path("/tmp/x.mp3")))

    def test_missing_voice_id_raises_auth_error_without_http_call(self):
        cfg = MiniMaxConfig(api_key="k", voice_id="")
        provider = _make_provider(lambda r: httpx.Response(200), config=cfg)
        with self.assertRaises(MiniMaxAuthError):
            _run(provider.synthesize("x", Path("/tmp/x.mp3")))

    def test_timeout_raises_timeout_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("simulated")

        cfg = MiniMaxConfig(api_key="k", voice_id="v", timeout_seconds=0.5)
        provider = _make_provider(handler, config=cfg)
        with self.assertRaises(MiniMaxTimeoutError):
            _run(provider.synthesize("x", Path("/tmp/x.mp3")))

    def test_network_error_raises_generic_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("simulated connect failure")

        provider = _make_provider(handler)
        # ConnectError is a subclass of HTTPError, so the generic
        # branch should fire (not the timeout branch).
        with self.assertRaises(MiniMaxError) as ctx:
            _run(provider.synthesize("x", Path("/tmp/x.mp3")))
        self.assertNotIsInstance(ctx.exception, MiniMaxTimeoutError)

    def test_non_json_response_raises_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="<html>not json</html>")

        provider = _make_provider(handler)
        with self.assertRaises(MiniMaxError):
            _run(provider.synthesize("x", Path("/tmp/x.mp3")))


class MiniMaxKeepAliveTests(unittest.TestCase):
    """Validate the keep-alive invariant: one client is reused across
    many requests, not recreated per call."""

    def test_single_client_used_across_calls(self):
        cfg = MiniMaxConfig(api_key="k", voice_id="v")
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: _mp3_hex_response()),
            timeout=httpx.Timeout(cfg.timeout_seconds),
        )
        provider = MiniMaxProvider(cfg, http_client=client)

        for _ in range(5):
            _run(provider.synthesize("x", Path("/tmp/k.mp3")))
        try:
            # We did not own the client, so aclose() is a no-op for us
            # but the client must still be the same instance we passed.
            _run(provider.aclose())
        finally:
            p = Path("/tmp/k.mp3")
            if p.exists():
                p.unlink()
        # The provider did not replace the client.
        self.assertIs(provider._client, client)


if __name__ == "__main__":
    unittest.main()