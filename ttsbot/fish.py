"""Fish Audio HTTP TTS with a shared keep-alive client and in-flight dedup."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from mimetypes import guess_type
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator

import httpx

log = logging.getLogger("tts_bot")


@dataclass(frozen=True)
class FishConfig:
    api_key: str
    reference_id: str = ""
    model: str = "s2.1-pro-free"
    format: str = "opus"
    latency: str = "low"
    chunk_length: int = 150
    opus_bitrate: int = 48000
    sample_rate: int = 48000
    normalize: bool = True
    base_url: str = "https://api.fish.audio"

    @classmethod
    def from_env(cls) -> "FishConfig":
        cfg = cls(
            api_key=os.getenv("FISH_API_KEY", "").strip(),
            reference_id=os.getenv("FISH_REFERENCE_ID", "").strip(),
            model=os.getenv("FISH_MODEL", "s2.1-pro-free").strip(),
            format=os.getenv("FISH_FORMAT", "opus").strip().lower(),
            latency=os.getenv("FISH_LATENCY", "low").strip().lower(),
            chunk_length=int(os.getenv("FISH_CHUNK_LENGTH", "150")),
            opus_bitrate=int(os.getenv("FISH_OPUS_BITRATE", "48000")),
            base_url=os.getenv("FISH_BASE_URL", "https://api.fish.audio").strip(),
        )
        if cfg.format != "opus" or cfg.latency not in {"low", "balanced", "normal"}:
            raise ValueError("Fish requires opus format and a supported latency mode")
        if not 100 <= cfg.chunk_length <= 300:
            raise ValueError("FISH_CHUNK_LENGTH must be between 100 and 300")
        if cfg.opus_bitrate not in {24000, 32000, 48000, 64000}:
            raise ValueError("FISH_OPUS_BITRATE must be 24000, 32000, 48000, or 64000")
        return cfg

    def cache_key(self, reference_id: str, params: object | None = None) -> str:
        """Include every Fish setting that can change the resulting audio."""
        data = {
            "provider": "fish", "reference_id": reference_id,
            "model": getattr(params, "model", "") or self.model,
            "format": self.format, "latency": self.latency,
            "chunk_length": self.chunk_length, "opus_bitrate": self.opus_bitrate,
            "sample_rate": self.sample_rate, "normalize": self.normalize,
            "speed": getattr(params, "speed", 1.0),
            "volume_db": getattr(params, "volume_db", 0.0),
            "pitch": getattr(params, "pitch", 0),
            "emotion": getattr(params, "emotion", ""),
            "temperature": getattr(params, "temperature", 0.7),
            "top_p": getattr(params, "top_p", 0.7),
        }
        return "fish:" + hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()

class FishError(RuntimeError):
    pass


def fish_tts_text(text: str, emotion: str) -> str:
    """Apply S2 emotion cues after Discord text normalization."""
    if emotion == "auto":
        letters = [ch for ch in text if ch.isalpha()]
        if len(letters) >= 4 and all(ch.isupper() for ch in letters):
            emotion = "angry"
        elif "?!" in text or "!?" in text:
            emotion = "surprised"
        elif text.rstrip().endswith("!"):
            emotion = "happy"
        else:
            emotion = ""
    emotion = {"fearful": "scared", "neutral": "indifferent"}.get(emotion, emotion)
    return f"[{emotion}] {text}" if emotion else text


@dataclass
class _Flight:
    chunks: list[bytes] = field(default_factory=list)
    condition: asyncio.Condition = field(default_factory=asyncio.Condition)
    listeners: int = 0
    done: bool = False
    error: BaseException | None = None
    task: asyncio.Task | None = None


class FishProvider:
    name = "fish"

    def __init__(self, config: FishConfig, client: httpx.AsyncClient | None = None) -> None:
        self.config = config
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=config.base_url,
            timeout=httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0),
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        )
        self._flights: dict[str, _Flight] = {}
        self._flight_lock = asyncio.Lock()
        self._session_requests = 0
        self._session_chars = 0

    @property
    def session_requests(self) -> int:
        return self._session_requests

    @property
    def session_chars(self) -> int:
        """Input characters in successful Fish requests, not billed usage."""
        return self._session_chars

    async def aclose(self) -> None:
        async with self._flight_lock:
            tasks = [flight.task for flight in self._flights.values() if flight.task]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._owns_client:
            await self._client.aclose()

    async def _produce(
        self, flight: _Flight, text: str, reference_id: str,
        params: object | None, cfg: FishConfig,
    ) -> None:
        body = {
            "text": fish_tts_text(text, getattr(params, "emotion", "")),
            "reference_id": reference_id,
            "format": cfg.format,
            "latency": cfg.latency,
            "chunk_length": cfg.chunk_length,
            "opus_bitrate": cfg.opus_bitrate,
            "sample_rate": cfg.sample_rate,
            "normalize": cfg.normalize,
            "temperature": getattr(params, "temperature", 0.7),
            "top_p": getattr(params, "top_p", 0.7),
            "prosody": {
                "speed": getattr(params, "speed", 1.0),
                "volume": getattr(params, "volume_db", 0.0),
                "normalize_loudness": True,
            },
        }
        try:
            async with self._client.stream(
                "POST", "/v1/tts", json=body,
                headers={
                    "Authorization": f"Bearer {cfg.api_key}",
                    "Content-Type": "application/json",
                    "model": getattr(params, "model", "") or cfg.model,
                },
            ) as response:
                if response.status_code != 200:
                    detail = (await response.aread())[:300].decode("utf-8", "replace")
                    raise FishError(f"Fish HTTP {response.status_code}: {detail}")
                async for chunk in response.aiter_bytes():
                    if chunk:
                        async with flight.condition:
                            flight.chunks.append(chunk)
                            flight.condition.notify_all()
            if not flight.chunks:
                raise FishError("Fish returned no audio")
            self._session_requests += 1
            self._session_chars += len(text)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            flight.error = exc
        finally:
            async with flight.condition:
                flight.done = True
                flight.condition.notify_all()

    async def stream_audio(
        self, text: str, *, reference_id: str = "", params: object | None = None,
        request_config: FishConfig | None = None,
    ) -> AsyncIterator[bytes]:
        cfg = request_config or self.config
        reference_id = reference_id or cfg.reference_id
        if not cfg.api_key or not reference_id:
            raise FishError("FISH_API_KEY and reference_id are required")
        key = hashlib.sha256(
            (cfg.cache_key(reference_id, params) + "\0" + text).encode("utf-8")
        ).hexdigest()
        async with self._flight_lock:
            flight = self._flights.get(key)
            if flight is None:
                flight = _Flight()
                self._flights[key] = flight
                flight.task = asyncio.create_task(self._produce(flight, text, reference_id, params, cfg))
            flight.listeners += 1
        index = 0
        try:
            while True:
                async with flight.condition:
                    await flight.condition.wait_for(
                        lambda: index < len(flight.chunks) or flight.done
                    )
                    if index < len(flight.chunks):
                        chunk = flight.chunks[index]
                        index += 1
                    elif flight.error:
                        raise flight.error
                    else:
                        return
                yield chunk
        finally:
            async with self._flight_lock:
                flight.listeners -= 1
                if flight.listeners == 0:
                    if not flight.done and flight.task:
                        flight.task.cancel()
                    self._flights.pop(key, None)

    async def synthesize(
        self, text: str, filename: Path, *, reference_id: str = "", params: object | None = None,
        request_config: FishConfig | None = None,
    ) -> None:
        with filename.open("wb") as output:
            async for chunk in self.stream_audio(
                text, reference_id=reference_id, params=params, request_config=request_config,
            ):
                output.write(chunk)

    async def clone_voice(
        self, sample: bytes, *, title: str, filename: str, description: str = "",
    ) -> str:
        """Create a private, reusable Fish voice and return its reference_id."""
        if not sample:
            raise FishError("Voice sample is empty")
        safe_name = Path(filename).name or "sample.wav"
        content_type = guess_type(safe_name)[0] or "application/octet-stream"
        response = await self._client.post(
            "/model",
            headers={"Authorization": f"Bearer {self.config.api_key}"},
            data={
                "type": "tts",
                "title": title,
                "train_mode": "fast",
                "visibility": "private",
                "description": description,
                "enhance_audio_quality": "true",
                "generate_sample": "false",
            },
            files={"voices": (safe_name, sample, content_type)},
            timeout=90.0,
        )
        if response.status_code != 201:
            raise FishError(f"Fish clone HTTP {response.status_code}: {response.text[:300]}")
        try:
            payload = response.json()
            reference_id = payload["_id"]
        except (ValueError, KeyError, TypeError) as exc:
            raise FishError("Fish clone response has no _id") from exc
        if not isinstance(payload, dict) or not isinstance(reference_id, str) or not reference_id:
            raise FishError("Fish clone response has an invalid _id")
        state = payload.get("state")
        for attempt in range(30):
            if state == "trained":
                return reference_id
            if state == "failed":
                raise FishError(f"Fish clone {reference_id} training failed")
            if attempt:
                await asyncio.sleep(2)
            model = await self._client.get(
                f"/model/{reference_id}",
                headers={"Authorization": f"Bearer {self.config.api_key}"},
                timeout=15.0,
            )
            if model.status_code != 200:
                raise FishError(
                    f"Fish clone {reference_id} state HTTP {model.status_code}: {model.text[:300]}"
                )
            try:
                state = model.json()["state"]
            except (ValueError, KeyError, TypeError) as exc:
                raise FishError(f"Fish clone {reference_id} state response is invalid") from exc
        raise FishError(f"Fish clone {reference_id} is still {state} after 60 seconds")
