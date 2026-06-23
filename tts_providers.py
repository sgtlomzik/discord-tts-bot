"""TTS provider abstraction layer.

This module introduces a thin indirection between the Discord bot and the
underlying TTS engine(s). The bot only knows about the
``TTSDispatcher.synthesize`` entry point; everything else is implementation
detail.

The split mirrors the rollout plan in ``docs/MINIMAX_INTEGRATION_PLAN.md``:

* ``LocalProvider`` — wraps the existing Piper-based synthesis without
  modifying it. Internal logic untouched.
* ``MiniMaxProvider`` — added in a later commit. Calls the MiniMax T2A
  HTTP endpoint, decodes the hex-encoded MP3 payload, writes it to disk.
* ``CircuitBreaker`` — short-circuits repeated failures so a flaky cloud
  API does not stall the bot.
* ``TTSDispatcher`` — facade that selects the primary provider, handles
  fallback to ``LocalProvider`` on any failure, and reports outcomes to
  the circuit breaker.

All providers satisfy the ``TTSProvider`` protocol: ``async def
synthesize(text, filename) -> None`` that writes a valid audio file at
``filename``. The bot's downstream ``ffmpeg``-based PCM pipeline is
format-agnostic (it re-encodes whatever the provider produced), so a
provider can safely emit WAV or MP3 as long as it is a valid container.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable, Optional, Protocol

if TYPE_CHECKING:  # pragma: no cover
    import httpx

log = logging.getLogger("tts_bot.providers")


# ---------------------------------------------------------------------------
# Provider protocol
# ---------------------------------------------------------------------------


class TTSProvider(Protocol):
    """Async interface every TTS backend must satisfy.

    Implementations are expected to write a valid audio container to
    ``filename``. They may raise any exception to signal a hard failure —
    the dispatcher will treat it as a fallback trigger.
    """

    name: str

    async def synthesize(self, text: str, filename: Path) -> None:
        ...


# A coroutine that performs local Piper synthesis. The dispatcher uses
# this callable to construct ``LocalProvider`` without depending on the
# concrete ``TTSBot`` class (avoids circular imports and keeps the
# provider layer pure-python and test-friendly).
PiperSynthFn = Callable[[str, Path, Optional[str]], Awaitable[None]]


# ---------------------------------------------------------------------------
# Local provider (thin wrapper over Piper)
# ---------------------------------------------------------------------------


class LocalProvider:
    """Pass-through wrapper around the existing Piper synthesis path.

    No internal Piper logic is changed — this class only adapts the
    ``(text, filename, voice_profile)`` signature to the uniform
    ``(text, filename)`` signature expected by ``TTSProvider``.
    """

    name = "local"

    def __init__(self, piper_synthesize: PiperSynthFn) -> None:
        self._piper_synthesize = piper_synthesize

    async def synthesize(self, text: str, filename: Path) -> None:
        # Voice profile is irrelevant for the wrapper itself; the bot
        # selects which Piper profile to use before calling the
        # dispatcher. Passing None here means "use default profile".
        await self._piper_synthesize(text, filename, None)
        log.debug("LocalProvider synthesized text=%d chars to %s", len(text), filename)


# ---------------------------------------------------------------------------
# Circuit breaker (skeleton; fully wired in a later commit)
# ---------------------------------------------------------------------------


class CircuitState(str, Enum):
    CLOSED = "closed"          # normal: every call hits the primary
    OPEN = "open"              # all calls short-circuit to fallback
    HALF_OPEN = "half_open"    # single probe call after cooldown


@dataclass
class CircuitBreakerConfig:
    failure_threshold: int = 3
    cooldown_seconds: float = 60.0


class CircuitBreaker:
    """Tracks consecutive failures of the cloud provider.

    Skeleton implementation — state tracking is in place, but the
    dispatcher does not yet consult ``allow_request``. The full
    open/half-open state machine is wired in commit 6 of the rollout
    plan.
    """

    def __init__(self, config: Optional[CircuitBreakerConfig] = None) -> None:
        self._config = config or CircuitBreakerConfig()
        self._state: CircuitState = CircuitState.CLOSED
        self._consecutive_failures: int = 0
        self._opened_at: Optional[float] = None

    @property
    def state(self) -> CircuitState:
        return self._state

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    def allow_request(self) -> bool:
        """Return True if a primary call should be attempted right now.

        With the skeleton behavior this is always True; the real gating
        logic arrives in commit 6.
        """
        return True

    def record_success(self) -> None:
        self._consecutive_failures = 0
        self._state = CircuitState.CLOSED
        self._opened_at = None

    def record_failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= self._config.failure_threshold:
            self._state = CircuitState.OPEN
            self._opened_at = time.perf_counter()


def load_circuit_breaker_from_env() -> CircuitBreaker:
    """Build a CircuitBreaker from the standard ``CB_*`` env vars."""
    threshold = int(os.getenv("CB_FAILURE_THRESHOLD", "3"))
    cooldown = float(os.getenv("CB_COOLDOWN_SECONDS", "60"))
    return CircuitBreaker(
        CircuitBreakerConfig(
            failure_threshold=max(1, threshold),
            cooldown_seconds=max(0.0, cooldown),
        )
    )


# ---------------------------------------------------------------------------
# Dispatcher (skeleton: routes everything to the local provider)
# ---------------------------------------------------------------------------


class PrimaryProvider(str, Enum):
    LOCAL = "local"
    MINIMAX = "minimax"


@dataclass
class DispatcherConfig:
    primary: PrimaryProvider = PrimaryProvider.LOCAL
    request_timeout_seconds: float = 2.5


# ---------------------------------------------------------------------------
# MiniMax provider (added in commit 3)
# ---------------------------------------------------------------------------


class MiniMaxError(Exception):
    """Base class for MiniMax failures that should trigger fallback.

    Subclasses let the bot log a precise reason without parsing strings.
    """


class MiniMaxAuthError(MiniMaxError):
    """Invalid API key / Group ID. Also covers missing config."""


class MiniMaxQuotaError(MiniMaxError):
    """HTTP 429 (rate-limited) or explicit quota status_code."""


class MiniMaxTimeoutError(MiniMaxError):
    """Request exceeded TTS_REQUEST_TIMEOUT."""


@dataclass
class MiniMaxConfig:
    api_key: str = ""
    group_id: str = ""
    voice_id: str = ""
    model: str = "speech-2.8-turbo"
    base_url: str = "https://api.minimax.io"
    language_boost: str = "Russian"
    timeout_seconds: float = 2.5
    sample_rate: int = 32000
    bitrate: int = 128000


def load_minimax_config_from_env() -> MiniMaxConfig:
    """Build a ``MiniMaxConfig`` from the standard ``MINIMAX_*`` env vars.

    Missing keys collapse to empty strings; the provider raises
    ``MiniMaxAuthError`` at call time if it is invoked without an API
    key or voice_id. This keeps the bot bootable even when only Piper
    is configured.
    """
    base_url = os.getenv("MINIMAX_BASE_URL", "https://api.minimax.io").strip()
    if not base_url:
        base_url = "https://api.minimax.io"
    model = os.getenv("MINIMAX_MODEL", "speech-2.8-turbo").strip()
    if not model:
        model = "speech-2.8-turbo"
    lang = os.getenv("MINIMAX_LANGUAGE_BOOST", "Russian").strip()
    if not lang:
        lang = "Russian"
    return MiniMaxConfig(
        api_key=os.getenv("MINIMAX_API_KEY", "").strip(),
        group_id=os.getenv("MINIMAX_GROUP_ID", "").strip(),
        voice_id=os.getenv("MINIMAX_VOICE_ID", "").strip(),
        model=model,
        base_url=base_url,
        language_boost=lang,
        timeout_seconds=float(os.getenv("TTS_REQUEST_TIMEOUT", "2.5")),
        sample_rate=int(os.getenv("MINIMAX_SAMPLE_RATE", "32000")),
        bitrate=int(os.getenv("MINIMAX_BITRATE", "128000")),
    )


def _is_valid_configured(cfg: MiniMaxConfig) -> bool:
    return bool(cfg.api_key) and bool(cfg.voice_id)


class MiniMaxProvider:
    """Calls MiniMax ``POST /v1/t2a_v2`` and writes MP3 to ``filename``.

    Per the rollout plan §11.1 (corrections feedback), this provider
    holds ONE long-lived ``httpx.AsyncClient`` with keep-alive so the
    TLS handshake is paid once per process lifetime, not once per
    message. This trims ~0.3s off every request after the first.
    """

    name = "minimax"

    def __init__(
        self,
        config: MiniMaxConfig,
        *,
        http_client: Optional["httpx.AsyncClient"] = None,
    ) -> None:
        # Imported lazily so the rest of the provider layer remains
        # usable on systems where httpx is missing (e.g. local CI runs
        # that only exercise the local provider).
        import httpx

        self._config = config
        self._owns_client = http_client is None
        # Keep-alive pool sized for the bot's modest concurrency
        # (one or two in-flight requests). The bot's queue is much
        # larger but each request is short, so 4 keep-alives is plenty.
        self._client = http_client or httpx.AsyncClient(
            timeout=httpx.Timeout(config.timeout_seconds),
            limits=httpx.Limits(max_keepalive_connections=4, max_connections=8),
        )

    async def aclose(self) -> None:
        """Gracefully close the underlying HTTP client.

        Call from the bot's ``close()`` hook. No-op when the client was
        injected (tests typically pass their own).
        """
        if self._owns_client:
            await self._client.aclose()

    def _build_url(self) -> str:
        base = self._config.base_url.rstrip("/")
        url = f"{base}/v1/t2a_v2"
        # Per MiniMax docs: if the response indicates an invalid api
        # key, retry with ?GroupId=<id>. We always add it when set so
        # the auth state matches what the user copied from the console.
        if self._config.group_id:
            url = f"{url}?GroupId={self._config.group_id}"
        return url

    def _build_body(self, text: str) -> dict:
        cfg = self._config
        return {
            "model": cfg.model,
            "text": text,
            "stream": False,
            "language_boost": cfg.language_boost,
            "output_format": "hex",
            "voice_setting": {
                "voice_id": cfg.voice_id,
                "speed": 1,
                "vol": 1,
                "pitch": 0,
            },
            "audio_setting": {
                "sample_rate": cfg.sample_rate,
                "bitrate": cfg.bitrate,
                "format": "mp3",
                "channel": 1,
            },
        }

    async def synthesize(self, text: str, filename: Path) -> None:
        cfg = self._config
        if not cfg.api_key:
            raise MiniMaxAuthError("MINIMAX_API_KEY is not set")
        if not cfg.voice_id:
            raise MiniMaxAuthError("MINIMAX_VOICE_ID is not set")

        import httpx  # lazy import, see __init__ for rationale

        url = self._build_url()
        headers = {
            "Authorization": f"Bearer {cfg.api_key}",
            "Content-Type": "application/json",
        }
        body = self._build_body(text)

        log.debug("MiniMax POST %s text=%d chars", url, len(text))

        try:
            response = await self._client.post(url, headers=headers, json=body)
        except httpx.TimeoutException as exc:
            raise MiniMaxTimeoutError(
                f"MiniMax request timed out after {cfg.timeout_seconds}s"
            ) from exc
        except httpx.HTTPError as exc:
            # Network failures, connection resets, DNS errors, etc.
            raise MiniMaxError(f"MiniMax network error: {exc}") from exc

        if response.status_code == 429:
            raise MiniMaxQuotaError("MiniMax rate-limited (HTTP 429)")

        if response.status_code >= 400:
            excerpt = response.text[:200] if response.text else ""
            raise MiniMaxError(
                f"MiniMax HTTP {response.status_code}: {excerpt}"
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise MiniMaxError(
                f"MiniMax returned non-JSON body: {response.text[:200]}"
            ) from exc

        base_resp = payload.get("base_resp") or {}
        status_code = int(base_resp.get("status_code", -1))
        status_msg = base_resp.get("status_msg", "") or ""
        if status_code != 0:
            # 'invalid api key' is the canonical case for adding
            # GroupId per MiniMax docs. Distinct exception type so the
            # bot logs a clear "auth" reason, not a generic failure.
            if "invalid api key" in status_msg.lower() or status_code in (1002, 1004):
                raise MiniMaxAuthError(
                    f"MiniMax auth failed (status_code={status_code}): {status_msg}"
                )
            raise MiniMaxError(
                f"MiniMax status_code={status_code}: {status_msg}"
            )

        data = payload.get("data") or {}
        audio_hex = data.get("audio")
        if not audio_hex:
            raise MiniMaxError("MiniMax response missing data.audio")

        try:
            audio_bytes = bytes.fromhex(audio_hex)
        except ValueError as exc:
            raise MiniMaxError(
                f"MiniMax audio field is not valid hex: {exc}"
            ) from exc

        filename.write_bytes(audio_bytes)
        log.debug(
            "MiniMaxProvider synthesized text=%d chars audio=%d bytes file=%s",
            len(text), len(audio_bytes), filename,
        )


def load_dispatcher_config_from_env() -> DispatcherConfig:
    """Build a ``DispatcherConfig`` from the standard ``TTS_*`` env vars.

    ``TTS_PRIMARY_PROVIDER`` accepts ``local`` or ``minimax``. Unknown
    values fall back to ``local`` so the bot never silently misroutes.

    Default request timeout is 2.5 seconds — see plan §11.4 for the
    RTT-based justification (typical request ≈ 1.2 sec end-to-end on a
    warm keep-alive connection; 2.5 sec is a 2x safety margin).
    """
    raw = os.getenv("TTS_PRIMARY_PROVIDER", "local").strip().lower()
    try:
        primary = PrimaryProvider(raw)
    except ValueError:
        log.warning("Unknown TTS_PRIMARY_PROVIDER=%s; falling back to local", raw)
        primary = PrimaryProvider.LOCAL
    timeout = float(os.getenv("TTS_REQUEST_TIMEOUT", "2.5"))
    return DispatcherConfig(
        primary=primary,
        request_timeout_seconds=max(0.1, timeout),
    )


class TTSDispatcher:
    """Facade that the bot calls to obtain synthesized audio.

    Routing logic (post commit 3):

    * ``primary=local`` → always local.
    * ``primary=minimax`` and cloud is configured:
      - if circuit breaker is OPEN → local (no API call attempted).
      - try cloud. On success: ``cb.record_success()``; return cloud.
      - on any error: ``cb.record_failure()``; fall back to local.
    * cloud not configured (missing key/voice_id) → local.

    The circuit breaker behavior is still the skeleton (always allows
    requests) until commit 6 wires the cooldown timer. The dispatcher
    structure, however, already consults ``allow_request()`` so commit 6
    is a one-line change in this file.
    """

    def __init__(
        self,
        local: LocalProvider,
        cloud: Optional[TTSProvider] = None,
        circuit_breaker: Optional[CircuitBreaker] = None,
        config: Optional[DispatcherConfig] = None,
    ) -> None:
        self._local = local
        self._cloud = cloud  # None when MiniMax is not configured
        self._cb = circuit_breaker or CircuitBreaker()
        self._config = config or DispatcherConfig()

    @property
    def config(self) -> DispatcherConfig:
        return self._config

    @property
    def circuit_breaker(self) -> CircuitBreaker:
        return self._cb

    @property
    def cloud(self) -> Optional[TTSProvider]:
        return self._cloud

    async def synthesize(self, text: str, filename: Path) -> str:
        """Produce audio at ``filename`` and return the provider used."""
        if (
            self._config.primary is PrimaryProvider.MINIMAX
            and self._cloud is not None
        ):
            if self._cb.allow_request():
                try:
                    await self._cloud.synthesize(text, filename)
                    self._cb.record_success()
                    return self._cloud.name
                except Exception as exc:
                    log.warning(
                        "TTS provider %s failed (%s: %s); falling back to %s",
                        self._cloud.name, type(exc).__name__, exc, self._local.name,
                    )
                    self._cb.record_failure()
            else:
                log.debug(
                    "Circuit breaker open; skipping %s, using %s",
                    self._cloud.name, self._local.name,
                )
        await self._local.synthesize(text, filename)
        return self._local.name

    async def warm_local(self, text: str, filename: Path) -> None:
        """Pre-load the local provider's resources (e.g. Piper ONNX model).

        Called from the bot's startup warmup. Bypasses ``primary`` and
        the circuit breaker on purpose: the whole point of warmup is to
        keep the local fallback hot regardless of which provider is
        currently marked primary. MiniMax is HTTP-only and does not need
        warming.
        """
        await self._local.synthesize(text, filename)
        log.debug("TTSDispatcher.warm_local completed via %s", self._local.name)