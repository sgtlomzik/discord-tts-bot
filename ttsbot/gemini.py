"""Gemini TTS through OpenRouter's OpenAI-compatible /audio/speech endpoint.

The model returns raw s16le PCM (24 kHz mono); the rate and channel count
come in the Content-Type header. OpenRouter does not stream this model: the
response headers arrive only once the whole clip is generated (measured
2026-09-28, docs/internal/GEMINI_OPENROUTER_PLAN.md §2.1). The transport
stays behind ``open_stream`` so a streaming route can replace it without
touching the PCM path.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator

import httpx

from ttsbot.errors import QuotaExhaustedError

log = logging.getLogger("tts_bot")

DEFAULT_MODEL = "google/gemini-3.8-flash-lite-tts"
DEFAULT_VOICE = "Kore"
DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"

# Prebuilt Gemini TTS voices, for slash-command autocomplete. Registration
# probes the voice with a real request, so this list is advisory only.
GEMINI_VOICES = (
    "Achernar", "Achird", "Algenib", "Algieba", "Alnilam", "Aoede", "Autonoe",
    "Callirrhoe", "Charon", "Despina", "Enceladus", "Erinome", "Fenrir", "Gacrux",
    "Iapetus", "Kore", "Laomedeia", "Leda", "Orus", "Puck", "Pulcherrima",
    "Rasalgethi", "Sadachbia", "Sadaltager", "Schedar", "Sulafat", "Umbriel",
    "Vindemiatrix", "Zephyr", "Zubenelgenubi",
)


def _env(name: str, default: str) -> str:
    """Env value with an empty string treated as unset (as in .env.example)."""
    return os.getenv(name, "").strip() or default


@dataclass(frozen=True)
class GeminiConfig:
    api_key: str
    model: str = DEFAULT_MODEL
    voice: str = DEFAULT_VOICE
    base_url: str = DEFAULT_BASE_URL
    app_title: str = ""
    referer: str = ""

    @classmethod
    def from_env(cls) -> "GeminiConfig":
        return cls(
            api_key=_env("OPENROUTER_API_KEY", ""),
            model=_env("GEMINI_TTS_MODEL", DEFAULT_MODEL),
            voice=_env("GEMINI_TTS_VOICE", DEFAULT_VOICE),
            base_url=_env("OPENROUTER_BASE_URL", DEFAULT_BASE_URL),
            app_title=_env("OPENROUTER_APP_TITLE", ""),
            referer=_env("OPENROUTER_REFERER", ""),
        )

    def cache_key(self, params: object | None = None) -> str:
        """Include every setting that changes the PCM the API returns.

        Volume is applied locally by PcmFramer, so it is left out: retuning
        the volume reuses the cached audio.
        """
        data = {
            "provider": "gemini",
            "model": getattr(params, "model", "") or self.model,
            "voice": getattr(params, "voice", "") or self.voice,
            "format": "pcm",
        }
        return "gemini:" + hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


class GeminiError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class GeminiAuthError(GeminiError):
    """HTTP 401/403: the OpenRouter key is invalid or not allowed."""


class GeminiQuotaExhaustedError(GeminiError, QuotaExhaustedError):
    """HTTP 402: the OpenRouter credits are used up."""


class GeminiRateLimitError(GeminiError):
    """HTTP 429; ``retry_after`` is the server's Retry-After, if it sent one."""

    def __init__(self, message: str, status_code: int | None = None,
                 retry_after: float | None = None) -> None:
        super().__init__(message, status_code)
        self.retry_after = retry_after


def _retry_after(value: str | None) -> float | None:
    try:
        seconds = float(value) if value else None
    except ValueError:
        return None  # HTTP-date form: treat as an ordinary failure
    return seconds if seconds is not None and seconds > 0 else None


def _http_error(response: httpx.Response, detail: str) -> GeminiError:
    status = response.status_code
    message = f"OpenRouter HTTP {status}: {detail}"
    if status in (401, 403):
        return GeminiAuthError(message, status)
    if status == 402:
        return GeminiQuotaExhaustedError(message, status)
    if status == 429:
        return GeminiRateLimitError(message, status, _retry_after(response.headers.get("retry-after")))
    return GeminiError(message, status)


def parse_pcm_content_type(content_type: str) -> tuple[int, int]:
    """``audio/pcm;rate=24000;channels=1`` -> ``(24000, 1)``."""
    mime, _, rest = content_type.partition(";")
    mime = mime.strip().lower()
    if mime not in {"audio/pcm", "audio/l16"}:
        raise GeminiError(f"Unexpected audio content type: {content_type or 'none'}")
    params = {}
    for part in rest.split(";"):
        key, sep, value = part.partition("=")
        if sep:
            params[key.strip().lower()] = value.strip()
    try:
        rate = int(params.get("rate", 24000))
        channels = int(params.get("channels", 1))
    except ValueError as exc:
        raise GeminiError(f"Bad audio content type: {content_type}") from exc
    if rate <= 0 or channels not in (1, 2):
        raise GeminiError(f"Unsupported audio format: {content_type}")
    return rate, channels


class GeminiAudioStream:
    """An accepted /audio/speech response: format known, body still to read."""

    def __init__(self, provider: "GeminiProvider", response: httpx.Response,
                 text: str, rate: int, channels: int) -> None:
        self._provider = provider
        self._response = response
        self._text = text
        self.rate = rate
        self.channels = channels

    async def chunks(self) -> AsyncIterator[bytes]:
        """Yield PCM chunks of any length; raise if the body had no audio."""
        received = 0
        async for chunk in self._response.aiter_bytes():
            if chunk:
                received += len(chunk)
                yield chunk
        if received < 2:
            raise GeminiError("Gemini returned no audio")
        self._provider._count(self._text)

    async def aclose(self) -> None:
        await self._response.aclose()


class GeminiProvider:
    name = "gemini"

    def __init__(self, config: GeminiConfig, client: httpx.AsyncClient | None = None) -> None:
        self.config = config
        self._owns_client = client is None
        # One keep-alive client: a warm connection saves ~110 ms per request.
        self._client = client or httpx.AsyncClient(
            base_url=config.base_url,
            timeout=httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0),
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        )
        self._session_requests = 0
        self._session_chars = 0

    @property
    def session_requests(self) -> int:
        return self._session_requests

    @property
    def session_chars(self) -> int:
        """Input characters in successful requests, not billed tokens."""
        return self._session_chars

    def _count(self, text: str) -> None:
        self._session_requests += 1
        self._session_chars += len(text)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _headers(self) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self.config.api_key}"}
        if self.config.app_title:
            headers["X-Title"] = self.config.app_title
        if self.config.referer:
            headers["HTTP-Referer"] = self.config.referer
        return headers

    async def open_stream(self, text: str, params: object | None = None) -> GeminiAudioStream:
        """Send the request and return once the response headers are in.

        Raises a GeminiError subclass for a non-200 status or a non-PCM body.
        The caller must ``aclose()`` the returned stream.
        """
        if not self.config.api_key:
            raise GeminiAuthError("OPENROUTER_API_KEY is not set")
        body = {
            "model": getattr(params, "model", "") or self.config.model,
            "input": text,
            "voice": getattr(params, "voice", "") or self.config.voice,
            "response_format": "pcm",
        }
        request = self._client.build_request(
            "POST", "/audio/speech", json=body, headers=self._headers(),
        )
        response = await self._client.send(request, stream=True)
        try:
            if response.status_code != 200:
                detail = (await response.aread())[:300].decode("utf-8", "replace")
                raise _http_error(response, detail)
            rate, channels = parse_pcm_content_type(response.headers.get("content-type", ""))
        except BaseException:
            await response.aclose()
            raise
        return GeminiAudioStream(self, response, text, rate, channels)

    async def fetch_pcm(self, text: str, params: object | None = None) -> tuple[int, int, bytes]:
        """Whole clip as ``(rate, channels, s16le bytes)``."""
        stream = await self.open_stream(text, params)
        try:
            pcm = b"".join([chunk async for chunk in stream.chunks()])
        finally:
            await stream.aclose()
        return stream.rate, stream.channels, pcm[:len(pcm) - len(pcm) % (2 * stream.channels)]

    async def synthesize(self, text: str, filename: Path, params: object | None = None) -> None:
        """Write the clip as a WAV file (file path and probes)."""
        rate, channels, pcm = await self.fetch_pcm(text, params)
        write_wav(filename, rate, channels, pcm)


def write_wav(filename: Path, rate: int, channels: int, pcm: bytes) -> None:
    with wave.open(str(filename), "wb") as out:
        out.setnchannels(channels)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(pcm)
