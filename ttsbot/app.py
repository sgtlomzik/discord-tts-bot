"""Composition root: build the bot from the environment and run it.

Nothing happens on import. ``create_bot()`` re-reads the environment,
builds a TTSBot and attaches the slash commands and event handlers bound
to it; ``main()`` checks the essentials and runs it.
"""

from __future__ import annotations

from ttsbot import config
from ttsbot import commands as tts_commands
from ttsbot import events as tts_events
from ttsbot.audio import load_opus
from ttsbot.core import TTSBot
from ttsbot.models import reload_voice_profiles


def create_bot() -> tuple[TTSBot, dict]:
    """A configured bot and the commands/handlers built for it, by name."""
    config.reload()
    config.setup_logging()
    reload_voice_profiles()  # Piper profiles from the (re)loaded config
    bot = TTSBot()
    objects = tts_commands.build_commands(bot)
    bot.tts_group = objects["tts_group"]
    bot.extra_groups = [objects["fallback_group"]]
    objects.update(tts_events.register_events(bot))
    return bot, objects


def main() -> None:
    bot, _ = create_bot()
    config.TMP_DIR.mkdir(parents=True, exist_ok=True)
    if not config.TOKEN:
        raise RuntimeError("DISCORD_TOKEN is not set")
    if not config.WHITELIST_USERS:
        raise RuntimeError("WHITELIST_USERS is empty")
    if not load_opus():
        raise RuntimeError("Opus is required for Discord voice playback")
    bot.run(config.TOKEN, log_handler=None)
