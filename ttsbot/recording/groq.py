"""Groq Whisper transcription client and its audio-seconds budget.

Groq's free plan allows 7,200 audio seconds per hour and 28,800 per day
(20 requests per minute, 25 MB per file). Each response carries
``x-ratelimit-remaining-audio-seconds`` and
``x-ratelimit-reset-audio-seconds`` for the hourly window, so the budget
trusts those and keeps its own ledger for what the headers do not show:
the day, and the time after a restart.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

log = logging.getLogger("tts_bot")

GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
MAX_UPLOAD_BYTES = 25 * 1024 * 1024


class TranscriptionError(Exception):
    """A request failed; ``retry`` says whether sending it again can help."""

    def __init__(self, message: str, *, retry: bool, wait: float = 0.0) -> None:
        super().__init__(message)
        self.retry = retry
        self.wait = wait


@dataclass(frozen=True, slots=True)
class Transcription:
    data: dict  # Groq's verbose_json
    took: float
    remaining_audio_seconds: float | None
    reset_audio_seconds: float | None


def parse_duration(value: str | None) -> float | None:
    """'4.5s', '1m2.5s', '2h0m3s', '120ms' -> seconds."""
    if not value:
        return None
    total, found = 0.0, False
    for number, unit in re.findall(r"([\d.]+)(ms|h|m|s)", value):
        found = True
        total += float(number) * {"ms": 0.001, "h": 3600, "m": 60, "s": 1}[unit]
    return total if found else None


class GroqTranscriber:
    def __init__(self, api_key: str, *, model: str, language: str, prompt: str = "",
                 timeout: float = 120.0, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=15.0), transport=transport,
            headers={"Authorization": f"Bearer {api_key}"},
        )
        self.model = model
        self.language = language
        self.prompt = prompt

    async def aclose(self) -> None:
        await self._client.aclose()

    async def transcribe(self, path: Path, prompt: str | None = None) -> Transcription:
        form = {
            "model": self.model,
            "response_format": "verbose_json",
            "timestamp_granularities[]": "segment",
            "temperature": "0",
        }
        if self.language:
            form["language"] = self.language
        if prompt or self.prompt:
            form["prompt"] = prompt or self.prompt
        started = time.perf_counter()
        try:
            with path.open("rb") as audio:
                response = await self._client.post(
                    GROQ_URL, data=form, files={"file": (path.name, audio, "audio/ogg")}
                )
        except httpx.HTTPError as exc:
            raise TranscriptionError(f"{type(exc).__name__}: {exc}", retry=True, wait=30.0) from exc
        took = time.perf_counter() - started
        headers = response.headers
        if response.status_code == 429:
            retry_after = headers.get("retry-after", "")
            wait = (float(retry_after) if retry_after.replace(".", "", 1).isdigit() else None) \
                or parse_duration(headers.get("x-ratelimit-reset-audio-seconds")) or 60.0
            raise TranscriptionError(f"HTTP 429: {response.text[:200]}", retry=True, wait=wait)
        if response.status_code >= 500:
            raise TranscriptionError(f"HTTP {response.status_code}: {response.text[:200]}", retry=True, wait=60.0)
        if response.status_code != 200:
            # 400 (bad file), 401 (key), 413 (too large): sending it again won't help.
            raise TranscriptionError(f"HTTP {response.status_code}: {response.text[:300]}", retry=False)
        try:
            data = response.json()
        except ValueError as exc:
            raise TranscriptionError("response is not JSON", retry=True, wait=60.0) from exc
        remaining = headers.get("x-ratelimit-remaining-audio-seconds")
        return Transcription(
            data=data, took=took,
            remaining_audio_seconds=float(remaining) if remaining else None,
            reset_audio_seconds=parse_duration(headers.get("x-ratelimit-reset-audio-seconds")),
        )


class AudioBudget:
    """Decide when the next file may be sent without hitting Groq's limits.

    Groq's hourly audio limit is a bucket that refills continuously (7,200
    s per hour = 2 s per second: 9 s used showed a 4.5 s reset). After each
    response the budget knows the bucket's level from the headers and
    extrapolates the refill. Before the first response (after a restart) it
    falls back to the ledger's last hour. The day limit always comes from
    the ledger (one ``[epoch, seconds]`` line per request, surviving
    restarts; lines older than a day are dropped when it is loaded).
    """

    def __init__(self, ledger: Path, *, per_hour: float, per_day: float, min_billed: float = 0.0,
                 requests_per_minute: int = 20) -> None:
        self._ledger = ledger
        self.per_hour = per_hour
        self.per_day = per_day
        self.min_billed = min_billed
        self._rate = per_hour / 3600.0
        self._spacing = 60.0 / max(1, requests_per_minute)
        self._entries: list[tuple[float, float]] = []
        self._server: tuple[float, float] | None = None  # (bucket level, when)
        self._blocked_until = 0.0
        self._last_request = 0.0
        if ledger.exists():
            cutoff = time.time() - 86400
            for line in ledger.read_text(encoding="utf-8").splitlines():
                try:
                    at, seconds = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if at >= cutoff:
                    self._entries.append((at, seconds))
            ledger.write_text("".join(json.dumps(list(e)) + "\n" for e in self._entries), encoding="utf-8")

    def billed(self, seconds: float) -> float:
        return max(seconds, self.min_billed)

    def used(self, window: float, now: float | None = None) -> float:
        now = time.time() if now is None else now
        return sum(s for at, s in self._entries if at > now - window)

    def wait_for(self, seconds: float, now: float | None = None) -> float:
        """Seconds to wait before a file of ``seconds`` may be sent (0 = now)."""
        now = time.time() if now is None else now
        need = self.billed(seconds)
        if need > self.per_hour or need > self.per_day:
            raise ValueError(f"{need:.0f} s never fits the limits")
        wait = max(0.0, self._blocked_until - now, self._last_request + self._spacing - now)
        if self._server is not None:
            level, at = self._server
            available = min(self.per_hour, level + self._rate * (now - at))
            if need > available:
                wait = max(wait, (need - available) / self._rate)
        else:
            wait = max(wait, self._window_wait(3600.0, self.per_hour, need, now))
        return max(wait, self._window_wait(86400.0, self.per_day, need, now))

    def _window_wait(self, window: float, limit: float, need: float, now: float) -> float:
        excess = self.used(window, now) + need - limit
        if excess <= 0:
            return 0.0
        # The oldest requests leave the window first.
        for at, s in sorted(e for e in self._entries if e[0] > now - window):
            excess -= s
            if excess <= 0:
                return at + window - now
        return window

    def record(self, seconds: float, remaining: float | None = None, now: float | None = None) -> None:
        """Book a sent file; ``remaining`` is Groq's bucket level after it."""
        now = time.time() if now is None else now
        entry = (now, round(self.billed(seconds), 3))
        self._entries.append(entry)
        self._last_request = now
        if remaining is not None:
            self._server = (remaining, now)
        elif self._server is not None:
            level, at = self._server
            self._server = (min(self.per_hour, level + self._rate * (now - at)) - entry[1], now)
        with self._ledger.open("a", encoding="utf-8") as out:
            out.write(json.dumps(list(entry)) + "\n")

    def block(self, seconds: float, now: float | None = None) -> None:
        """Send nothing for ``seconds`` (after a 429 or a server error)."""
        now = time.time() if now is None else now
        self._blocked_until = max(self._blocked_until, now + seconds)
        self._last_request = max(self._last_request, now)
