"""A pause never outlasts its length when the wall clock jumps back.

Breaker times are wall-clock (they are saved and restored across
restarts), so a clock stepped back (an NTP fix, a host without an RTC)
or a restored pause dated in the future stretched the pause by the jump.
"""

from __future__ import annotations

import unittest

from ttsbot.health import CircuitBreaker, CircuitBreakerConfig, FailureKind


class Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class BreakerClockTests(unittest.TestCase):
    def _open_breaker(self, clock):
        breaker = CircuitBreaker(CircuitBreakerConfig(failure_threshold=1, cooldown_seconds=60), clock=clock)
        breaker.record_failure(FailureKind.TRANSIENT, error="down")
        return breaker

    def test_a_clock_stepped_back_does_not_stretch_the_pause(self):
        clock = Clock(1_000_000.0)
        breaker = self._open_breaker(clock)
        clock.now -= 86_400  # the clock jumps a day back
        self.assertLessEqual(breaker.cooldown_remaining, 60)
        clock.now += 61
        self.assertTrue(breaker.allow_request())

    def test_a_restored_pause_from_the_future_is_capped(self):
        saved = self._open_breaker(Clock(2_000_000.0)).to_dict()
        breaker = CircuitBreaker(CircuitBreakerConfig(failure_threshold=1, cooldown_seconds=60),
                                 clock=Clock(1_000_000.0))
        breaker.restore(saved)
        self.assertLessEqual(breaker.cooldown_remaining, 60)


if __name__ == "__main__":
    unittest.main()
