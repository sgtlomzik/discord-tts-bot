"""Message intake for TTSBot: every message is queued as it arrives.

The enqueue boundary normalizes text once for every caller (messages,
/voicebot test) and applies the per-message length cap; jobs keep the
order in which their messages arrived.
"""

import asyncio
import logging
import time

import discord

from ttsbot import config
from ttsbot.models import TTSJob
from ttsbot.textnorm import normalize_for_tts, speakable_text

log = logging.getLogger("tts_bot")


class MessageQueueMixin:
    """Enqueue and queue clearing; mixed into TTSBot."""

    async def enqueue_tts(
        self,
        text: str,
        voice_channel: discord.VoiceChannel,
        author_id: int,
        text_channel_id: int,
        message_ts: float | None = None,
    ) -> bool:
        # Normalize once at the boundary so every caller gets identical
        # cleanup. The TTS_MAX_CHARS cap is enforced here: it protects the
        # cloud quotas from walls of text and keeps Piper CPU bounded.
        cleaned = normalize_for_tts(
            text,
            max_chars=self.config_store.max_chars(),
            emoji_aliases=self.config_store.emoji_say_map(),
        )
        if not cleaned:
            log.info(
                "Skipped TTS enqueue author=%s reason=empty_after_normalize",
                author_id,
            )
            return False
        try:
            now = time.perf_counter()
            voice_profile = self.config_store.voice_for_user(voice_channel.guild.id, author_id)
            job = TTSJob(
                text=cleaned,
                voice_channel=voice_channel,
                queued_at=now,
                author_id=author_id,
                guild_id=voice_channel.guild.id,
                text_channel_id=text_channel_id,
                voice_profile=voice_profile,
                message_ts=message_ts or now,
            )
            session = self.start_session(voice_channel.guild.id)
            await asyncio.wait_for(
                session.queue.put(job),
                timeout=max(config.TTS_QUEUE_PUT_TIMEOUT_MS, 1) / 1000.0,
            )
            log.info(
                "Queued TTS guild=%s text_channel=%s voice_channel=%s author=%s queue=%s chars=%s voice=%s job=%s",
                voice_channel.guild.id,
                text_channel_id,
                voice_channel.id,
                author_id,
                session.queue.qsize(),
                len(text),
                voice_profile,
                job.job_id,
            )
            return True
        except (asyncio.QueueFull, TimeoutError):
            log.warning("TTS enqueue failed author=%s enqueue_fail_reason=queue_timeout", author_id)
            return False

    async def queue_message(
        self,
        text: str,
        voice_channel: discord.VoiceChannel,
        author_id: int,
        text_channel_id: int,
        mentions: dict[str, str] | None = None,
    ) -> None:
        """Queue one Discord message for speech, if anything in it is speakable."""
        spoken = speakable_text(text, self.config_store.emoji_say_map(), mentions)
        if spoken:
            await self.enqueue_tts(spoken, voice_channel, author_id, text_channel_id,
                                   message_ts=time.perf_counter())

    def clear_queue_for_guild(self, guild_id: int) -> int:
        """Drop the guild's queued messages and cancel the ones in flight
        (neither played nor finishing their generation); return how many."""
        session = self.sessions.get(guild_id)
        return session.cancel_all() if session is not None else 0
