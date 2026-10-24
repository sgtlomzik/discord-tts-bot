"""TTS providers: the dispatcher and the phrase cache.

* ``TTSDispatcher`` - the cloud providers by name with their circuit
  breakers (ttsbot.health) and health checks, and Piper's file path.
* ``LocalProvider`` - wraps the Piper synthesis callable.
* ``TTSPhraseCache`` - the on-disk phrase cache shared by every provider.

A cloud provider plugs into the pipeline with ``stream_spec(text, voice)``
(see ttsbot.streams); each provider lives in its own module and
ttsbot.plugins builds the configured ones.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import (
    Awaitable,
    Callable,
    Mapping,
    Optional,
    Protocol,
)

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


@dataclass
class DispatcherConfig:
    # How long to stop calling a provider after a balance/plan error.
    quota_cooldown_seconds: float = 1800.0


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


def load_dispatcher_config_from_env() -> DispatcherConfig:
    """Build a ``DispatcherConfig`` from ``TTS_QUOTA_COOLDOWN_SECONDS``."""
    return DispatcherConfig(
        quota_cooldown_seconds=max(0.0, float(os.getenv(
            "TTS_QUOTA_COOLDOWN_SECONDS", os.getenv("MINIMAX_QUOTA_COOLDOWN_SECONDS", "1800"),
        ))),
    )


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
    """The cloud providers by name, their health, and Piper's file path.

    Providers: ``register(name, provider)`` (or ``providers=`` at
    construction) adds a cloud provider under the name its voices use,
    with its own CircuitBreaker; ``provider_for`` / ``breaker_for`` find
    them. Cloud voices stream (``SynthesisPipelineMixin._stream_voice_into``,
    driven by each provider's ``stream_spec``); ``synthesize_piper`` writes
    a Piper voice to a file, through the phrase cache.

    Health: ``record_failure`` classifies an exception and pauses the
    provider for as long as that kind of failure calls for; ``check_health``
    (the bot runs it every TTS_HEALTH_CHECK_INTERVAL seconds) checks paused
    providers without spending a user message: a quota report, or a ping
    of the host.
    """

    def __init__(
        self,
        local: LocalProvider,
        providers: Optional[Mapping[str, object]] = None,
        *,
        breakers: Optional[Mapping[str, CircuitBreaker]] = None,
        config: Optional[DispatcherConfig] = None,
        cache: Optional[TTSPhraseCache] = None,
    ) -> None:
        self._local = local
        # Cloud providers by the name voice records use ("fish", ...), each
        # with its own circuit breaker; an unconfigured one is left out.
        self._providers: dict[str, object] = {}
        self._breakers: dict[str, CircuitBreaker] = {}
        for name, provider in (providers or {}).items():
            self.register(name, provider, (breakers or {}).get(name))
        # Providers whose check_quota() could not tell (e.g. keys without
        # the permission to read their quota): probed on the backoff instead.
        self._quota_unknown: set[str] = set()
        self._config = config or DispatcherConfig()
        self._cache = cache  # None means caching disabled

    @property
    def config(self) -> DispatcherConfig:
        return self._config

    def register(self, name: str, provider, breaker: Optional[CircuitBreaker] = None) -> None:
        """Add a cloud provider under the name its voices use.

        It gets its own circuit breaker (from the CB_* settings unless one
        is given). ``None`` (not configured) is ignored.
        """
        if provider is None:
            return
        breaker = breaker or load_circuit_breaker_from_env()
        breaker.name = breaker.name or PROVIDER_LABELS.get(name, name)
        self._providers[name] = provider
        self._breakers[name] = breaker

    # -- provider health ------------------------------------------------

    def breaker_for(self, provider: str | None) -> Optional[CircuitBreaker]:
        """The circuit breaker of a provider name (None for Piper/unknown)."""
        return self._breakers.get(provider or "")

    def provider_names(self) -> list[str]:
        """The configured cloud providers, in registration order."""
        return list(self._providers)

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

    def provider_for(self, provider: str | None):
        """The configured provider object for a voice record's ``provider``
        value, or None (Piper, unknown, or not configured)."""
        return self._providers.get(provider or "")

    @property
    def cache(self) -> Optional[TTSPhraseCache]:
        return self._cache

    # -- synthesis --------------------------------------------------------

    async def synthesize_piper(self, text: str, filename: Path, voice=None) -> str:
        """Speak ``text`` with the Piper ``voice`` to ``filename``.

        Returns "cache" when the cached audio was copied there, else the
        local provider's name. Cloud voices never come here: they stream.
        """
        voice_key = voice_cache_key(voice)
        if self._cache_hit(text, filename, voice_key):
            return "cache"
        await self._local.synthesize(text, filename, getattr(voice, "name", None))
        self._maybe_cache(text, filename, voice_key, suffix="wav")
        return self._local.name

    def _cache_hit(self, text: str, filename: Path, voice_key: str) -> bool:
        if self._cache is None:
            return False
        cached = self._cache.lookup(text, voice_key)
        if cached is None:
            return False
        shutil.copyfile(cached, filename)
        log.debug("TTS cache HIT text=%d chars voice=%s", len(text), voice_key)
        return True

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
