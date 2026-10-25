"""The bot's state in one SQLite file (``data/state.db``).

Guild settings, emoji aliases and runtime settings (``BotConfigStore``),
the voice catalog (``voice_registry``) and provider pauses
(``HealthStore``) each have their own tables. Every save is one
transaction, so a crash or a full disk leaves the previous state, never
half of it. The stores keep their data in memory and parse what they load
as before; this module only stores it. Nested values (a guild's user
lists, a voice's provider parameters) are JSON text columns.

The JSON files of earlier versions (``config.json``, ``voices.json``,
``provider_health.json``) are imported once by ``import_json_state()`` and
left where they are, so an older image still finds them. A database that
cannot be opened raises ``StateFileError``, like a damaged JSON file did:
startup stops and the file is left as it is. A save SQLite refuses (a full
disk, a lock held too long) raises ``StateWriteError``, an ``OSError`` like
the failed JSON writes it replaces, and leaves the database usable for the
next save.

Inspect it with ``sqlite3 data/state.db .dump``.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from ttsbot.statefile import StateFileError, read_json_object

log = logging.getLogger("tts_bot.state")

SCHEMA_VERSION = 1
CONFIG_SCHEMA_VERSION = 1  # the newest config.json this build imports

_SCHEMA = """
CREATE TABLE guilds (guild_id INTEGER PRIMARY KEY, config TEXT NOT NULL);
CREATE TABLE emoji_aliases (emoji_id TEXT PRIMARY KEY, name TEXT NOT NULL, say TEXT NOT NULL);
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE voices (name TEXT PRIMARY KEY, record TEXT NOT NULL);
CREATE TABLE voice_catalog (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE provider_health (provider TEXT PRIMARY KEY, snapshot TEXT NOT NULL);
CREATE TABLE imports (source TEXT PRIMARY KEY, path TEXT NOT NULL, imported_at REAL NOT NULL);
"""


class StateWriteError(OSError):
    """A save SQLite refused; nothing of it was written."""


class StateDB:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        in_memory = str(path) == ":memory:"
        if not in_memory:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._conn = sqlite3.connect(str(path), isolation_level=None)
            self._conn.execute("PRAGMA synchronous = FULL")
            version = self._conn.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise StateFileError(
                    f"{path} has schema version {version}; this build reads up to {SCHEMA_VERSION}"
                )
            if version == 0:
                self._conn.executescript(
                    f"BEGIN IMMEDIATE;{_SCHEMA}PRAGMA user_version = {SCHEMA_VERSION};COMMIT;"
                )
        except sqlite3.DatabaseError as exc:
            raise StateFileError(f"{path} is not a usable SQLite database ({exc}); fix or move it, then restart") from exc

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def _transaction(self):
        """One write transaction. A failure anywhere, COMMIT included, rolls
        it back, so the connection is never left inside a transaction that
        would make every later BEGIN fail."""
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
                self._conn.execute("COMMIT")
            except BaseException:
                if self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")
                raise
        except sqlite3.Error as exc:
            raise StateWriteError(f"Cannot save to {self.path}: {exc}") from exc

    def _rows(self, sql: str) -> list[tuple]:
        try:
            return self._conn.execute(sql).fetchall()
        except sqlite3.DatabaseError as exc:
            raise StateFileError(f"Cannot read {self.path}: {exc}") from exc

    def _json(self, text: str, where: str):
        try:
            return json.loads(text)
        except ValueError as exc:
            raise StateFileError(f"{self.path}: {where} is not valid JSON ({exc})") from exc

    # -- guild settings (BotConfigStore) ----------------------------------

    def load_config(self) -> dict:
        """Guilds, emoji aliases and settings, shaped like config.json."""
        return {
            "guilds": {
                str(guild_id): self._json(text, f"guild {guild_id}")
                for guild_id, text in self._rows("SELECT guild_id, config FROM guilds")
            },
            "emoji_aliases": {
                emoji_id: {"name": name, "say": say}
                for emoji_id, name, say in self._rows("SELECT emoji_id, name, say FROM emoji_aliases")
            },
            "settings": {
                key: self._json(text, f"setting {key}")
                for key, text in self._rows("SELECT key, value FROM settings")
            },
        }

    def save_config(self, data: dict) -> None:
        with self._transaction() as db:
            _replace_config(db, data)

    # -- voice catalog (voice_registry) -----------------------------------

    def load_voice_catalog(self) -> dict | None:
        """The catalog shaped like voices.json, or None if none was saved."""
        meta = {key: self._json(text, f"catalog {key}") for key, text in self._rows("SELECT key, value FROM voice_catalog")}
        if "version" not in meta:
            return None
        meta["voices"] = {
            name: self._json(text, f"voice {name}")
            for name, text in self._rows("SELECT name, record FROM voices")
        }
        return meta

    def save_voice_catalog(self, data: dict) -> None:
        with self._transaction() as db:
            _replace_voice_catalog(db, data)

    # -- provider pauses (HealthStore) -------------------------------------

    def load_health(self) -> dict[str, dict]:
        return {
            provider: self._json(text, f"health of {provider}")
            for provider, text in self._rows("SELECT provider, snapshot FROM provider_health")
        }

    def save_health(self, provider: str, snapshot: dict) -> None:
        with self._transaction() as db:
            db.execute(
                "INSERT OR REPLACE INTO provider_health (provider, snapshot) VALUES (?, ?)",
                (provider, _dump(snapshot)),
            )

    def forget_health(self, provider: str) -> None:
        with self._transaction() as db:
            db.execute("DELETE FROM provider_health WHERE provider = ?", (provider,))


def _dump(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _object(value) -> dict:
    return value if isinstance(value, dict) else {}


def _replace_config(db: sqlite3.Connection, data: dict) -> None:
    db.execute("DELETE FROM guilds")
    db.execute("DELETE FROM emoji_aliases")
    db.execute("DELETE FROM settings")
    for guild_id, guild in _object(data.get("guilds")).items():
        if str(guild_id).isdigit():
            db.execute("INSERT INTO guilds (guild_id, config) VALUES (?, ?)", (int(guild_id), _dump(guild)))
    for emoji_id, entry in _object(data.get("emoji_aliases")).items():
        if isinstance(entry, dict) and isinstance(entry.get("say"), str):
            name = entry.get("name")
            db.execute(
                "INSERT INTO emoji_aliases (emoji_id, name, say) VALUES (?, ?, ?)",
                (str(emoji_id), name if isinstance(name, str) else "", entry["say"]),
            )
    for key, value in _object(data.get("settings")).items():
        db.execute("INSERT INTO settings (key, value) VALUES (?, ?)", (str(key), _dump(value)))


def _replace_voice_catalog(db: sqlite3.Connection, data: dict) -> None:
    db.execute("DELETE FROM voices")
    db.execute("DELETE FROM voice_catalog")
    for name, record in _object(data.get("voices")).items():
        db.execute("INSERT INTO voices (name, record) VALUES (?, ?)", (str(name), _dump(record)))
    for key, value in data.items():
        if key != "voices":
            db.execute("INSERT INTO voice_catalog (key, value) VALUES (?, ?)", (key, _dump(value)))


def _config_from_file(path: Path, data: dict, env_users: set[int] | frozenset[int]) -> dict:
    version = data.get("version", CONFIG_SCHEMA_VERSION)
    if not isinstance(version, int) or version > CONFIG_SCHEMA_VERSION:
        raise StateFileError(
            f"{path} has schema version {version!r}; this build reads up to {CONFIG_SCHEMA_VERSION}"
        )
    # Earlier versions copied WHITELIST_USERS into every guild they saved;
    # those copies would outlive the user's removal from the env. The env
    # list is checked live now, so the copies go.
    guilds = {}
    for guild_id, guild in _object(data.get("guilds")).items():
        allowed = guild.get("allowed_users") if isinstance(guild, dict) else None
        if isinstance(allowed, list):
            guild = {**guild, "allowed_users": [u for u in allowed if not (str(u).isdigit() and int(u) in env_users)]}
        guilds[guild_id] = guild
    return {**data, "guilds": guilds}


def _catalog_from_file(path: Path, data: dict) -> dict:
    if not isinstance(data.get("voices", {}), dict):
        raise StateFileError(f"{path} has an invalid structure (\"voices\" must be an object)")
    return {**data, "version": data.get("version", 1)}


def import_json_state(
    db: StateDB,
    *,
    config_path: Path | None = None,
    voices_path: Path | None = None,
    health_path: Path | None = None,
    env_users: set[int] | frozenset[int] = frozenset(),
) -> list[str]:
    """Import the JSON state files of earlier versions, each only once.

    A file already imported is never read again, so it cannot bring back
    what was changed since. The files are left in place. A damaged config
    or voices file raises StateFileError and nothing of it is imported. A
    damaged health file is skipped with a warning: it only caches provider
    pauses, so starting with every provider unpaused is safe. Returns the
    names of the files imported now. ``env_users`` (WHITELIST_USERS) are
    dropped from the guilds' allowed users of config.json, where earlier
    versions copied them.
    """
    imported = {source for (source,) in db._rows("SELECT source FROM imports")}
    done = []
    for source, path in (("config", config_path), ("voices", voices_path), ("health", health_path)):
        if path is None or source in imported:
            continue
        damaged = False
        try:
            data = read_json_object(path)
        except StateFileError as exc:
            if source != "health":
                raise
            log.warning("Ignoring the provider pauses in %s: %s", path, exc)
            data, damaged = {}, True
        if data is None:
            continue
        with db._transaction() as conn:
            if source == "config":
                _replace_config(conn, _config_from_file(path, data, env_users))
            elif source == "voices":
                _replace_voice_catalog(conn, _catalog_from_file(path, data))
            else:
                for provider, snapshot in _object(data.get("breakers")).items():
                    if isinstance(snapshot, dict):
                        conn.execute(
                            "INSERT OR REPLACE INTO provider_health (provider, snapshot) VALUES (?, ?)",
                            (str(provider), _dump(snapshot)),
                        )
            conn.execute(
                "INSERT INTO imports (source, path, imported_at) VALUES (?, ?, ?)",
                (source, str(path), time.time()),
            )
        if damaged:
            continue
        log.info("Imported %s into %s; the JSON file is kept and no longer read", path, db.path)
        done.append(path.name)
    return done
