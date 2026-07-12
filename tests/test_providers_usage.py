"""Tests for the §3 fix: MiniMaxProvider must log usage_characters
on every successful synthesis and track a session-level cumulative
counter that survives across calls (resets only on process restart).
"""

from __future__ import annotations

import unittest
from pathlib import Path

import httpx

from ttsbot.providers import (
    MiniMaxConfig,
    MiniMaxProvider,
)


def _mp3_response(usage: int) -> httpx.Response:
    payload_bytes = b"\x00\x01\x02\x03\x04"
    body = {
        "data": {"audio": payload_bytes.hex(), "status": 2},
        "extra_info": {
            "audio_length": 12345,
            "audio_sample_rate": 32000,
            "audio_size": len(payload_bytes),
            "bitrate": 128000,
            "word_count": 5,
            "usage_characters": usage,
            "audio_format": "mp3",
        },
        "trace_id": "trace-id",
        "base_resp": {"status_code": 0, "status_msg": "success"},
    }
    return httpx.Response(200, json=body)


def _make_provider(handler, *, config=None) -> MiniMaxProvider:
    cfg = config or MiniMaxConfig(
        api_key="k", voice_id="v", base_url="https://api.minimax.io",
        timeout_seconds=2.5,
    )
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport, timeout=httpx.Timeout(2.5))
    return MiniMaxProvider(cfg, http_client=client)


def _run(coro):
    import asyncio
    return asyncio.new_event_loop().run_until_complete(coro)


class MiniMaxUsageLoggingTests(unittest.TestCase):
    def test_session_counter_starts_at_zero(self):
        provider = _make_provider(lambda r: _mp3_response(usage=10))
        self.assertEqual(provider._session_chars, 0)

    def test_session_counter_increments_per_request(self):
        usages = [10, 25, 7]

        def handler(request: httpx.Request) -> httpx.Response:
            return _mp3_response(usage=usages.pop(0))

        provider = _make_provider(handler)
        target = Path("/tmp/usage1.mp3")
        try:
            _run(provider.synthesize("a", target))
            self.assertEqual(provider._session_chars, 10)
            _run(provider.synthesize("bb", Path("/tmp/usage2.mp3")))
            self.assertEqual(provider._session_chars, 35)
            _run(provider.synthesize("ccc", Path("/tmp/usage3.mp3")))
            self.assertEqual(provider._session_chars, 42)
        finally:
            for n in ("usage1", "usage2", "usage3"):
                p = Path(f"/tmp/{n}.mp3")
                if p.exists():
                    p.unlink()

    def test_missing_usage_characters_does_not_crash(self):
        """If extra_info is missing or usage_characters is absent, the
        provider must still succeed; the counter simply doesn't grow.
        This protects operators from quota-tracking bugs breaking
        playback."""

        def handler(request: httpx.Request) -> httpx.Response:
            body = {
                "data": {"audio": b"\xff\xfb\x90\x00".hex(), "status": 2},
                "extra_info": {"audio_format": "mp3"},  # no usage_characters
                "base_resp": {"status_code": 0, "status_msg": "success"},
            }
            return httpx.Response(200, json=body)

        provider = _make_provider(handler)
        target = Path("/tmp/usage_no_field.mp3")
        try:
            _run(provider.synthesize("hello", target))
            self.assertEqual(provider._session_chars, 0)
        finally:
            if target.exists():
                target.unlink()

    def test_non_numeric_usage_characters_does_not_crash(self):
        def handler(request: httpx.Request) -> httpx.Response:
            body = {
                "data": {"audio": b"\xff\xfb\x90\x00".hex(), "status": 2},
                "extra_info": {"usage_characters": "not-a-number", "audio_format": "mp3"},
                "base_resp": {"status_code": 0, "status_msg": "success"},
            }
            return httpx.Response(200, json=body)

        provider = _make_provider(handler)
        target = Path("/tmp/usage_garbage.mp3")
        try:
            _run(provider.synthesize("hello", target))
            self.assertEqual(provider._session_chars, 0)
        finally:
            if target.exists():
                target.unlink()

    def test_session_counter_persists_across_calls(self):
        """After two successful calls, _session_chars equals the sum —
        the counter does not get reset between requests."""
        sequence = [3, 8]

        def handler(request: httpx.Request) -> httpx.Response:
            return _mp3_response(usage=sequence.pop(0))

        provider = _make_provider(handler)
        _run(provider.synthesize("a", Path("/tmp/persist1.mp3")))
        _run(provider.synthesize("bb", Path("/tmp/persist2.mp3")))
        self.assertEqual(provider._session_chars, 11)
        for n in ("persist1", "persist2"):
            p = Path(f"/tmp/{n}.mp3")
            if p.exists():
                p.unlink()


if __name__ == "__main__":
    unittest.main()