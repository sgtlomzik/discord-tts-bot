"""The /voicebot and /fallback slash commands and the legacy prefix commands.

``build_commands(bot)`` creates fresh ``app_commands.Group`` objects with
every command bound (via closure) to *bot* and returns a namespace dict
that the composition root re-exports. Building per bot instance keeps
each test bot's command objects tied to its own TTSBot.

Each module registers one area of commands on the groups through
``register(bot, tts_group, fallback_group, shared)``; ``shared`` holds
what the earlier modules defined (autocomplete callbacks, embed
builders), and each module returns its own names into it. The modules are
registered in the order Discord lists the subcommands in.

Helpers that tests patch (``is_guild_manager``, ``require_bot_owner``,
``resolve_tts_command_voice_channel``...) live in ``common`` and are called
as ``common.<name>``, so ``unittest.mock.patch`` on that module reaches
every command.
"""

import logging

import discord
from discord import app_commands

from ttsbot.commands import completion, emoji, fallback, prefix, server, status, voices
from ttsbot.commands.common import (  # noqa: F401  (re-exported for callers and tests)
    _ELEVENLABS_MODEL_CHOICES,
    _EMOTION_CHOICES,
    _FISH_LATENCY_CHOICES,
    _FISH_MODEL_CHOICES,
    _MODEL_CHOICES,
    _VOICE_ADD_PROVIDER_CHOICES,
    _chunk_lines,
    _fmt_int,
    _fmt_uptime,
    _parse_chain,
    _provider_tag,
    _render_emoji,
    is_bot_owner,
    is_guild_manager,
    require_bot_owner,
    require_guild_manager,
    resolve_tts_command_voice_channel,
)

log = logging.getLogger("tts_bot")

# Registration order = the order of the subcommands in Discord.
MODULES = (completion, server, voices, emoji, fallback, status, prefix)


def build_commands(bot):
    """Create the /voicebot and /fallback groups + prefix commands bound to *bot*.

    Returns a dict of every object the modules define (groups, command
    objects, embed builders, StatsView) for the composition root to
    re-export.
    """
    tts_group = app_commands.Group(name="voicebot", description="Управление озвучкой сообщений")
    # /voicebot is at Discord's limit of 25 subcommands, so the fallback
    # chain and provider health get their own group.
    fallback_group = app_commands.Group(
        name="fallback", description="Запасные голоса и состояние TTS-провайдеров",
    )
    ns: dict = {"tts_group": tts_group, "fallback_group": fallback_group}
    for module in MODULES:
        ns.update(module.register(bot, tts_group, fallback_group, ns))

    async def on_app_command_error(
        interaction: discord.Interaction, error: app_commands.AppCommandError,
    ) -> None:
        """Answer every failed slash command instead of leaving it unanswered."""
        if isinstance(error, app_commands.CheckFailure):
            text = "Недостаточно прав для этой команды."
        else:
            original = getattr(error, "original", error)
            name = getattr(getattr(interaction, "command", None), "qualified_name", "?")
            log.error("Slash command /%s failed", name, exc_info=original)
            text = f"Команда не выполнена: {type(original).__name__}: {original}"[:1900]
        try:
            if interaction.response.is_done():
                await interaction.followup.send(text, ephemeral=True)
            else:
                await interaction.response.send_message(text, ephemeral=True)
        except discord.HTTPException:
            log.warning("Could not report a failed slash command to the user")

    bot.tree.error(on_app_command_error)
    ns["on_app_command_error"] = on_app_command_error

    for name in ("bot", "shared"):
        ns.pop(name, None)
    return ns
