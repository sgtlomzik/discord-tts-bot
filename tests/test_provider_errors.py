"""How a provider error is classified decides whether the provider pauses.

A rejection of one request (bad input, unknown voice, text too long) must
not count toward pausing the provider for every other message.
"""

from __future__ import annotations

import unittest

import httpx

from ttsbot.gemini import GeminiError
from ttsbot.openrouter import http_error as gemini_http_error
from ttsbot.health import FailureKind, classify_failure


def _response(status, headers=None):
    return httpx.Response(status, headers=headers or {}, request=httpx.Request("POST", "https://openrouter.ai/x"))


class GeminiErrorKindTests(unittest.TestCase):
    def test_a_rejected_request_does_not_pause_gemini(self):
        for status, detail in ((400, "bad input"), (413, "too long"), (422, "bad input"),
                               (404, "voice 'Kore2' not found")):
            error = gemini_http_error(_response(status), detail)
            self.assertIsInstance(error, GeminiError)
            self.assertIs(classify_failure(error), FailureKind.REQUEST, status)

    def test_a_missing_model_or_endpoint_pauses_gemini(self):
        # A misspelled GEMINI_TTS_MODEL or a model OpenRouter retired fails
        # every request: the breaker must open instead of paying a round
        # trip per message forever.
        for status, detail in ((404, "No endpoints found for google/gemini-tts"), (404, "Not Found"),
                               (400, "google/gemini-ttz is not a valid model ID")):
            error = gemini_http_error(_response(status), detail)
            self.assertIsInstance(error, GeminiError)
            self.assertIs(classify_failure(error), FailureKind.TRANSIENT, detail)

    def test_provider_wide_failures_keep_their_kinds(self):
        kinds = {
            401: FailureKind.AUTH, 402: FailureKind.QUOTA,
            429: FailureKind.RATE_LIMIT, 500: FailureKind.TRANSIENT, 503: FailureKind.TRANSIENT,
        }
        for status, kind in kinds.items():
            self.assertIs(classify_failure(gemini_http_error(_response(status), "x")), kind, status)


if __name__ == "__main__":
    unittest.main()
