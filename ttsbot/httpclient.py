"""HTTP connection settings shared by the cloud TTS clients.

Every cloud provider builds its httpx client with ``provider_limits()`` and
calls ``attach_warmer()`` on it; the dispatcher's ``provider_for()`` then
lets the bot warm any voice's connection while its user is typing. A new
provider gets both by doing the same two calls.
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
# idle connection open for 60 s and more; httpx drops a connection the
# server has already closed before reusing it, so a longer value is safe.
KEEPALIVE_EXPIRY = 120.0


def provider_limits(max_connections: int = 10, max_keepalive_connections: int = 5) -> httpx.Limits:
    return httpx.Limits(
        max_connections=max_connections,
        max_keepalive_connections=max_keepalive_connections,
        keepalive_expiry=KEEPALIVE_EXPIRY,
    )


# Warm only when the connection has been idle this long: a busier one is
# already open, and this caps warm-ups at one per 30 s per provider.
WARM_IDLE_SECONDS = 30.0


class ConnectionWarmer:
    """Opens (or refreshes) a provider connection before a request needs it.

    The bot calls ``maybe_warm()`` when an allowed user starts typing, a few
    seconds before the message arrives. The warm-up is an unauthenticated
    ``GET`` of ``url`` on the provider host: it only establishes TCP + TLS
    for the keep-alive pool, costs no credits and its status code is
    ignored. Create it with ``attach_warmer`` so every request made through
    the client counts as activity.
    """

    def __init__(self, client: httpx.AsyncClient, name: str, url: str = "/") -> None:
        self._client = client
        self._name = name
        self._url = url
        self._last_used = float("-inf")
        self._task: asyncio.Task | None = None

    def touch(self) -> None:
        self._last_used = time.monotonic()

    def maybe_warm(self) -> asyncio.Task | None:
        if time.monotonic() - self._last_used < WARM_IDLE_SECONDS:
            return None
        if self._task is not None and not self._task.done():
            return None
        self.touch()
        self._task = asyncio.create_task(self._warm(), name=f"warm-{self._name}")
        return self._task

    async def _warm(self) -> None:
        started = time.perf_counter()
        try:
            response = await self._client.get(self._url, timeout=5.0)
        except httpx.HTTPError as exc:
            log.info("%s warm-up failed: %s: %s", self._name, type(exc).__name__, exc)
            return
        log.info(
            "%s connection warmed status=%s took=%.3fs",
            self._name, response.status_code, time.perf_counter() - started,
        )


def attach_warmer(client: httpx.AsyncClient, name: str, url: str = "/") -> ConnectionWarmer:
    """Return a warmer for ``client`` that every request through it touches.

    ``url`` must be on the provider's API host; the default ``/`` suits a
    client with a ``base_url``.
    """
    warmer = ConnectionWarmer(client, name, url)

    async def touch(request: httpx.Request) -> None:
        warmer.touch()

    client.event_hooks["request"] = [*client.event_hooks.get("request", []), touch]
    return warmer
