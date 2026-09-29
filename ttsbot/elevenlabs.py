"""ElevenLabs TTS over the HTTP streaming endpoint.

``POST /v1/text-to-speech/{voice_id}/stream`` sends audio while it is being
generated (measured 2026-09-29 with eleven_v4_turbo from this host: first
audio 0.22-0.26 s for 2 to 133 characters). The output format picks the
playback path:

- ``opus_48000_*``: Ogg/Opus, mono, 48 kHz, 20 ms packets - the same shape
  as Fish, so the packets go to Discord as-is (no decode, no ffmpeg);
- ``pcm_*``: raw s16le mono at the rate in the format name, framed
  in-process by PcmFramer like Gemini.

Several API keys form a ring (ELEVENLABS_API_KEYS). A key that is out of
credits or no longer valid is skipped and the same request is sent with
the next key, before any audio plays; after the last key comes the first.
"Out of credits" is judged from the remaining balance ElevenLabs reports:
a message that is merely longer than what a key has left goes to the next
key without moving the ring off it.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Callable

import httpx

from ttsbot.errors import QuotaExhaustedError
from ttsbot.gemini import write_wav

log = logging.getLogger("tts_bot")

DEFAULT_MODEL = "eleven_v4_turbo"
DEFAULT_FORMAT = "opus_48000_64"
DEFAULT_BASE_URL = "https://api.elevenlabs.io"
OPUS_FORMATS = frozenset(f"opus_48000_{kbps}" for kbps in (32, 64, 96, 128, 192))
PCM_RATES = frozenset({8000, 16000, 22050, 24000, 32000, 44100, 48000})
_VOICE_LIST_TTL = 300.0
# A key with fewer credits left than this counts as empty (~100 characters
# on v4 Turbo); with more, a quota_exceeded only means "this message is too
# long for what is left".
EMPTY_KEY_CREDITS = 50
_REMAINING_RE = re.compile(r"([\d,]+)\s+credits?\s+remaining", re.IGNORECASE)
# 401/403 reasons that are about the key or its account, not the request.
_KEY_REASONS = ("invalid_api_key", "missing_permissions", "detected_unusual_activity")


def _env(name: str, default: str) -> str:
    """Env value with an empty string treated as unset (as in .env.example)."""
    return os.getenv(name, "").strip() or default


def key_fingerprint(key: str) -> str:
    """Stable id of a key for persistence; the key itself is never stored."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def mask_key(key: str) -> str:
    return f"…{key[-4:]}" if len(key) > 8 else "…"


def format_kind(output_format: str) -> tuple[str, int]:
    """``opus_48000_64`` -> ``("opus", 48000)``, ``pcm_24000`` -> ``("pcm", 24000)``."""
    if output_format in OPUS_FORMATS:
        return "opus", 48000
    kind, _, rate = output_format.partition("_")
    if kind == "pcm" and rate.isdigit() and int(rate) in PCM_RATES:
        return "pcm", int(rate)
    raise ValueError(
        f"Unsupported ELEVENLABS_FORMAT {output_format!r}: use opus_48000_<32|64|96|128|192> "
        "or pcm_<rate>"
    )


@dataclass(frozen=True)
class ElevenLabsConfig:
    api_key: str
    # More keys for the ring, tried after api_key in this order.
    api_keys: tuple[str, ...] = ()
    model: str = DEFAULT_MODEL
    format: str = DEFAULT_FORMAT
    voice_id: str = ""
    language_code: str = ""
    base_url: str = DEFAULT_BASE_URL

    @classmethod
    def from_env(cls) -> "ElevenLabsConfig":
        ring = tuple(k for k in _env("ELEVENLABS_API_KEYS", "").replace(",", " ").split() if k)
        single = _env("ELEVENLABS_API_KEY", "")
        cfg = cls(
            api_key=single or (ring[0] if ring else ""),
            api_keys=ring,
            model=_env("ELEVENLABS_MODEL", DEFAULT_MODEL),
            format=_env("ELEVENLABS_FORMAT", DEFAULT_FORMAT).lower(),
            voice_id=_env("ELEVENLABS_VOICE_ID", ""),
            language_code=_env("ELEVENLABS_LANGUAGE_CODE", "").lower(),
            base_url=_env("ELEVENLABS_BASE_URL", DEFAULT_BASE_URL),
        )
        format_kind(cfg.format)
        return cfg

    @property
    def kind(self) -> str:
        return format_kind(self.format)[0]

    @property
    def keys(self) -> tuple[str, ...]:
        """api_key then api_keys, without blanks and duplicates."""
        return tuple(dict.fromkeys(k for k in (self.api_key, *self.api_keys) if k))

    def cache_key(self, params: object | None = None) -> str:
        """Include every setting that changes the audio the API returns."""
        data = {
            "provider": "elevenlabs",
            "voice_id": getattr(params, "voice_id", "") or self.voice_id,
            "model": getattr(params, "model", "") or self.model,
            "format": self.format,
            "language_code": self.language_code,
            "stability": getattr(params, "stability", None),
            "similarity_boost": getattr(params, "similarity_boost", None),
        }
        return "elevenlabs:" + hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


class ElevenLabsError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class ElevenLabsKeyError(ElevenLabsError):
    """A problem of the API key, not of the request: try the next key."""


class ElevenLabsAuthError(ElevenLabsKeyError):
    """Invalid or revoked key, or one without the text_to_speech permission."""


class ElevenLabsCreditsError(ElevenLabsKeyError):
    """The key's credits do not cover this request (quota_exceeded).

    ``remaining`` is the balance ElevenLabs reported, None if it did not say.
    """

    def __init__(self, message: str, status_code: int | None = None,
                 remaining: int | None = None) -> None:
        super().__init__(message, status_code)
        self.remaining = remaining

    @property
    def key_empty(self) -> bool:
        return self.remaining is None or self.remaining < EMPTY_KEY_CREDITS


def _remaining_credits(message: str) -> int | None:
    match = _REMAINING_RE.search(message)
    return int(match.group(1).replace(",", "")) if match else None


class ElevenLabsRequestError(ElevenLabsError):
    """A problem of this request, not of the provider: the pipeline falls
    back to Piper without counting it against the circuit breaker."""


class ElevenLabsVoiceNotFoundError(ElevenLabsRequestError):
    """HTTP 404 voice_not_found, or no voice_id at all."""


class ElevenLabsMessageTooLongError(ElevenLabsRequestError):
    """No key has enough credits left for this message, but some still have
    credits for shorter ones."""


class ElevenLabsQuotaExhaustedError(ElevenLabsKeyError, QuotaExhaustedError):
    """HTTP 402, or every key in the ring is out of credits: pause the provider."""


class ElevenLabsRateLimitError(ElevenLabsError):
    """HTTP 429; ``retry_after`` is the server's Retry-After, if it sent one."""

    def __init__(self, message: str, status_code: int | None = None,
                 retry_after: float | None = None) -> None:
        super().__init__(message, status_code)
        self.retry_after = retry_after


def _retry_after(value: str | None) -> float | None:
    try:
        seconds = float(value) if value else None
    except ValueError:
        return None
    return seconds if seconds is not None and seconds > 0 else None


def _http_error(response: httpx.Response, body: bytes) -> ElevenLabsError:
    """Map an error response; ElevenLabs puts the reason in ``detail.status``."""
    status = response.status_code
    reason, message = "", body[:300].decode("utf-8", "replace")
    try:
        detail = json.loads(body).get("detail")
    except (ValueError, AttributeError):
        detail = None
    if isinstance(detail, dict):
        reason = str(detail.get("status") or detail.get("code") or "")
        message = str(detail.get("message") or message)
    elif isinstance(detail, str):
        message = detail
    text = f"ElevenLabs HTTP {status} {reason}: {message}".replace(" :", ":")
    if status == 402:
        return ElevenLabsQuotaExhaustedError(text, status)
    if reason == "quota_exceeded":
        return ElevenLabsCreditsError(text, status, _remaining_credits(message))
    if reason == "voice_not_found":
        return ElevenLabsVoiceNotFoundError(text, status)
    if status == 429:
        return ElevenLabsRateLimitError(text, status, _retry_after(response.headers.get("retry-after")))
    if reason.startswith(_KEY_REASONS) or (status == 401 and not reason):
        return ElevenLabsAuthError(text, status)
    return ElevenLabsError(text, status)


class ElevenLabsAudioStream:
    """An accepted /stream response: format known, body still to read."""

    def __init__(self, provider: "ElevenLabsProvider", response: httpx.Response,
                 text: str, output_format: str) -> None:
        self._provider = provider
        self._response = response
        self._text = text
        self.format = output_format
        self.kind, self.rate = format_kind(output_format)
        self.channels = 1

    async def chunks(self) -> AsyncIterator[bytes]:
        """Yield audio chunks of any length; raise if the body had no audio."""
        received = 0
        async for chunk in self._response.aiter_bytes():
            if chunk:
                received += len(chunk)
                yield chunk
        if received < 2:
            raise ElevenLabsError("ElevenLabs returned no audio")

    async def aclose(self) -> None:
        await self._response.aclose()


class ElevenLabsProvider:
    name = "elevenlabs"

    def __init__(
        self, config: ElevenLabsConfig, client: httpx.AsyncClient | None = None, *,
        active_key: str = "", on_key_switch: Callable[[str], None] | None = None,
    ) -> None:
        """``active_key`` is a saved key_fingerprint to resume the ring at;
        ``on_key_switch(fingerprint)`` is called whenever the ring moves."""
        self.config = config
        self._owns_client = client is None
        # One keep-alive client: the warm connection is what keeps the first
        # audio at ~0.25 s (a cold TLS handshake measured ~0.66 s).
        self._client = client or httpx.AsyncClient(
            base_url=config.base_url,
            timeout=httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0),
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        )
        self._session_requests = 0
        self._session_chars = 0
        self._session_credits = 0
        self._voices: list[tuple[str, str]] = []
        self._voices_at = 0.0
        self._voices_key = -1
        keys = config.keys
        self._key_credits = [0] * len(keys)
        saved = next(
            (i for i, key in enumerate(keys) if active_key and key_fingerprint(key) == active_key), None,
        )
        if active_key and saved is None:
            log.warning("Saved ElevenLabs key is no longer configured; starting at key #1")
        self._active = saved or 0
        self._on_key_switch = on_key_switch

    @property
    def session_requests(self) -> int:
        return self._session_requests

    @property
    def session_chars(self) -> int:
        """Input characters in successful requests."""
        return self._session_chars

    @property
    def session_credits(self) -> int:
        """Billed credits from the ``character-cost`` header (v4 Turbo: 0.5/char)."""
        return self._session_credits

    @property
    def active_key_index(self) -> int:
        return self._active

    def key_usage(self) -> list[tuple[str, int, bool]]:
        """``[(masked key, session credits, active)]`` in ring order."""
        return [
            (mask_key(key), self._key_credits[i], i == self._active)
            for i, key in enumerate(self.config.keys)
        ]

    def _key_label(self, index: int) -> str:
        keys = self.config.keys
        return f"#{index + 1}/{len(keys)} ({mask_key(keys[index])})"

    def _advance_from(self, index: int, reason: str) -> bool:
        """Move the ring past ``index`` unless another request already did."""
        keys = self.config.keys
        if self._active != index or len(keys) < 2:
            return False
        self._active = (index + 1) % len(keys)
        log.warning(
            "ElevenLabs key %s %s; switching to key %s",
            self._key_label(index), reason, self._key_label(self._active),
        )
        return True

    def _save_active(self) -> None:
        if self._on_key_switch is None:
            return
        try:
            self._on_key_switch(key_fingerprint(self.config.keys[self._active]))
        except Exception:
            log.exception("Could not save the active ElevenLabs key")

    def _count(self, text: str, cost: str | None, key_index: int = 0) -> None:
        """Count an accepted (billed) request, even if it is read only in part."""
        self._session_requests += 1
        self._session_chars += len(text)
        try:
            credits = round(float(cost)) if cost else 0
        except ValueError:
            credits = 0
        self._session_credits += credits
        if key_index < len(self._key_credits):
            self._key_credits[key_index] += credits
        log.info(
            "ElevenLabs usage key=%s credits=%d session_credits=%d text_len=%d",
            self._key_label(key_index), credits, self._session_credits, len(text),
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _body(self, text: str, params: object | None) -> dict:
        body: dict = {"text": text, "model_id": getattr(params, "model", "") or self.config.model}
        if self.config.language_code:
            body["language_code"] = self.config.language_code
        settings = {
            key: float(value)
            for key in ("stability", "similarity_boost")
            if (value := getattr(params, key, None)) is not None
        }
        if settings:
            body["voice_settings"] = settings
        return body

    async def open_stream(
        self, text: str, params: object | None = None, *, output_format: str | None = None,
    ) -> ElevenLabsAudioStream:
        """Send the request and return once the response headers are in.

        Walks the key ring from the active key and retries with the next key
        on a key error. An empty or invalid key moves the ring on; a key that
        only lacks credits for this long message is skipped for this request
        alone. When no key took the request, raises:

        - ElevenLabsMessageTooLongError if some key still has credits for
          shorter messages (Piper speaks this one, no provider pause);
        - ElevenLabsQuotaExhaustedError if the keys are empty (long pause);
        - otherwise the last key error (invalid keys).

        Other errors are raised as is. The caller must ``aclose()`` the stream.
        """
        keys = self.config.keys
        if not keys:
            raise ElevenLabsAuthError("ELEVENLABS_API_KEY is not set")
        voice_id = getattr(params, "voice_id", "") or self.config.voice_id
        if not voice_id:
            raise ElevenLabsVoiceNotFoundError("No ElevenLabs voice_id")
        output_format = output_format or self.config.format
        failures: list[ElevenLabsKeyError] = []
        start = self._active
        moved = False
        try:
            for step in range(len(keys)):
                index = (start + step) % len(keys)
                try:
                    return await self._open_with_key(index, voice_id, text, params, output_format)
                except ElevenLabsKeyError as exc:
                    failures.append(exc)
                    if isinstance(exc, ElevenLabsCreditsError) and not exc.key_empty:
                        log.info(
                            "ElevenLabs key %s has %d credits left, not enough for %d chars; "
                            "trying the next key for this message only",
                            self._key_label(index), exc.remaining, len(text),
                        )
                    else:
                        moved |= self._advance_from(index, f"rejected ({type(exc).__name__})")
        finally:
            if moved and self._active != start:
                self._save_active()  # once per request, not per step
        last = failures[-1]
        if any(isinstance(exc, ElevenLabsCreditsError) and not exc.key_empty for exc in failures):
            raise ElevenLabsMessageTooLongError(
                f"No ElevenLabs key has enough credits for {len(text)} chars: {last}", last.status_code,
            )
        if any(isinstance(exc, (ElevenLabsCreditsError, QuotaExhaustedError)) for exc in failures):
            raise ElevenLabsQuotaExhaustedError(
                f"No ElevenLabs key has credits left ({len(keys)} tried): {last}", last.status_code,
            )
        raise last

    async def _open_with_key(
        self, index: int, voice_id: str, text: str, params: object | None, output_format: str,
    ) -> ElevenLabsAudioStream:
        request = self._client.build_request(
            "POST", f"/v1/text-to-speech/{voice_id}/stream",
            params={"output_format": output_format},
            json=self._body(text, params),
            headers={"xi-api-key": self.config.keys[index]},
        )
        response = await self._client.send(request, stream=True)
        try:
            if response.status_code != 200:
                raise _http_error(response, await response.aread())
            content_type = response.headers.get("content-type", "")
            if content_type.startswith("application/json"):
                raise ElevenLabsError(f"ElevenLabs returned JSON instead of audio: {content_type}")
        except BaseException:
            await response.aclose()
            raise
        # The cost header comes with the 200: the request is billed even when
        # the caller stops early (length limit, cancel, mid-stream error).
        self._count(text, response.headers.get("character-cost"), index)
        return ElevenLabsAudioStream(self, response, text, output_format)

    async def stream_audio(self, text: str, params: object | None = None) -> AsyncIterator[bytes]:
        """Audio bytes as they arrive, in the configured format."""
        stream = await self.open_stream(text, params)
        try:
            async with contextlib.aclosing(stream.chunks()) as chunks:
                async for chunk in chunks:
                    yield chunk
        finally:
            await stream.aclose()

    async def fetch(self, text: str, params: object | None = None) -> tuple[str, int, bytes]:
        """Whole clip as ``(kind, rate, bytes)``; PCM is trimmed to whole samples."""
        stream = await self.open_stream(text, params)
        try:
            async with contextlib.aclosing(stream.chunks()) as chunks:
                data = b"".join([chunk async for chunk in chunks])
        finally:
            await stream.aclose()
        if stream.kind == "pcm":
            data = data[:len(data) - len(data) % 2]
        return stream.kind, stream.rate, data

    async def synthesize(self, text: str, filename: Path, params: object | None = None) -> str:
        """Write the clip for the file path: Ogg/Opus as-is, PCM as WAV.

        Returns the kind ("opus" or "pcm"). ffmpeg detects the container by
        content, so the file name suffix does not matter.
        """
        kind, rate, data = await self.fetch(text, params)
        if kind == "pcm":
            write_wav(filename, rate, 1, data)
        else:
            filename.write_bytes(data)
        return kind

    async def list_voices(self) -> list[tuple[str, str]]:
        """``[(voice_id, name)]`` for autocomplete; cached, [] on any failure.

        Needs the key's voices_read permission; without it autocomplete is
        simply empty and voice-add still accepts a pasted voice_id. Failures
        are cached too, so autocomplete does not retry on every keystroke.
        Only the first 100 voices are listed.
        """
        if (
            self._voices_at and self._voices_key == self._active
            and time.monotonic() - self._voices_at < _VOICE_LIST_TTL
        ):
            return self._voices
        self._voices_key = self._active
        try:
            response = await self._client.get(
                "/v2/voices", params={"page_size": 100},
                headers={"xi-api-key": self.config.keys[self._active]}, timeout=2.0,
            )
            response.raise_for_status()
            voices = [
                (str(v["voice_id"]), str(v.get("name") or v["voice_id"]))
                for v in response.json().get("voices", [])
                if isinstance(v, dict) and v.get("voice_id")
            ]
        except (httpx.HTTPError, ValueError, KeyError, AttributeError) as exc:
            log.info("ElevenLabs voice list unavailable: %s", exc)
            self._voices_at = time.monotonic()
            return self._voices
        self._voices, self._voices_at = voices, time.monotonic()
        return voices
