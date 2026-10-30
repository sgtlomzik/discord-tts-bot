"""Composition root: build the bot from the environment and run it.

Nothing happens on import. ``create_bot()`` re-reads the environment,
builds a TTSBot and attaches the slash commands and event handlers bound
to it; ``main()`` checks the essentials and runs it.
"""

from __future__ import annotations

import asyncio
import logging
import signal

from ttsbot import config
from ttsbot import commands as tts_commands
from ttsbot import events as tts_events
from ttsbot.audio import load_opus
from ttsbot.core import TTSBot
from ttsbot.models import reload_voice_profiles

log = logging.getLogger("tts_bot")


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
    asyncio.run(serve(bot, config.TOKEN))


async def serve(bot: TTSBot, token: str) -> None:
    """Run *bot* until it closes; SIGTERM (docker stop) closes it cleanly.

    The bot is PID 1 in its container, and PID 1 ignores a signal it has
    no handler for: docker stop waited 10 s and killed the bot, cutting
    off the open recordings.
    """
    closing: list[asyncio.Task] = []

    def shut_down(sig: signal.Signals) -> None:
        if not closing:
            log.info("Got %s, shutting down", sig.name)
            closing.append(asyncio.create_task(bot.close()))

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, shut_down, sig)
        except NotImplementedError:
            pass  # Windows: Ctrl+C still raises KeyboardInterrupt
    async with bot:
        await bot.start(token)
    if closing:
        await closing[0]
