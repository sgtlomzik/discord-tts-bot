"""The dispatcher keeps providers in a registry, not in named slots.

It had one attribute and one breaker property per provider (cloud,
fish, gemini, elevenlabs), so every new provider meant editing the
dispatcher, the pipeline and the commands.
"""

from __future__ import annotations

import types
import unittest
from unittest.mock import MagicMock

from ttsbot.health import CircuitBreaker
from ttsbot.providers import TTSDispatcher


def _provider(name):
    return types.SimpleNamespace(name=name, config=types.SimpleNamespace(api_key=f"{name}-key"))


class DispatcherRegistryTests(unittest.TestCase):
    def test_providers_are_found_by_name_in_registration_order(self):
        fish, eleven = _provider("fish"), _provider("elevenlabs")
        d = TTSDispatcher(local=MagicMock(), providers={"fish": fish, "elevenlabs": eleven})
        self.assertIs(d.provider_for("fish"), fish)
        self.assertIs(d.provider_for("elevenlabs"), eleven)
        self.assertIsNone(d.provider_for("minimax"))
        self.assertIsNone(d.provider_for(None))
        self.assertEqual(d.provider_names(), ["fish", "elevenlabs"])

    def test_each_provider_gets_its_own_labelled_breaker(self):
        d = TTSDispatcher(local=MagicMock(), providers={"fish": _provider("fish"), "gemini": _provider("gemini")})
        fish_cb, gemini_cb = d.breaker_for("fish"), d.breaker_for("gemini")
        self.assertIsInstance(fish_cb, CircuitBreaker)
        self.assertIsNot(fish_cb, gemini_cb)
        self.assertEqual((fish_cb.name, gemini_cb.name), ("Fish", "Gemini"))
        self.assertIsNone(d.breaker_for("minimax"))  # not configured

    def test_a_given_breaker_is_used(self):
        cb = CircuitBreaker()
        d = TTSDispatcher(local=MagicMock(), providers={"minimax": _provider("minimax")}, breakers={"minimax": cb})
        self.assertIs(d.breaker_for("minimax"), cb)

    def test_a_new_provider_needs_no_dispatcher_change(self):
        d = TTSDispatcher(local=MagicMock())
        acme = _provider("acme")
        d.register("acme", acme)
        self.assertIs(d.provider_for("acme"), acme)
        self.assertEqual(d.provider_names(), ["acme"])
        self.assertIsNotNone(d.breaker_for("acme"))

    def test_an_unconfigured_provider_is_not_registered(self):
        d = TTSDispatcher(local=MagicMock(), providers={"fish": None})
        self.assertEqual(d.provider_names(), [])
        self.assertIsNone(d.breaker_for("fish"))

    def test_the_named_slots_are_gone(self):
        d = TTSDispatcher(local=MagicMock())
        for name in ("cloud", "fish", "gemini", "elevenlabs", "circuit_breaker",
                     "fish_circuit_breaker", "gemini_circuit_breaker", "elevenlabs_circuit_breaker"):
            self.assertFalse(hasattr(d, name), name)


if __name__ == "__main__":
    unittest.main()
