"""The bot's state lives in one SQLite file, data/state.db.

Guild settings (config.json), the voice catalog (voices.json) and provider
pauses (provider_health.json) were three JSON files, each rewritten whole on
every change by its own code. They are now tables in one database; every
save is one transaction. The JSON files of earlier versions are imported
once and left in place, so an older image still finds them.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from ttsbot import config, models, voice_registry
from ttsbot.health import CircuitBreaker, CircuitState, FailureKind, HealthStore
from ttsbot.models import VOICE_PROFILES
from ttsbot.statedb import SCHEMA_VERSION, StateDB, import_json_state
from ttsbot.statefile import StateFileError
from ttsbot.store import BotConfigStore


class _Dir(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)
        self.db_path = self.root / "state.db"

    def tearDown(self):
        self.dir.cleanup()

    def reopen(self) -> StateDB:
        return StateDB(self.db_path)


class GuildSettingsTests(_Dir):
    def test_guild_settings_survive_a_restart(self):
        store = BotConfigStore(StateDB(self.db_path), set())
        store.add_user(1, 5)
        store.set_default_voice(1, "piper-irina")
        store.set_user_voice(1, 5, "piper-ruslan")
        store.set_user_fixed_phrase(1, 5, "привет")
        store.set_emoji_alias("42", "kek", "кек")
        store.set_tts_max_chars(700)
        store.set_enabled(2, False)

        loaded = BotConfigStore(self.reopen(), set())
        self.assertTrue(loaded.is_allowed(1, 5))
        self.assertEqual(loaded.get_guild(1).default_voice, "piper-irina")
        self.assertEqual(loaded.voice_for_user(1, 5), "piper-ruslan")
        self.assertEqual(loaded.fixed_phrase_for_user(1, 5), "привет")
        self.assertEqual(loaded.emoji_say_map(), {"42": "кек"})
        self.assertEqual(loaded.max_chars(), 700)
        self.assertFalse(loaded.is_enabled(2))

    def test_removed_entries_stay_removed(self):
        store = BotConfigStore(StateDB(self.db_path), set())
        store.add_user(1, 5)
        store.set_emoji_alias("42", "kek", "кек")
        store.remove_user(1, 5)
        store.remove_emoji_alias("42")
        loaded = BotConfigStore(self.reopen(), set())
        self.assertFalse(loaded.is_allowed(1, 5))
        self.assertEqual(loaded.emoji_aliases, {})

    def test_a_failed_save_changes_nothing(self):
        db = StateDB(self.db_path)
        store = BotConfigStore(db, set())
        store.add_user(1, 5)
        with patch.object(db, "save_config", side_effect=sqlite3.OperationalError("disk I/O error")):
            with self.assertRaises(sqlite3.OperationalError):
                store.add_user(1, 6)
        self.assertFalse(store.is_allowed(1, 6))
        self.assertTrue(BotConfigStore(self.reopen(), set()).is_allowed(1, 5))


class VoiceCatalogTests(_Dir):
    def test_the_catalog_is_seeded_once_and_then_loaded(self):
        reg = voice_registry.load_or_seed(StateDB(self.db_path), VOICE_PROFILES, fallback_profile="piper-ruslan")
        reg.fallback_chain = ["piper-irina"]
        voice_registry.save_registry(StateDB(self.db_path), reg)
        again = voice_registry.load_or_seed(self.reopen(), {}, fallback_profile="piper-ruslan")
        self.assertIn("piper-ruslan", again)
        self.assertEqual(again.fallback_chain, ["piper-irina"])

    def test_an_empty_database_has_no_catalog(self):
        self.assertIsNone(voice_registry.load_registry(StateDB(self.db_path)))


class ProviderHealthTests(_Dir):
    def test_a_pause_survives_a_restart(self):
        breaker = CircuitBreaker(name="fish")
        HealthStore(StateDB(self.db_path)).bind("fish", breaker, "fp")
        breaker.record_failure(FailureKind.QUOTA, until=time.time() + 3600, error="no credits")
        restored = CircuitBreaker(name="fish")
        HealthStore(self.reopen()).bind("fish", restored, "fp")
        self.assertIsNot(restored.state, CircuitState.CLOSED)


class DamagedDatabaseTests(_Dir):
    def test_a_file_that_is_not_a_database_stops_startup_and_is_kept(self):
        self.db_path.write_bytes(b"definitely not sqlite " * 50)
        with self.assertRaises(StateFileError) as ctx:
            StateDB(self.db_path)
        self.assertIn(str(self.db_path), str(ctx.exception))
        self.assertEqual(self.db_path.read_bytes(), b"definitely not sqlite " * 50)

    def test_a_newer_schema_is_refused(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
        conn.close()
        with self.assertRaises(StateFileError):
            StateDB(self.db_path)


class JsonImportTests(_Dir):
    def setUp(self):
        super().setUp()
        self.config_path = self.root / "config.json"
        self.voices_path = self.root / "voices.json"
        self.health_path = self.root / "provider_health.json"
        self.config_path.write_text(json.dumps({
            "version": 1,
            "guilds": {"1": {"enabled": True, "allowed_users": [5], "default_voice": "piper-irina"}},
            "emoji_aliases": {"42": {"name": "kek", "say": "кек"}},
            "settings": {"tts_max_chars": 640},
        }), encoding="utf-8")
        reg = voice_registry.seed_registry(VOICE_PROFILES, fallback_profile="piper-ruslan")
        reg.fallback_chain = ["piper-irina"]
        self.voices_path.write_text(json.dumps(voice_registry.registry_to_dict(reg)), encoding="utf-8")
        breaker = CircuitBreaker(name="fish")
        breaker.record_failure(FailureKind.QUOTA, until=time.time() + 3600, error="no credits")
        self.health_path.write_text(json.dumps({
            "version": 1, "breakers": {"fish": {**breaker.to_dict(), "fingerprint": ""}},
        }), encoding="utf-8")

    def _import(self, db):
        return import_json_state(
            db, config_path=self.config_path, voices_path=self.voices_path, health_path=self.health_path,
        )

    def test_the_json_files_are_imported_and_kept(self):
        before = {p: p.read_bytes() for p in (self.config_path, self.voices_path, self.health_path)}
        db = StateDB(self.db_path)
        self.assertEqual(len(self._import(db)), 3)
        store = BotConfigStore(db, set())
        self.assertTrue(store.is_allowed(1, 5))
        self.assertEqual(store.get_guild(1).default_voice, "piper-irina")
        self.assertEqual(store.emoji_say_map(), {"42": "кек"})
        self.assertEqual(store.max_chars(), 640)
        self.assertEqual(voice_registry.load_registry(db).fallback_chain, ["piper-irina"])
        restored = CircuitBreaker(name="fish")
        HealthStore(db).bind("fish", restored)
        self.assertIsNot(restored.state, CircuitState.CLOSED)
        self.assertEqual({p: p.read_bytes() for p in before}, before)  # an older image still finds them

    def test_each_file_is_imported_only_once(self):
        db = StateDB(self.db_path)
        self._import(db)
        BotConfigStore(db, set()).remove_user(1, 5)
        self.assertEqual(self._import(self.reopen()), [])
        self.assertFalse(BotConfigStore(self.reopen(), set()).is_allowed(1, 5))

    def test_missing_files_are_skipped(self):
        db = StateDB(self.db_path)
        self.assertEqual(import_json_state(
            db, config_path=self.root / "a.json", voices_path=self.root / "b.json", health_path=self.root / "c.json",
        ), [])
        self.assertIsNone(voice_registry.load_registry(db))

    def test_a_damaged_json_file_stops_startup_and_imports_nothing(self):
        self.voices_path.write_text('{"voices": {"fish-default": {', encoding="utf-8")
        db = StateDB(self.db_path)
        with self.assertRaises(StateFileError):
            self._import(db)
        self.assertIsNone(voice_registry.load_registry(db))

    def test_a_newer_config_schema_is_refused(self):
        self.config_path.write_text(json.dumps({"version": 99, "guilds": {}}), encoding="utf-8")
        with self.assertRaises(StateFileError):
            self._import(StateDB(self.db_path))

    def test_raw_values_are_still_checked_when_loaded(self):
        self.config_path.write_text(json.dumps({"guilds": {"1": {
            "enabled": "false", "allowed_users": [5, "x"], "user_voices": ["x"], "user_fixed_phrases": 3,
        }}}), encoding="utf-8")
        db = StateDB(self.db_path)
        self._import(db)
        store = BotConfigStore(db, set())
        self.assertFalse(store.is_enabled(1))
        self.assertTrue(store.is_allowed(1, 5))


class BotStateTests(_Dir):
    def tearDown(self):
        config.reload()
        models.reload_voice_profiles()
        super().tearDown()

    def _bot(self):
        from support import load_bot_module
        env = {"STATE_DB_PATH": str(self.db_path), "BOT_CONFIG_PATH": str(self.root / "config.json")}
        with patch.dict(os.environ, env):
            return load_bot_module().bot

    def test_the_bot_keeps_its_state_in_state_db(self):
        bot = self._bot()
        bot.config_store.add_user(1, 5)
        self.assertTrue(self.db_path.exists())
        self.assertTrue(self._bot().config_store.is_allowed(1, 5))
        self.assertFalse((self.root / "config.json").exists())
        self.assertFalse((self.root / "voices.json").exists())

    def test_the_bot_imports_the_old_json_files_on_start(self):
        (self.root / "config.json").write_text(json.dumps({"guilds": {"7": {"allowed_users": [9]}}}), encoding="utf-8")
        self.assertTrue(self._bot().config_store.is_allowed(7, 9))


if __name__ == "__main__":
    unittest.main()
