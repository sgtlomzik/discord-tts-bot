"""Discord gateway event handlers.

``register_events(bot)`` attaches on_ready / on_message /
on_voice_state_update to *bot* and returns them in a dict so the
composition root can re-export them (the tests reference the handlers
as module globals of bot.py).
"""

import logging

import discord

from ttsbot import config
from ttsbot.models import VOICE_PROFILES
from ttsbot.textnorm import build_mention_say_map

log = logging.getLogger("tts_bot")


def register_events(bot):
    @bot.event
    async def on_ready() -> None:
        log.info("TTS bot logged in as %s (%s)", bot.user, bot.user.id if bot.user else "unknown")
        log.info("Opus loaded: %s", discord.opus.is_loaded())
        log.info("Env fallback whitelist users: %s", ",".join(str(user_id) for user_id in sorted(config.WHITELIST_USERS)))
        log.info("Bot config path: %s", config.BOT_CONFIG_PATH)
        log.info("Voice profiles: %s", ",".join(sorted(VOICE_PROFILES)))
        log.info("Piper tuning: speaker=%s length_scale=%.2f", config.PIPER_SPEAKER, config.PIPER_LENGTH_SCALE)
        log.info(
            "Per-message cap: max_text_length=%s tts_max_chars=%s",
            config.MAX_TEXT_LENGTH, config.TTS_MAX_CHARS,
        )
        log.info(
            "Playback tuning: preroll_ms=%s preroll_mode=%s preroll_volume_db=%s tail_ms=%s trim_silence=%s ffmpeg_low_delay=%s",
            config.TTS_PREROLL_MS,
            config.TTS_PREROLL_MODE,
            config.TTS_PREROLL_VOLUME_DB,
            config.TTS_SILENCE_TAIL_MS,
            config.TTS_TRIM_SILENCE,
            config.FFMPEG_LOW_DELAY,
        )
        log.info(
            "Continuous stream: enabled=%s idle_mode=%s idle_volume_db=%s stream_tail_ms=%s max_idle_seconds=%s",
            config.TTS_CONTINUOUS_STREAM,
            config.TTS_IDLE_FRAME_MODE,
            config.TTS_IDLE_VOLUME_DB,
            config.TTS_STREAM_TAIL_MS,
            config.TTS_MAX_CONTINUOUS_IDLE_SECONDS,
        )
        log.info(
            "Merge tuning: algorithm=%s enabled=%s max_chars=%s window_ms=%s max_parts=%s selective_enabled=%s selective_scope=all_allowed_users reaction_pause_ms=%s",
            config.TTS_MERGE_ALGORITHM,
            config.TTS_MERGE_SHORT_MESSAGES,
            config.TTS_MERGE_MAX_CHARS,
            config.TTS_MERGE_WINDOW_MS,
            config.TTS_MERGE_MAX_PARTS,
            config.TTS_SELECTIVE_HOLD_ENABLED,
            config.TTS_SELECTIVE_HOLD_REACTION_PAUSE_MS,
        )


    @bot.event
    async def on_message(message: discord.Message) -> None:
        if message.author.bot:
            return

        if (
            message.guild is not None
            and bot.config_store.is_enabled(message.guild.id)
            and bot.config_store.is_allowed(message.guild.id, message.author.id)
            and message.author.voice
            and isinstance(message.author.voice.channel, discord.VoiceChannel)
        ):
            fixed_phrase = bot.config_store.fixed_phrase_for_user(message.guild.id, message.author.id)
            await bot.queue_or_merge_message(
                fixed_phrase if fixed_phrase is not None else message.content,
                message.author.voice.channel,
                message.author.id,
                message.channel.id,
                mentions={} if fixed_phrase is not None else build_mention_say_map(message),
            )

        await bot.process_commands(message)


    @bot.event
    async def on_voice_state_update(
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        if member.bot:
            return
        if not bot.config_store.is_enabled(member.guild.id):
            return
        if not bot.config_store.is_allowed(member.guild.id, member.id):
            return

        if isinstance(after.channel, discord.VoiceChannel):
            await bot.auto_connect_for_member(member, after.channel)

        guild = member.guild
        vc = discord.utils.get(bot.voice_clients, guild=guild)
        if not vc or not vc.is_connected():
            return

        if isinstance(vc.channel, discord.VoiceChannel):
            whitelisted_present = any(
                (not user.bot) and bot.config_store.is_allowed(guild.id, user.id)
                for user in vc.channel.members
            )
            if not whitelisted_present and not bot.has_active_voice_playback(guild.id, vc):
                bot.schedule_idle_disconnect(guild)

    return {
        "on_ready": on_ready,
        "on_message": on_message,
        "on_voice_state_update": on_voice_state_update,
    }
