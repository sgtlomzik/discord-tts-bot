"""A damaged state file stops startup instead of being replaced.

voices.json used to be reseeded (and overwritten) when it could not be
read, losing every cloud voice and the fallback chain; config.json was
silently ignored and then overwritten by the next save. The container
restarts on failure, so the file must stay exactly as it was.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ttsbot import voice_registry
from ttsbot.models import VOICE_PROFILES
from ttsbot.statefile import StateFileError, read_json_object
from ttsbot.store import BotConfigStore


class ReadJsonObjectTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "state.json"

    def tearDown(self):
        self.dir.cleanup()

    def test_missing_file_is_none(self):
        self.assertIsNone(read_json_object(self.path))

    def test_object_is_returned(self):
        self.path.write_text('{"a": 1}', encoding="utf-8")
        self.assertEqual(read_json_object(self.path), {"a": 1})

    def test_broken_json_raises_with_the_path(self):
        self.path.write_text('{"a": ', encoding="utf-8")
        with self.assertRaises(StateFileError) as ctx:
            read_json_object(self.path)
        self.assertIn(str(self.path), str(ctx.exception))

    def test_non_object_raises(self):
        self.path.write_text("[1, 2]", encoding="utf-8")
        with self.assertRaises(StateFileError):
            read_json_object(self.path)


class VoiceRegistryCorruptTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "voices.json"

    def tearDown(self):
        self.dir.cleanup()

    def _load(self):
        return voice_registry.load_or_seed(self.path, VOICE_PROFILES, fallback_profile="piper-ruslan")

    def test_broken_voices_json_is_kept_and_startup_stops(self):
        broken = '{"voices": {"fish-default": {"provider": "fish"'
        self.path.write_text(broken, encoding="utf-8")
        with self.assertRaises(StateFileError):
            self._load()
        self.assertEqual(self.path.read_text(encoding="utf-8"), broken)

    def test_wrong_structure_is_kept_and_startup_stops(self):
        self.path.write_text('{"voices": []}', encoding="utf-8")
        with self.assertRaises(StateFileError):
            self._load()
        self.assertEqual(self.path.read_text(encoding="utf-8"), '{"voices": []}')

    def test_missing_file_is_still_seeded(self):
        reg = self._load()
        self.assertIn("piper-ruslan", reg)
        self.assertTrue(self.path.exists())


class ConfigStoreCorruptTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "config.json"

    def tearDown(self):
        self.dir.cleanup()

    def test_broken_config_json_is_kept_and_startup_stops(self):
        self.path.write_text('{"guilds": {', encoding="utf-8")
        with self.assertRaises(StateFileError):
            BotConfigStore(self.path, set())
        self.assertEqual(self.path.read_text(encoding="utf-8"), '{"guilds": {')

    def test_string_enabled_false_is_disabled(self):
        self.path.write_text(json.dumps({"guilds": {"1": {"enabled": "false"}}}), encoding="utf-8")
        store = BotConfigStore(self.path, set())
        self.assertFalse(store.is_enabled(1))

    def test_wrong_field_types_do_not_crash_or_drop_the_guild(self):
        self.path.write_text(json.dumps({"guilds": {"1": {
            "enabled": True, "allowed_users": [5], "user_voices": ["x"], "user_fixed_phrases": 3,
        }}}), encoding="utf-8")
        store = BotConfigStore(self.path, set())
        self.assertTrue(store.is_enabled(1))
        self.assertTrue(store.is_allowed(1, 5))

    def test_newer_schema_version_is_refused(self):
        self.path.write_text(json.dumps({"version": 99, "guilds": {}}), encoding="utf-8")
        with self.assertRaises(StateFileError):
            BotConfigStore(self.path, set())


if __name__ == "__main__":
    unittest.main()
