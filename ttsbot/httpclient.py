"""HTTP connection settings shared by the cloud TTS clients.

Every cloud provider builds its httpx client with ``provider_limits()`` and
calls ``attach_warmer()`` on it; the dispatcher's ``provider_for()`` then
lets the bot warm any voice's connection while its user is typing, and keep
it open while the bot sits in a voice channel. A new provider gets all of
this, and the per-request log line, by doing the same two calls.
"""

from __future__ import annotations

import asyncio
import logging
import time

import httpx

log = logging.getLogger("tts_bot")

# How long an idle keep-alive connection stays open. httpx closes it after
# 5 s by default, so almost every message after a pause paid for a new TLS
# handshake: first audio 0.33 s instead of 0.18-0.20 s (ElevenLabs through
# the VPN, 2026-09-29), 0.63 s for the very first request. ElevenLabs kept an
# idle connection open for 130 s and more; httpx drops a connection the
# server has already closed before reusing it, so a longer value is safe.
KEEPALIVE_EXPIRY = 120.0


def provider_limits(max_connections: int = 10, max_keepalive_connections: int = 5) -> httpx.Limits:
    return httpx.Limits(
        max_connections=max_connections,
        max_keepalive_connections=max_keepalive_connections,
        keepalive_expiry=KEEPALIVE_EXPIRY,
    )


# Warm on typing only when the connection has been idle this long: a busier
# one is already open, and this caps warm-ups at one per 30 s per provider.
WARM_IDLE_SECONDS = 30.0

# httpcore trace event that means the request did not reuse a pooled
# connection (a TCP + TLS handshake follows).
_NEW_CONNECTION_EVENT = "connection.connect_tcp.started"


class ConnectionWarmer:
    """Opens (or refreshes) a provider connection before a request needs it.

    The bot calls ``maybe_warm()`` when an allowed user starts typing, a few
    seconds before the message arrives, and every few seconds while an
    allowed user with this provider's voice is in the bot's voice channel
    (``reason="keepalive"``, with a longer ``idle``). The warm-up is an
    unauthenticated ``GET`` of ``url`` on the provider host: it only
    establishes TCP + TLS for the keep-alive pool, costs no credits and its
    status code is ignored. Create it with ``attach_warmer`` so every
    request made through the client counts as activity.
    """

    def __init__(self, client: httpx.AsyncClient, name: str, url: str = "/") -> None:
        self._client = client
        self.name = name
        self._url = url
        self._last_used = float("-inf")
        self._task: asyncio.Task | None = None

    def touch(self) -> float | None:
        """Mark activity; return the seconds since the previous one (None: first)."""
        now = time.monotonic()
        idle = now - self._last_used
        self._last_used = now
        return idle if idle != float("inf") else None

    def maybe_warm(self, reason: str = "typing", idle: float | None = None) -> asyncio.Task | None:
        """Warm unless the connection was used in the last ``idle`` seconds
        (WARM_IDLE_SECONDS by default) or a warm-up is already running."""
        if time.monotonic() - self._last_used < (WARM_IDLE_SECONDS if idle is None else idle):
            return None
        if self._task is not None and not self._task.done():
            return None
        self._task = asyncio.create_task(self._warm(reason), name=f"warm-{self.name}")
        return self._task

    async def _warm(self, reason: str) -> None:
        started = time.perf_counter()
        try:
            response = await self._client.get(
                self._url, timeout=5.0, extensions={"ttsbot_purpose": reason},
            )
        except httpx.HTTPError as exc:
            log.info("%s warm-up failed reason=%s: %s: %s", self.name, reason, type(exc).__name__, exc)
            return
        timing = response.request.extensions.get("ttsbot_timing") or {}
        new = timing.get("new", False)
        # A keep-alive ping that found the connection open is routine; one
        # that had to reconnect, and every typing warm-up, is worth a line.
        level = logging.DEBUG if reason == "keepalive" and not new else logging.INFO
        log.log(
            level, "%s connection warmed status=%s reason=%s conn=%s idle_before_s=%s took=%.3fs",
            self.name, response.status_code, reason, "new" if new else "reused",
            _seconds(timing.get("idle")), time.perf_counter() - started,
        )


def _seconds(value: float | None) -> str:
    return "-" if value is None else f"{value:.1f}"


def attach_warmer(client: httpx.AsyncClient, name: str, url: str = "/") -> ConnectionWarmer:
    """Return a warmer for ``client`` and log every request made through it.

    Each request counts as activity for the warmer, and one line per
    response says whether it reused a pooled connection, how long the
    client had been idle and how long the response headers took. ``url``
    must be on the provider's API host; the default ``/`` suits a client
    with a ``base_url``.
    """
    warmer = ConnectionWarmer(client, name, url)

    async def on_request(request: httpx.Request) -> None:
        timing = {"started": time.perf_counter(), "new": False, "idle": warmer.touch()}
        previous = request.extensions.get("trace")

        async def trace(event: str, info: dict) -> None:
            if event == _NEW_CONNECTION_EVENT:
                timing["new"] = True
            if previous is not None:
                result = previous(event, info)
                if asyncio.iscoroutine(result):
                    await result

        request.extensions["trace"] = trace
        request.extensions["ttsbot_timing"] = timing

    async def on_response(response: httpx.Response) -> None:
        request = response.request
        timing = request.extensions.get("ttsbot_timing")
        if timing is None or "ttsbot_purpose" in request.extensions:
            return  # warm-ups log their own line
        log.info(
            "%s HTTP %s %s status=%d conn=%s idle_before_s=%s headers_s=%.3f",
            name, request.method, request.url.path, response.status_code,
            "new" if timing["new"] else "reused", _seconds(timing["idle"]),
            time.perf_counter() - timing["started"],
        )

    client.event_hooks["request"] = [*client.event_hooks.get("request", []), on_request]
    client.event_hooks["response"] = [*client.event_hooks.get("response", []), on_response]
    return warmer
