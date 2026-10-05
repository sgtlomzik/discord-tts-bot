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
* ``CircuitBreaker`` (ttsbot.health) — pauses a provider according to why
  it failed, so a flaky or exhausted cloud API does not stall the bot.
* ``TTSDispatcher`` — facade that speaks a message with a voice or the
  next voice of its fallback chain, and checks paused providers.

All providers satisfy the ``TTSProvider`` protocol: ``async def
synthesize(text, filename) -> None`` that writes a valid audio file at
``filename``. The bot's downstream ``ffmpeg``-based PCM pipeline is
format-agnostic (it re-encodes whatever the provider produced), so a
provider can safely emit WAV or MP3 as long as it is a valid container.
"""

from __future__ import annotations

import asyncio
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

from ttsbot.errors import AuthError, QuotaExhaustedError, RateLimitError, RequestError
from ttsbot import config as bot_config
from ttsbot.health import (  # noqa: F401  (CircuitState is re-exported)
    LONG_KINDS,
    CircuitBreaker,
    CircuitBreakerConfig,
    CircuitState,
    FailureKind,
    HealthStore,
    classify_failure,
    fingerprint,
)
from ttsbot.elevenlabs import ElevenLabsProvider
from ttsbot.fish import FishProvider
from ttsbot.gemini import GeminiProvider, write_wav
from ttsbot.httpclient import attach_warmer, provider_limits
from ttsbot.pcm import apply_gain, pcm_cache_header, read_pcm_cache

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
# Circuit breaker: lives in ttsbot.health, re-exported here for callers
# ---------------------------------------------------------------------------


def load_circuit_breaker_from_env() -> CircuitBreaker:
    """Build a CircuitBreaker from the ``CB_*`` and ``TTS_*_RECHECK`` env vars."""
    def number(name: str, default: str) -> float:
        return float(os.getenv(name, "").strip() or default)

    return CircuitBreaker(
        CircuitBreakerConfig(
            failure_threshold=max(1, int(number("CB_FAILURE_THRESHOLD", "3"))),
            cooldown_seconds=max(0.0, number("CB_COOLDOWN_SECONDS", "60")),
            backoff_factor=max(1.0, number("CB_BACKOFF_FACTOR", "2")),
            max_cooldown_seconds=max(0.0, number("CB_MAX_COOLDOWN_SECONDS", "900")),
            auth_recheck_seconds=max(60.0, number("TTS_AUTH_RECHECK_SECONDS", "3600")),
            max_recheck_seconds=max(60.0, number("TTS_MAX_RECHECK_SECONDS", "86400")),
        )
    )


# ---------------------------------------------------------------------------
# Dispatcher (skeleton: routes everything to the local provider)
# ---------------------------------------------------------------------------


class PrimaryProvider(str, Enum):
    LOCAL = "local"
    MINIMAX = "minimax"
    FISH = "fish"
    GEMINI = "gemini"
    ELEVENLABS = "elevenlabs"


@dataclass
class DispatcherConfig:
    primary: PrimaryProvider = PrimaryProvider.LOCAL
    request_timeout_seconds: float = 2.5
    # How long to stop calling a provider after a balance/plan error.
    quota_cooldown_seconds: float = 1800.0


# ---------------------------------------------------------------------------
# MiniMax provider (added in commit 3)
# ---------------------------------------------------------------------------


class MiniMaxError(Exception):
    """Base class for MiniMax failures that should trigger fallback.

    Subclasses let the bot log a precise reason without parsing strings.
    """


class MiniMaxAuthError(MiniMaxError, AuthError):
    """Invalid API key / Group ID. Also covers missing config."""


class MiniMaxQuotaError(MiniMaxError, RateLimitError):
    """HTTP 429 (rate-limited) or explicit quota status_code."""


class MiniMaxQuotaExhaustedError(MiniMaxQuotaError, QuotaExhaustedError):
    """Balance or plan exhausted: retrying soon is pointless."""


# 1008 insufficient balance, 2056 Token Plan usage limit.
MINIMAX_EXHAUSTED_STATUS_CODES = frozenset({1008, 2056})
MINIMAX_RATE_LIMIT_STATUS_CODES = frozenset({1039})


def _base_resp_status(payload: object, missing: int = -1) -> tuple[int, str]:
    """Return ``(status_code, status_msg)`` from a MiniMax JSON body.

    ``missing`` is used when there is no ``base_resp``/``status_code`` at all
    (streamed audio events omit it); a malformed code maps to -1.
    """
    base_resp = payload.get("base_resp") if isinstance(payload, dict) else None
    if not isinstance(base_resp, dict) or base_resp.get("status_code") is None:
        return missing, ""
    try:
        code = int(base_resp["status_code"])
    except (TypeError, ValueError):
        code = -1
    return code, str(base_resp.get("status_msg") or "")


class MiniMaxTimeoutError(MiniMaxError):
    """Request exceeded TTS_REQUEST_TIMEOUT."""


class MiniMaxVoiceNotFoundError(MiniMaxError, RequestError):
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
# mp3: MiniMax, opus: Fish/ElevenLabs Ogg, dopus: Discord-ready Opus frames,
# pcm: Gemini/ElevenLabs raw PCM with a pcm_cache_header.
CACHE_SUFFIXES = ("mp3", "opus", "dopus", "pcm")
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
            files = [p for suffix in CACHE_SUFFIXES for p in cache_dir.glob(f"*.{suffix}") if p.is_file()]
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
            size = self._sizes.pop(evicted_key, 0)
            self._total_bytes -= size
            try:
                evicted_path.unlink()
            except OSError:
                log.warning("TTS cache: failed to evict %s", evicted_path, exc_info=True)
            else:
                log.info(
                    "TTS cache evicted file=%s bytes=%d entries=%d total_bytes=%d",
                    evicted_path.name, size, len(self._entries), self._total_bytes,
                )

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
            log.debug("TTS cache miss chars=%d voice=%s", len(text), voice_name[:40])
            return None
        if not cached.exists():
            # Cache file vanished (manual cleanup, disk full).
            self._drop(key)
            self._record(False)
            log.info("TTS cache file vanished file=%s; regenerating", cached.name)
            return None
        # LRU touch: move to the back.
        self._entries.move_to_end(key)
        self._record(True)
        log.info(
            "TTS cache hit file=%s bytes=%d chars=%d hits=%d misses=%d",
            cached.name, self._sizes.get(key, 0), len(text), self._hits, self._misses,
        )
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
        if suffix not in CACHE_SUFFIXES:
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

    def store_bytes(self, text: str, data: bytes, voice_name: str = "", suffix: str = "pcm") -> Optional[Path]:
        """Atomically write ``data`` as the cache entry for ``(voice, text)``."""
        if not self._config.enabled:
            return None
        target = self.cache_path_for(text, voice_name, suffix)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.parent / f"{target.stem}.{uuid.uuid4().hex}.tmp"
        try:
            temporary.write_bytes(data)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return self._register(self.hash_text(text, voice_name), target)

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
        log.info(
            "TTS cache stored file=%s bytes=%d entries=%d total_bytes=%d",
            target.name, size, len(self._entries), self._total_bytes,
        )
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
            limits=provider_limits(max_connections=8, max_keepalive_connections=4),
        )
        # This client has no base_url, so the warm-up targets the API host.
        self.warmer = attach_warmer(self._client, "MiniMax", config.base_url.rstrip("/") + "/")
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

        status_code, status_msg = _base_resp_status(payload)
        if status_code != 0:
            # 'invalid api key' is the canonical case for adding
            # GroupId per MiniMax docs. Distinct exception type so the
            # bot logs a clear "auth" reason, not a generic failure.
            if "invalid api key" in status_msg.lower() or status_code in (1002, 1004):
                raise MiniMaxAuthError(
                    f"MiniMax auth failed (status_code={status_code}): {status_msg}"
                )
            self._raise_for_status_code(status_code, status_msg)

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
        status_code, status_msg = _base_resp_status(payload, missing=0)
        if status_code != 0:
            self._raise_for_status_code(status_code, status_msg)
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
        status_code, status_msg = _base_resp_status(payload)
        if status_code != 0:
            self._raise_for_status_code(status_code, status_msg)
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
        if status_code in MINIMAX_EXHAUSTED_STATUS_CODES:
            raise MiniMaxQuotaExhaustedError(
                f"MiniMax quota exhausted (status_code={status_code}): {status_msg}"
            )
        if status_code in MINIMAX_RATE_LIMIT_STATUS_CODES:
            raise MiniMaxQuotaError(
                f"MiniMax rate-limited (status_code={status_code}): {status_msg}"
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
                # Errors (quota, auth, bad voice) arrive as a plain JSON body,
                # possibly pretty-printed, instead of SSE events. Collect such
                # lines rather than trusting the Content-Type header.
                body_lines: list[str] = []
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        if line.strip() and not line.startswith((":", "event:", "id:", "retry:")):
                            body_lines.append(line)
                        continue
                    payload_str = line[len("data:"):].strip()
                    if not payload_str:
                        continue
                    try:
                        payload = json.loads(payload_str)
                    except ValueError:
                        continue
                    if not isinstance(payload, dict):
                        continue
                    sc, status_msg = _base_resp_status(payload, missing=0)
                    if sc != 0:
                        self._raise_for_status_code(sc, status_msg)
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
                if not chunks and body_lines:
                    raw = "\n".join(body_lines)
                    try:
                        payload = json.loads(raw)
                    except ValueError as exc:
                        raise MiniMaxError(
                            f"MiniMax returned non-SSE body: {raw[:200]!r}"
                        ) from exc
                    sc, status_msg = _base_resp_status(payload)
                    if sc != 0:
                        self._raise_for_status_code(sc, status_msg)
                    raise MiniMaxError("MiniMax returned JSON instead of an audio stream")
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

    ``TTS_PRIMARY_PROVIDER`` accepts ``local``, ``minimax``, ``fish``, ``gemini`` or
    ``elevenlabs``. Unknown
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
        quota_cooldown_seconds=max(0.0, float(os.getenv(
            "TTS_QUOTA_COOLDOWN_SECONDS", os.getenv("MINIMAX_QUOTA_COOLDOWN_SECONDS", "1800"),
        ))),
    )


class NoVoiceAvailableError(RuntimeError):
    """No voice of the fallback chain could speak the message."""


# A provider that can report its quota is asked this soon after a QUOTA or
# AUTH failure, to learn the reset time instead of guessing.
QUICK_CHECK_SECONDS = 60.0
# Quotas reset at the reported second; check a little after it.
RESET_MARGIN_SECONDS = 120.0

PROVIDER_LABELS = {
    "minimax": "MiniMax", "fish": "Fish", "gemini": "Gemini", "elevenlabs": "ElevenLabs",
    "piper": "Piper",
}


class TTSDispatcher:
    """Facade that the bot calls to synthesize a message to a file.

    Routing: ``synthesize_chain`` tries a voice, then its fallback voices
    (``VoiceRegistry.chain_for``) in order. Each try goes through the
    voice's provider: cache, then the provider's circuit breaker, then the
    API. A provider that failed for any reason other than this one request
    (see ttsbot.health) is skipped for the rest of the chain, so a Fish
    outage does not cost a second Fish timeout on the next Fish voice.
    ``synthesize`` without ``fallbacks`` keeps the pre-chain behavior: the
    voice, then the Piper ``fallback_profile``.

    Health: every cloud provider has a CircuitBreaker. ``record_failure``
    classifies an exception and pauses the provider for as long as that
    kind of failure calls for; ``check_health`` (the bot runs it every
    TTS_HEALTH_CHECK_INTERVAL seconds) checks paused providers without
    spending a user message: a quota report, or a ping of the host.
    """

    def __init__(
        self,
        local: LocalProvider,
        cloud: Optional[TTSProvider] = None,
        fish: Optional[FishProvider] = None,
        gemini: Optional[GeminiProvider] = None,
        elevenlabs: Optional[ElevenLabsProvider] = None,
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
        self._gemini = gemini
        self._gemini_cb = load_circuit_breaker_from_env()
        self._elevenlabs = elevenlabs
        self._elevenlabs_cb = load_circuit_breaker_from_env()
        self._breakers: dict[str, CircuitBreaker] = {
            "minimax": self._cb, "fish": self._fish_cb,
            "gemini": self._gemini_cb, "elevenlabs": self._elevenlabs_cb,
        }
        for name, breaker in self._breakers.items():
            breaker.name = breaker.name or PROVIDER_LABELS[name]
        # Providers whose check_quota() could not tell (e.g. keys without
        # the permission to read their quota): probed on the backoff instead.
        self._quota_unknown: set[str] = set()
        self._config = config or DispatcherConfig()
        self._cache = cache  # None means caching disabled
        # Piper profile spoken when a voice fails and no chain is given.
        self._fallback_profile = fallback_profile

    @property
    def config(self) -> DispatcherConfig:
        return self._config

    @property
    def circuit_breaker(self) -> CircuitBreaker:
        return self._cb

    # -- provider health ------------------------------------------------

    def breaker_for(self, provider: str | None) -> Optional[CircuitBreaker]:
        """The circuit breaker of a provider name (None for Piper/unknown)."""
        return self._breakers.get(provider or "")

    def provider_names(self) -> list[str]:
        """The configured cloud providers, in a stable order."""
        return [name for name in self._breakers if self.provider_for(name) is not None]

    def _name_of(self, breaker: CircuitBreaker) -> str:
        return next((name for name, b in self._breakers.items() if b is breaker), "")

    @staticmethod
    def health_fingerprint(provider) -> str:
        """Id of a provider's credentials, so saved health follows its key."""
        value = getattr(provider, "health_fingerprint", None)
        if isinstance(value, str):
            return value
        cfg = getattr(provider, "config", None) or getattr(provider, "_config", None)
        return fingerprint(str(getattr(cfg, "api_key", "") or ""), str(getattr(cfg, "group_id", "") or ""))

    def attach_health_store(self, store: HealthStore) -> None:
        """Restore every configured provider's breaker and save its changes."""
        for name in self.provider_names():
            store.bind(name, self._breakers[name], self.health_fingerprint(self.provider_for(name)))

    def record_failure(
        self, breaker: CircuitBreaker, exc: BaseException | None = None,
    ) -> None:
        """Count a provider failure; how long it pauses depends on its kind.

        - this request only (unknown voice, ...): not counted at all;
        - a rate limit with Retry-After: paused for exactly that long;
        - quota used up / key rejected: paused until a check says otherwise
          (soon, when the provider can report its quota);
        - anything else: counted toward the CB_* threshold.
        """
        kind = classify_failure(exc)
        if kind is FailureKind.REQUEST:
            log.info(
                "%s rejected this request only (%s: %s); provider stays available",
                breaker.name or "Provider", type(exc).__name__, exc,
            )
            if breaker.state is CircuitState.HALF_OPEN:
                breaker.record_success()  # the probe got a real answer: it works
            return
        retry_after = getattr(exc, "retry_after", None)
        if kind is FailureKind.RATE_LIMIT and retry_after:
            log.warning(
                "Rate limited (%s); pausing the provider for %.0fs (Retry-After)",
                type(exc).__name__, retry_after,
            )
            breaker.record_failure(kind, error=exc, retry_after=float(retry_after))
            return
        if kind in LONG_KINDS:
            name = self._name_of(breaker)
            provider = self.provider_for(name)
            # A quota report proves nothing about a rejected key, and a
            # check that was wrong before (or could not tell) is not asked.
            checkable = (
                kind is FailureKind.QUOTA and breaker.check_trusted and not breaker.closed_by_check
                and callable(getattr(provider, "check_quota", None)) and name not in self._quota_unknown
            )
            log.warning(
                "%s (%s); pausing the provider until it is %s: %s",
                "Quota exhausted" if kind is FailureKind.QUOTA else "Key rejected",
                type(exc).__name__, "checked" if checkable else "probed", exc,
            )
            breaker.record_failure(
                kind, error=exc,
                until=breaker.now() + QUICK_CHECK_SECONDS if checkable else None,
                base=self._config.quota_cooldown_seconds if kind is FailureKind.QUOTA else None,
            )
            breaker.await_check = checkable
            return
        was_open = breaker.state is CircuitState.OPEN
        breaker.record_failure(kind, error=exc)
        if not was_open and breaker.state is CircuitState.OPEN:
            # Let the health worker ping the host before a message probes.
            breaker.await_check = self._pingable(self.provider_for(self._name_of(breaker)))

    @staticmethod
    def _pingable(provider) -> bool:
        return callable(getattr(getattr(provider, "warmer", None), "reachable", None))

    async def check_health(self) -> None:
        """Check every paused provider whose pause is over (health worker)."""
        for name, breaker in self._breakers.items():
            provider = self.provider_for(name)
            if provider is None or not breaker.check_due:
                continue
            try:
                await self._check_provider(name, provider, breaker)
            except Exception:
                log.exception("Health check of %s failed", breaker.name)
                breaker.await_check = False  # let a real request decide

    async def _check_provider(self, name: str, provider, breaker: CircuitBreaker) -> None:
        kind = breaker.kind
        if kind is FailureKind.QUOTA:
            checker = getattr(provider, "check_quota", None)
            report = await checker() if callable(checker) else None
            if report is None:
                # Cannot tell: the next request probes, and later failures
                # wait on the backoff rather than on this check.
                self._quota_unknown.add(name)
                breaker.await_check = False
                return
            self._quota_unknown.discard(name)
            if report.available:
                log.info("%s quota check: available again (%s)", breaker.name, report.detail)
                breaker.record_success(confirmed=False)
                return
            log.info("%s quota check: still unavailable (%s)", breaker.name, report.detail)
            breaker.record_failure(
                kind, error=report.detail or "quota check: unavailable",
                until=report.reset_at + RESET_MARGIN_SECONDS if report.reset_at else None,
                base=self._config.quota_cooldown_seconds if kind is FailureKind.QUOTA else None,
            )
            breaker.await_check = True
            return
        if kind is FailureKind.TRANSIENT and self._pingable(provider):
            if await provider.warmer.reachable():
                log.info("%s host answers again; the next request probes it", breaker.name)
                breaker.await_check = False
            else:
                breaker.reopen(f"{breaker.name} host unreachable")
                breaker.await_check = True
            return
        breaker.await_check = False

    # -- providers --------------------------------------------------------

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
    def gemini(self) -> Optional[GeminiProvider]:
        return self._gemini

    @property
    def gemini_circuit_breaker(self) -> CircuitBreaker:
        return self._gemini_cb

    @property
    def elevenlabs(self) -> Optional[ElevenLabsProvider]:
        return self._elevenlabs

    @property
    def elevenlabs_circuit_breaker(self) -> CircuitBreaker:
        return self._elevenlabs_cb

    def provider_for(self, provider: str | None):
        """The configured provider object for a voice record's ``provider``
        value, or None (Piper, unknown, or not configured). Register every
        new cloud provider here (and in ``_breakers``) so typing warm-ups,
        health checks and the fallback chain reach it."""
        return {
            "minimax": self._cloud,
            "fish": self._fish,
            "gemini": self._gemini,
            "elevenlabs": self._elevenlabs,
        }.get(provider or "")

    @property
    def cache(self) -> Optional[TTSPhraseCache]:
        return self._cache

    # -- synthesis --------------------------------------------------------

    def route(self, voice) -> str:
        """Provider name for a voice record; without one, TTS_PRIMARY_PROVIDER."""
        if voice is not None:
            return getattr(voice, "provider", None) or "piper"
        primary = self._config.primary
        if primary is PrimaryProvider.LOCAL:
            return "piper"
        if primary is PrimaryProvider.ELEVENLABS and not (
            self._elevenlabs is not None and self._elevenlabs.config.voice_id
        ):
            return "piper"  # no ELEVENLABS_VOICE_ID: no voice to use
        return primary.value

    async def synthesize(self, text: str, filename: Path, voice=None, fallbacks=None) -> str:
        """Produce audio at ``filename`` and return the provider used."""
        used, _ = await self.synthesize_chain(text, filename, voice, fallbacks)
        return used

    async def synthesize_chain(
        self, text: str, filename: Path, voice=None, fallbacks=None,
    ) -> tuple[str, object]:
        """Speak ``text`` with ``voice`` or the first of ``fallbacks`` that can.

        Returns ``(provider used, voice used)``; the provider is "cache" for
        a cache hit. ``fallbacks=None`` means the pre-chain behavior: Piper's
        fallback profile after a cloud voice. With a list, raises
        NoVoiceAvailableError when no voice of it could speak.
        """
        candidates = [voice] if fallbacks is None else [voice, *fallbacks]
        legacy_tail = fallbacks is None and self.route(voice) != "piper"
        skipped: set[str] = set()
        for index, candidate in enumerate(candidates):
            provider = self.route(candidate)
            if provider in skipped:
                continue
            more = legacy_tail or index + 1 < len(candidates)
            try:
                used, error = await self._synthesize_one(text, filename, candidate, provider)
            except Exception as exc:
                if not more:
                    raise
                used, error = "", exc
                log.warning("%s synthesis failed (%s: %s)", PROVIDER_LABELS.get(provider, provider),
                            type(exc).__name__, exc)
            if used:
                if index:
                    log.info("Fallback voice %s spoke for %s", getattr(candidate, "name", "-"),
                             getattr(voice, "name", "-"))
                return used, candidate
            if classify_failure(error) is not FailureKind.REQUEST:
                skipped.add(provider)
        if legacy_tail:
            await self._local.synthesize(text, filename, self._fallback_profile or None)
            return self._local.name, None
        names = ", ".join(getattr(c, "name", "-") for c in candidates)
        raise NoVoiceAvailableError(f"No voice could speak the message (tried {names})")

    async def _synthesize_one(self, text: str, filename: Path, voice, provider: str):
        """One voice, no fallback: ``(provider used or "", error or None)``."""
        if provider == "gemini":
            if self._gemini is None:
                return "", None
            return await self._synthesize_gemini(text, filename, getattr(voice, "gemini", None))
        if provider == "elevenlabs":
            if self._elevenlabs is None:
                return "", None
            return await self._synthesize_elevenlabs(text, filename, getattr(voice, "elevenlabs", None))
        if provider == "fish":
            if self._fish is None:
                return "", None
            return await self._synthesize_fish(text, filename, voice)
        if provider == "minimax":
            if self._cloud is None:
                return "", None
            return await self._synthesize_minimax(text, filename, voice)
        voice_key = voice_cache_key(voice)
        if self._cache_hit(text, filename, voice_key):
            return "cache", None
        await self._local.synthesize(text, filename, getattr(voice, "name", None))
        self._maybe_cache(text, filename, voice_key)
        return self._local.name, None

    def _cache_hit(self, text: str, filename: Path, voice_key: str) -> bool:
        if self._cache is None:
            return False
        cached = self._cache.lookup(text, voice_key)
        if cached is None:
            return False
        shutil.copyfile(cached, filename)
        log.debug("TTS cache HIT text=%d chars voice=%s", len(text), voice_key)
        return True

    def _breaker_closed_to(self, breaker: CircuitBreaker) -> bool:
        """True (and a log line) when the breaker does not allow a request."""
        if breaker.allow_request():
            return False
        log.info("%s circuit breaker open (%s); trying the next voice",
                 breaker.name, breaker.describe())
        return True

    async def _synthesize_fish(self, text: str, filename: Path, voice):
        params = getattr(voice, "fish", None)
        cfg = self._fish.config
        reference_id = getattr(params, "reference_id", "") or cfg.reference_id
        cache_key = cfg.cache_key(reference_id, params)
        if self._cache_hit(text, filename, cache_key):
            return "cache", None
        if self._breaker_closed_to(self._fish_cb):
            return "", None
        try:
            await self._fish.synthesize(
                text, filename, reference_id=reference_id, params=params, request_config=cfg,
            )
        except Exception as exc:
            self.record_failure(self._fish_cb, exc)
            log.warning("Fish synthesis failed (%s: %s)", type(exc).__name__, exc)
            return "", exc
        self._fish_cb.record_success()
        self._maybe_cache(text, filename, cache_key, suffix="opus")
        return "fish", None

    async def _synthesize_minimax(self, text: str, filename: Path, voice):
        voice_key = voice_cache_key(voice)
        if self._cache_hit(text, filename, voice_key):
            return "cache", None
        if self._breaker_closed_to(self._cb):
            return "", None
        mm = getattr(voice, "minimax", None) if voice is not None else None
        try:
            if mm is not None:
                await self._cloud.synthesize(
                    text, filename,
                    voice_id=mm.voice_id, model=mm.model,
                    speed=mm.speed, vol=mm.vol, pitch=mm.pitch,
                    emotion=mm.emotion, language_boost=mm.language_boost,
                )
            else:
                await self._cloud.synthesize(text, filename)
        except Exception as exc:
            log.warning("TTS provider %s failed (%s: %s)", self._cloud.name, type(exc).__name__, exc)
            self.record_failure(self._cb, exc)
            return "", exc
        self._cb.record_success()
        self._maybe_cache(text, filename, voice_key)
        return self._cloud.name, None

    async def _synthesize_gemini(self, text: str, filename: Path, params):
        """Gemini file path: cache, then the API."""
        cache_key = self._gemini.config.cache_key(params)
        volume_db = float(getattr(params, "volume_db", 0.0) or 0.0)
        if self._cache is not None:
            cached = self._cache.lookup(text, cache_key)
            if cached is not None:
                try:
                    rate, channels, pcm = read_pcm_cache(cached)
                    write_wav(filename, rate, channels, apply_gain(pcm, volume_db))
                    return "cache", None
                except (OSError, ValueError) as exc:
                    log.warning("Unreadable Gemini cache file %s (%s); regenerating", cached, exc)
        if self._breaker_closed_to(self._gemini_cb):
            return "", None
        try:
            rate, channels, pcm = await self._gemini.fetch_pcm(text, params)
        except Exception as exc:
            self.record_failure(self._gemini_cb, exc)
            log.warning("Gemini synthesis failed (%s: %s)", type(exc).__name__, exc)
            return "", exc
        self._gemini_cb.record_success()
        write_wav(filename, rate, channels, apply_gain(pcm, volume_db))
        if self._cache is not None:
            try:
                self._cache.store_bytes(text, pcm_cache_header(rate, channels) + pcm, cache_key, "pcm")
            except OSError as exc:
                log.warning("TTS cache store failed: %s", exc)
        return "gemini", None

    async def _synthesize_elevenlabs(self, text: str, filename: Path, params):
        """ElevenLabs file path: cache, then the API.

        Opus is cached as the raw Ogg (.opus, decoded by ffmpeg), PCM as a
        .pcm entry; the streaming path keeps its own Discord-frame entries.
        """
        cfg = self._elevenlabs.config
        cache_key = cfg.cache_key(params)
        if self._cache is not None:
            cached = self._cache.lookup(text, cache_key)
            if cached is not None:
                try:
                    if cfg.kind == "pcm":
                        rate, channels, pcm = read_pcm_cache(cached)
                        write_wav(filename, rate, channels, pcm)
                    else:
                        shutil.copyfile(cached, filename)
                    return "cache", None
                except (OSError, ValueError) as exc:
                    log.warning("Unreadable ElevenLabs cache file %s (%s); regenerating", cached, exc)
        if self._breaker_closed_to(self._elevenlabs_cb):
            return "", None
        # The file path reads the whole clip, so the budget grows with the text.
        budget = bot_config.ELEVENLABS_TTFA_TIMEOUT + 0.03 * len(text)
        try:
            kind, rate, data = await asyncio.wait_for(self._elevenlabs.fetch(text, params), budget)
        except Exception as exc:
            self.record_failure(self._elevenlabs_cb, exc)
            log.warning("ElevenLabs synthesis failed (%s: %s)", type(exc).__name__, exc)
            return "", exc
        self._elevenlabs_cb.record_success()
        if kind == "pcm":
            write_wav(filename, rate, 1, data)
            entry, suffix = pcm_cache_header(rate, 1) + data, "pcm"
        else:
            filename.write_bytes(data)
            entry, suffix = data, "opus"
        if self._cache is not None:
            try:
                self._cache.store_bytes(text, entry, cache_key, suffix)
            except OSError as exc:
                log.warning("TTS cache store failed: %s", exc)
        return "elevenlabs", None

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
