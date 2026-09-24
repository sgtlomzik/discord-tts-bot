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
import json
import logging
import os
import shutil
import uuid
import time
from collections import OrderedDict
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    AsyncIterator,
    Awaitable,
    Callable,
    Optional,
    Protocol,
)

from ttsbot.fish import FishProvider

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
    FISH = "fish"


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


class MiniMaxVoiceNotFoundError(MiniMaxError):
    """status_code 2054 — the requested voice_id does not exist."""


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


_DEFAULT_CACHE_DIR = "/app/data/tts_cache"
_DEFAULT_CACHE_MAX_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB


@dataclass
class TTSCacheConfig:
    enabled: bool = False
    # Primary eviction policy is by total bytes on disk (hard cap). A
    # non-zero ``max_entries`` adds an optional secondary count cap (0 =
    # unlimited count, rely on bytes).
    max_bytes: int = _DEFAULT_CACHE_MAX_BYTES
    max_entries: int = 0
    cache_dir: Path = Path(_DEFAULT_CACHE_DIR)


def load_cache_config_from_env() -> TTSCacheConfig:
    enabled_raw = os.getenv("TTS_CACHE_ENABLED", "0").strip().lower()
    enabled = enabled_raw in ("1", "true", "yes", "on")
    max_bytes = int(os.getenv("TTS_CACHE_MAX_BYTES", str(_DEFAULT_CACHE_MAX_BYTES)))
    max_entries = int(os.getenv("TTS_CACHE_MAX_ENTRIES", "0"))
    cache_dir_raw = os.getenv("TTS_CACHE_DIR", _DEFAULT_CACHE_DIR).strip()
    cache_dir = Path(cache_dir_raw) if cache_dir_raw else Path(_DEFAULT_CACHE_DIR)
    return TTSCacheConfig(
        enabled=enabled,
        max_bytes=max(0, max_bytes),
        max_entries=max(0, max_entries),
        cache_dir=cache_dir,
    )


class TTSPhraseCache:
    """LRU cache over sha256(voice + text) -> cached audio file path.

    Skips API/Piper for repeated short phrases ("бб", "пака", "давайте",
    "gg" and similar). Cache files live in ``TTS_CACHE_DIR`` (default
    ``/app/data/tts_cache`` — disk, in the mounted volume) so hot phrases
    survive a restart: the index is rehydrated from the existing files at
    startup. Eviction is by total bytes (hard cap ``TTS_CACHE_MAX_BYTES``,
    default 2 GiB), LRU order, with an optional secondary count cap.
    """

    def __init__(self, config: TTSCacheConfig) -> None:
        self._config = config
        self._entries: "OrderedDict[str, Path]" = OrderedDict()
        self._sizes: dict[str, int] = {}
        self._total_bytes = 0
        # Lightweight hit/miss counters so operators can measure the real
        # hit-rate on live traffic (logged every _LOG_EVERY lookups).
        self._hits = 0
        self._misses = 0
        self._LOG_EVERY = 100
        if self._config.enabled:
            self._load_existing()

    def _load_existing(self) -> None:
        """Rehydrate the index from cache files left by a previous run."""
        cache_dir = self._config.cache_dir
        if not cache_dir.exists():
            return
        try:
            files = [p for suffix in ("mp3", "opus", "dopus") for p in cache_dir.glob(f"*.{suffix}") if p.is_file()]
        except OSError:
            log.warning("TTS cache: failed to scan %s", cache_dir, exc_info=True)
            return
        # Remove orphan ".part" files left by an interrupted streaming write.
        for part in list(cache_dir.glob("*.part")) + list(cache_dir.glob("*.tmp")):
            try:
                part.unlink()
            except OSError:
                pass
        # Oldest first so LRU order roughly reflects last use across restarts.
        files.sort(key=lambda p: p.stat().st_mtime)
        for path in files:
            key = path.stem
            try:
                size = path.stat().st_size
            except OSError:
                continue
            self._entries[key] = path
            self._sizes[key] = size
            self._total_bytes += size
        # Enforce the byte cap immediately in case the limit shrank.
        self._evict_to_fit()
        if self._entries:
            log.info(
                "TTS cache rehydrated entries=%d bytes=%d from %s",
                len(self._entries), self._total_bytes, cache_dir,
            )

    @property
    def hits(self) -> int:
        return self._hits

    @property
    def misses(self) -> int:
        return self._misses

    def _record(self, hit: bool) -> None:
        if hit:
            self._hits += 1
        else:
            self._misses += 1
        total = self._hits + self._misses
        if total % self._LOG_EVERY == 0:
            log.info(
                "TTS cache stats hits=%d misses=%d hit_rate=%.1f%% entries=%d",
                self._hits, self._misses, 100.0 * self._hits / total, len(self._entries),
            )

    @property
    def config(self) -> TTSCacheConfig:
        return self._config

    @property
    def size(self) -> int:
        return len(self._entries)

    @property
    def total_bytes(self) -> int:
        return self._total_bytes

    def _drop(self, key: str) -> None:
        """Remove a key from the index + size accounting (no unlink)."""
        self._entries.pop(key, None)
        self._total_bytes -= self._sizes.pop(key, 0)

    def _evict_to_fit(self) -> None:
        """Evict LRU entries until within the byte (and optional count) caps."""
        cfg = self._config
        while self._entries and (
            self._total_bytes > cfg.max_bytes
            or (cfg.max_entries > 0 and len(self._entries) > cfg.max_entries)
        ):
            evicted_key, evicted_path = self._entries.popitem(last=False)
            self._total_bytes -= self._sizes.pop(evicted_key, 0)
            try:
                evicted_path.unlink()
            except OSError:
                log.warning("TTS cache: failed to evict %s", evicted_path, exc_info=True)

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
            self._record(False)
            return None
        if not cached.exists():
            # Cache file vanished (manual cleanup, disk full).
            self._drop(key)
            self._record(False)
            return None
        # LRU touch: move to the back.
        self._entries.move_to_end(key)
        self._record(True)
        return cached

    def store(self, text: str, source_path: Path, voice_name: str = "", suffix: str | None = None) -> Path:
        """Copy ``source_path`` into the cache and return the cache path."""
        if not self._config.enabled:
            return source_path
        if not source_path.exists():
            raise FileNotFoundError(source_path)
        suffix = suffix or ("opus" if source_path.suffix == ".opus" else "mp3")
        return self._store_key(self.hash_text(text, voice_name), source_path, suffix)

    def cache_path_for(self, text: str, voice_name: str = "", suffix: str = "mp3") -> Path:
        """Return the on-disk path a (voice, text) pair would cache to.

        Used by the streaming path to write chunks directly to the final
        cache file (then ``commit_file`` registers it).
        """
        if suffix not in {"mp3", "opus", "dopus"}:
            raise ValueError(f"Unsupported cache suffix: {suffix}")
        return self._config.cache_dir / f"{self.hash_text(text, voice_name)}.{suffix}"

    def commit_file(self, text: str, cache_file: Path, voice_name: str = "") -> Optional[Path]:
        """Register an already-written cache file (e.g. from streaming).

        Returns the registered path, or None if caching is disabled / the
        file is missing. The file must already live under ``cache_dir`` with
        the canonical name from ``cache_path_for``.
        """
        if not self._config.enabled:
            return None
        if not cache_file.exists():
            return None
        key = self.hash_text(text, voice_name)
        return self._register(key, cache_file)

    def _store_key(self, key: str, source_path: Path, suffix: str = "mp3") -> Path:
        target = self._config.cache_dir / f"{key}.{suffix}"
        self._config.cache_dir.mkdir(parents=True, exist_ok=True)
        if target != source_path:
            temporary = self._config.cache_dir / f"{key}.{uuid.uuid4().hex}.tmp"
            try:
                shutil.copyfile(source_path, temporary)
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
        return self._register(key, target)

    def _register(self, key: str, target: Path) -> Path:
        try:
            size = target.stat().st_size
        except OSError:
            return target
        # Skip files larger than the whole cap (would churn-evict everything).
        if self._config.max_bytes and size > self._config.max_bytes:
            return target
        # Update accounting (replace if the key already existed).
        self._total_bytes += size - self._sizes.get(key, 0)
        self._sizes[key] = size
        self._entries[key] = target
        self._entries.move_to_end(key)
        self._evict_to_fit()
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


# MiniMax t2a emotion values (besides "" = omit / provider default).
MINIMAX_EMOTIONS = (
    "neutral", "happy", "sad", "angry", "fearful", "disgusted", "surprised",
)


def derive_auto_emotion(text: str) -> str:
    """Heuristic per-message emotion from punctuation/case.

    Used when a voice's emotion is set to ``"auto"``. It is a pure function of
    the (already normalized) text, which is part of the cache key — so the
    same text always maps to the same emotion and the cache stays consistent.
    Returns one of MINIMAX_EMOTIONS, or "" for neutral/no emotion.
    """
    if not text:
        return ""
    stripped = text.strip()
    letters = [c for c in stripped if c.isalpha()]
    if len(letters) >= 4 and all(c.isupper() for c in letters):
        return "angry"  # SHOUTING
    if "?!" in stripped or "!?" in stripped:
        return "surprised"
    if stripped.endswith("!"):
        return "happy"
    return ""


def resolve_emotion(emotion: str, text: str) -> str:
    """Translate the ``"auto"`` sentinel into a concrete emotion for the given
    text; pass any other value through unchanged."""
    return derive_auto_emotion(text) if emotion == "auto" else emotion


def voice_cache_key(voice) -> str:
    """Cache key fragment for a voice record.

    A piper voice is keyed by name. A MiniMax voice also folds in the params
    that change the audio (speed/vol/pitch/emotion/model) so that re-tuning a
    voice produces a fresh key — the old entry is no longer served and evicts
    by LRU. ``emotion="auto"`` is a constant here; the concrete emotion is
    derived from the text, which is already part of the full cache key.
    """
    if voice is None:
        return ""
    name = getattr(voice, "name", "")
    mm = getattr(voice, "minimax", None)
    if getattr(voice, "provider", None) == "minimax" and mm is not None:
        return "|".join(
            str(x) for x in (name, mm.speed, mm.vol, mm.pitch, mm.emotion, mm.model)
        )
    return name


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

    @property
    def session_chars(self) -> int:
        """Cumulative MiniMax characters billed since process start."""
        return self._session_chars

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
        # "auto" -> a concrete emotion derived from this text (cache-safe).
        emotion = resolve_emotion(emotion, text)
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
            if status_code == 2054:
                raise MiniMaxVoiceNotFoundError(
                    f"MiniMax voice id not exist (status_code=2054): {status_msg}"
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

    def _build_clone_url(self, path: str) -> str:
        base = self._config.base_url.rstrip("/")
        url = f"{base}{path}"
        if self._config.group_id:
            url = f"{url}?GroupId={self._config.group_id}"
        return url

    async def clone_voice(
        self,
        sample: bytes,
        *,
        voice_id: str,
        filename: str = "sample.mp3",
        model: str = "speech-2.8-hd",
    ) -> None:
        """Upload ``sample`` audio and trigger a MiniMax voice clone.

        Two calls, mirroring ``scripts/clone_voice.py`` but reusing this
        provider's keep-alive client: ``POST /v1/files/upload`` (multipart,
        ``purpose=voice_clone``) to obtain an integer ``file_id``, then
        ``POST /v1/voice_clone`` to register ``voice_id``. Both override the
        short realtime-synth timeout with a generous 60s budget — upload and
        the server-side clone are far slower than a t2a request. Raises a
        ``MiniMaxError`` subclass on any failure so the caller can surface a
        precise reason; on success ``voice_id`` is immediately usable via
        :meth:`synthesize`.
        """
        cfg = self._config
        if not cfg.api_key:
            raise MiniMaxAuthError("MINIMAX_API_KEY is not set")
        if not sample:
            raise MiniMaxError("empty audio sample")
        size_mb = len(sample) / (1024 * 1024)
        if size_mb > 20:
            raise MiniMaxError(
                f"sample is {size_mb:.1f} MB; MiniMax limit is 20 MB"
            )

        import httpx  # lazy import, see __init__ for rationale

        # Upload + server-side clone are slow relative to a realtime synth;
        # override the short keep-alive timeout for these two calls only.
        timeout = httpx.Timeout(60.0, connect=10.0)
        auth = {"Authorization": f"Bearer {cfg.api_key}"}

        # --- 1) upload the sample, get an integer file_id --------------
        upload_url = self._build_clone_url("/v1/files/upload")
        files = {"file": (filename, sample, "application/octet-stream")}
        try:
            resp = await self._client.post(
                upload_url,
                headers=auth,
                files=files,
                data={"purpose": "voice_clone"},
                timeout=timeout,
            )
        except httpx.TimeoutException as exc:
            raise MiniMaxTimeoutError("MiniMax upload timed out") from exc
        except httpx.HTTPError as exc:
            raise MiniMaxError(f"MiniMax upload network error: {exc}") from exc
        if resp.status_code >= 400:
            raise MiniMaxError(
                f"MiniMax upload HTTP {resp.status_code}: {resp.text[:200]}"
            )
        try:
            payload = resp.json()
        except ValueError as exc:
            raise MiniMaxError(
                f"MiniMax upload returned non-JSON: {resp.text[:200]}"
            ) from exc
        base_resp = payload.get("base_resp") or {}
        status_code = int(base_resp.get("status_code", 0) or 0)
        if status_code != 0:
            self._raise_for_status_code(
                status_code, base_resp.get("status_msg", "") or ""
            )
        file_id = (payload.get("file") or {}).get("file_id") or payload.get("file_id")
        if not file_id:
            raise MiniMaxError(f"MiniMax upload missing file_id: {payload}")

        # --- 2) trigger the clone --------------------------------------
        clone_url = self._build_clone_url("/v1/voice_clone")
        body = {
            "file_id": int(file_id),  # string file_id -> 2013 invalid params
            "voice_id": voice_id,
            "model": model,
        }
        headers = {**auth, "Content-Type": "application/json"}
        try:
            resp = await self._client.post(
                clone_url, headers=headers, json=body, timeout=timeout
            )
        except httpx.TimeoutException as exc:
            raise MiniMaxTimeoutError("MiniMax voice_clone timed out") from exc
        except httpx.HTTPError as exc:
            raise MiniMaxError(
                f"MiniMax voice_clone network error: {exc}"
            ) from exc
        if resp.status_code >= 400:
            raise MiniMaxError(
                f"MiniMax voice_clone HTTP {resp.status_code}: {resp.text[:200]}"
            )
        try:
            payload = resp.json()
        except ValueError as exc:
            raise MiniMaxError(
                f"MiniMax voice_clone returned non-JSON: {resp.text[:200]}"
            ) from exc
        base_resp = payload.get("base_resp") or {}
        status_code = int(base_resp.get("status_code", -1))
        if status_code != 0:
            self._raise_for_status_code(
                status_code, base_resp.get("status_msg", "") or ""
            )
        log.info(
            "MiniMax voice cloned voice_id=%s model=%s file_id=%s sample_bytes=%d",
            voice_id, model, file_id, len(sample),
        )

    def _raise_for_status_code(self, status_code: int, status_msg: str) -> None:
        """Map a non-zero MiniMax status_code to a precise exception."""
        if "invalid api key" in status_msg.lower() or status_code in (1002, 1004):
            raise MiniMaxAuthError(
                f"MiniMax auth failed (status_code={status_code}): {status_msg}"
            )
        if status_code == 2054:
            raise MiniMaxVoiceNotFoundError(
                f"MiniMax voice id not exist (status_code=2054): {status_msg}"
            )
        raise MiniMaxError(f"MiniMax status_code={status_code}: {status_msg}")

    async def stream_audio(
        self,
        text: str,
        *,
        voice_id: Optional[str] = None,
        model: Optional[str] = None,
        speed: Optional[float] = None,
        vol: Optional[float] = None,
        pitch: Optional[int] = None,
        emotion: Optional[str] = None,
        language_boost: Optional[str] = None,
    ) -> AsyncIterator[bytes]:
        """Yield decoded MP3 chunks from a streaming ``/v1/t2a_v2`` call.

        Uses ``stream: true`` + ``stream_options.exclude_aggregated_audio``
        so the final SSE event does NOT re-send the full clip (otherwise the
        audio would be duplicated). Each ``data:`` event carries a hex chunk
        in ``data.audio``; the final event (``data.status == 2``) carries
        ``extra_info.usage_characters``.

        Errors raised BEFORE the first yielded chunk let the caller fall back
        cleanly to Piper. An error event mid-stream raises as well; the caller
        must handle a partial stream (no clean rollback once audio is playing).
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
        body["stream"] = True
        # Without this the final event repeats the entire clip -> doubled audio.
        body["stream_options"] = {"exclude_aggregated_audio": True}

        log.debug("MiniMax STREAM POST %s text=%d chars", url, len(text))

        # The first-chunk (TTFA) budget is enforced by the caller. Here we
        # only need a generous per-read (inter-chunk) timeout so a long
        # message that legitimately streams for seconds is not truncated by
        # the short default request timeout; gaps between chunks are tiny in
        # practice, so this just guards against a fully stalled connection.
        stream_timeout = httpx.Timeout(
            connect=cfg.timeout_seconds,
            read=max(cfg.timeout_seconds, 10.0),
            write=cfg.timeout_seconds,
            pool=cfg.timeout_seconds,
        )

        usage = 0
        chunks = 0
        try:
            async with self._client.stream(
                "POST", url, headers=headers, json=body, timeout=stream_timeout
            ) as response:
                if response.status_code == 429:
                    raise MiniMaxQuotaError("MiniMax rate-limited (HTTP 429)")
                if response.status_code >= 400:
                    excerpt = (await response.aread())[:200].decode("utf-8", "replace")
                    raise MiniMaxError(
                        f"MiniMax HTTP {response.status_code}: {excerpt}"
                    )
                async for line in response.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    payload_str = line[len("data:"):].strip()
                    if not payload_str:
                        continue
                    try:
                        payload = json.loads(payload_str)
                    except ValueError:
                        continue
                    base_resp = payload.get("base_resp") or {}
                    sc = int(base_resp.get("status_code", 0) or 0)
                    if sc != 0:
                        self._raise_for_status_code(
                            sc, base_resp.get("status_msg", "") or ""
                        )
                    extra = payload.get("extra_info") or {}
                    if isinstance(extra, dict) and extra.get("usage_characters"):
                        try:
                            usage = int(extra.get("usage_characters") or 0)
                        except (TypeError, ValueError):
                            pass
                    data = payload.get("data") or {}
                    audio_hex = data.get("audio")
                    if audio_hex:
                        try:
                            chunk = bytes.fromhex(audio_hex)
                        except ValueError:
                            continue
                        if chunk:
                            chunks += 1
                            yield chunk
        except httpx.TimeoutException as exc:
            raise MiniMaxTimeoutError(
                f"MiniMax stream timed out after {cfg.timeout_seconds}s"
            ) from exc
        except httpx.HTTPError as exc:
            raise MiniMaxError(f"MiniMax stream network error: {exc}") from exc
        finally:
            if usage:
                self._session_chars += usage
                log.info(
                    "MiniMax usage (stream) chars=%d session_total=%d text_len=%d chunks=%d",
                    usage, self._session_chars, len(text), chunks,
                )


def load_dispatcher_config_from_env() -> DispatcherConfig:
    """Build a ``DispatcherConfig`` from the standard ``TTS_*`` env vars.

    ``TTS_PRIMARY_PROVIDER`` accepts ``local``, ``minimax`` or ``fish``. Unknown
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
        fish: Optional[FishProvider] = None,
        circuit_breaker: Optional[CircuitBreaker] = None,
        config: Optional[DispatcherConfig] = None,
        cache: Optional[TTSPhraseCache] = None,
        fallback_profile: str = "",
    ) -> None:
        self._local = local
        self._cloud = cloud  # None when MiniMax is not configured
        self._fish = fish
        self._cb = circuit_breaker or CircuitBreaker()
        self._fish_cb = load_circuit_breaker_from_env()
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
    def fish(self) -> Optional[FishProvider]:
        return self._fish

    @property
    def fish_circuit_breaker(self) -> CircuitBreaker:
        return self._fish_cb

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
        # Cache key must include the voice so two voices never collide, plus
        # the MiniMax params so re-tuning a voice (speed/pitch/emotion/vol)
        # yields a fresh key instead of serving stale audio.
        provider = getattr(voice, "provider", None) if voice is not None else None
        want_fish = (
            provider == "fish" or
            (voice is None and self._config.primary is PrimaryProvider.FISH)
        ) and self._fish is not None
        fish_ref = (
            getattr(getattr(voice, "fish", None), "reference_id", "")
            or (self._fish.config.reference_id if self._fish is not None else "")
        )
        fish_cfg = self._fish.config if want_fish else None
        voice_key = (
            fish_cfg.cache_key(fish_ref, getattr(voice, "fish", None))
            if want_fish else voice_cache_key(voice)
        )

        # 1. Cache hit short-circuits everything.
        if self._cache is not None:
            cached = self._cache.lookup(text, voice_key)
            if cached is not None:
                shutil.copyfile(cached, filename)
                log.debug("TTS cache HIT text=%d chars voice=%s", len(text), voice_key)
                return "cache"

        if want_fish and self._fish_cb.allow_request():
            try:
                await self._fish.synthesize(
                    text, filename, reference_id=fish_ref, params=getattr(voice, "fish", None),
                    request_config=fish_cfg,
                )
                self._fish_cb.record_success()
                self._maybe_cache(text, filename, voice_key, suffix="opus")
                return "fish"
            except Exception as exc:
                self._fish_cb.record_failure()
                log.warning("Fish synthesis failed (%s: %s); falling back to Piper", type(exc).__name__, exc)

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
        elif provider in {"minimax", "fish"} or want_minimax or want_fish:
            fallback_name = self._fallback_profile or None
        else:
            fallback_name = None
        await self._local.synthesize(text, filename, fallback_name)
        # Cache regardless of provider: when TTS_CACHE_ENABLED=1 the
        # operator has opted in, and a cache hit on a repeated short
        # phrase is a win whether the underlying provider is Piper
        # or MiniMax (the local file copy is faster than even Piper).
        if not want_fish:
            self._maybe_cache(text, filename, voice_key)
        return self._local.name

    def _maybe_cache(self, text: str, filename: Path, voice_key: str = "", suffix: str | None = None) -> None:
        if self._cache is None:
            return
        try:
            self._cache.store(text, filename, voice_key, suffix=suffix)
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
