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
from typing import Awaitable, Callable, Optional, Protocol

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

    Behavior in this commit: it always delegates to the configured
    ``LocalProvider`` regardless of the ``primary`` flag. The full
    primary/fallback/CB logic lands in commits 3 and 6 of the rollout
    plan; introducing the indirection now means those later commits
    touch only this file and its tests, never ``bot.py``.
    """

    def __init__(
        self,
        local: LocalProvider,
        cloud: Optional[TTSProvider] = None,
        circuit_breaker: Optional[CircuitBreaker] = None,
        config: Optional[DispatcherConfig] = None,
    ) -> None:
        self._local = local
        self._cloud = cloud  # may be None until commit 3
        self._cb = circuit_breaker or CircuitBreaker()
        self._config = config or DispatcherConfig()

    @property
    def config(self) -> DispatcherConfig:
        return self._config

    @property
    def circuit_breaker(self) -> CircuitBreaker:
        return self._cb

    async def synthesize(self, text: str, filename: Path) -> str:
        """Produce audio at ``filename`` and return the provider used.

        Skeleton behavior: always delegates to the local provider. Later
        commits will route through the cloud provider when configured
        and fall back on failure.
        """
        # Future: consult self._cb.allow_request(); if False → fallback.
        # Future: if self._config.primary is PrimaryProvider.MINIMAX and
        #         self._cloud is not None → try cloud, on exception call
        #         self._cb.record_failure() and fall back to local.
        provider_used = self._local.name
        await self._local.synthesize(text, filename)
        return provider_used

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