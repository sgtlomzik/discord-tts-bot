"""Provider health: what a failure means, how long to avoid the provider,
and how to find out it works again.

Every cloud provider has a ``CircuitBreaker``. A failure is first sorted
into a ``FailureKind`` (by the marker classes in ttsbot.errors), and the
kind decides the pause:

- REQUEST: this message only (unknown voice, too long for the credits
  left). The provider stays available; the next voice in the fallback
  chain speaks this one message.
- TRANSIENT: network error, timeout, 5xx, no audio in time. After
  ``failure_threshold`` in a row the provider pauses for
  ``cooldown_seconds``; every failed probe doubles the pause up to
  ``max_cooldown_seconds``. While paused, the health worker pings the host
  (free) and lets a real request probe only once the host answers.
- RATE_LIMIT: pause for the server's Retry-After.
- QUOTA: balance or credits used up. A provider that can report its quota
  (``check_quota``, e.g. ElevenLabs' free subscription endpoint) is asked
  right away and then at its reset time, at least once a day; one that
  cannot is probed by a real request after 30 min, 1 h, 2 h ... 24 h.
- AUTH: the key is invalid. Same schedule as QUOTA, starting at 1 h.

Breaker state survives restarts in ``data/provider_health.json``
(``HealthStore``), keyed by provider with a fingerprint of its keys: a
changed key starts from a clean state.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Optional

from ttsbot.errors import AuthError, QuotaExhaustedError, RateLimitError, RequestError

log = logging.getLogger("tts_bot.health")


class FailureKind(str, Enum):
    REQUEST = "request"
    TRANSIENT = "transient"
    RATE_LIMIT = "rate_limit"
    QUOTA = "quota"
    AUTH = "auth"


# Kinds that pause a provider until a check says otherwise.
LONG_KINDS = frozenset({FailureKind.QUOTA, FailureKind.AUTH})
# Shortest QUOTA/AUTH pause taken from a reported reset time.
MIN_RECHECK_SECONDS = 30.0


def classify_failure(exc: BaseException | None) -> FailureKind:
    """The kind of a provider failure; None (a timeout without an
    exception, a stream with no audio) counts as TRANSIENT."""
    if exc is None:
        return FailureKind.TRANSIENT
    if isinstance(exc, RequestError):
        return FailureKind.REQUEST
    if isinstance(exc, QuotaExhaustedError):
        return FailureKind.QUOTA
    if isinstance(exc, AuthError):
        return FailureKind.AUTH
    if isinstance(exc, RateLimitError) or getattr(exc, "retry_after", None):
        return FailureKind.RATE_LIMIT
    return FailureKind.TRANSIENT


def fingerprint(*secrets: str) -> str:
    """Stable id of a provider's credentials; the secrets are never stored."""
    joined = "\0".join(s for s in secrets if s)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16] if joined else ""


@dataclass(frozen=True)
class QuotaReport:
    """What a provider's ``check_quota()`` found out.

    ``available``: a request can succeed now. ``reset_at``: Unix time the
    quota comes back, if the provider says. ``detail``: one line for logs
    and /fallback status.
    """

    available: bool
    reset_at: float | None = None
    detail: str = ""


class CircuitState(str, Enum):
    CLOSED = "closed"          # normal: every call hits the provider
    OPEN = "open"              # paused: calls go to the next voice in the chain
    HALF_OPEN = "half_open"    # one probe call after the pause


@dataclass
class CircuitBreakerConfig:
    failure_threshold: int = 3
    cooldown_seconds: float = 60.0
    # Each failed probe multiplies a TRANSIENT pause, up to the max.
    backoff_factor: float = 2.0
    max_cooldown_seconds: float = 900.0
    # QUOTA without a known reset time; the dispatcher passes its own.
    quota_recheck_seconds: float = 1800.0
    auth_recheck_seconds: float = 3600.0
    # Longest QUOTA/AUTH pause: the provider is rechecked at least daily.
    max_recheck_seconds: float = 86400.0
    # A probe that never reported back (job cancelled) frees the slot.
    probe_timeout_seconds: float = 120.0
    # A pause waiting for the health worker's check lets a real request
    # probe anyway this long after it ended, in case the worker is stuck.
    check_grace_seconds: float = 300.0


class CircuitBreaker:
    """Tracks the failures of one provider (or one API key).

    - CLOSED: every call is allowed.
    - OPEN: no calls until the pause ends (``cooldown_remaining``).
    - HALF_OPEN: the pause ended; exactly one caller gets ``True`` from
      ``allow_request`` as the probe. Its success closes the breaker, its
      failure opens it again with a longer pause.

    ``await_check`` (set by the dispatcher when the provider can be checked
    without a real request) holds the breaker OPEN past the pause until the
    health worker has checked; ``check_grace_seconds`` later a real request
    may probe anyway.
    """

    def __init__(
        self,
        config: Optional[CircuitBreakerConfig] = None,
        *,
        clock: Optional[Callable[[], float]] = None,
        name: str = "",
    ) -> None:
        self._config = config or CircuitBreakerConfig()
        self.name = name
        # Wall clock: pauses are persisted and must survive a restart.
        # Tests inject a fake clock.
        self._clock = clock or time.time
        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._opened_at: Optional[float] = None
        self._cooldown = 0.0
        self._kind: Optional[FailureKind] = None
        # Pauses since the last success: drives the backoff. Counted per
        # kind of failure, so a network outage does not lengthen the first
        # quota pause after it.
        self._escalation = 0
        self._escalation_kind: Optional[FailureKind] = None
        # Closed by a health check, not yet confirmed by a real request.
        self._closed_by_check = False
        # False once a check said "available" and the next request still
        # failed the same way: the check cannot see this problem, so the
        # backoff decides instead (dispatcher). Reset by a real success.
        self.check_trusted = True
        self._probe_started: Optional[float] = None
        self._await_check = False
        self.last_error = ""
        self.last_failure_at: Optional[float] = None
        self.last_success_at: Optional[float] = None
        self.down_since: Optional[float] = None
        # Called after every state change (persistence).
        self.on_change: Optional[Callable[["CircuitBreaker"], None]] = None

    # -- read-only view -------------------------------------------------

    @property
    def config(self) -> CircuitBreakerConfig:
        return self._config

    @property
    def state(self) -> CircuitState:
        return self._state

    @property
    def kind(self) -> Optional[FailureKind]:
        """Why the breaker is not CLOSED (None while CLOSED)."""
        return self._kind

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    @property
    def closed_by_check(self) -> bool:
        """Closed by a health check and not yet confirmed by a request."""
        return self._closed_by_check

    @property
    def await_check(self) -> bool:
        return self._await_check

    @await_check.setter
    def await_check(self, value: bool) -> None:
        if self._await_check != bool(value):
            self._await_check = bool(value)
            self._changed()

    def now(self) -> float:
        return self._clock()

    @property
    def retry_at(self) -> Optional[float]:
        """When the current pause ends (clock time), None unless OPEN."""
        if self._state is not CircuitState.OPEN or self._opened_at is None:
            return None
        now = self._clock()
        if self._opened_at > now:
            # The wall clock stepped back (or a saved pause is dated in the
            # future): restart the pause now so it never outlasts its length.
            self._opened_at = now
        return self._opened_at + self._cooldown

    @property
    def cooldown_remaining(self) -> float:
        """Seconds remaining in the current pause, or 0.0 if not OPEN."""
        retry_at = self.retry_at
        return 0.0 if retry_at is None else max(0.0, retry_at - self._clock())

    @property
    def check_due(self) -> bool:
        """OPEN, waiting for a check, and the pause is over: time for the
        health worker. After an inconclusive check ``await_check`` is off and
        the next real request probes instead, so the worker stays quiet."""
        return (
            self._state is CircuitState.OPEN and self._await_check
            and self.cooldown_remaining <= 0.0
        )

    # -- transitions ----------------------------------------------------

    def allow_request(self) -> bool:
        """Return True if a call should be attempted right now.

        HALF_OPEN lets exactly one caller through (the probe); a second
        caller arriving in the same event-loop tick is blocked.
        """
        if self._state is CircuitState.CLOSED:
            return True
        now = self._clock()
        if self._state is CircuitState.HALF_OPEN:
            started = self._probe_started
            if started is not None and now - started > self._config.probe_timeout_seconds:
                self._probe_started = now  # the last probe never reported back
                return True
            return False
        if self.cooldown_remaining > 0.0:
            return False
        if self.await_check and now - (self.retry_at or now) < self._config.check_grace_seconds:
            return False
        self._state = CircuitState.HALF_OPEN
        self._probe_started = now
        self._changed()
        return True

    def record_success(self, *, confirmed: bool = True) -> None:
        """Close the breaker. ``confirmed=False``: a health check (not a
        real request) says the provider works; the backoff is kept until a
        request confirms it."""
        was_down = self._state is not CircuitState.CLOSED
        self._consecutive_failures = 0
        self._state = CircuitState.CLOSED
        self._opened_at = None
        self._cooldown = 0.0
        self._kind = None
        if confirmed:
            self._escalation = 0
            self._escalation_kind = None
            self.check_trusted = True
        self._closed_by_check = not confirmed
        self._probe_started = None
        self._await_check = False
        self.last_success_at = self._clock()
        if was_down:
            if self.down_since is not None:
                log.info("%s is available again after %s", self._label(),
                         format_duration(self.last_success_at - self.down_since))
            self.down_since = None
            self._changed()

    def record_failure(
        self,
        kind: FailureKind = FailureKind.TRANSIENT,
        *,
        error: BaseException | str | None = None,
        retry_after: float | None = None,
        until: float | None = None,
        base: float | None = None,
    ) -> None:
        """Count a failure of ``kind`` and pause the provider if it calls for it.

        ``retry_after``: RATE_LIMIT pause in seconds. ``until``: clock time
        a QUOTA/AUTH pause ends (a known reset time); without it the pause
        follows the backoff from ``base`` (or the configured recheck).
        """
        if kind is FailureKind.REQUEST:
            return
        self._consecutive_failures += 1
        self.last_failure_at = self._clock()
        if error is not None:
            text = error if isinstance(error, str) else f"{type(error).__name__}: {error}"
            self.last_error = text[:300]
        if kind is FailureKind.RATE_LIMIT and retry_after:
            pause = min(float(retry_after), self._config.max_recheck_seconds)
            if self._state is CircuitState.OPEN and self.cooldown_remaining >= pause:
                return  # a longer pause is already in force
            self._open(kind, pause)
            return
        if kind in LONG_KINDS:
            if self._closed_by_check:
                self.check_trusted = False  # the check said it works; it does not
                self._closed_by_check = False
            # A reset time already (nearly) past would mean a check every
            # tick: fall back to the backoff then.
            if until is not None and until - self._clock() >= MIN_RECHECK_SECONDS:
                cooldown = until - self._clock()
            else:
                default = (
                    self._config.auth_recheck_seconds if kind is FailureKind.AUTH
                    else self._config.quota_recheck_seconds
                )
                cooldown = self._backoff(kind, default if base is None else base,
                                         self._config.max_recheck_seconds)
            self._open(kind, min(max(cooldown, 0.0), self._config.max_recheck_seconds))
            return
        # TRANSIENT, and a rate limit without Retry-After.
        if self._state is CircuitState.OPEN:
            return  # already paused (a late reply of a request sent before)
        if (
            self._state is CircuitState.HALF_OPEN
            or self._consecutive_failures >= self._config.failure_threshold
        ):
            self._open(FailureKind.TRANSIENT, self._backoff(
                FailureKind.TRANSIENT, self._config.cooldown_seconds, self._config.max_cooldown_seconds,
            ))

    def reopen(self, error: str = "") -> None:
        """A health check found the provider still down: pause again, longer."""
        kind = self._kind or FailureKind.TRANSIENT
        if error:
            self.last_error = error[:300]
        if kind in LONG_KINDS:
            default = (
                self._config.auth_recheck_seconds if kind is FailureKind.AUTH
                else self._config.quota_recheck_seconds
            )
            self._open(kind, self._backoff(kind, default, self._config.max_recheck_seconds))
        else:
            self._open(kind, self._backoff(kind, self._config.cooldown_seconds, self._config.max_cooldown_seconds))

    def trip(self, cooldown_seconds: float, kind: FailureKind = FailureKind.QUOTA) -> None:
        """Open immediately for ``cooldown_seconds``."""
        self._consecutive_failures += 1
        self.last_failure_at = self._clock()
        self._open(kind, max(0.0, cooldown_seconds))

    def reset(self) -> None:
        """Forget the failures (manual reset from /fallback status)."""
        self.record_success()
        self.last_error = ""
        self._changed()

    def _backoff(self, kind: FailureKind, base: float, cap: float) -> float:
        family = FailureKind.TRANSIENT if kind is FailureKind.RATE_LIMIT else kind
        if family is not self._escalation_kind:
            self._escalation = 0
            self._escalation_kind = family
        cooldown = base * (self._config.backoff_factor ** self._escalation)
        self._escalation += 1
        return min(max(cooldown, 0.0), cap)

    def _open(self, kind: FailureKind, cooldown: float) -> None:
        now = self._clock()
        self._state = CircuitState.OPEN
        self._opened_at = now
        self._cooldown = cooldown
        self._kind = kind
        self._probe_started = None
        self._await_check = False
        if self.down_since is None:
            self.down_since = now
        if self.name:
            log.warning(
                "%s paused for %s (%s): %s", self._label(), format_duration(cooldown),
                kind.value, self.last_error or "-",
            )
        self._changed()

    def _label(self) -> str:
        return self.name or "provider"

    def _changed(self) -> None:
        if self.on_change is not None:
            try:
                self.on_change(self)
            except Exception:
                log.exception("Could not save the health of %s", self._label())

    # -- persistence ----------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "state": self._state.value,
            "kind": self._kind.value if self._kind else None,
            "opened_at": self._opened_at,
            "cooldown": self._cooldown,
            "escalation": self._escalation,
            "escalation_kind": self._escalation_kind.value if self._escalation_kind else None,
            "consecutive_failures": self._consecutive_failures,
            "await_check": self.await_check,
            "last_error": self.last_error,
            "last_failure_at": self.last_failure_at,
            "last_success_at": self.last_success_at,
            "down_since": self.down_since,
        }

    def restore(self, data: dict) -> None:
        """Load a ``to_dict()`` snapshot; an unreadable one is ignored."""
        def time_or_none(value) -> Optional[float]:
            return None if value is None else float(value)

        try:
            state = CircuitState(data.get("state", "closed"))
            kind = FailureKind(data["kind"]) if data.get("kind") else None
            escalation_kind = FailureKind(data["escalation_kind"]) if data.get("escalation_kind") else kind
            opened_at = time_or_none(data.get("opened_at"))
            cooldown = float(data.get("cooldown") or 0.0)
            escalation = int(data.get("escalation") or 0)
            failures = int(data.get("consecutive_failures") or 0)
            last_failure_at = time_or_none(data.get("last_failure_at"))
            last_success_at = time_or_none(data.get("last_success_at"))
            down_since = time_or_none(data.get("down_since"))
        except (TypeError, ValueError, KeyError):
            log.warning("Ignoring unreadable saved health of %s", self._label())
            return
        self.last_error = str(data.get("last_error") or "")
        self.last_failure_at = last_failure_at
        self.last_success_at = last_success_at
        if state is CircuitState.CLOSED or opened_at is None:
            return
        # A probe in flight when the bot stopped never finished: pause over,
        # the next request (or check) probes again.
        self._state = CircuitState.OPEN
        self._opened_at = opened_at
        self._cooldown = cooldown if state is CircuitState.OPEN else 0.0
        self._kind = kind or FailureKind.TRANSIENT
        self._escalation = escalation
        self._escalation_kind = escalation_kind
        self._consecutive_failures = failures
        self._await_check = bool(data.get("await_check"))
        self.down_since = down_since or opened_at

    def describe(self) -> str:
        """One line for /fallback status."""
        if self._state is CircuitState.CLOSED:
            if self._consecutive_failures:
                return f"работает (ошибок подряд: {self._consecutive_failures})"
            return "работает"
        reason = KIND_LABELS.get(self._kind, "сбой")
        if self._state is CircuitState.HALF_OPEN:
            return f"проверяется ({reason})"
        left = self.cooldown_remaining
        when = f"проверка через {format_duration(left)}" if left else "ждёт проверки"
        return f"на паузе: {reason}, {when}"


KIND_LABELS = {
    FailureKind.TRANSIENT: "сбой связи",
    FailureKind.RATE_LIMIT: "лимит запросов",
    FailureKind.QUOTA: "кончилась квота",
    FailureKind.AUTH: "ключ не принят",
}


def format_duration(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    if seconds < 90:
        return f"{seconds} с"
    if seconds < 90 * 60:
        return f"{round(seconds / 60)} мин"
    if seconds < 36 * 3600:
        return f"{seconds / 3600:.1f} ч".replace(".0 ч", " ч")
    return f"{seconds / 86400:.1f} дн".replace(".0 дн", " дн")


class HealthStore:
    """Breaker snapshots in one JSON file, written atomically on change."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._data: dict[str, dict] = {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and isinstance(raw.get("breakers"), dict):
                self._data = {str(k): v for k, v in raw["breakers"].items() if isinstance(v, dict)}
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            log.warning("Ignoring unreadable %s", path, exc_info=True)

    def bind(self, key: str, breaker: CircuitBreaker, fingerprint: str = "") -> None:
        """Restore ``breaker`` from the file and save it on every change.

        A snapshot taken with other credentials (``fingerprint``) is dropped.
        """
        saved = self._data.get(key)
        if saved is not None:
            if saved.get("fingerprint", "") == fingerprint:
                breaker.restore(saved)
                if breaker.state is not CircuitState.CLOSED:
                    log.info("Restored health of %s: %s", key, breaker.describe())
            else:
                log.info("Credentials of %s changed; forgetting its saved health", key)
                self._data.pop(key, None)

        def save(changed: CircuitBreaker) -> None:
            self._data[key] = {**changed.to_dict(), "fingerprint": fingerprint}
            self._write()

        breaker.on_change = save

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "breakers": dict(sorted(self._data.items()))}
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=str(self.path.parent), delete=False, suffix=".tmp",
        ) as tmp:
            try:
                json.dump(payload, tmp, ensure_ascii=False, indent=2)
                tmp.write("\n")
                tmp.flush()
                os.fsync(tmp.fileno())
            except BaseException:
                tmp.close()
                os.unlink(tmp.name)
                raise
        os.replace(tmp.name, self.path)
