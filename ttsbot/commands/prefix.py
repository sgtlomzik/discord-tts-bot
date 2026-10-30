"""Legacy prefix commands: !tts stop and !tts join."""

import logging

import discord
from discord.ext import commands

from ttsbot.voice_lifecycle import VoiceCooldown
from ttsbot.commands import common

log = logging.getLogger("tts_bot")


def register(bot, tts_group, fallback_group, shared: dict) -> dict:
    @bot.command()
    async def stop(ctx: commands.Context) -> None:
        if ctx.guild is None:
            await ctx.reply("Command can only be used in a guild.", mention_author=False)
            return
        if not isinstance(ctx.author, discord.Member) or not common.is_guild_manager(ctx.author):
            await ctx.reply("Only server managers can stop TTS.", mention_author=False)
            return

        await bot.disconnect_guild_voice(ctx.guild)
        await ctx.reply("TTS disconnected.", mention_author=False)


    @bot.command()
    async def join(ctx: commands.Context) -> None:
        if ctx.guild is None:
            await ctx.reply("Command can only be used in a guild.", mention_author=False)
            return
        if not isinstance(ctx.author, discord.Member):
            await ctx.reply("Command can only be used by guild members.", mention_author=False)
            return
        if not bot.config_store.is_allowed(ctx.guild.id, ctx.author.id) and not common.is_guild_manager(ctx.author):
            await ctx.reply("You are not allowed to use TTS.", mention_author=False)
            return

        if not bot.config_store.is_enabled(ctx.guild.id):
            await ctx.reply("TTS is turned off on this server (/voicebot on).", mention_author=False)
            return

        if not ctx.author.voice or not isinstance(ctx.author.voice.channel, discord.VoiceChannel):
            await ctx.reply("You must be in a voice channel.", mention_author=False)
            return

        try:
            await bot.ensure_voice(ctx.author.voice.channel)
        except VoiceCooldown as exc:
            await ctx.reply(f"Voice is reconnecting, try again shortly ({exc}).", mention_author=False)
            return
        except Exception:
            log.exception("!tts join failed guild=%s", ctx.guild.id)
            await ctx.reply("Could not connect to voice.", mention_author=False)
            return
        await ctx.reply("TTS connected.", mention_author=False)

    return dict(locals())
