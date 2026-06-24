"""One-shot script to clone a voice on MiniMax and capture the voice_id.

Run once per voice, before switching the bot to primary=minimax. The
resulting voice_id goes into ``MINIMAX_VOICE_ID`` in the bot's ``.env``.

Usage (from the repo root, with the bot's venv active):

    python scripts/clone_voice.py scripts/voice_samples/bussshy.mp3 bussshy

The first argument is the path to a clean audio sample (10 seconds to
5 minutes, mp3/m4a/wav, up to 20 MB). The second argument is the
target voice_id you want to assign (a free-form string; MiniMax
preserves the clone for 7 days of first use, then it sticks as long
as you keep using it).

The script does three things:

1. Upload the sample to ``POST /v1/files/upload`` with
   ``purpose=voice_clone``; get back a ``file_id``.
2. Call ``POST /v1/voice_clone`` with that ``file_id``, your target
   ``voice_id``, and ``model=speech-2.8-hd`` for preview quality.
3. Run a single sanity check via ``POST /v1/t2a_v2`` with the new
   ``voice_id`` and a short Russian phrase, write the result to
   ``scripts/voice_samples/_preview_<voice_id>.mp3`` so you can
   listen to it and confirm the clone sounds right.

Requirements:
- ``MINIMAX_API_KEY`` set in the environment (Subscription Key).
- httpx installed (``pip install httpx``).
- A clean voice sample (no music, no background noise).
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import httpx


BASE_URL = "https://api.minimax.io"
UPLOAD_URL = f"{BASE_URL}/v1/files/upload"
CLONE_URL = f"{BASE_URL}/v1/voice_clone"
T2A_URL = f"{BASE_URL}/v1/t2a_v2"

# 5..20 MB chunked upload, 60-second ceiling for the whole pipeline.
HTTP_TIMEOUT = httpx.Timeout(60.0, connect=10.0)
PREVIEW_PHRASE = "Привет, это проверка клонированного голоса."


def _headers(api_key: str, *, with_content_type: bool = True) -> dict:
    h = {"Authorization": f"Bearer {api_key}"}
    if with_content_type:
        h["Content-Type"] = "application/json"
    return h


def upload_sample(client: httpx.Client, api_key: str, sample: Path) -> str:
    """Upload the audio file and return the ``file_id``."""
    if not sample.exists():
        raise SystemExit(f"Sample file not found: {sample}")
    size_mb = sample.stat().st_size / (1024 * 1024)
    if size_mb > 20:
        raise SystemExit(
            f"Sample is {size_mb:.1f} MB; MiniMax limit is 20 MB."
        )
    with sample.open("rb") as f:
        files = {"file": (sample.name, f, "application/octet-stream")}
        data = {"purpose": "voice_clone"}
        response = client.post(
            UPLOAD_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            files=files,
            data=data,
            timeout=HTTP_TIMEOUT,
        )
    if response.status_code >= 400:
        raise SystemExit(
            f"Upload failed: HTTP {response.status_code} {response.text[:300]}"
        )
    payload = response.json()
    file_id = payload.get("file", {}).get("file_id") or payload.get("file_id")
    if not file_id:
        raise SystemExit(f"Upload response missing file_id: {payload}")
    print(f"[1/3] uploaded sample: file_id={file_id}")
    return str(file_id)


def clone_voice(client: httpx.Client, api_key: str, file_id: str, voice_id: str) -> dict:
    """Trigger the clone and return the raw response payload."""
    body = {
        "file_id": int(file_id),  # MiniMax voice_clone rejects a string file_id (2013 invalid params)
        "voice_id": voice_id,
        "model": "speech-2.8-hd",  # preview-quality clone
    }
    response = client.post(
        CLONE_URL,
        headers=_headers(api_key),
        json=body,
        timeout=HTTP_TIMEOUT,
    )
    if response.status_code >= 400:
        raise SystemExit(
            f"Clone failed: HTTP {response.status_code} {response.text[:300]}"
        )
    payload = response.json()
    base_resp = payload.get("base_resp") or {}
    if base_resp.get("status_code", -1) != 0:
        raise SystemExit(f"Clone API returned error: {base_resp}")
    print(
        f"[2/3] clone triggered: voice_id={voice_id} "
        f"(demo_version={payload.get('demo_version')!r})"
    )
    return payload


def sanity_check(
    client: httpx.Client,
    api_key: str,
    voice_id: str,
    out_path: Path,
) -> int:
    """Synthesize a short phrase with the new voice to confirm it works.

    Returns the byte size of the saved MP3. Raises SystemExit on any
    failure so the operator sees a clear error rather than a silent
    "all good" that hides an unusable voice.
    """
    body = {
        "model": "speech-2.8-turbo",
        "text": PREVIEW_PHRASE,
        "stream": False,
        "language_boost": "Russian",
        "output_format": "hex",
        "voice_setting": {"voice_id": voice_id, "speed": 1, "vol": 1, "pitch": 0},
        "audio_setting": {
            "sample_rate": 32000,
            "bitrate": 128000,
            "format": "mp3",
            "channel": 1,
        },
    }
    response = client.post(
        T2A_URL,
        headers=_headers(api_key),
        json=body,
        timeout=HTTP_TIMEOUT,
    )
    if response.status_code >= 400:
        raise SystemExit(
            f"Sanity-check synthesis failed: HTTP {response.status_code} "
            f"{response.text[:300]}"
        )
    payload = response.json()
    base_resp = payload.get("base_resp") or {}
    if base_resp.get("status_code", -1) != 0:
        raise SystemExit(
            f"Sanity-check synthesis API returned error: {base_resp}"
        )
    audio_hex = (payload.get("data") or {}).get("audio")
    if not audio_hex:
        raise SystemExit("Sanity-check response missing data.audio")
    out_path.write_bytes(bytes.fromhex(audio_hex))
    print(f"[3/3] preview saved: {out_path} ({out_path.stat().st_size} bytes)")
    return out_path.stat().st_size


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "sample",
        type=Path,
        help="Path to the audio sample (mp3/m4a/wav, 10s-5min, <=20MB).",
    )
    parser.add_argument(
        "voice_id",
        help="Target voice_id to assign (free-form string).",
    )
    args = parser.parse_args(argv)

    api_key = os.getenv("MINIMAX_API_KEY", "").strip()
    if not api_key:
        raise SystemExit(
            "MINIMAX_API_KEY is not set. Export it first:\n"
            "  export MINIMAX_API_KEY=sk-cp-..."
        )

    with httpx.Client(timeout=HTTP_TIMEOUT) as client:
        file_id = upload_sample(client, api_key, args.sample)
        clone_voice(client, api_key, file_id, args.voice_id)
        preview_path = args.sample.parent / f"_preview_{args.voice_id}.mp3"
        sanity_check(client, api_key, args.voice_id, preview_path)

    print("")
    print("All three steps passed.")
    print(f"Add to your .env:")
    print(f"  MINIMAX_VOICE_ID={args.voice_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())