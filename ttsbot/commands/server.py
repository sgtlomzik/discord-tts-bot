"""/voicebot on, off, allow and deny: the guild's switch and its user list."""

import logging

import discord
from discord import app_commands

from ttsbot.commands import common

log = logging.getLogger("tts_bot")


def register(bot, tts_group, fallback_group, shared: dict) -> dict:
    @tts_group.command(name="off", description="Отключить озвучку на сервере")
    async def slash_tts_off(interaction: discord.Interaction) -> None:
        if not await common.require_guild_manager(interaction):
            return
        guild = interaction.guild
        assert guild is not None
        # Disconnecting can take longer than Discord's 3 s to answer.
        await interaction.response.defer(ephemeral=True)
        bot.config_store.set_enabled(guild.id, False)
        cleared = bot.clear_queue_for_guild(guild.id)
        await bot.disconnect_guild_voice(guild)
        await interaction.followup.send(f"TTS отключен. Очередь очищена: {cleared}.", ephemeral=True)


    @tts_group.command(name="on", description="Включить озвучку на сервере")
    async def slash_tts_on(interaction: discord.Interaction) -> None:
        if not await common.require_guild_manager(interaction):
            return
        guild = interaction.guild
        assert guild is not None
        bot.config_store.set_enabled(guild.id, True)
        await interaction.response.send_message("TTS включен.", ephemeral=True)


    @tts_group.command(name="allow", description="Добавить пользователя в озвучку")
    @app_commands.describe(user="Пользователь, чьи сообщения нужно озвучивать")
    async def slash_tts_allow(interaction: discord.Interaction, user: discord.Member) -> None:
        if not await common.require_guild_manager(interaction):
            return
        guild = interaction.guild
        assert guild is not None
        bot.config_store.add_user(guild.id, user.id)
        await interaction.response.send_message(f"Добавлен в озвучку: {user.mention}", ephemeral=True)


    @tts_group.command(name="deny", description="Исключить пользователя из озвучки")
    @app_commands.describe(user="Пользователь, чьи сообщения больше не нужно озвучивать")
    async def slash_tts_deny(interaction: discord.Interaction, user: discord.Member) -> None:
        if not await common.require_guild_manager(interaction):
            return
        guild = interaction.guild
        assert guild is not None
        bot.config_store.remove_user(guild.id, user.id)
        note = (" на этом сервере (на остальных его озвучивает WHITELIST_USERS из .env)"
                if bot.config_store.is_env_user(user.id) else "")
        await interaction.response.send_message(f"Исключен из озвучки: {user.mention}{note}", ephemeral=True)

    return dict(locals())
