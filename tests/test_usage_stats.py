"""Tests for scripts/usage_stats.py: log parsing, log↔Discord matching, estimates.

Pure functions only — no Discord API or docker access.
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


def load_module():
    path = Path(__file__).resolve().parent.parent / "scripts" / "usage_stats.py"
    spec = importlib.util.spec_from_file_location("usage_stats", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module  # dataclasses resolve annotations via sys.modules
    spec.loader.exec_module(module)
    return module


us = load_module()
UTC = timezone.utc
USER = 995245409960214558


def queued_line(ts: str, author: int, chars: int, channel: int = 10) -> str:
    return (
        f"{ts} INFO [tts_bot] Queued TTS guild=1 text_channel={channel} voice_channel=2 "
        f"author={author} queue=0 chars={chars} voice=fish-default"
    )


def msg(ts: datetime, raw: str, channel: int = 10, generated: str | None = "") -> "us.Message":
    return us.Message(us.to_snowflake(ts), ts, channel, raw, raw if generated == "" else generated)


class ParseLogTests(unittest.TestCase):
    def test_filters_author_and_keeps_first_timestamp(self):
        lines = [
            "2026-09-25 16:07:17,374 INFO [tts_bot.registry] Voice registry loaded",
            queued_line("2026-09-26 10:52:24,043", USER, 13),
            queued_line("2026-09-26 10:52:25,220", 42, 5),
            "Traceback (most recent call last):",
        ]
        events, first = us.parse_log(lines, USER)
        self.assertEqual(first, datetime(2026, 9, 25, 16, 7, 17, 374000, UTC))
        self.assertEqual(len(events), 1)
        ev = events[0]
        self.assertEqual((ev.guild_id, ev.channel_id, ev.author_id, ev.raw_chars), (1, 10, USER, 13))
        self.assertEqual(ev.ts, datetime(2026, 9, 26, 10, 52, 24, 43000, UTC))


class SnowflakeTests(unittest.TestCase):
    def test_round_trip_to_millisecond(self):
        dt = datetime(2026, 9, 26, 10, 52, 24, 43000, UTC)
        self.assertEqual(us.snowflake_time(us.to_snowflake(dt)), dt)


class MatchTests(unittest.TestCase):
    def test_matches_nearest_same_length_message_once(self):
        t = datetime(2026, 9, 26, 12, 0, 0, tzinfo=UTC)
        messages = [msg(t, "привет"), msg(t + timedelta(seconds=1), "пока!!"), msg(t + timedelta(seconds=2), "да")]
        events = [
            us.QueuedEvent(t + timedelta(seconds=1.5), 1, 10, USER, 6),
            us.QueuedEvent(t + timedelta(seconds=2.5), 1, 10, USER, 6),
        ]
        unmatched = us.match_events(events, messages)
        self.assertEqual(unmatched, [])
        self.assertEqual([m.voiced for m in messages], [True, True, False])

    def test_other_channel_or_too_late_stays_unmatched(self):
        t = datetime(2026, 9, 26, 12, 0, 0, tzinfo=UTC)
        messages = [msg(t, "привет", channel=11), msg(t - timedelta(minutes=5), "привет")]
        events = [us.QueuedEvent(t, 1, 10, USER, 6)]
        self.assertEqual(us.match_events(events, messages), events)
        self.assertFalse(any(m.voiced for m in messages))


class EstimateTests(unittest.TestCase):
    def test_calibrated_scales_pre_log_text_by_voiced_share(self):
        end = datetime(2026, 9, 28, 0, 0, tzinfo=UTC)
        start = end - timedelta(days=4)
        log_start = datetime(2026, 9, 26, 0, 0, tzinfo=UTC)
        before = [msg(datetime(2026, 9, 24, 12, tzinfo=UTC), "x" * 100),
                  msg(datetime(2026, 9, 25, 12, tzinfo=UTC), "x" * 100)]
        inside = [msg(datetime(2026, 9, 26, 12, tzinfo=UTC), "y" * 30),
                  msg(datetime(2026, 9, 27, 12, tzinfo=UTC), "z" * 70)]
        inside[0].voiced = True  # 30 of the 100 chars inside the window were voiced
        rep = us.build_report(USER, before + inside, [], [], log_start, start, end)
        est = us.estimates(rep)
        self.assertAlmostEqual(est["covered_days"], 2.0)
        self.assertEqual(est["exact_chars"], 30)
        self.assertAlmostEqual(est["voiced_share"], 0.3)
        self.assertAlmostEqual(est["calibrated_chars"], 30 + 200 * 0.3)
        self.assertAlmostEqual(est["linear_chars"], 30 / 2 * 4)
        self.assertAlmostEqual(est["calibrated_per_day"], 90 / 4)
        self.assertEqual(est["candidate_chars"], 300)

    def test_skipped_messages_and_repeats(self):
        end = datetime(2026, 9, 28, tzinfo=UTC)
        start = end - timedelta(days=1)
        a = msg(end - timedelta(hours=3), "ага")
        b = msg(end - timedelta(hours=2), "ага")
        empty = msg(end - timedelta(hours=1), "https://x", generated=None)
        a.voiced = b.voiced = True
        rep = us.build_report(USER, [a, b, empty], [], [], start, start, end)
        self.assertEqual(rep.repeat_chars, 3)
        self.assertEqual(us.estimates(rep)["exact_chars"], 6)
        self.assertEqual(rep.days[a.ts.date()].messages, 3)

    def test_unmatched_events_count_raw_chars(self):
        end = datetime(2026, 9, 28, tzinfo=UTC)
        start = end - timedelta(days=1)
        ev = us.QueuedEvent(end - timedelta(hours=1), 1, 10, USER, 12)
        rep = us.build_report(USER, [], [ev], [ev], start, start, end)
        self.assertEqual(rep.matched, 0)
        self.assertEqual(us.estimates(rep)["exact_chars"], 12)


if __name__ == "__main__":
    unittest.main()
