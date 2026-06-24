"""Unit tests for MiniMaxProvider.stream_audio (SSE) using MockTransport."""

from __future__ import annotations

import asyncio
import json
import unittest

import httpx

from tts_providers import (
    MiniMaxConfig,
    MiniMaxProvider,
    MiniMaxVoiceNotFoundError,
)


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        # Clean up any async generators / pending stream-close tasks so the
        # short-lived loop does not emit "Task was destroyed" warnings.
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.close()


def _sse(events: list[dict]) -> bytes:
    """Render a list of payload dicts as an SSE body (data: ... \\n\\n)."""
    return ("".join(f"data: {json.dumps(e)}\n\n" for e in events)).encode("utf-8")


def _provider(handler, *, config=None) -> MiniMaxProvider:
    cfg = config or MiniMaxConfig(
        api_key="k", voice_id="cfg-voice", base_url="https://api.minimax.io",
        timeout_seconds=2.5,
    )
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        timeout=httpx.Timeout(cfg.timeout_seconds),
    )
    return MiniMaxProvider(cfg, http_client=client)


async def _collect(provider, **kwargs) -> list[bytes]:
    out: list[bytes] = []
    async for chunk in provider.stream_audio("привет", **kwargs):
        out.append(chunk)
    return out


class StreamAudioTests(unittest.TestCase):
    def test_yields_decoded_chunks_and_skips_final_aggregate(self):
        captured = {}
        a = b"\xaa\xbb"
        b = b"\xcc\xdd\xee"

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content.decode("utf-8"))
            body = _sse([
                {"data": {"audio": a.hex(), "status": 1}, "base_resp": {"status_code": 0}},
                {"data": {"audio": b.hex(), "status": 1}, "base_resp": {"status_code": 0}},
                # final event: no audio (exclude_aggregated_audio), carries usage
                {"data": {"status": 2}, "extra_info": {"usage_characters": 6},
                 "base_resp": {"status_code": 0}},
            ])
            return httpx.Response(200, content=body)

        provider = _provider(handler)
        chunks = _run(_collect(provider, voice_id="bussshy01"))

        self.assertEqual(chunks, [a, b])  # two chunks, final aggregate not double-counted
        # request asked to exclude the aggregated final audio + stream=true
        self.assertTrue(captured["body"]["stream"])
        self.assertEqual(
            captured["body"]["stream_options"]["exclude_aggregated_audio"], True
        )
        self.assertEqual(captured["body"]["voice_setting"]["voice_id"], "bussshy01")
        # usage accounted from the final event
        self.assertEqual(provider._session_chars, 6)

    def test_error_event_before_first_chunk_raises(self):
        def handler(request: httpx.Request) -> httpx.Response:
            body = _sse([
                {"base_resp": {"status_code": 2054, "status_msg": "voice id not exist"}},
            ])
            return httpx.Response(200, content=body)

        provider = _provider(handler)
        with self.assertRaises(MiniMaxVoiceNotFoundError):
            _run(_collect(provider, voice_id="ghost"))

    def test_http_error_status_raises_before_streaming(self):
        from tts_providers import MiniMaxError

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, content=b"upstream boom")

        provider = _provider(handler)
        with self.assertRaises(MiniMaxError):
            _run(_collect(provider, voice_id="v"))

    def test_malformed_data_lines_are_skipped(self):
        good = b"\x01\x02"

        def handler(request: httpx.Request) -> httpx.Response:
            raw = (
                b"data: not-json\n\n"
                b": this is an SSE comment\n\n"
                + _sse([
                    {"data": {"audio": good.hex(), "status": 1}, "base_resp": {"status_code": 0}},
                    {"data": {"status": 2}, "extra_info": {"usage_characters": 2},
                     "base_resp": {"status_code": 0}},
                ])
            )
            return httpx.Response(200, content=raw)

        provider = _provider(handler)
        chunks = _run(_collect(provider, voice_id="v"))
        self.assertEqual(chunks, [good])


if __name__ == "__main__":
    unittest.main()
