"""Voice-connection lifecycle policy for TTSBot.

Connect/move with per-guild locks and cooldowns, stale-client recovery,
idle disconnect scheduling, continuous-stream idle stop and auto-connect
suppression. No synthesis concerns here — the TTS backend must never own
voice lifecycle policy.
"""

import asyncio
import logging
import time

import discord

from ttsbot import config

log = logging.getLogger("tts_bot")


class VoiceLifecycleMixin:
    """Voice connect/disconnect policy; mixed into TTSBot."""

    def cancel_idle_disconnect(self, guild_id: int) -> None:
        task = self.idle_disconnect_tasks.pop(guild_id, None)
        if task and not task.done():
            task.cancel()
            log.info("Cancelled idle disconnect guild=%s", guild_id)

    def cancel_continuous_idle_stop(self, guild_id: int) -> None:
        task = self.continuous_idle_stop_tasks.pop(guild_id, None)
        if task and not task.done():
            task.cancel()
            log.info("Cancelled continuous stream idle stop guild=%s", guild_id)

    def get_voice_connect_lock(self, guild_id: int) -> asyncio.Lock:
        lock = self.voice_connect_locks.get(guild_id)
        if not lock:
            lock = asyncio.Lock()
            self.voice_connect_locks[guild_id] = lock
        return lock

    def set_voice_connect_cooldown(self, guild_id: int, reason: str) -> None:
        until = time.monotonic() + config.VOICE_CONNECT_COOLDOWN_SECONDS
        self.voice_connect_cooldown_until[guild_id] = until
        log.warning(
            "Voice connect cooldown set guild=%s seconds=%s reason=%s",
            guild_id,
            config.VOICE_CONNECT_COOLDOWN_SECONDS,
            reason,
        )

    def voice_connect_cooldown_remaining(self, guild_id: int) -> float:
        until = self.voice_connect_cooldown_until.get(guild_id)
        if until is None:
            return 0.0

        remaining = until - time.monotonic()
        if remaining <= 0:
            self.voice_connect_cooldown_until.pop(guild_id, None)
            return 0.0
        return remaining

    def suppress_auto_connect(self, guild_id: int, reason: str) -> None:
        if config.AUTO_CONNECT_SUPPRESS_SECONDS <= 0:
            return
        until = time.monotonic() + config.AUTO_CONNECT_SUPPRESS_SECONDS
        self.suppress_auto_connect_until[guild_id] = until
        log.info(
            "Auto-connect suppressed guild=%s seconds=%s reason=%s",
            guild_id,
            config.AUTO_CONNECT_SUPPRESS_SECONDS,
            reason,
        )

    def suppress_auto_connect_remaining(self, guild_id: int) -> float:
        until = self.suppress_auto_connect_until.get(guild_id)
        if until is None:
            return 0.0

        remaining = until - time.monotonic()
        if remaining <= 0:
            self.suppress_auto_connect_until.pop(guild_id, None)
            return 0.0
        return remaining

    def schedule_idle_disconnect(self, guild: discord.Guild) -> None:
        self.cancel_idle_disconnect(guild.id)
        task = asyncio.create_task(
            self._idle_disconnect_after_timeout(guild),
            name=f"idle-disconnect-{guild.id}",
        )
        self.idle_disconnect_tasks[guild.id] = task
        log.info("Scheduled idle disconnect guild=%s timeout=%ss", guild.id, config.IDLE_DISCONNECT_SECONDS)

    def has_active_voice_playback(self, guild_id: int, vc: discord.VoiceClient) -> bool:
        source = self.continuous_sources.get(guild_id)
        if source and not source.stopped:
            return not source.is_drained
        return vc.is_playing() or vc.is_paused()

    def _whitelisted_user_in_channel(
        self, guild: discord.Guild, channel: discord.VoiceChannel | None
    ) -> bool:
        if channel is None:
            return False
        return any(
            (not user.bot) and self.config_store.is_allowed(guild.id, user.id)
            for user in channel.members
        )

    async def _idle_disconnect_after_timeout(self, guild: discord.Guild) -> None:
        try:
            poll_interval = 5.0
            elapsed = 0.0
            while elapsed < config.IDLE_DISCONNECT_SECONDS:
                remaining = config.IDLE_DISCONNECT_SECONDS - elapsed
                await asyncio.sleep(min(poll_interval, remaining))
                elapsed += poll_interval

                vc = discord.utils.get(self.voice_clients, guild=guild)
                if not vc or not vc.is_connected():
                    return

                channel = vc.channel if isinstance(vc.channel, discord.VoiceChannel) else None
                if self._whitelisted_user_in_channel(guild, channel):
                    log.info(
                        "Cancel idle disconnect guild=%s reason=whitelisted_user_present",
                        guild.id,
                    )
                    return

                if self.has_active_voice_playback(guild.id, vc):
                    log.info("Skip idle disconnect guild=%s reason=playback_active", guild.id)
                    return

            vc = discord.utils.get(self.voice_clients, guild=guild)
            if not vc or not vc.is_connected():
                return

            if self.has_active_voice_playback(guild.id, vc):
                log.info("Skip idle disconnect guild=%s reason=playback_active", guild.id)
                return

            channel = vc.channel if isinstance(vc.channel, discord.VoiceChannel) else None
            if self._whitelisted_user_in_channel(guild, channel):
                log.info(
                    "Cancel idle disconnect guild=%s reason=whitelisted_user_present",
                    guild.id,
                )
                return

            source = self.continuous_sources.pop(guild.id, None)
            if source:
                source.stop()
                if vc.is_playing() or vc.is_paused():
                    vc.stop()

            await vc.disconnect(force=True)
            self.suppress_auto_connect(guild.id, "idle_disconnect")
            log.info("Idle disconnect executed guild=%s", guild.id)
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("Idle disconnect task failed guild=%s", guild.id)

    def schedule_continuous_idle_stop(self, guild: discord.Guild) -> None:
        if not config.TTS_CONTINUOUS_STREAM or config.TTS_MAX_CONTINUOUS_IDLE_SECONDS <= 0:
            return

        self.cancel_continuous_idle_stop(guild.id)
        task = asyncio.create_task(
            self._continuous_idle_stop_after_timeout(guild),
            name=f"continuous-idle-stop-{guild.id}",
        )
        self.continuous_idle_stop_tasks[guild.id] = task
        log.info(
            "Scheduled continuous stream idle stop guild=%s timeout=%ss",
            guild.id,
            config.TTS_MAX_CONTINUOUS_IDLE_SECONDS,
        )

    async def _continuous_idle_stop_after_timeout(self, guild: discord.Guild) -> None:
        try:
            await asyncio.sleep(config.TTS_MAX_CONTINUOUS_IDLE_SECONDS)

            source = self.continuous_sources.get(guild.id)
            if not source or source.stopped:
                return
            if not source.is_drained:
                log.info("Skip continuous stream idle stop guild=%s reason=speech_pending", guild.id)
                return

            self.continuous_sources.pop(guild.id, None)
            source.stop()

            vc = discord.utils.get(self.voice_clients, guild=guild)
            if vc and vc.is_connected() and (vc.is_playing() or vc.is_paused()):
                vc.stop()

            log.info("Continuous stream idle stop executed guild=%s", guild.id)
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("Continuous stream idle stop task failed guild=%s", guild.id)

    async def ensure_voice(self, voice_channel: discord.VoiceChannel) -> discord.VoiceClient:
        started = time.perf_counter()
        guild_id = voice_channel.guild.id
        self.cancel_idle_disconnect(guild_id)
        lock = self.get_voice_connect_lock(guild_id)

        async with lock:
            remaining = self.voice_connect_cooldown_remaining(guild_id)
            if remaining > 0:
                raise RuntimeError(f"Voice connect cooldown active for {remaining:.1f}s")

            vc = discord.utils.get(self.voice_clients, guild=voice_channel.guild)

            if not vc or not vc.is_connected():
                # A prior handshake may have died (network flakiness) yet left a
                # VoiceClient registered on the guild whose socket is dead.
                # connect() would then raise "Already connected" and we'd get
                # stuck cooldown-looping forever, so force-clean the stale client
                # before (re)connecting.
                stale_vc = getattr(voice_channel.guild, "voice_client", None)
                if stale_vc is not None and not stale_vc.is_connected():
                    log.warning(
                        "Cleaning up stale voice client guild=%s channel=%s",
                        guild_id,
                        voice_channel.id,
                    )
                    try:
                        await stale_vc.disconnect(force=True)
                    except Exception:
                        log.exception(
                            "Stale voice client cleanup failed guild=%s", guild_id
                        )

                log.info(
                    "Connecting to voice channel guild=%s channel=%s",
                    guild_id,
                    voice_channel.id,
                )
                try:
                    vc = await voice_channel.connect(timeout=60.0, self_deaf=True)
                except discord.errors.ClientException as exc:
                    if "Already connected" in str(exc):
                        # State desync: discord.py internal state has an active voice client
                        # that isn't reflected in self.voice_clients yet. Recover it instead
                        # of setting a cooldown and looping forever.
                        existing_vc = voice_channel.guild.voice_client
                        if existing_vc and existing_vc.is_connected():
                            log.warning(
                                "Voice state desync recovered guild=%s channel=%s",
                                guild_id,
                                voice_channel.id,
                            )
                            if existing_vc.channel != voice_channel:
                                await existing_vc.move_to(voice_channel)
                            return existing_vc
                    self.set_voice_connect_cooldown(guild_id, type(exc).__name__)
                    raise
                except Exception as exc:
                    self.set_voice_connect_cooldown(guild_id, type(exc).__name__)
                    raise

                log.info(
                    "Voice connect done guild=%s channel=%s took=%.3fs",
                    guild_id,
                    voice_channel.id,
                    time.perf_counter() - started,
                )
            elif vc.channel != voice_channel:
                log.info(
                    "Moving voice client guild=%s from=%s to=%s",
                    guild_id,
                    getattr(vc.channel, "id", "unknown"),
                    voice_channel.id,
                )
                try:
                    await vc.move_to(voice_channel)
                except Exception as exc:
                    self.set_voice_connect_cooldown(guild_id, f"move:{type(exc).__name__}")
                    raise

                log.info(
                    "Voice move done guild=%s channel=%s took=%.3fs",
                    guild_id,
                    voice_channel.id,
                    time.perf_counter() - started,
                )
            else:
                log.info(
                    "Voice ready guild=%s channel=%s took=%.3fs",
                    guild_id,
                    voice_channel.id,
                    time.perf_counter() - started,
                )

        return vc

    async def disconnect_guild_voice(self, guild: discord.Guild) -> None:
        self.cancel_idle_disconnect(guild.id)
        self.cancel_continuous_idle_stop(guild.id)
        source = self.continuous_sources.pop(guild.id, None)
        if source:
            source.stop()
        vc = discord.utils.get(self.voice_clients, guild=guild)
        if not vc:
            return
        try:
            await vc.disconnect(force=True)
            self.suppress_auto_connect(guild.id, "explicit_disconnect")
            log.info("Voice disconnected guild=%s", guild.id)
        except Exception:
            log.exception("Voice disconnect cleanup failed")

    async def auto_connect_for_member(self, member: discord.Member, channel: discord.VoiceChannel) -> None:
        if not config.TTS_AUTO_CONNECT_ENABLED:
            log.info(
                "Skip auto-connect guild=%s member=%s reason=disabled",
                channel.guild.id,
                member.id,
            )
            return
        if not self.config_store.is_enabled(channel.guild.id):
            return
        if not self.config_store.is_allowed(channel.guild.id, member.id):
            return

        remaining = self.voice_connect_cooldown_remaining(channel.guild.id)
        if remaining > 0:
            log.info(
                "Skip auto-connect guild=%s member=%s reason=cooldown remaining=%.1fs",
                channel.guild.id,
                member.id,
                remaining,
            )
            return

        suppress_remaining = self.suppress_auto_connect_remaining(channel.guild.id)
        if suppress_remaining > 0:
            log.info(
                "Skip auto-connect guild=%s member=%s reason=recent_disconnect remaining=%.1fs",
                channel.guild.id,
                member.id,
                suppress_remaining,
            )
            return

        try:
            await self.ensure_voice(channel)
            log.info(
                "Auto-connected for whitelisted user guild=%s member=%s channel=%s",
                channel.guild.id,
                member.id,
                channel.id,
            )
        except Exception:
            log.exception(
                "Auto-connect failed guild=%s member=%s channel=%s",
                channel.guild.id,
                member.id,
                channel.id,
            )
