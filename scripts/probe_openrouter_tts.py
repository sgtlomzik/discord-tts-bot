#!/usr/bin/env python3
"""Probe an OpenRouter TTS model: streaming behaviour, audio format, latency, errors.

Answers the questions that decide how the bot integrates the model:
does the audio arrive progressively (time to first byte vs. total), what
format and sample rate come back, whether chunks can split a sample, what
``speed`` / ``instructions`` do, and what errors look like.

Run it inside the bot container so it uses the bot's network path:

    docker exec -i -e OPENROUTER_API_KEY=... discord_tts_bot \\
        python - --out /tmp/probe < scripts/probe_openrouter_tts.py

Every successful request is billed by OpenRouter (a few cents in total).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import time
import wave
from pathlib import Path

import httpx

TEXTS = {
    "short": "Кто со мной?",
    "medium": "Сегодня вечером собираемся в голосовом, начинаем в девять.",
    "long": (
        "Короче, я вчера наконец прошёл тот самый уровень, на котором застрял на неделю. "
        "Оказалось, нужно было вернуться и забрать ключ из сундука у входа. Классика."
    ),
}


def parse_rate(content_type: str) -> tuple[int, int]:
    params = dict(
        part.strip().split("=", 1) for part in content_type.split(";")[1:] if "=" in part
    )
    return int(params.get("rate", 24000)), int(params.get("channels", 1))


async def speak(client: httpx.AsyncClient, body: dict) -> dict:
    t0 = time.perf_counter()
    first = None
    sizes: list[int] = []
    audio = bytearray()
    async with client.stream("POST", "/audio/speech", json=body) as resp:
        head = time.perf_counter() - t0
        if resp.status_code != 200:
            return {
                "status": resp.status_code,
                "headers": {k: v for k, v in resp.headers.items()
                            if k.lower() in {"content-type", "retry-after"} or "ratelimit" in k.lower()},
                "body": (await resp.aread()).decode("utf-8", "replace")[:600],
            }
        async for chunk in resp.aiter_raw():
            if not chunk:
                continue
            if first is None:
                first = time.perf_counter() - t0
            sizes.append(len(chunk))
            audio += chunk
        headers = dict(resp.headers)
    total = time.perf_counter() - t0
    ctype = headers.get("content-type", "")
    rate, channels = parse_rate(ctype) if "pcm" in ctype else (0, 0)
    return {
        "status": 200, "headers_s": head, "ttfb": first, "total": total, "bytes": len(audio),
        "chunks": len(sizes), "odd_chunks": sum(s % 2 for s in sizes),
        "first_chunk": sizes[0] if sizes else 0, "median_chunk": statistics.median(sizes) if sizes else 0,
        "content_type": ctype, "rate": rate, "channels": channels,
        "audio_s": len(audio) / (2 * channels * rate) if rate else None,
        "other_headers": {k: v for k, v in headers.items() if k.lower().startswith(("x-", "or-", "openrouter"))},
        "_audio": bytes(audio),
    }


def save_wav(path: Path, pcm: bytes, rate: int, channels: int) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)


def show(tag: str, r: dict) -> None:
    print(tag, json.dumps({k: v for k, v in r.items() if not k.startswith("_")}, ensure_ascii=False), flush=True)


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--model", default="google/gemini-3.8-flash-lite-tts")
    p.add_argument("--voice", default="Kore")
    p.add_argument("--runs", type=int, default=5)
    p.add_argument("--out", default="/tmp/probe", help="directory for WAV samples")
    p.add_argument("--base-url", default="https://openrouter.ai/api/v1")
    args = p.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    key = os.environ["OPENROUTER_API_KEY"]
    headers = {"Authorization": f"Bearer {key}", "X-Title": "discord-tts-bot probe"}

    async with httpx.AsyncClient(base_url=args.base_url, headers=headers, timeout=60) as client:
        base = {"model": args.model, "voice": args.voice, "response_format": "pcm"}

        print("## warm-up + format", flush=True)
        r = await speak(client, {**base, "input": TEXTS["short"]})
        show("warmup", r)
        if r["status"] != 200:
            return

        print("## latency on a warm connection (ttfb close to total = buffered, not streamed)", flush=True)
        for name, text in TEXTS.items():
            ttfb, total = [], []
            for i in range(args.runs):
                r = await speak(client, {**base, "input": text})
                show(f"{name}#{i}", r)
                if r["status"] != 200:
                    break
                ttfb.append(r["ttfb"])
                total.append(r["total"])
                if i == 0:
                    save_wav(out / f"{name}_{args.voice}.wav", r["_audio"], r["rate"], r["channels"])
            if ttfb:
                print(f"== {name}: ttfb median {statistics.median(ttfb):.3f}s min {min(ttfb):.3f}s "
                      f"max {max(ttfb):.3f}s | total median {statistics.median(total):.3f}s", flush=True)

        print("## optional parameters", flush=True)
        for label, extra in {
            "speed=1.5": {"speed": 1.5},
            "instructions": {"instructions": "Говори очень радостно и взволнованно."},
            "voice=Puck": {"voice": "Puck"},
            "mp3": {"response_format": "mp3"},
        }.items():
            r = await speak(client, {**base, "input": TEXTS["medium"], **extra})
            show(label, r)
            if r["status"] == 200 and r["rate"]:
                save_wav(out / f"medium_{label.replace('=', '_')}.wav", r["_audio"], r["rate"], r["channels"])

        print("## edge inputs", flush=True)
        for label, text in {"one-word": "ну", "emoji-name": "смеющееся лицо", "latin": "gg wp"}.items():
            r = await speak(client, {**base, "input": text})
            show(label, r)

    print("## errors", flush=True)
    async with httpx.AsyncClient(base_url=args.base_url, timeout=30,
                                 headers={"Authorization": "Bearer sk-or-v1-invalid"}) as bad:
        show("bad-key", await speak(bad, {"model": args.model, "input": "тест", "voice": args.voice}))
    async with httpx.AsyncClient(base_url=args.base_url, headers=headers, timeout=30) as client:
        show("bad-voice", await speak(client, {"model": args.model, "input": "тест", "voice": "NoSuchVoice"}))
        show("empty-input", await speak(client, {"model": args.model, "input": "", "voice": args.voice}))


if __name__ == "__main__":
    asyncio.run(main())
