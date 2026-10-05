"""Exception types shared by the TTS providers.

Provider errors inherit one of these markers alongside their own base
class, so the health layer (ttsbot.health) can tell what a failure means
for the provider with one ``isinstance`` check, whatever the provider.
"""


class QuotaExhaustedError(Exception):
    """The provider's balance or plan is used up; retrying soon is pointless.

    Provider errors inherit it alongside their own base class, so the
    dispatcher can pause any provider with one ``isinstance`` check.
    """


class AuthError(Exception):
    """The API key is invalid, revoked or lacks a permission: nothing will
    work until the configuration changes."""


class RateLimitError(Exception):
    """Too many requests (HTTP 429). ``retry_after`` is the server's
    Retry-After in seconds, None if it did not send one."""

    retry_after: float | None = None


class RequestError(Exception):
    """A problem of this one request (unknown voice, too long for the
    credits left, plan does not allow this voice), not of the provider:
    other requests to the same provider still work."""
