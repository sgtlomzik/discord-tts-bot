"""Shared test helper: a fresh bot plus the package's names in one namespace.

``load_bot_module()`` re-reads the environment (so each test starts from a
clean configuration), builds a new bot through ttsbot.app.create_bot and
returns a namespace with the bot, its commands and event handlers, the
public names of the text, message, model, store, audio and command
modules, the config module and the env-derived constants.
"""

from __future__ import annotations

import asyncio
import time
import types

import discord

from ttsbot import app, audio, config, messages, models, store, textnorm, voice_registry
from ttsbot import commands as tts_commands
from ttsbot import events as tts_events
from ttsbot.core import TTSBot, intents


def _names(module) -> dict:
    return {name: value for name, value in vars(module).items() if not name.startswith("__")}


def load_bot_module() -> types.SimpleNamespace:
    bot, objects = app.create_bot()
    ns: dict = {}
    for module in (textnorm, messages, models, store, audio, tts_commands):
        ns.update(_names(module))
    ns.update({name: value for name, value in vars(config).items() if name.isupper()})
    ns.update(objects)
    ns.update(
        bot=bot, TTSBot=TTSBot, intents=intents, config=config, voice_registry=voice_registry,
        tts_commands=tts_commands, tts_events=tts_events, parse_user_ids=config.parse_user_ids,
        asyncio=asyncio, time=time, discord=discord,
    )
    return types.SimpleNamespace(**ns)
