"""How a provider error is classified decides whether the provider pauses.

A rejection of one request (bad input, unknown voice, text too long) must
not count toward pausing the provider for every other message.
"""

from __future__ import annotations

import unittest

import httpx

from ttsbot.gemini import GeminiError, _http_error as gemini_http_error
from ttsbot.health import FailureKind, classify_failure


def _response(status, headers=None):
    return httpx.Response(status, headers=headers or {}, request=httpx.Request("POST", "https://openrouter.ai/x"))


class GeminiErrorKindTests(unittest.TestCase):
    def test_a_rejected_request_does_not_pause_gemini(self):
        for status in (400, 404, 413, 422):
            error = gemini_http_error(_response(status), "bad input")
            self.assertIsInstance(error, GeminiError)
            self.assertIs(classify_failure(error), FailureKind.REQUEST, status)

    def test_provider_wide_failures_keep_their_kinds(self):
        kinds = {
            401: FailureKind.AUTH, 402: FailureKind.QUOTA,
            429: FailureKind.RATE_LIMIT, 500: FailureKind.TRANSIENT, 503: FailureKind.TRANSIENT,
        }
        for status, kind in kinds.items():
            self.assertIs(classify_failure(gemini_http_error(_response(status), "x")), kind, status)


if __name__ == "__main__":
    unittest.main()
