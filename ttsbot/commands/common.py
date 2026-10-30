"""Helpers shared by the command modules.

Tests patch the guards here (``is_guild_manager``, ``require_bot_owner``,
``resolve_tts_command_voice_channel``...), and the command modules call
them as ``common.<name>`` so the patch reaches every call."""

import logging
import re

import discord
from discord import app_commands

from ttsbot import config

log = logging.getLogger("tts_bot")


def is_guild_manager(member: discord.Member | discord.User) -> bool:
    permissions = getattr(member, "guild_permissions", None)
    return bool(
        permissions
        and (getattr(permissions, "manage_guild", False) or getattr(permissions, "administrator", False))
    )


async def require_guild_manager(interaction: discord.Interaction) -> bool:
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
        return False
    if not is_guild_manager(interaction.user):
        await interaction.response.send_message("Нужны права Manage Server или Administrator.", ephemeral=True)
        return False
    return True


async def is_bot_owner(bot, user: discord.abc.User) -> bool:
    """OWNER_IDS from the environment, or the Discord application's owner."""
    if user.id in config.OWNER_IDS:
        return True
    try:
        return await bot.is_owner(user)
    except discord.HTTPException:
        log.exception("Could not look up the bot owner")
        return False


async def require_bot_owner(bot, interaction: discord.Interaction) -> bool:
    """Gate for settings shared by every guild (voices, tuning, limits)."""
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
        return False
    if not await is_bot_owner(bot, interaction.user):
        await interaction.response.send_message(
            "Это общая настройка для всех серверов: её меняет только владелец бота.", ephemeral=True
        )
        return False
    return True


_EMOTION_CHOICES = [
    app_commands.Choice(name="auto (по тексту)", value="auto"),
    app_commands.Choice(name="без эмоции", value="none"),
    app_commands.Choice(name="нейтрально", value="neutral"),
    app_commands.Choice(name="радость", value="happy"),
    app_commands.Choice(name="грусть", value="sad"),
    app_commands.Choice(name="злость", value="angry"),
    app_commands.Choice(name="страх", value="fearful"),
    app_commands.Choice(name="отвращение", value="disgusted"),
    app_commands.Choice(name="удивление", value="surprised"),
]

# Current MiniMax T2A model ids (platform.minimax.io/docs/api-reference,
# checked 2026-07-05). HD = higher quality/cloning fidelity, Turbo = lower
# latency; 2.8 is the newest generation.
_MODEL_CHOICES = [
    app_commands.Choice(name="2.8 HD (новейшая, макс. качество)", value="speech-2.8-hd"),
    app_commands.Choice(name="2.8 Turbo (новейшая, быстрее)", value="speech-2.8-turbo"),
    app_commands.Choice(name="2.6 HD", value="speech-2.6-hd"),
    app_commands.Choice(name="2.6 Turbo", value="speech-2.6-turbo"),
    app_commands.Choice(name="02 HD", value="speech-02-hd"),
    app_commands.Choice(name="02 Turbo", value="speech-02-turbo"),
]

_FISH_MODEL_CHOICES = [
    app_commands.Choice(name="S2.1 Pro Free", value="s2.1-pro-free"),
    app_commands.Choice(name="S2.1 Pro", value="s2.1-pro"),
    app_commands.Choice(name="S2 Pro", value="s2-pro"),
]

_VOICE_ADD_PROVIDER_CHOICES = [
    app_commands.Choice(name="MiniMax (voice_id)", value="minimax"),
    app_commands.Choice(name="Gemini через OpenRouter (Kore, Puck…)", value="gemini"),
    app_commands.Choice(name="ElevenLabs (voice_id)", value="elevenlabs"),
]

_ELEVENLABS_MODEL_CHOICES = [
    app_commands.Choice(name="ElevenLabs v4 Turbo (быстрая, ~0.25 с)", value="eleven_v4_turbo"),
    app_commands.Choice(name="ElevenLabs v4 (максимум выразительности)", value="eleven_v4"),
]

_FISH_LATENCY_CHOICES = [
    app_commands.Choice(name="low (минимальная задержка)", value="low"),
    app_commands.Choice(name="balanced (баланс скорости и качества)", value="balanced"),
    app_commands.Choice(name="normal (максимальное качество)", value="normal"),
]


def _render_emoji(guild: discord.Guild | None, emoji_id: str, name: str) -> str:
    """Render a stored alias's emoji: prefer the live guild emoji object (so
    animated emoji and exact image render correctly); fall back to a shortcode
    if the emoji was deleted."""
    if guild is not None:
        try:
            live = discord.utils.get(guild.emojis, id=int(emoji_id))
        except (TypeError, ValueError):
            live = None
        if live is not None:
            return str(live)
    return f"`:{name or emoji_id}:`"


def _fmt_uptime(seconds: float) -> str:
    seconds = int(max(0, seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}д")
    if hours:
        parts.append(f"{hours}ч")
    parts.append(f"{minutes}м")
    return " ".join(parts)


def _provider_tag(rec) -> str:
    if rec.is_fish:
        return "Fish Audio"
    if rec.is_gemini:
        return "Gemini"
    if rec.is_mai:
        return "MAI"
    if rec.is_elevenlabs:
        return "ElevenLabs"
    return "MiniMax" if rec.is_minimax else "Piper"


def _parse_chain(text: str) -> list[str] | None:
    """``"fish-default, piper-ruslan"`` -> names; "default" -> None; "none" -> []."""
    value = text.strip().lower()
    if value in {"default", "-", "по умолчанию"}:
        return None
    if value in {"none", "off", "нет"}:
        return []
    names = [n for n in re.split(r"->|=>|[\s,;>→]+", value) if n and n.strip("-")]
    return list(dict.fromkeys(names))


def _chunk_lines(lines: list[str], limit: int) -> list[str]:
    """Join lines into blocks of at most ``limit`` characters, never
    splitting a line (Discord caps messages at 2000, embed fields at 1024)."""
    blocks: list[str] = []
    current = ""
    for line in lines:
        line = line[:limit]
        if current and len(current) + 1 + len(line) > limit:
            blocks.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        blocks.append(current)
    return blocks


def _fmt_int(value: int) -> str:
    return f"{value:,}".replace(",", " ")



def resolve_tts_command_voice_channel(
    bot,
    interaction: discord.Interaction,
    requested_channel: discord.VoiceChannel | None = None,
) -> discord.VoiceChannel | None:
    if requested_channel is not None:
        return requested_channel

    if interaction.guild is not None:
        vc = discord.utils.get(bot.voice_clients, guild=interaction.guild)
        if vc and vc.is_connected() and isinstance(vc.channel, discord.VoiceChannel):
            return vc.channel

    user_voice = getattr(getattr(interaction.user, "voice", None), "channel", None)
    if isinstance(user_voice, discord.VoiceChannel):
        return user_voice

    return None

