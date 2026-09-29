"""HTTP connection settings shared by the cloud TTS clients."""

from __future__ import annotations

import httpx

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
