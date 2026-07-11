"""Message queueing and merge (selective hold) policy for TTSBot.

The enqueue boundary normalizes text once for every caller; the merge
layer decides, per author+channel, whether a message plays immediately,
starts a hold buffer, appends to one, or forces a flush — preserving
message order throughout.
"""

import asyncio
import logging
import time

import discord

from ttsbot import config
from ttsbot.messages import MergeBufferState, ParsedMessage, analyze_message_for_merge, is_reaction_like
from ttsbot.models import TTSJob
from ttsbot.textnorm import normalize_for_tts

log = logging.getLogger("tts_bot")


class MergeQueueMixin:
    """Enqueue + merge/selective-hold decisions; mixed into TTSBot."""

    async def enqueue_tts(
        self,
        text: str,
        voice_channel: discord.VoiceChannel,
        author_id: int,
        text_channel_id: int,
        message_ts: float | None = None,
    ) -> bool:
        # Normalize once at the boundary so every caller (merge buffer,
        # /voicebot test, future commands) gets identical cleanup. The
        # TTS_MAX_CHARS cap is enforced here (300 by default, per spec
        # §"Препроцессинг текста") — it protects the cloud API quota
        # from accidental walls of text and keeps Piper CPU bounded.
        cleaned = normalize_for_tts(
            text,
            max_chars=config.TTS_MAX_CHARS,
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
            await asyncio.wait_for(
                self.message_queue.put(job),
                timeout=max(config.TTS_QUEUE_PUT_TIMEOUT_MS, 1) / 1000.0,
            )
            log.info(
                "Queued TTS guild=%s text_channel=%s voice_channel=%s author=%s queue=%s chars=%s voice=%s",
                voice_channel.guild.id,
                text_channel_id,
                voice_channel.id,
                author_id,
                self.message_queue.qsize(),
                len(text),
                voice_profile,
            )
            return True
        except (asyncio.QueueFull, TimeoutError):
            log.warning("TTS enqueue failed author=%s enqueue_fail_reason=queue_timeout", author_id)
            return False

    def _merge_lock(self, key: tuple[int, int]) -> asyncio.Lock:
        lock = self.merge_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self.merge_locks[key] = lock
        return lock

    def _next_merge_generation(self, key: tuple[int, int]) -> int:
        generation = self.merge_generations.get(key, 0) + 1
        self.merge_generations[key] = generation
        return generation

    def _decision_log(self, decision: str, parsed: ParsedMessage, **extra: object) -> None:
        if not config.TTS_SELECTIVE_HOLD_LOG_DECISIONS:
            return
        payload = {
            "decision_policy": config.TTS_MERGE_ALGORITHM,
            "decision": decision,
            "raw_length": parsed.raw_length,
            "effective_length": parsed.effective_length,
            "word_count": parsed.word_count,
            "emoji_count": parsed.emoji_count,
            "custom_emoji_count": parsed.custom_emoji_count,
            "animated_custom_emoji_count": parsed.animated_custom_emoji_count,
            "mention_count": parsed.mention_count,
            "url_count": parsed.url_count,
        }
        payload.update(extra)
        log.info("Selective hold decision %s", payload)

    async def _enqueue_buffer_state(self, state: MergeBufferState, reason: str) -> bool:
        if not state.items:
            return True
        text = state.join_separator.join(item.spoken_text for item in state.items if item.spoken_text).strip()
        if not text:
            return True
        try:
            ok = await self.enqueue_tts(
                text,
                state.voice_channel,
                state.author_id,
                state.text_channel_id,
                message_ts=state.first_ts,
            )
            if ok:
                log.info(
                    "Merged buffer flushed key=%s parts=%s reason=%s buffer_age_ms=%s effective_length=%s",
                    state.key,
                    len(state.items),
                    reason,
                    round((time.perf_counter() - state.first_ts) * 1000),
                    state.effective_len_total,
                )
            return ok
        except Exception:
            log.exception("Failed to enqueue merged state key=%s reason=%s", state.key, reason)
            return False

    async def _flush_buffer_locked(self, key: tuple[int, int], reason: str, expected_generation: int | None = None) -> bool:
        state = self.merge_buffers.get(key)
        if not state:
            return True
        if expected_generation is not None and state.generation_id != expected_generation:
            log.info(
                "Selective hold stale_timer_ignored key=%s expected_generation=%s current_generation=%s",
                key,
                expected_generation,
                state.generation_id,
            )
            return False
        if state.timer_task and not state.timer_task.done():
            state.timer_task.cancel()
        ok = await self._enqueue_buffer_state(state, reason)
        if ok:
            self.merge_buffers.pop(key, None)
        return ok

    async def _flush_merge_after_delay(self, key: tuple[int, int], generation_id: int, deadline_ts: float) -> None:
        sleep_for = max(0.0, deadline_ts - time.perf_counter())
        try:
            await asyncio.sleep(sleep_for)
            async with self._merge_lock(key):
                await self._flush_buffer_locked(key, "timer_flush", expected_generation=generation_id)
        except asyncio.CancelledError:
            return
        except asyncio.QueueFull:
            return
        except Exception:
            log.exception("Failed to flush merged messages key=%s", key)

    async def queue_or_merge_message(
        self,
        text: str,
        voice_channel: discord.VoiceChannel,
        author_id: int,
        text_channel_id: int,
        mentions: dict[str, str] | None = None,
    ) -> None:
        key = (author_id, voice_channel.id)
        now = time.perf_counter()
        parsed = analyze_message_for_merge(
            text, self.config_store.emoji_say_map(), mentions
        )
        previous_ts = self.last_user_message_ts.get(key)
        gap_prev_ms = None if previous_ts is None else round((now - previous_ts) * 1000)
        self.last_user_message_ts[key] = now

        if config.TTS_MERGE_ALGORITHM == "off":
            if parsed.spoken_text:
                await self.enqueue_tts(parsed.spoken_text, voice_channel, author_id, text_channel_id, message_ts=now)
            return

        selective_enabled_for_author = (
            config.TTS_MERGE_ALGORITHM == "selective_hold_v2"
            and config.TTS_SELECTIVE_HOLD_ENABLED
        )
        if not selective_enabled_for_author:
            if not parsed.spoken_text:
                return
            if not config.TTS_MERGE_SHORT_MESSAGES or len(parsed.spoken_text) > config.TTS_MERGE_MAX_CHARS:
                await self.enqueue_tts(parsed.spoken_text, voice_channel, author_id, text_channel_id, message_ts=now)
                return
            async with self._merge_lock(key):
                state = self.merge_buffers.get(key)
                if state is None:
                    state = MergeBufferState(
                        key=key,
                        voice_channel=voice_channel,
                        author_id=author_id,
                        text_channel_id=text_channel_id,
                        first_ts=now,
                        last_ts=now,
                        deadline_ts=now + max(config.TTS_MERGE_WINDOW_MS, 0) / 1000.0,
                        generation_id=self._next_merge_generation(key),
                        join_separator=". ",
                        items=[],
                    )
                    self.merge_buffers[key] = state
                state.items.append(parsed)
                if state.timer_task and not state.timer_task.done():
                    state.timer_task.cancel()
                state.generation_id += 1
                state.timer_task = asyncio.create_task(
                    self._flush_merge_after_delay(key, state.generation_id, time.perf_counter() + max(config.TTS_MERGE_WINDOW_MS, 0) / 1000.0)
                )
            return

        async with self._merge_lock(key):
            state = self.merge_buffers.get(key)
            if state is None:
                if not parsed.spoken_text:
                    self._decision_log("drop_empty", parsed)
                    return
                if parsed.effective_length >= max(config.TTS_MERGE_MAX_CHARS, 40):
                    self._decision_log("immediate_long", parsed)
                    await self.enqueue_tts(parsed.spoken_text, voice_channel, author_id, text_channel_id, message_ts=now)
                    return
                if parsed.is_special_only or parsed.is_caps_shout or parsed.is_keyboard_smash or parsed.is_question_or_terminal:
                    self._decision_log("immediate_special", parsed)
                    if parsed.spoken_text:
                        await self.enqueue_tts(parsed.spoken_text, voice_channel, author_id, text_channel_id, message_ts=now)
                    return
                if (
                    is_reaction_like(parsed)
                    and (
                        previous_ts is None
                        or gap_prev_ms is not None
                        and gap_prev_ms > config.TTS_SELECTIVE_HOLD_REACTION_PAUSE_MS
                    )
                ):
                    self._decision_log(
                        "immediate_isolated_reaction",
                        parsed,
                        gap_prev_ms=gap_prev_ms,
                    )
                    await self.enqueue_tts(parsed.spoken_text, voice_channel, author_id, text_channel_id, message_ts=now)
                    return
                strong = (
                    parsed.effective_length >= config.TTS_SELECTIVE_HOLD_START_EFFECTIVE_LEN
                    or (
                        parsed.effective_length >= config.TTS_SELECTIVE_HOLD_START_MIN_EFFECTIVE_LEN_ALT
                        and parsed.word_count >= config.TTS_SELECTIVE_HOLD_START_MIN_WORDS_ALT
                    )
                    or (parsed.is_single_digit and parsed.effective_length >= 4)
                )
                if not strong:
                    self._decision_log("immediate_default", parsed)
                    await self.enqueue_tts(parsed.spoken_text, voice_channel, author_id, text_channel_id, message_ts=now)
                    return
                generation = self._next_merge_generation(key)
                deadline = now + max(config.TTS_SELECTIVE_HOLD_HARD_CAP_MS, 1) / 1000.0
                state = MergeBufferState(
                    key=key,
                    voice_channel=voice_channel,
                    author_id=author_id,
                    text_channel_id=text_channel_id,
                    first_ts=now,
                    last_ts=now,
                    deadline_ts=deadline,
                    generation_id=generation,
                    join_separator=config.TTS_SELECTIVE_HOLD_JOIN_SEPARATOR,
                    items=[parsed],
                    has_substantive_starter=True,
                )
                state.timer_task = asyncio.create_task(self._flush_merge_after_delay(key, generation, deadline))
                self.merge_buffers[key] = state
                self._decision_log(
                    "hold_start",
                    parsed,
                    chosen_timeout_ms=config.TTS_SELECTIVE_HOLD_HARD_CAP_MS,
                    messages_in_buffer=1,
                    buffer_age_ms=0,
                )
                return

            if state.voice_channel.id != voice_channel.id or state.text_channel_id != text_channel_id:
                await self._flush_buffer_locked(key, "voice_context_changed")
                if parsed.spoken_text:
                    await self.enqueue_tts(parsed.spoken_text, voice_channel, author_id, text_channel_id, message_ts=now)
                return

            hard_break = (
                parsed.is_special_only
                or parsed.is_question_or_terminal
                or parsed.is_caps_shout
                or parsed.is_keyboard_smash
                or now > state.deadline_ts
            )
            if hard_break:
                self._decision_log(
                    "hard_break",
                    parsed,
                    messages_in_buffer=len(state.items),
                    buffer_effective_length=state.effective_len_total,
                    buffer_age_ms=round((now - state.first_ts) * 1000),
                )
                ok = await self._flush_buffer_locked(key, "flush_before_hard_break")
                if ok and parsed.spoken_text:
                    await self.enqueue_tts(parsed.spoken_text, voice_channel, author_id, text_channel_id, message_ts=now)
                return

            prospective_parts = len(state.items) + 1
            prospective_effective = state.effective_len_total + parsed.effective_length
            if (
                prospective_parts > max(config.TTS_SELECTIVE_HOLD_MAX_PARTS, 1)
                or prospective_effective > max(config.TTS_SELECTIVE_HOLD_MAX_GROUP_EFFECTIVE_LEN, 1)
            ):
                ok = await self._flush_buffer_locked(key, "flush_before_reclassify")
                if ok:
                    await self.enqueue_tts(parsed.spoken_text, voice_channel, author_id, text_channel_id, message_ts=now)
                return

            state.items.append(parsed)
            state.last_ts = now
            self._decision_log(
                "append_soft",
                parsed,
                messages_in_buffer=len(state.items),
                buffer_effective_length=state.effective_len_total,
                buffer_age_ms=round((now - state.first_ts) * 1000),
            )

    def clear_merge_buffers(self, guild_id: int) -> None:
        stale_keys = [key for key, state in self.merge_buffers.items() if state.voice_channel.guild.id == guild_id]
        for key in stale_keys:
            state = self.merge_buffers.pop(key, None)
            if state and state.timer_task and not state.timer_task.done():
                state.timer_task.cancel()

    def clear_queue_for_guild(self, guild_id: int) -> int:
        kept: list[TTSJob] = []
        removed = 0
        while True:
            try:
                job = self.message_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if job.guild_id == guild_id:
                removed += 1
                self.message_queue.task_done()
            else:
                kept.append(job)
                self.message_queue.task_done()

        for job in kept:
            self.message_queue.put_nowait(job)

        # Cancel prefetched/in-flight prepared audio for this guild so it is
        # neither played nor finishes burning quota on generation.
        for prepared in self.active_prepared:
            if prepared.job.guild_id == guild_id and not prepared.cancelled:
                prepared.cancelled = True
                removed += 1
        return removed
