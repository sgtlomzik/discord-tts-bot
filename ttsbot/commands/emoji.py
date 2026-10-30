"""/voicebot emoji-alias*: pronunciations for custom emoji, owned by their guild."""

import logging

import discord
from discord import app_commands

from ttsbot.textnorm import (
    parse_custom_emoji_arg,
    sanitize_pronunciation,
)
from ttsbot.commands import common
from ttsbot.commands.common import (
    _render_emoji,
)

log = logging.getLogger("tts_bot")


def register(bot, tts_group, fallback_group, shared: dict) -> dict:
    # Aliases are keyed by emoji id and shared by every guild, so only the
    # guild that owns an emoji may set, remove or list its pronunciation.
    # Otherwise any server's manager could make the bot say arbitrary text
    # in other servers whenever their emoji is posted.
    def _owned_emoji_ids(guild) -> set[str]:
        return {str(e.id) for e in (getattr(guild, "emojis", None) or ())}

    _FOREIGN_EMOJI = (
        "Этот эмодзи не с этого сервера. Произношение эмодзи задаёт только "
        "сервер, которому он принадлежит."
    )

    async def emoji_alias_autocomplete(
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        current = current.lower()
        owned = _owned_emoji_ids(interaction.guild)
        choices: list[app_commands.Choice[str]] = []
        for emoji_id, entry in bot.config_store.emoji_aliases.items():
            if emoji_id not in owned:
                continue
            name = entry.get("name") or emoji_id
            say = entry.get("say", "")
            if current in name.lower() or current in say.lower() or current in emoji_id:
                label = f":{name}: → {say}"[:100]
                choices.append(app_commands.Choice(name=label, value=emoji_id))
        return choices[:25]


    @tts_group.command(
        name="emoji-alias", description="Задать, как бот произносит кастомный эмодзи"
    )
    @app_commands.describe(
        emoji="Кастомный эмодзи сервера (или :имя:)",
        pronunciation="Чем его озвучивать",
    )
    async def slash_emoji_alias(
        interaction: discord.Interaction, emoji: str, pronunciation: str
    ) -> None:
        if not await common.require_guild_manager(interaction):
            return
        parsed = parse_custom_emoji_arg(emoji, interaction.guild)
        if parsed is None:
            await interaction.response.send_message(
                "Нужен кастомный эмодзи этого сервера — пришлите сам эмодзи, "
                "токен `<:Имя:123…>` или `:Имя:`. Обычные Unicode-эмодзи и текст "
                "не поддерживаются.",
                ephemeral=True,
            )
            return
        emoji_id, name = parsed
        if emoji_id not in _owned_emoji_ids(interaction.guild):
            await interaction.response.send_message(_FOREIGN_EMOJI, ephemeral=True)
            return
        say = sanitize_pronunciation(pronunciation)
        if not say:
            await interaction.response.send_message(
                "Произношение пустое или состоит только из недопустимых символов.",
                ephemeral=True,
            )
            return
        bot.config_store.set_emoji_alias(emoji_id, name, say)
        rendered = _render_emoji(interaction.guild, emoji_id, name)
        await interaction.response.send_message(
            f"Готово: {rendered} будет озвучиваться как «{say}».", ephemeral=True
        )


    @tts_group.command(name="emoji-alias-remove", description="Удалить алиас эмодзи")
    @app_commands.describe(emoji="Эмодзи или его текущий алиас")
    @app_commands.autocomplete(emoji=emoji_alias_autocomplete)
    async def slash_emoji_alias_remove(
        interaction: discord.Interaction, emoji: str
    ) -> None:
        if not await common.require_guild_manager(interaction):
            return
        parsed = parse_custom_emoji_arg(emoji, interaction.guild)
        if parsed is not None:
            emoji_id = parsed[0]
        elif emoji.strip().isdigit():  # raw id, e.g. picked from autocomplete
            emoji_id = emoji.strip()
        else:
            await interaction.response.send_message(
                "Не распознан эмодзи. Выберите из списка автодополнения "
                "или пришлите сам эмодзи.",
                ephemeral=True,
            )
            return
        if emoji_id not in _owned_emoji_ids(interaction.guild):
            await interaction.response.send_message(_FOREIGN_EMOJI, ephemeral=True)
            return
        if bot.config_store.remove_emoji_alias(emoji_id):
            await interaction.response.send_message(
                f"Алиас удалён (id `{emoji_id}`).", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                f"Алиаса для id `{emoji_id}` нет.", ephemeral=True
            )


    @tts_group.command(name="emoji-aliases", description="Показать заданные алиасы эмодзи")
    async def slash_emoji_aliases(interaction: discord.Interaction) -> None:
        if not await common.require_guild_manager(interaction):
            return
        owned = _owned_emoji_ids(interaction.guild)
        aliases = {
            emoji_id: entry for emoji_id, entry in bot.config_store.emoji_aliases.items()
            if emoji_id in owned
        }
        if not aliases:
            await interaction.response.send_message(
                "Для эмодзи этого сервера алиасы не заданы. Добавьте: `/voicebot emoji-alias`.",
                ephemeral=True,
            )
            return
        lines: list[str] = []
        for emoji_id, entry in aliases.items():
            rendered = _render_emoji(interaction.guild, emoji_id, entry.get("name", ""))
            lines.append(f"{rendered} → «{entry.get('say', '')}»")
        await interaction.response.send_message("\n".join(lines)[:1900], ephemeral=True)

    return dict(locals())
