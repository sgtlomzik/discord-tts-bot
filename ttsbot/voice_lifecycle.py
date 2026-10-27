"""Voice-connection lifecycle policy for TTSBot.

Connect/move with per-guild locks and cooldowns, stale-client recovery,
idle disconnect scheduling, continuous-stream idle stop and auto-connect
suppression. No synthesis concerns here — the TTS backend must never own
voice lifecycle policy.
"""

import asyncio
import logging
import time
from collections.abc import Sequence
from typing import TYPE_CHECKING

import discord

from ttsbot import config
from ttsbot.recording.client import RecordingVoiceClient

if TYPE_CHECKING:
    from ttsbot.session import GuildSession
    from ttsbot.store import BotConfigStore

log = logging.getLogger("tts_bot")


def records_guild(guild_id: int) -> bool:
    """Whether the bot records voice in this guild (RECORDING_GUILD_IDS)."""
    guilds = config.RECORDING_GUILD_IDS
    return config.RECORDING_ENABLED and (not guilds or guild_id in guilds)


class VoiceCooldown(RuntimeError):
    """A connect or move was refused because a recent one failed.

    The bot may still be connected and playing elsewhere in the guild, so
    callers must not treat this as a broken connection.
    """


class VoiceLifecycleMixin:
    """Voice connect/disconnect policy; mixed into TTSBot."""

    if TYPE_CHECKING:
        # Provided by TTSBot and discord.Client; declared for the type checker.
        config_store: BotConfigStore
        rejoin_task: asyncio.Task | None

        def session_for(self, guild_id: int) -> GuildSession: ...

        @property
        def guilds(self) -> Sequence[discord.Guild]: ...

    def cancel_idle_disconnect(self, guild_id: int) -> None:
        session = self.sessions.get(guild_id)
        task = session.idle_disconnect_task if session is not None else None
        if task is not None:
            session.idle_disconnect_task = None
            if not task.done():
                task.cancel()
                log.debug("Cancelled idle disconnect guild=%s", guild_id)

    def cancel_continuous_idle_stop(self, guild_id: int) -> None:
        session = self.sessions.get(guild_id)
        task = session.idle_stop_task if session is not None else None
        if task is not None:
            session.idle_stop_task = None
            if not task.done():
                task.cancel()
                log.debug("Cancelled continuous stream idle stop guild=%s", guild_id)

    def get_voice_connect_lock(self, guild_id: int) -> asyncio.Lock:
        return self.session_for(guild_id).voice_lock

    def set_voice_connect_cooldown(self, guild_id: int, reason: str) -> None:
        self.session_for(guild_id).connect_cooldown_until = time.monotonic() + config.VOICE_CONNECT_COOLDOWN_SECONDS
        log.warning(
            "Voice connect cooldown set guild=%s seconds=%s reason=%s",
            guild_id,
            config.VOICE_CONNECT_COOLDOWN_SECONDS,
            reason,
        )

    def _check_voice_cooldown(self, guild_id: int) -> None:
        remaining = self.voice_connect_cooldown_remaining(guild_id)
        if remaining > 0:
            raise VoiceCooldown(f"Voice connect cooldown active for {remaining:.1f}s")

    def voice_connect_cooldown_remaining(self, guild_id: int) -> float:
        session = self.sessions.get(guild_id)
        if session is None:
            return 0.0
        return max(0.0, session.connect_cooldown_until - time.monotonic())

    def suppress_auto_connect(self, guild_id: int, reason: str) -> None:
        if config.AUTO_CONNECT_SUPPRESS_SECONDS <= 0:
            return
        self.session_for(guild_id).auto_connect_suppressed_until = (
            time.monotonic() + config.AUTO_CONNECT_SUPPRESS_SECONDS
        )
        log.info(
            "Auto-connect suppressed guild=%s seconds=%s reason=%s",
            guild_id,
            config.AUTO_CONNECT_SUPPRESS_SECONDS,
            reason,
        )

    def suppress_auto_connect_remaining(self, guild_id: int) -> float:
        session = self.sessions.get(guild_id)
        if session is None:
            return 0.0
        return max(0.0, session.auto_connect_suppressed_until - time.monotonic())

    def schedule_idle_disconnect(self, guild: discord.Guild) -> None:
        self.cancel_idle_disconnect(guild.id)
        self.session_for(guild.id).idle_disconnect_task = asyncio.create_task(
            self._idle_disconnect_after_timeout(guild),
            name=f"idle-disconnect-{guild.id}",
        )
        log.debug("Scheduled idle disconnect guild=%s timeout=%ss", guild.id, config.IDLE_DISCONNECT_SECONDS)

    def has_active_voice_playback(self, guild_id: int, vc: discord.VoiceClient) -> bool:
        session = self.sessions.get(guild_id)
        source = session.player if session is not None else None
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
                    log.debug(
                        "Cancel idle disconnect guild=%s reason=whitelisted_user_present",
                        guild.id,
                    )
                    return

                if self.has_active_voice_playback(guild.id, vc):
                    log.debug("Skip idle disconnect guild=%s reason=playback_active", guild.id)
                    return

            # Under the connect lock: a connect or move that is in progress
            # finishes first, and the checks below see where it landed.
            async with self.get_voice_connect_lock(guild.id):
                vc = discord.utils.get(self.voice_clients, guild=guild)
                if not vc or not vc.is_connected():
                    return

                if self.has_active_voice_playback(guild.id, vc):
                    log.debug("Skip idle disconnect guild=%s reason=playback_active", guild.id)
                    return

                channel = vc.channel if isinstance(vc.channel, discord.VoiceChannel) else None
                if self._whitelisted_user_in_channel(guild, channel):
                    log.debug(
                        "Cancel idle disconnect guild=%s reason=whitelisted_user_present",
                        guild.id,
                    )
                    return

                source = self.session_for(guild.id).take_player()
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
        if config.TTS_MAX_CONTINUOUS_IDLE_SECONDS <= 0:
            return

        self.cancel_continuous_idle_stop(guild.id)
        self.session_for(guild.id).idle_stop_task = asyncio.create_task(
            self._continuous_idle_stop_after_timeout(guild),
            name=f"continuous-idle-stop-{guild.id}",
        )
        log.debug(
            "Scheduled continuous stream idle stop guild=%s timeout=%ss",
            guild.id,
            config.TTS_MAX_CONTINUOUS_IDLE_SECONDS,
        )

    async def _continuous_idle_stop_after_timeout(self, guild: discord.Guild) -> None:
        try:
            await asyncio.sleep(config.TTS_MAX_CONTINUOUS_IDLE_SECONDS)

            session = self.session_for(guild.id)
            source = session.player
            if not source or source.stopped:
                return
            if not source.is_drained:
                log.debug("Skip continuous stream idle stop guild=%s reason=speech_pending", guild.id)
                return

            session.take_player()
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
            vc = discord.utils.get(self.voice_clients, guild=voice_channel.guild)

            # The cooldown holds back new connects and moves only: the
            # channel the bot is already in stays usable.
            if not vc or not vc.is_connected():
                self._check_voice_cooldown(guild_id)
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
                    if records_guild(guild_id):
                        # A deafened member receives no audio.
                        vc = await voice_channel.connect(
                            timeout=60.0, self_deaf=False, cls=RecordingVoiceClient
                        )
                    else:
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
                self._check_voice_cooldown(guild_id)
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
                log.debug(
                    "Voice ready guild=%s channel=%s took=%.3fs",
                    guild_id,
                    voice_channel.id,
                    time.perf_counter() - started,
                )

        return vc

    async def disconnect_guild_voice(self, guild: discord.Guild, *, cancel_jobs: bool = True) -> None:
        """Leave the guild's voice channel.

        A deliberate disconnect (``/voicebot off``, ``!tts leave``) also
        cancels the jobs being generated or played: they have nowhere to go.
        Recovery from a broken connection passes ``cancel_jobs=False``: the
        next job reconnects, so one failed message does not drop the rest.
        """
        session = self.sessions.get(guild.id)
        for prepared in session.active if cancel_jobs and session is not None else ():
            prepared.cancelled = True
        self.cancel_idle_disconnect(guild.id)
        self.cancel_continuous_idle_stop(guild.id)
        source = self.session_for(guild.id).take_player()
        if source:
            source.stop()
        # The audio stops at once; the disconnect waits for a connect or
        # move in progress, which would otherwise land after it.
        async with self.get_voice_connect_lock(guild.id):
            vc = discord.utils.get(self.voice_clients, guild=guild)
            if not vc:
                return
            try:
                await vc.disconnect(force=True)
                self.suppress_auto_connect(guild.id, "explicit_disconnect")
                log.info("Voice disconnected guild=%s", guild.id)
            except Exception:
                log.exception("Voice disconnect cleanup failed")

    def schedule_voice_rejoin(self, reason: str) -> None:
        """Rejoin occupied voice channels in the background (see below)."""
        if self.rejoin_task is not None and not self.rejoin_task.done():
            return
        self.rejoin_task = asyncio.create_task(self.rejoin_occupied_voice(reason), name="voice-rejoin")

    async def rejoin_occupied_voice(self, reason: str) -> None:
        """Join allowed members already sitting in voice.

        Auto-connect only reacts to a member joining or switching a channel,
        so after a restart, or a gateway outage long enough to drop the voice
        connection, the bot stayed out (and nothing was recorded) until
        someone moved. Runs on every on_ready: in each guild the bot is not
        connected in, it picks the voice channel with the most allowed
        members and goes through auto_connect_for_member and its checks.
        """
        for guild in list(self.guilds):
            try:
                if not config.TTS_AUTO_CONNECT_ENABLED or not self.config_store.is_enabled(guild.id):
                    continue
                vc = guild.voice_client
                if isinstance(vc, discord.VoiceClient) and vc.is_connected():
                    continue
                best: discord.VoiceChannel | None = None
                best_allowed: list[discord.Member] = []
                for channel in guild.voice_channels:
                    allowed = [
                        user for user in channel.members
                        if not user.bot and self.config_store.is_allowed(guild.id, user.id)
                    ]
                    if len(allowed) > len(best_allowed):
                        best, best_allowed = channel, allowed
                if best is None:
                    continue
                log.info(
                    "Rejoining occupied voice guild=%s channel=%s allowed=%s reason=%s",
                    guild.id,
                    best.id,
                    len(best_allowed),
                    reason,
                )
                await self.auto_connect_for_member(best_allowed[0], best)
            except Exception:
                log.exception("Voice rejoin failed guild=%s reason=%s", guild.id, reason)

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

        vc = discord.utils.get(self.voice_clients, guild=channel.guild)
        if vc is not None and vc.is_connected():
            if vc.channel == channel:
                # Mute/deafen/stream toggles land here too; nothing to do.
                return
            if self._whitelisted_user_in_channel(channel.guild, vc.channel) or self.has_active_voice_playback(
                channel.guild.id, vc
            ):
                # Following one member would strand everyone else, and a move
                # stalls playback for up to discord.py's 30 s move timeout.
                log.debug(
                    "Skip auto-connect guild=%s member=%s reason=busy current=%s target=%s",
                    channel.guild.id,
                    member.id,
                    getattr(vc.channel, "id", "unknown"),
                    channel.id,
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
