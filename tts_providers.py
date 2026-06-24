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

import hashlib
import logging
import os
import shutil
import time
from collections import OrderedDict
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

    def __init__(
        self,
        piper_synthesize: PiperSynthFn,
        *,
        default_voice_profile: str = "",
    ) -> None:
        self._piper_synthesize = piper_synthesize
        # When the caller does not pass a voice_profile (e.g. the
        # dispatcher during warmup), the wrapper uses this string as
        # the profile name. Empty string means "use the pipeline's
        # own default", which is what the underlying piper callable
        # interprets as None.
        self._default_voice_profile = default_voice_profile

    async def synthesize(
        self, text: str, filename: Path, voice_profile: Optional[str] = None
    ) -> None:
        # The bot resolves which Piper profile to use (per-user/default,
        # or the registry fallback when a cloud voice fails) and passes
        # its name here. When omitted (warmup), fall back to this
        # wrapper's configured default; empty string => pipeline default.
        profile = voice_profile if voice_profile is not None else (
            self._default_voice_profile or None
        )
        await self._piper_synthesize(text, filename, profile)
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

    State machine:
    - CLOSED: normal operation, every call hits the cloud provider.
    - OPEN: the provider has just produced ``failure_threshold``
      consecutive failures. All requests short-circuit to fallback.
    - HALF_OPEN: the cooldown (``cooldown_seconds``) since ``OPEN``
      has elapsed. The very next request is allowed through as a
      single probe; success returns the breaker to CLOSED, failure
      flips it back to OPEN with a fresh cooldown.
    """

    def __init__(
        self,
        config: Optional[CircuitBreakerConfig] = None,
        *,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        self._config = config or CircuitBreakerConfig()
        self._state: CircuitState = CircuitState.CLOSED
        self._consecutive_failures: int = 0
        self._opened_at: Optional[float] = None
        self._probe_outstanding: bool = False
        # Clock injection point so tests can fast-forward through the
        # cooldown without sleeping the real wall clock.
        self._clock = clock or time.perf_counter

    @property
    def state(self) -> CircuitState:
        return self._state

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    @property
    def cooldown_remaining(self) -> float:
        """Seconds remaining in the current cooldown, or 0.0 if not OPEN."""
        if self._state is not CircuitState.OPEN or self._opened_at is None:
            return 0.0
        elapsed = self._clock() - self._opened_at
        return max(0.0, self._config.cooldown_seconds - elapsed)

    def allow_request(self) -> bool:
        """Return True if a primary call should be attempted right now.

        - CLOSED: always True.
        - OPEN: False until cooldown elapses; then transitions to
          HALF_OPEN and returns True for exactly one probe.
        - HALF_OPEN: True for the first caller (the probe), then
          False for every subsequent caller until ``record_success``
          or ``record_failure`` resolves the state. The one-shot
          guarantee prevents two concurrent callers from both
          slipping through during the probe window.
        """
        if self._state is CircuitState.CLOSED:
            return True
        if self._state is CircuitState.HALF_OPEN:
            if self._probe_outstanding:
                # Consume the probe atomically so a second caller
                # arriving in the same event-loop tick does not also
                # see True and bypass the breaker.
                self._probe_outstanding = False
                return True
            return False
        # OPEN: maybe promote to HALF_OPEN if cooldown has elapsed.
        # The current allow_request call IS the probe — consume it
        # immediately so a second caller arriving in the same tick
        # sees _probe_outstanding=False and is blocked.
        if self.cooldown_remaining <= 0.0:
            self._state = CircuitState.HALF_OPEN
            self._probe_outstanding = False
            return True
        return False

    def record_success(self) -> None:
        self._consecutive_failures = 0
        self._state = CircuitState.CLOSED
        self._opened_at = None
        self._probe_outstanding = False

    def record_failure(self) -> None:
        self._consecutive_failures += 1
        if self._state is CircuitState.HALF_OPEN:
            # The probe failed — back to OPEN with a fresh cooldown.
            self._state = CircuitState.OPEN
            self._opened_at = self._clock()
            self._probe_outstanding = False
            return
        if self._consecutive_failures >= self._config.failure_threshold:
            self._state = CircuitState.OPEN
            self._opened_at = self._clock()
            self._probe_outstanding = False


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


# ---------------------------------------------------------------------------
# Optional LRU cache for frequently-spoken phrases (commit 7)
# ---------------------------------------------------------------------------


@dataclass
class TTSCacheConfig:
    enabled: bool = False
    max_entries: int = 128
    cache_dir: Path = Path("/dev/shm/tts_cache")


def load_cache_config_from_env() -> TTSCacheConfig:
    enabled_raw = os.getenv("TTS_CACHE_ENABLED", "0").strip().lower()
    enabled = enabled_raw in ("1", "true", "yes", "on")
    max_entries = int(os.getenv("TTS_CACHE_MAX_ENTRIES", "128"))
    cache_dir_raw = os.getenv("TTS_CACHE_DIR", "/dev/shm/tts_cache").strip()
    cache_dir = Path(cache_dir_raw) if cache_dir_raw else Path("/dev/shm/tts_cache")
    return TTSCacheConfig(
        enabled=enabled,
        max_entries=max(0, max_entries),
        cache_dir=cache_dir,
    )


class TTSPhraseCache:
    """In-memory LRU over sha256(text) -> cached audio file path.

    Skips API/Piper for repeated short phrases ("бб", "пака", "давайте",
    "gg" and similar). Cache files live in ``TTS_CACHE_DIR`` (default
    ``/dev/shm/tts_cache``); on a normal bot restart the cache is wiped
    which is fine because the next message rehydrates the hot entries.
    """

    def __init__(self, config: TTSCacheConfig) -> None:
        self._config = config
        self._entries: "OrderedDict[str, Path]" = OrderedDict()
        # Note: cache_dir is created lazily on the first store() call
        # so that operators can point TTS_CACHE_DIR at a path that
        # does not yet exist.

    @property
    def config(self) -> TTSCacheConfig:
        return self._config

    @property
    def size(self) -> int:
        return len(self._entries)

    @staticmethod
    def hash_text(text: str, voice_name: str = "") -> str:
        # The cache key MUST include the voice name: the same text spoken
        # by two different voices produces different audio, so keying on
        # text alone would return one voice's audio for the other. A NUL
        # separator keeps (voice, text) pairs unambiguous.
        payload = f"{voice_name}\x00{text}".encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def lookup(self, text: str, voice_name: str = "") -> Optional[Path]:
        """Return the cached audio path for ``(voice, text)``, or None."""
        if not self._config.enabled:
            return None
        key = self.hash_text(text, voice_name)
        cached = self._entries.get(key)
        if cached is None:
            return None
        if not cached.exists():
            # Cache file vanished (manual cleanup, /dev/shm full).
            self._entries.pop(key, None)
            return None
        # LRU touch: move to the back.
        self._entries.move_to_end(key)
        return cached

    def store(self, text: str, source_path: Path, voice_name: str = "") -> Path:
        """Copy ``source_path`` into the cache and return the cache path."""
        if not self._config.enabled:
            return source_path
        if not source_path.exists():
            raise FileNotFoundError(source_path)
        key = self.hash_text(text, voice_name)
        target = self._config.cache_dir / f"{key}.mp3"
        # Lazily create the cache directory on first store.
        self._config.cache_dir.mkdir(parents=True, exist_ok=True)
        if target.exists():
            # Already cached from a previous call — just touch the LRU.
            self._entries[key] = target
            self._entries.move_to_end(key)
            return target
        shutil.copyfile(source_path, target)
        self._entries[key] = target
        self._entries.move_to_end(key)
        # Evict oldest entries past max_entries.
        while len(self._entries) > self._config.max_entries:
            evicted_key, evicted_path = self._entries.popitem(last=False)
            try:
                evicted_path.unlink()
            except OSError:
                log.warning(
                    "TTS cache: failed to evict %s", evicted_path, exc_info=True,
                )
        return target


# ---------------------------------------------------------------------------
# MiniMax provider (added in commit 3)
# ---------------------------------------------------------------------------


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

    Tracks a session-level cumulative ``usage_characters`` counter so
    operators can monitor quota burn across bot restarts (counter
    resets only on process restart — by design, since MiniMax
    accounting is per-account, not per-process).
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
        # Cumulative chars billed this process lifetime. Logged on
        # every successful synthesis for quota monitoring.
        self._session_chars: int = 0

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

    def _build_body(
        self,
        text: str,
        *,
        voice_id: str,
        model: str,
        speed: float,
        vol: float,
        pitch: int,
        emotion: str,
        language_boost: str,
    ) -> dict:
        cfg = self._config
        voice_setting: dict = {
            "voice_id": voice_id,
            "speed": speed,
            "vol": vol,
            "pitch": pitch,
        }
        # MiniMax rejects an empty emotion; only include it when set.
        if emotion:
            voice_setting["emotion"] = emotion
        return {
            "model": model,
            "text": text,
            "stream": False,
            "language_boost": language_boost,
            "output_format": "hex",
            "voice_setting": voice_setting,
            "audio_setting": {
                "sample_rate": cfg.sample_rate,
                "bitrate": cfg.bitrate,
                "format": "mp3",
                "channel": 1,
            },
        }

    async def synthesize(
        self,
        text: str,
        filename: Path,
        *,
        voice_id: Optional[str] = None,
        model: Optional[str] = None,
        speed: Optional[float] = None,
        vol: Optional[float] = None,
        pitch: Optional[int] = None,
        emotion: Optional[str] = None,
        language_boost: Optional[str] = None,
    ) -> None:
        """Synthesize ``text`` to ``filename``.

        Voice parameters default to the env-seeded config when not passed;
        the dispatcher supplies them per-call from the registry record.
        """
        cfg = self._config
        if not cfg.api_key:
            raise MiniMaxAuthError("MINIMAX_API_KEY is not set")
        eff_voice_id = voice_id or cfg.voice_id
        if not eff_voice_id:
            raise MiniMaxAuthError("MINIMAX voice_id is not set")

        import httpx  # lazy import, see __init__ for rationale

        url = self._build_url()
        headers = {
            "Authorization": f"Bearer {cfg.api_key}",
            "Content-Type": "application/json",
        }
        body = self._build_body(
            text,
            voice_id=eff_voice_id,
            model=model or cfg.model,
            speed=1.0 if speed is None else speed,
            vol=1.0 if vol is None else vol,
            pitch=0 if pitch is None else pitch,
            emotion=emotion or "",
            language_boost=language_boost or cfg.language_boost,
        )

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
        # Track quota usage. usage_characters is the number MiniMax
        # actually billed for this request (may differ slightly from
        # len(text) due to language_boost normalization).
        usage = 0
        extra = payload.get("extra_info") or {}
        if isinstance(extra, dict):
            try:
                usage = int(extra.get("usage_characters") or 0)
            except (TypeError, ValueError):
                usage = 0
        self._session_chars += usage
        log.info(
            "MiniMax usage chars=%d session_total=%d text_len=%d audio_bytes=%d",
            usage, self._session_chars, len(text), len(audio_bytes),
        )
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

    Cache (post commit 7): if enabled, the dispatcher checks the cache
    BEFORE invoking any provider and short-circuits to a copy on hit.
    Cache misses go through the normal provider chain, then the
    resulting audio is stored in the cache for next time.
    """

    def __init__(
        self,
        local: LocalProvider,
        cloud: Optional[TTSProvider] = None,
        circuit_breaker: Optional[CircuitBreaker] = None,
        config: Optional[DispatcherConfig] = None,
        cache: Optional[TTSPhraseCache] = None,
        fallback_profile: str = "",
    ) -> None:
        self._local = local
        self._cloud = cloud  # None when MiniMax is not configured
        self._cb = circuit_breaker or CircuitBreaker()
        self._config = config or DispatcherConfig()
        self._cache = cache  # None means caching disabled
        # Piper profile name to fall back to when a cloud (MiniMax) voice
        # fails. Sourced from the registry's fallback_profile.
        self._fallback_profile = fallback_profile

    @property
    def config(self) -> DispatcherConfig:
        return self._config

    @property
    def circuit_breaker(self) -> CircuitBreaker:
        return self._cb

    @property
    def cloud(self) -> Optional[TTSProvider]:
        return self._cloud

    @property
    def cache(self) -> Optional[TTSPhraseCache]:
        return self._cache

    async def synthesize(self, text: str, filename: Path, voice=None) -> str:
        """Produce audio at ``filename`` and return the provider used.

        ``voice`` is a registry ``VoiceRecord`` (or None). Its ``provider``
        decides routing: a ``minimax`` record goes to the cloud with the
        record's voice_id/params; a ``piper`` record goes to local Piper
        with the record's profile name. A cloud failure falls back to the
        registry ``fallback_profile`` (Piper).
        """
        # Cache key must include the voice so two voices never collide.
        voice_key = getattr(voice, "name", "") if voice is not None else ""

        # 1. Cache hit short-circuits everything.
        if self._cache is not None:
            cached = self._cache.lookup(text, voice_key)
            if cached is not None:
                shutil.copyfile(cached, filename)
                log.debug("TTS cache HIT text=%d chars voice=%s", len(text), voice_key)
                return "cache"

        provider = getattr(voice, "provider", None) if voice is not None else None

        # 2. Cloud (MiniMax) path: when the record is a minimax voice, or
        #    (no record) the env default is minimax. Requires a configured
        #    cloud provider.
        want_minimax = (
            provider == "minimax"
            or (voice is None and self._config.primary is PrimaryProvider.MINIMAX)
        ) and self._cloud is not None

        if want_minimax:
            if self._cb.allow_request():
                try:
                    mm = getattr(voice, "minimax", None) if voice is not None else None
                    if mm is not None:
                        await self._cloud.synthesize(
                            text, filename,
                            voice_id=mm.voice_id, model=mm.model,
                            speed=mm.speed, vol=mm.vol, pitch=mm.pitch,
                            emotion=mm.emotion, language_boost=mm.language_boost,
                        )
                    else:
                        await self._cloud.synthesize(text, filename)
                    self._cb.record_success()
                    self._maybe_cache(text, filename, voice_key)
                    return self._cloud.name
                except Exception as exc:
                    log.warning(
                        "TTS provider %s failed (%s: %s); falling back to %s",
                        self._cloud.name, type(exc).__name__, exc, self._local.name,
                    )
                    self._cb.record_failure()
            else:
                log.debug(
                    "Circuit breaker open; skipping %s, using fallback Piper",
                    self._cloud.name,
                )

        # 3. Local (Piper) path. Pick the profile name: the requested piper
        #    voice, or the registry fallback when a cloud voice was wanted.
        if provider == "piper":
            fallback_name = getattr(voice, "name", None)
        elif provider == "minimax" or want_minimax:
            fallback_name = self._fallback_profile or None
        else:
            fallback_name = None
        await self._local.synthesize(text, filename, fallback_name)
        # Cache regardless of provider: when TTS_CACHE_ENABLED=1 the
        # operator has opted in, and a cache hit on a repeated short
        # phrase is a win whether the underlying provider is Piper
        # or MiniMax (the local file copy is faster than even Piper).
        self._maybe_cache(text, filename, voice_key)
        return self._local.name

    def _maybe_cache(self, text: str, filename: Path, voice_key: str = "") -> None:
        if self._cache is None:
            return
        try:
            self._cache.store(text, filename, voice_key)
        except OSError as exc:
            # /dev/shm full or read-only mount — log and continue.
            log.warning("TTS cache store failed: %s", exc)

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