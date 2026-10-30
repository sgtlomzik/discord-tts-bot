"""OpenRouter's OpenAI-compatible /audio/speech endpoint, shared by its TTS plugins.

Gemini and MAI-Voice are both reached through it with one OPENROUTER_API_KEY.
The endpoint returns raw s16le PCM with the rate and channel count in the
Content-Type header (``audio/pcm;rate=24000;channels=1``). A plugin
subclasses :class:`OpenRouterSpeechProvider` and supplies its config, the
request's voice and ``stream_spec``; the request, error mapping and PCM
reading live here.
"""

from __future__ import annotations

import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator

import httpx

from ttsbot.errors import AuthError, QuotaExhaustedError, RateLimitError, RequestError
from ttsbot.httpclient import attach_warmer, provider_limits, retry_after_seconds

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"


@dataclass(frozen=True)
class OpenRouterConfig:
    api_key: str = field(repr=False)  # never in logs or tracebacks
    model: str = ""
    voice: str = ""
    base_url: str = DEFAULT_BASE_URL
    app_title: str = ""
    referer: str = ""


class OpenRouterError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class OpenRouterAuthError(OpenRouterError, AuthError):
    """HTTP 401/403: the OpenRouter key is invalid or not allowed."""


class OpenRouterRequestError(OpenRouterError, RequestError):
    """HTTP 400/413/422, or a 404 about the voice: OpenRouter rejected this
    request (input, voice, length), not the provider: other messages still
    work."""


class OpenRouterQuotaExhaustedError(OpenRouterError, QuotaExhaustedError):
    """HTTP 402: the OpenRouter credits are used up."""


class OpenRouterRateLimitError(OpenRouterError, RateLimitError):
    """HTTP 429; ``retry_after`` is the server's Retry-After, if it sent one."""

    def __init__(self, message: str, status_code: int | None = None,
                 retry_after: float | None = None) -> None:
        super().__init__(message, status_code)
        self.retry_after = retry_after


_NO_MODEL_PHRASES = ("not a valid model", "model not found", "no such model", "unknown model", "no endpoints")


def http_error(response: httpx.Response, detail: str) -> OpenRouterError:
    status = response.status_code
    message = f"OpenRouter HTTP {status}: {detail}"
    if status in (401, 403):
        return OpenRouterAuthError(message, status)
    if status == 402:
        return OpenRouterQuotaExhaustedError(message, status)
    if status == 429:
        return OpenRouterRateLimitError(message, status, retry_after_seconds(response.headers.get("retry-after")))
    # A missing model or endpoint (a misspelled model setting, a model
    # OpenRouter retired) fails every request: count it against the
    # provider so its breaker opens. A 404 is only this request's when it
    # is about the voice.
    lowered = detail.lower()
    no_model = any(phrase in lowered for phrase in _NO_MODEL_PHRASES)
    if status in (413, 422) or (status == 400 and not no_model):
        return OpenRouterRequestError(message, status)
    if status == 404 and "voice" in lowered and not no_model:
        return OpenRouterRequestError(message, status)
    return OpenRouterError(message, status)


def parse_pcm_content_type(content_type: str) -> tuple[int, int]:
    """``audio/pcm;rate=24000;channels=1`` -> ``(24000, 1)``."""
    mime, _, rest = content_type.partition(";")
    mime = mime.strip().lower()
    if mime not in {"audio/pcm", "audio/l16"}:
        raise OpenRouterError(f"Unexpected audio content type: {content_type or 'none'}")
    params = {}
    for part in rest.split(";"):
        key, sep, value = part.partition("=")
        if sep:
            params[key.strip().lower()] = value.strip()
    try:
        rate = int(params.get("rate", 24000))
        channels = int(params.get("channels", 1))
    except ValueError as exc:
        raise OpenRouterError(f"Bad audio content type: {content_type}") from exc
    if rate <= 0 or channels not in (1, 2):
        raise OpenRouterError(f"Unsupported audio format: {content_type}")
    return rate, channels


class OpenRouterAudioStream:
    """An accepted /audio/speech response: format known, body still to read."""

    def __init__(self, provider: "OpenRouterSpeechProvider", response: httpx.Response,
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
            raise OpenRouterError(f"{self._provider.label} returned no audio")
        self._provider._count(self._text)

    async def aclose(self) -> None:
        await self._response.aclose()


class OpenRouterSpeechProvider:
    """One OpenRouter speech model; subclasses set ``name``, ``label`` and
    ``request_voice`` and answer ``stream_spec``."""

    name = "openrouter"
    label = "OpenRouter"

    def __init__(self, config: OpenRouterConfig, client: httpx.AsyncClient | None = None) -> None:
        self.config = config
        self._owns_client = client is None
        # One keep-alive client: a warm connection saves ~110 ms per request.
        self._client = client or httpx.AsyncClient(
            base_url=config.base_url,
            timeout=httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0),
            limits=provider_limits(),
        )
        self.warmer = attach_warmer(self._client, self.label)
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

    def request_model(self, params: object | None) -> str:
        return getattr(params, "model", "") or self.config.model

    def request_voice(self, params: object | None) -> str:
        return getattr(params, "voice", "") or self.config.voice

    async def open_stream(self, text: str, params: object | None = None) -> OpenRouterAudioStream:
        """Send the request and return once the response headers are in.

        Raises an OpenRouterError subclass for a non-200 status or a non-PCM
        body. The caller must ``aclose()`` the returned stream.
        """
        if not self.config.api_key:
            raise OpenRouterAuthError("OPENROUTER_API_KEY is not set")
        body = {
            "model": self.request_model(params),
            "input": text,
            "voice": self.request_voice(params),
            "response_format": "pcm",
        }
        request = self._client.build_request(
            "POST", "/audio/speech", json=body, headers=self._headers(),
        )
        response = await self._client.send(request, stream=True)
        try:
            if response.status_code != 200:
                detail = (await response.aread())[:300].decode("utf-8", "replace")
                raise http_error(response, detail)
            rate, channels = parse_pcm_content_type(response.headers.get("content-type", ""))
        except BaseException:
            await response.aclose()
            raise
        return OpenRouterAudioStream(self, response, text, rate, channels)

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
