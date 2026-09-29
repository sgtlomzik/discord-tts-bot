#!/usr/bin/env python3
"""Per-user TTS character usage: exact where the bot logs cover, estimated elsewhere.

Two real data sources:

* Bot logs (``docker logs``): every ``Queued TTS ... author=<id> chars=<n>``
  line is a message the bot actually sent to synthesis. Docker drops these
  logs when the container is recreated, so they usually cover only the days
  since the last deploy.
* Discord channel history (REST API, bot token): every message the user wrote
  in the channels the bot reads, for the whole period. The bot voices a
  message only while its author sits in a voice channel, so history alone
  over-counts.

Each logged message is matched to its Discord message and passed through the
bot's own ``normalize_for_tts`` (emoji aliases, mentions, URL stripping, the
TTS_MAX_CHARS cap), so "generated chars" is the text length the provider
received. The share of the user's text that was voiced inside the log window
is then applied to the Discord history outside it to fill the full period.

Runs inside the bot image (needs ``ttsbot``, ``discord`` and ``emoji``); see
"Статистика использования" in README.ru.md for the exact command.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

API = "https://discord.com/api/v10"
USER_AGENT = "DiscordBot (https://github.com/sgtlomzik/discord-tts-bot, usage-stats)"
DISCORD_EPOCH_MS = 1420070400000
# How far a Queued TTS log line may lag behind the Discord message timestamp
# (merge window + queue put); matching also requires the same raw length.
MATCH_WINDOW_S = 15.0

LINE_TS_RE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),(\d{3}) ")
QUEUED_RE = re.compile(
    r"Queued TTS guild=(\d+) text_channel=(\d+) voice_channel=\d+ author=(\d+) queue=\d+ chars=(\d+)"
)


@dataclass
class QueuedEvent:
    ts: datetime
    guild_id: int
    channel_id: int
    author_id: int
    raw_chars: int


@dataclass
class Message:
    id: int
    ts: datetime
    channel_id: int
    raw: str  # text the bot would pass to normalization (content or fixed phrase)
    generated: str | None  # normalized text, None when the bot would skip it
    voiced: bool = False


@dataclass
class DayStats:
    messages: int = 0
    candidate_chars: int = 0  # generated chars if every message had been voiced
    outside_chars: int = 0  # part of candidate_chars written before the logs start
    voiced_chars: int = 0  # exact, from matched log events
    covered: float = 0.0  # fraction of the day inside the log window


@dataclass
class Report:
    user_id: int
    start: datetime
    end: datetime
    log_start: datetime | None
    days: dict[date, DayStats] = field(default_factory=dict)
    events: int = 0
    matched: int = 0
    unmatched_raw_chars: int = 0
    repeat_chars: int = 0


# --------------------------------------------------------------------------
# Pure helpers (unit-tested)
# --------------------------------------------------------------------------


def parse_log(lines: Iterable[str], author_id: int | None = None) -> tuple[list[QueuedEvent], datetime | None]:
    """Return Queued TTS events (optionally for one author) and the first log timestamp."""
    events: list[QueuedEvent] = []
    first: datetime | None = None
    for line in lines:
        m = LINE_TS_RE.match(line)
        if not m:
            continue
        ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(
            microsecond=int(m.group(2)) * 1000, tzinfo=timezone.utc,
        )
        if first is None:
            first = ts
        q = QUEUED_RE.search(line)
        if q and (author_id is None or int(q.group(3)) == author_id):
            events.append(QueuedEvent(ts, int(q.group(1)), int(q.group(2)), int(q.group(3)), int(q.group(4))))
    return events, first


def to_snowflake(dt: datetime) -> int:
    return (int(dt.timestamp() * 1000) - DISCORD_EPOCH_MS) << 22


def snowflake_time(value: int | str) -> datetime:
    return datetime.fromtimestamp(((int(value) >> 22) + DISCORD_EPOCH_MS) / 1000, timezone.utc)


def match_events(events: list[QueuedEvent], messages: list[Message]) -> list[QueuedEvent]:
    """Mark the Discord message behind each log event as voiced.

    A match is the nearest not-yet-used message in the same channel whose raw
    length equals the logged ``chars`` and which was sent no later than
    ``MATCH_WINDOW_S`` before the log line. Returns the events left unmatched.
    """
    by_channel: dict[int, list[Message]] = defaultdict(list)
    for msg in messages:
        by_channel[msg.channel_id].append(msg)
    unmatched: list[QueuedEvent] = []
    for ev in events:
        best: Message | None = None
        for msg in by_channel.get(ev.channel_id, ()):
            lag = (ev.ts - msg.ts).total_seconds()
            if msg.voiced or len(msg.raw) != ev.raw_chars or not -1.0 <= lag <= MATCH_WINDOW_S:
                continue
            if best is None or abs(lag) < abs((ev.ts - best.ts).total_seconds()):
                best = msg
        if best is None:
            unmatched.append(ev)
        else:
            best.voiced = True
    return unmatched


def day_coverage(day: date, log_start: datetime | None, end: datetime) -> float:
    """Fraction of ``day`` (UTC) that lies inside [log_start, end]."""
    if log_start is None:
        return 0.0
    lo = datetime.combine(day, datetime.min.time(), timezone.utc)
    hi = lo + timedelta(days=1)
    overlap = (min(hi, end) - max(lo, log_start)).total_seconds()
    return max(0.0, overlap) / 86400.0


def build_report(
    user_id: int, messages: list[Message], events: list[QueuedEvent],
    unmatched: list[QueuedEvent], log_start: datetime | None, start: datetime, end: datetime,
) -> Report:
    rep = Report(user_id, start, end, log_start, events=len(events), matched=len(events) - len(unmatched))
    day = start.date()
    while day <= end.date():
        rep.days[day] = DayStats(covered=day_coverage(day, log_start, end))
        day += timedelta(days=1)
    seen: set[str] = set()
    for msg in messages:
        st = rep.days.get(msg.ts.date())
        if st is None:
            continue
        st.messages += 1
        n = len(msg.generated or "")
        st.candidate_chars += n
        if log_start is None or msg.ts < log_start:
            st.outside_chars += n
        if msg.voiced:
            st.voiced_chars += n
            if msg.generated in seen:
                rep.repeat_chars += n
            seen.add(msg.generated or "")
    for ev in unmatched:
        rep.unmatched_raw_chars += ev.raw_chars
    return rep


def estimates(rep: Report) -> dict[str, float]:
    """Exact figures inside the log window plus two ways to fill the rest.

    * ``calibrated``: the user's real Discord text written before the logs
      start, times the voiced share measured after. Primary figure.
    * ``linear``: exact voiced chars per covered day times the period length.
    """
    days = rep.days.values()
    covered_days = sum(d.covered for d in days)
    # Unmatched log events still cost chars; count their raw length (upper bound).
    exact = sum(d.voiced_chars for d in days) + rep.unmatched_raw_chars
    cand_in = sum(d.candidate_chars - d.outside_chars for d in days)
    # Unmatched raw chars can push this past 1; nobody voices more than they write.
    share = min(exact / cand_in, 1.0) if cand_in else 0.0
    calibrated = exact + sum(d.outside_chars for d in days) * share
    period_days = (rep.end - rep.start).total_seconds() / 86400.0
    linear = exact / covered_days * period_days if covered_days else 0.0
    return {
        "period_days": period_days,
        "covered_days": covered_days,
        "exact_chars": exact,
        "voiced_share": share,
        "candidate_chars": sum(d.candidate_chars for d in rep.days.values()),
        "calibrated_chars": calibrated,
        "linear_chars": linear,
        "calibrated_per_day": calibrated / period_days if period_days else 0.0,
        "calibrated_per_30d": calibrated / period_days * 30 if period_days else 0.0,
    }


# --------------------------------------------------------------------------
# Discord + bot-state I/O
# --------------------------------------------------------------------------


class DiscordAPI:
    def __init__(self, token: str) -> None:
        self._headers = {"Authorization": f"Bot {token}", "User-Agent": USER_AGENT}

    def get(self, path: str):
        while True:
            req = urllib.request.Request(API + path, headers=self._headers)
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    return json.load(resp)
            except urllib.error.HTTPError as exc:
                if exc.code == 429:
                    time.sleep(float(json.load(exc).get("retry_after", 1.0)) + 0.1)
                    continue
                raise

    def history(self, channel_id: int, after: datetime) -> Iterable[dict]:
        """Messages newer than ``after``, newest first."""
        before: str | None = None
        while True:
            page = self.get(f"/channels/{channel_id}/messages?limit=100" + (f"&before={before}" if before else ""))
            if not page:
                return
            for msg in page:
                if snowflake_time(msg["id"]) < after:
                    return
                yield msg
            before = page[-1]["id"]


def mention_map(msg: dict, roles: dict[str, str], channels: dict[str, str]) -> dict[str, str]:
    """REST equivalent of textnorm.build_mention_say_map (display names)."""
    say: dict[str, str] = {}
    for user in msg.get("mentions", ()):
        say[user["id"]] = (user.get("member") or {}).get("nick") or user.get("global_name") or user["username"]
    for role_id in msg.get("mention_roles", ()):
        if role_id in roles:
            say[role_id] = roles[role_id]
    for channel_id in re.findall(r"<#(\d+)>", msg.get("content", "")):
        if channel_id in channels:
            say[channel_id] = f"канал {channels[channel_id]}"
    return say


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--user", type=int, required=True, help="Discord user id")
    p.add_argument("--days", type=float, default=30.0, help="period length ending now (default 30)")
    p.add_argument("--log-file", default="-", help="bot log file, '-' = stdin (default)")
    p.add_argument("--guild", type=int, help="guild id (default: taken from the logs)")
    p.add_argument("--channel", type=int, action="append", default=[],
                   help="text channel id to read; repeatable (default: channels seen in the logs)")
    p.add_argument("--json", action="store_true", help="print JSON instead of Markdown")
    args = p.parse_args(argv)

    from ttsbot import config
    from ttsbot.store import BotConfigStore
    from ttsbot.textnorm import normalize_for_tts

    config.reload()
    store = BotConfigStore(config.BOT_CONFIG_PATH, set())
    store.load()  # applies the runtime tts_max_chars override to config
    aliases = store.emoji_say_map()

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=args.days)
    stream = sys.stdin if args.log_file == "-" else open(args.log_file, encoding="utf-8", errors="replace")
    events, log_start = parse_log(stream, args.user)
    events = [e for e in events if e.ts >= start]
    guild_id = args.guild or (events[0].guild_id if events else None)
    channel_ids = args.channel or sorted({e.channel_id for e in events})
    if guild_id is None or not channel_ids:
        p.error("no Queued TTS lines for this user in the logs; pass --guild and --channel")

    api = DiscordAPI(os.environ["DISCORD_TOKEN"])
    roles = {r["id"]: r["name"] for r in api.get(f"/guilds/{guild_id}/roles")}
    channels = {c["id"]: c["name"] for c in api.get(f"/guilds/{guild_id}/channels")}
    fixed = store.fixed_phrase_for_user(guild_id, args.user)

    messages: list[Message] = []
    for channel_id in channel_ids:
        for msg in api.history(channel_id, start):
            if int(msg["author"]["id"]) != args.user:
                continue
            raw = fixed if fixed is not None else msg.get("content", "")
            generated = normalize_for_tts(
                raw, max_chars=config.TTS_MAX_CHARS, emoji_aliases=aliases,
                mentions={} if fixed is not None else mention_map(msg, roles, channels),
            )
            messages.append(Message(int(msg["id"]), snowflake_time(msg["id"]), channel_id, raw, generated))
    messages.sort(key=lambda m: m.ts)

    unmatched = match_events(events, messages)
    rep = build_report(args.user, messages, events, unmatched, log_start, start, end)
    est = estimates(rep)
    if args.json:
        print(json.dumps({
            "user_id": args.user, "guild_id": guild_id, "channels": channel_ids,
            "start": start.isoformat(), "end": end.isoformat(),
            "log_start": log_start.isoformat() if log_start else None,
            "log_events": rep.events, "log_matched": rep.matched, "repeat_chars": rep.repeat_chars,
            **est,
            "days": {d.isoformat(): vars(s) for d, s in rep.days.items()},
        }, ensure_ascii=False, indent=2))
    else:
        print(render_markdown(rep, est, guild_id, channel_ids))
    return 0


def _n(value: float) -> str:
    return f"{round(value):,}".replace(",", " ")


def render_markdown(rep: Report, est: dict[str, float], guild_id: int, channel_ids: list[int]) -> str:
    out = [
        f"# TTS usage for user `{rep.user_id}`",
        "",
        f"Period: `{rep.start:%Y-%m-%d %H:%M}` .. `{rep.end:%Y-%m-%d %H:%M}` UTC ({est['period_days']:.1f} days)  ",
        f"Guild `{guild_id}`, channels {', '.join(f'`{c}`' for c in channel_ids)}  ",
        f"Bot logs from: `{rep.log_start:%Y-%m-%d %H:%M}` UTC ({est['covered_days']:.2f} days of the period)"
        if rep.log_start else "Bot logs: none",
        "",
        "## Summary",
        "",
        "| Metric | Chars |",
        "|---|---:|",
        f"| Generated, exact (log window) | {_n(est['exact_chars'])} |",
        f"| **Generated, full period (calibrated estimate)** | **{_n(est['calibrated_chars'])}** |",
        f"| Average per day | {_n(est['calibrated_per_day'])} |",
        f"| Per 30 days | {_n(est['calibrated_per_30d'])} |",
        f"| Linear extrapolation of the log window (cross-check) | {_n(est['linear_chars'])} |",
        f"| Upper bound: all of the user's text, as if always in voice | {_n(est['candidate_chars'])} |",
        "",
        f"Voiced share inside the log window: {est['voiced_share']:.0%}. "
        f"Log events matched to Discord messages: {rep.matched}/{rep.events}"
        + (f" (unmatched add {_n(rep.unmatched_raw_chars)} raw chars)." if rep.unmatched_raw_chars else ".")
        + f" Repeated phrases in the log window: {_n(rep.repeat_chars)} chars (may have been served from the cache).",
        "",
        "## By day (UTC)",
        "",
        "| Day | Messages | User text, chars | Voiced, chars | Source |",
        "|---|---:|---:|---:|---|",
    ]
    share = est["voiced_share"]
    for day, st in rep.days.items():
        if st.covered >= 0.999:
            voiced, source = _n(st.voiced_chars), "log"
        elif st.covered > 0:
            voiced = _n(st.voiced_chars + st.outside_chars * share)
            source = f"log {st.covered:.0%} + estimate"
        else:
            voiced, source = f"~{_n(st.outside_chars * share)}", "estimate"
        out.append(f"| {day} | {st.messages} | {_n(st.candidate_chars)} | {voiced} | {source} |")
    out += [
        "",
        "Notes: \"generated\" is the text length after the bot's normalization (what the TTS provider"
        " receives). The estimate assumes the user was in voice for the same share of their chat"
        " outside the log window as inside it; it is only as good as that window is representative.",
    ]
    return "\n".join(out)


if __name__ == "__main__":
    sys.exit(main())
