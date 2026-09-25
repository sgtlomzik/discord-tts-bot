"""Exception types shared by the TTS providers."""


class QuotaExhaustedError(Exception):
    """The provider's balance or plan is used up; retrying soon is pointless.

    Provider errors inherit it alongside their own base class, so the
    dispatcher can pause any provider with one ``isinstance`` check.
    """
