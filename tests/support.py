"""Shared test helper: a fresh bot plus the package's names in one namespace.

``load_bot_module()`` re-reads the environment (so each test starts from a
clean configuration), builds a new bot through ttsbot.app.create_bot and
returns a namespace with the bot, its commands and event handlers, the
public names of the text, message, model, store, audio and command
modules, the config module and the env-derived constants.
"""

from __future__ import annotations

import asyncio
import os
import time
import types

import discord

from ttsbot import app, audio, config, models, store, textnorm, voice_registry
from ttsbot import commands as tts_commands
from ttsbot import events as tts_events
from ttsbot.core import TTSBot, intents
from ttsbot.statedb import StateDB, import_json_state


# Each test bot keeps its state in its own in-memory database.
os.environ.setdefault("STATE_DB_PATH", ":memory:")


def state_from(config_path, env_users: set[int] | None = None) -> StateDB:
    """The state database in ``config_path``'s directory, with that
    config.json (an earlier version's file) imported if it exists, minus
    the ``env_users`` (WHITELIST_USERS) the old bot copied into it. Opening
    it again gives the same state, as after a restart."""
    db = StateDB(config_path.parent / "state.db")
    import_json_state(db, config_path=config_path, env_users=env_users or set())
    return db


def with_spec(fake, cls):
    """``fake`` with ``cls.stream_spec`` bound to it, so the pipeline can
    ask a stand-in provider for its stream like a real one."""
    fake.stream_spec = types.MethodType(cls.stream_spec, fake)
    return fake


def _names(module) -> dict:
    return {name: value for name, value in vars(module).items() if not name.startswith("__")}


def load_bot_module() -> types.SimpleNamespace:
    bot, objects = app.create_bot()
    ns: dict = {}
    for module in (textnorm, models, store, audio, tts_commands):
        ns.update(_names(module))
    ns.update({name: value for name, value in vars(config).items() if name.isupper()})
    ns.update(objects)
    ns.update(
        bot=bot, TTSBot=TTSBot, intents=intents, config=config, voice_registry=voice_registry,
        tts_commands=tts_commands, tts_events=tts_events, parse_user_ids=config.parse_user_ids,
        asyncio=asyncio, time=time, discord=discord,
    )
    return types.SimpleNamespace(**ns)
