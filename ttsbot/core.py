"""The TTSBot Discord client: queueing, merging, synthesis and playback.

This is the runtime core. Slash commands and event handlers are attached
from the outside (see ttsbot.commands / ttsbot.events and the bot.py
composition root); the class itself owns the pipeline state and the
voice-connection lifecycle.
"""

import asyncio
import logging
import os
import time
import uuid
import wave
from dataclasses import replace
from pathlib import Path

import discord
from discord.ext import commands

try:
    from piper import PiperVoice, SynthesisConfig
except Exception:  # pragma: no cover - optional dependency
    PiperVoice = None
    SynthesisConfig = None

import voice_registry
from tts_providers import (
    LocalProvider,
    MiniMaxError,
    MiniMaxProvider,
    MiniMaxVoiceNotFoundError,
    TTSDispatcher,
    TTSPhraseCache,
    load_cache_config_from_env,
    load_circuit_breaker_from_env,
    load_dispatcher_config_from_env,
    load_minimax_config_from_env,
)
from ttsbot import config
from ttsbot.audio import (
    PCM_FRAME_BYTES,
    PCM_FRAME_MS,
    ContinuousTTSAudioSource,
    build_idle_pcm_frame,
    build_playback_prepare_command,
    build_tts_pcm_command,
    build_tts_stream_pcm_command,
    split_pcm_frames,
)
from ttsbot.messages import MergeBufferState, ParsedMessage, analyze_message_for_merge, is_reaction_like
from ttsbot.models import PreparedAudio, TTSJob, VOICE_PROFILES, VoiceProfile
from ttsbot.store import BotConfigStore
from ttsbot.textnorm import normalize_for_tts

log = logging.getLogger("tts_bot")

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
intents.guilds = True


class TTSBot(commands.Bot):
    def __init__(self) -> None:
        super().__init__(command_prefix=("!tts ", "!tts"), intents=intents)
        self.started_at = time.time()  # process start, for the stats uptime
        # The /voicebot command group; attached by the composition root
        # (bot.py) before the bot starts, added to the tree in setup_hook.
        self.tts_group: discord.app_commands.Group | None = None
        self.message_queue: asyncio.Queue[TTSJob] = asyncio.Queue(maxsize=config.QUEUE_MAXSIZE)
        self.worker_task: asyncio.Task[None] | None = None
        # Prefetch pipeline: generation worker fills ready_queue (bounded by
        # lookahead) with PreparedAudio; playback worker drains it in order.
        self.ready_queue: asyncio.Queue[PreparedAudio] = asyncio.Queue(
            maxsize=config.TTS_PREFETCH_LOOKAHEAD
        )
        self.generation_task: asyncio.Task[None] | None = None
        self.playback_task: asyncio.Task[None] | None = None
        # In-flight + buffered prepared items, so queue-clear can cancel them.
        self.active_prepared: set[PreparedAudio] = set()
        self.idle_disconnect_tasks: dict[int, asyncio.Task[None]] = {}
        self.continuous_idle_stop_tasks: dict[int, asyncio.Task[None]] = {}
        self.voice_connect_locks: dict[int, asyncio.Lock] = {}
        self.voice_connect_cooldown_until: dict[int, float] = {}
        self.suppress_auto_connect_until: dict[int, float] = {}
        # Unified voice catalog (Piper + MiniMax). Seeded on first start
        # from the hardcoded VOICE_PROFILES + MINIMAX_VOICE_ID; thereafter
        # loaded from data/voices.json. Selection state stays separate in
        # BotConfigStore.
        _mm_seed = load_minimax_config_from_env()
        self.voice_registry = voice_registry.load_or_seed(
            config.VOICES_REGISTRY_PATH,
            VOICE_PROFILES,
            fallback_profile=config.DEFAULT_VOICE_PROFILE,
            minimax_voice_id=_mm_seed.voice_id,
            minimax_model=_mm_seed.model,
            minimax_language_boost=_mm_seed.language_boost,
        )
        self.config_store = BotConfigStore(
            config.BOT_CONFIG_PATH, config.WHITELIST_USERS, voice_registry=self.voice_registry
        )
        self.piper_voices: dict[tuple[str, str], object] = {}
        # TTS provider abstraction (see tts_providers.py). Skeleton
        # behavior in this commit: dispatcher always routes to local.
        # Cloud provider (MiniMax) and full CB logic land in commits 3+
        # and 6 respectively.
        cache_cfg = load_cache_config_from_env()
        self.tts_cache = TTSPhraseCache(cache_cfg) if cache_cfg.enabled else None
        # Wrap generate_piper_file so the dispatcher's piper callback
        # signature (str | None profile name) matches what
        # generate_piper_file expects (VoiceProfile object).
        async def _piper_synthesize(text: str, filename: Path, voice_profile: str | None) -> None:
            await self.generate_piper_file(
                text, filename, self._resolve_piper_profile(voice_profile)
            )
        self.tts_dispatcher = TTSDispatcher(
            local=LocalProvider(_piper_synthesize),
            cloud=self._build_cloud_provider(),
            config=load_dispatcher_config_from_env(),
            circuit_breaker=load_circuit_breaker_from_env(),
            cache=self.tts_cache,
            fallback_profile=self.voice_registry.fallback_profile,
        )
        self.continuous_sources: dict[int, ContinuousTTSAudioSource] = {}
        self.merge_buffers: dict[tuple[int, int], MergeBufferState] = {}
        self.merge_locks: dict[tuple[int, int], asyncio.Lock] = {}
        self.merge_generations: dict[tuple[int, int], int] = {}
        self.last_user_message_ts: dict[tuple[int, int], float] = {}

    async def setup_hook(self) -> None:
        if self.tts_group is not None:
            self.tree.add_command(self.tts_group)
        try:
            synced = await self.tree.sync()
            log.info(
                "Slash commands synced count=%s group=/%s",
                len(synced),
                self.tts_group.name if self.tts_group is not None else "?",
            )
        except Exception:
            log.exception("Failed to sync slash commands")
        if config.TTS_PREFETCH_ENABLED:
            self.generation_task = asyncio.create_task(
                self._generation_worker(), name="tts-generation")
            self.playback_task = asyncio.create_task(
                self._playback_worker(), name="tts-playback")
        else:
            self.worker_task = asyncio.create_task(self.tts_worker(), name="tts-worker")

    async def close(self) -> None:
        for task in (self.worker_task, self.generation_task, self.playback_task):
            if task:
                task.cancel()

        for task in self.idle_disconnect_tasks.values():
            task.cancel()
        for task in self.continuous_idle_stop_tasks.values():
            task.cancel()
        for state in self.merge_buffers.values():
            if state.timer_task and not state.timer_task.done():
                state.timer_task.cancel()
        for source in self.continuous_sources.values():
            source.stop()

        # Gracefully close the cloud provider's HTTP keep-alive pool.
        # Local Piper has no async resources to release.
        cloud = getattr(self.tts_dispatcher, "cloud", None)
        if cloud is not None and hasattr(cloud, "aclose"):
            try:
                await cloud.aclose()
            except Exception:
                log.exception("Failed to close cloud TTS provider cleanly")

        await super().close()

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

    async def warmup_tts(self) -> None:
        filename = config.TMP_DIR / f"warmup_{uuid.uuid4().hex}.wav"
        try:
            # Warm Piper ONNX directly, bypassing the dispatcher. The
            # whole point of warmup is to preload the local model so the
            # first real request (including a fallback to local) is not
            # cold. If TTS_PRIMARY_PROVIDER=minimax, warming the cloud
            # provider is pointless (it is HTTP) and would burn an API
            # call on every restart.
            await self.tts_dispatcher.warm_local("Привет", filename)
            log.info("TTS warmup completed")
        except Exception:
            log.exception("TTS warmup failed")
        finally:
            if filename.exists():
                try:
                    filename.unlink()
                except OSError:
                    log.exception("Failed to remove warmup file: %s", filename)

    def _build_cloud_provider(self):
        """Construct the MiniMax provider if the bot has credentials.

        Returns ``None`` when MINIMAX_API_KEY or MINIMAX_VOICE_ID is
        missing — the dispatcher then silently uses the local provider
        regardless of TTS_PRIMARY_PROVIDER, and the bot starts cleanly.
        """
        cfg = load_minimax_config_from_env()
        # Only the API key is required now: the per-message voice_id comes
        # from the registry record, so the cloud provider is usable even
        # when MINIMAX_VOICE_ID is empty (as long as a minimax voice is in
        # the catalog). MINIMAX_VOICE_ID remains the first-start seed.
        if not cfg.api_key:
            log.info("MiniMax provider disabled (api_key MISSING); fall back to local")
            return None
        log.info(
            "MiniMax provider enabled model=%s default_voice_id=%s base_url=%s timeout=%.1fs",
            cfg.model, cfg.voice_id or "(per-record)", cfg.base_url, cfg.timeout_seconds,
        )
        return MiniMaxProvider(cfg)

    def _resolve_piper_profile(self, voice_profile: str | None) -> VoiceProfile:
        """Resolve a voice name to a Piper ``VoiceProfile`` via the registry.

        Falls back to the registry ``fallback_profile`` then ``VOICE_PROFILES``
        so a missing or non-Piper name still yields a usable Piper voice.
        """
        name = (
            voice_profile
            or self.voice_registry.fallback_profile
            or config.DEFAULT_VOICE_PROFILE
        )
        rec = self.voice_registry.get(name)
        if rec is not None and rec.is_piper and rec.piper is not None:
            return VoiceProfile(
                name=rec.name,
                label=rec.label,
                piper_model_path=rec.piper.model_path,
                piper_config_path=rec.piper.config_path,
                piper_speaker=rec.piper.speaker,
                piper_length_scale=rec.piper.length_scale,
            )
        return VOICE_PROFILES.get(name, VOICE_PROFILES[config.DEFAULT_VOICE_PROFILE])

    def persist_voice_registry(self) -> None:
        """Atomically write the current catalog to data/voices.json."""
        voice_registry.save_registry(config.VOICES_REGISTRY_PATH, self.voice_registry)

    async def validate_minimax_voice(self, voice_id: str) -> tuple[bool, str]:
        """Probe a MiniMax voice_id with a short phrase.

        Returns (ok, error_message). ok=True means status_code 0; a 2054
        (voice id not exist) yields a clear rejection. Used by voice-add
        before persisting a new voice.
        """
        cloud = getattr(self.tts_dispatcher, "cloud", None)
        if cloud is None:
            return False, "MiniMax не настроен (нет MINIMAX_API_KEY)."
        tmp = config.TMP_DIR / f"voiceadd_{uuid.uuid4().hex}.mp3"
        try:
            await cloud.synthesize("проверка голоса", tmp, voice_id=voice_id)
            return True, ""
        except MiniMaxVoiceNotFoundError:
            return False, f"voice_id `{voice_id}` не существует (MiniMax 2054)."
        except MiniMaxError as exc:
            return False, f"Ошибка MiniMax: {exc}"
        except Exception as exc:  # network/timeout/etc
            return False, f"Не удалось проверить голос: {type(exc).__name__}: {exc}"
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    async def clone_minimax_voice(
        self,
        *,
        name: str,
        voice_id: str,
        sample: bytes,
        filename: str,
        description: str,
    ) -> tuple[bool, str]:
        """Clone ``sample`` on MiniMax, then register + persist the voice.

        Wraps the provider's upload+clone, confirms the fresh clone actually
        synthesizes (a clone that 'succeeds' but won't speak is useless), then
        adds the record to the registry and saves data/voices.json. Returns
        (ok, error_message); on ok the voice ``name`` is ready for
        voice-set/voice-user. Used by /voicebot voice-clone.
        """
        cloud = getattr(self.tts_dispatcher, "cloud", None)
        if cloud is None:
            return False, "MiniMax не настроен (нет MINIMAX_API_KEY)."
        clone = getattr(cloud, "clone_voice", None)
        if clone is None:
            return False, "Провайдер не поддерживает клонирование."
        try:
            await clone(sample, voice_id=voice_id, filename=filename)
        except MiniMaxError as exc:
            return False, f"Ошибка клонирования: {exc}"
        except Exception as exc:  # network/timeout/decode/etc
            return False, f"Не удалось клонировать: {type(exc).__name__}: {exc}"
        # A clone can report success but not yet be usable; confirm it speaks
        # before we register it so we never persist a dead voice.
        ok, err = await self.validate_minimax_voice(voice_id)
        if not ok:
            return False, f"Клон создан, но не воспроизводится: {err}"
        record = voice_registry.VoiceRecord(
            name=name,
            label=f"{name} (клон)",
            description=description,
            provider=voice_registry.PROVIDER_MINIMAX,
            minimax=voice_registry.MiniMaxParams(voice_id=voice_id),
        )
        self.voice_registry.add(record)
        try:
            self.persist_voice_registry()
        except OSError as exc:
            log.exception("Failed to persist voices.json after voice-clone")
            return False, f"Голос создан, но не сохранён на диск: {exc}"
        return True, ""

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

    async def generate_piper_file(self, text: str, filename: Path, profile: VoiceProfile) -> None:
        if PiperVoice is None:
            raise RuntimeError("piper-tts is not installed")
        model_value = profile.piper_model_path or config.PIPER_MODEL_PATH
        config_value = profile.piper_config_path or config.PIPER_CONFIG_PATH
        if not model_value:
            raise RuntimeError("PIPER_MODEL_PATH is not set")
        model_path = Path(model_value)
        if not model_path.exists():
            raise RuntimeError(f"Piper model not found: {model_path}")
        config_path = Path(config_value) if config_value else None
        if config_path and not config_path.exists():
            raise RuntimeError(f"Piper config not found: {config_path}")

        voice_key = (str(model_path), str(config_path) if config_path else "")
        piper_voice = self.piper_voices.get(voice_key)
        if piper_voice is None:
            piper_voice = await asyncio.to_thread(
                PiperVoice.load,
                str(model_path),
                str(config_path) if config_path else None,
            )
            self.piper_voices[voice_key] = piper_voice

        started = time.perf_counter()
        log.info("Generating Piper TTS chars=%s model=%s profile=%s", len(text), model_path, profile.name)

        syn_config = None
        if SynthesisConfig is not None and (
            profile.piper_speaker >= 0 or abs(profile.piper_length_scale - 1.0) > 1e-6
        ):
            syn_config = SynthesisConfig(
                speaker_id=profile.piper_speaker if profile.piper_speaker >= 0 else None,
                length_scale=profile.piper_length_scale,
            )

        def _synthesize() -> None:
            with wave.open(str(filename), "wb") as wav_file:
                piper_voice.synthesize_wav(text, wav_file, syn_config=syn_config)

        await asyncio.to_thread(_synthesize)
        if not filename.exists() or filename.stat().st_size == 0:
            raise RuntimeError("Piper did not produce audio output")

        log.info(
            "Piper generated file=%s size=%s took=%.3fs",
            filename,
            filename.stat().st_size,
            time.perf_counter() - started,
        )

    async def generate_tts_file(self, text: str, filename: Path, voice_profile: str | None = None) -> str:
        # Resolve the stored voice name to a registry record; the dispatcher
        # routes by record.provider (piper -> local, minimax -> cloud) and
        # falls back to the registry fallback_profile on cloud failure.
        record = self.voice_registry.get(voice_profile)
        if record is None:
            record = self.voice_registry.fallback_record()
        provider_used = await self.tts_dispatcher.synthesize(text, filename, voice=record)
        log.info(
            "TTS engine used: %s voice=%s",
            provider_used,
            record.name if record is not None else (voice_profile or "default"),
        )
        return provider_used

    def _should_attempt_stream(self, voice) -> bool:
        """True when a job qualifies for the streaming fast path."""
        return (
            config.TTS_STREAMING_ENABLED
            and config.TTS_CONTINUOUS_STREAM
            and voice is not None
            and getattr(voice, "is_minimax", False)
            and self.tts_dispatcher.cloud is not None
        )

    async def _run_streaming_job(self, job: TTSJob, voice, worker_started: float) -> str:
        """Drive a streaming MiniMax job. Returns "done" or "fallback".

        "done": the job is fully handled (streamed ok, truncated mid-play, or
        a connection error that has nothing to fall back to). "fallback": the
        caller should run the Piper file path (pre-audio failure or open
        circuit breaker).
        """
        cb = self.tts_dispatcher.circuit_breaker
        # Connect first — enqueueing frames needs the voice client. Usually
        # instant because the continuous stream keeps the session open.
        try:
            vc = await self.ensure_voice(job.voice_channel)
        except Exception:
            log.exception("Voice prepare failed (streaming)")
            existing = discord.utils.get(self.voice_clients, guild=job.voice_channel.guild)
            if existing and existing.is_connected():
                await self.disconnect_guild_voice(job.voice_channel.guild)
            return "done"

        # Cache hit: play the stored audio from disk, no API call, no breaker
        # probe consumed. (Cache key already includes the voice.)
        cache = self.tts_dispatcher.cache
        if cache is not None:
            cached = cache.lookup(job.text, voice.name)
            if cached is not None:
                source = self.ensure_continuous_player(vc)
                try:
                    frames = await self.prepare_tts_pcm_frames(cached)
                except Exception:
                    log.exception("Cached audio decode failed; streaming instead")
                    frames = []
                if frames:
                    source.enqueue_frames(frames)
                    log.info(
                        "Stream cache HIT guild=%s channel=%s frames=%d "
                        "message_to_audio_s=%.3f (no API)",
                        job.voice_channel.guild.id, job.voice_channel.id, len(frames),
                        time.perf_counter() - job.message_ts,
                    )
                    await source.wait_until_drained()
                    self.schedule_continuous_idle_stop(job.voice_channel.guild)
                    self.schedule_idle_disconnect(job.voice_channel.guild)
                    return "done"

        # Consume the breaker probe only now that we are about to call cloud.
        if not cb.allow_request():
            log.debug(
                "Circuit breaker open; skipping stream guild=%s", job.voice_channel.guild.id
            )
            return "fallback"

        log.info(
            "Ready to stream guild=%s channel=%s queue_wait=%.3fs prep_total=%.3fs",
            job.voice_channel.guild.id, job.voice_channel.id,
            worker_started - job.queued_at, time.perf_counter() - worker_started,
        )
        source = self.ensure_continuous_player(vc)
        try:
            status, frames = await self._stream_tts_to_source(source, voice, job)
        except Exception:
            log.exception("Streaming playback crashed; falling back to Piper")
            cb.record_failure()
            return "fallback"

        if status == "pre_audio":
            cb.record_failure()
            return "fallback"
        # Audio started: ok (full) or truncated (mid-stream failure). Either
        # way we do NOT overlay Piper on top of already-playing audio.
        cb.record_success() if status == "ok" else cb.record_failure()
        await source.wait_until_drained()
        log.info(
            "Playback finished (stream) guild=%s channel=%s frames=%d total_since_queue=%.3fs",
            job.voice_channel.guild.id, job.voice_channel.id, frames,
            time.perf_counter() - job.queued_at,
        )
        self.schedule_continuous_idle_stop(job.voice_channel.guild)
        self.schedule_idle_disconnect(job.voice_channel.guild)
        return "done"

    async def _stream_tts_to_source(
        self, source: "ContinuousTTSAudioSource", voice, job: TTSJob
    ) -> tuple[str, int]:
        """Stream a MiniMax voice into the continuous player frame-by-frame.

        Returns (status, frames_enqueued) where status is "ok", "truncated"
        (audio started then the stream failed mid-way) or "pre_audio" (failed
        before any audio — caller falls back to Piper). Circuit-breaker
        accounting is the caller's job.
        """
        cloud = self.tts_dispatcher.cloud
        mm = voice.minimax
        agen = cloud.stream_audio(
            job.text,
            voice_id=mm.voice_id, model=mm.model, speed=mm.speed,
            vol=mm.vol, pitch=mm.pitch, emotion=mm.emotion,
            language_boost=mm.language_boost,
        )
        # 1. First chunk under the TTFA budget; any failure here => clean
        #    fallback to Piper (no audio has played yet).
        try:
            first_chunk = await asyncio.wait_for(
                agen.__anext__(), timeout=config.TTS_STREAM_TTFA_TIMEOUT
            )
        except StopAsyncIteration:
            await agen.aclose()
            log.warning("Stream produced no audio; falling back to Piper")
            return ("pre_audio", 0)
        except asyncio.TimeoutError:
            await agen.aclose()
            log.warning(
                "Stream TTFA exceeded %.2fs; falling back to Piper", config.TTS_STREAM_TTFA_TIMEOUT
            )
            return ("pre_audio", 0)
        except Exception as exc:
            await agen.aclose()
            log.warning(
                "Stream failed before first audio (%s: %s); Piper fallback",
                type(exc).__name__, exc,
            )
            return ("pre_audio", 0)

        # 2. We have audio. Decode the MP3 byte stream via ffmpeg (stdin ->
        #    s16le stdout) while feeding chunks concurrently.
        proc = await asyncio.create_subprocess_exec(
            *build_tts_stream_pcm_command(),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        mid_error: list[BaseException] = []

        # Tee the MP3 byte stream to a cache ".part" file so a future repeat
        # of this (voice, text) plays from disk without hitting the API. Only
        # committed on a clean finish — partial streams are never cached.
        cache = self.tts_dispatcher.cache
        cache_final: Path | None = None
        cache_part = None
        if cache is not None:
            try:
                cache_final = cache.cache_path_for(job.text, voice.name)
                cache_final.parent.mkdir(parents=True, exist_ok=True)
                cache_part = open(str(cache_final) + ".part", "wb")
            except OSError:
                cache_final = None
                cache_part = None

        async def _feed() -> None:
            try:
                proc.stdin.write(first_chunk)
                await proc.stdin.drain()
                if cache_part is not None:
                    cache_part.write(first_chunk)
                async for chunk in agen:
                    proc.stdin.write(chunk)
                    await proc.stdin.drain()
                    if cache_part is not None:
                        cache_part.write(chunk)
            except Exception as exc:  # mid-stream API/network failure
                mid_error.append(exc)
            finally:
                try:
                    proc.stdin.close()
                except Exception:
                    pass
                if cache_part is not None:
                    try:
                        cache_part.close()
                    except Exception:
                        pass

        feeder = asyncio.create_task(_feed())

        frames_enqueued = 0
        first_frame_ts: float | None = None
        leftover = b""
        try:
            while True:
                data = await proc.stdout.read(PCM_FRAME_BYTES * 16)
                if not data:
                    break
                buf = leftover + data
                n = len(buf) - (len(buf) % PCM_FRAME_BYTES)
                if n:
                    out_frames = [buf[i:i + PCM_FRAME_BYTES] for i in range(0, n, PCM_FRAME_BYTES)]
                    source.enqueue_frames(out_frames)
                    if first_frame_ts is None:
                        first_frame_ts = time.perf_counter()
                        log.info(
                            "Stream first audio guild=%s channel=%s "
                            "message_to_first_audio_s=%.3f queue_to_first_audio_s=%.3f",
                            job.voice_channel.guild.id, job.voice_channel.id,
                            first_frame_ts - job.message_ts,
                            first_frame_ts - job.queued_at,
                        )
                    frames_enqueued += len(out_frames)
                leftover = buf[n:]
        finally:
            await feeder
            if leftover:
                source.enqueue_frames(
                    [leftover + b"\x00" * (PCM_FRAME_BYTES - len(leftover))]
                )
                frames_enqueued += 1
            try:
                await proc.wait()
            except Exception:
                pass

        part_path = (str(cache_final) + ".part") if cache_final is not None else None

        def _discard_cache() -> None:
            if part_path:
                try:
                    os.unlink(part_path)
                except OSError:
                    pass

        if mid_error:
            _discard_cache()  # never cache a partial stream
            log.warning(
                "Stream failed mid-playback after %d frames (%s); truncated",
                frames_enqueued, mid_error[0],
            )
            return ("truncated", frames_enqueued)
        if frames_enqueued == 0:
            _discard_cache()
            return ("pre_audio", 0)
        # Clean finish: finalize the cache file so repeats skip the API.
        if cache is not None and cache_final is not None and part_path:
            try:
                os.replace(part_path, cache_final)
                cache.commit_file(job.text, cache_final, voice.name)
            except OSError:
                _discard_cache()
        return ("ok", frames_enqueued)

    # ------------------------------------------------------------------
    # Prefetch pipeline (TTS_PREFETCH_ENABLED): generation_worker produces
    # PreparedAudio ahead of playback_worker, which plays strictly FIFO.
    # ------------------------------------------------------------------

    async def _generation_worker(self) -> None:
        await self.wait_until_ready()
        await self.warmup_tts()
        log.info("TTS generation worker started (lookahead=%d)", config.TTS_PREFETCH_LOOKAHEAD)
        while not self.is_closed():
            job = await self.message_queue.get()
            prepared = PreparedAudio(job=job, channel=asyncio.Queue())
            self.active_prepared.add(prepared)
            try:
                # Backpressure: blocks here when we are already `lookahead`
                # messages ahead of playback.
                await self.ready_queue.put(prepared)
                await self._prepare_into(prepared)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("TTS generation pipeline error")
                await prepared.channel.put(None)
            finally:
                self.message_queue.task_done()

    async def _prepare_into(self, prepared: PreparedAudio) -> None:
        """Generate a job's audio into ``prepared.channel`` (frame batches)."""
        job = prepared.job
        voice = self.voice_registry.get(job.voice_profile) or self.voice_registry.fallback_record()
        try:
            if prepared.cancelled:
                return
            if self._should_attempt_stream(voice):
                status = await self._generate_stream_into(prepared, voice)
                if status != "pre_audio":
                    return  # ok / truncated / cache / cancelled
                voice = self.voice_registry.fallback_record()  # pre-audio -> Piper
            await self._generate_file_into(prepared, voice)
        finally:
            # Always terminate the channel so the consumer never hangs.
            await prepared.channel.put(None)

    async def _safe_pcm_frames(self, path: Path) -> list[bytes]:
        try:
            return await self.prepare_tts_pcm_frames(path)
        except Exception:
            log.exception("PCM decode failed for %s", path)
            return []

    async def _generate_stream_into(self, prepared: PreparedAudio, voice) -> str:
        job = prepared.job
        cb = self.tts_dispatcher.circuit_breaker
        cache = self.tts_dispatcher.cache
        if cache is not None:
            cached = cache.lookup(job.text, voice.name)
            if cached is not None:
                frames = await self._safe_pcm_frames(cached)
                if frames and not prepared.cancelled:
                    await prepared.channel.put(frames)
                    prepared.provider = "cache"
                    log.info(
                        "Cache HIT (prefetch) guild=%s voice=%s frames=%d (no API)",
                        job.guild_id, voice.name, len(frames),
                    )
                    return "cache"
        if prepared.cancelled:
            return "cancelled"
        if not cb.allow_request():
            return "pre_audio"  # breaker open -> caller does Piper fallback
        # Set the label before streaming so the playback worker logs the
        # provider on the first frame (a pre-audio fallback overwrites it
        # with the Piper provider in _generate_file_into).
        prepared.provider = "minimax"
        status, _ = await self._stream_to_channel(prepared, voice)
        if status == "pre_audio":
            cb.record_failure()
            return "pre_audio"
        if status == "cancelled":
            return "cancelled"
        cb.record_success() if status == "ok" else cb.record_failure()
        return status

    async def _generate_file_into(self, prepared: PreparedAudio, voice) -> None:
        if prepared.cancelled:
            return
        job = prepared.job
        filename = config.TMP_DIR / f"tts_{uuid.uuid4().hex}.wav"
        try:
            prepared.provider = await self.tts_dispatcher.synthesize(
                job.text, filename, voice=voice
            )
            if prepared.cancelled:
                return
            frames = await self._safe_pcm_frames(filename)
            if frames:
                await prepared.channel.put(frames)
        finally:
            if filename.exists():
                try:
                    filename.unlink()
                except OSError:
                    log.exception("Failed to remove temp file: %s", filename)

    async def _stream_to_channel(self, prepared: PreparedAudio, voice) -> tuple[str, int]:
        """Stream MiniMax -> ffmpeg -> frame batches into ``prepared.channel``.

        Like _stream_tts_to_source but writes to the prefetch channel (not the
        live player) and honors cancellation. Returns (status, frames) with
        status in ok/truncated/pre_audio/cancelled.
        """
        job = prepared.job
        cloud = self.tts_dispatcher.cloud
        mm = voice.minimax
        agen = cloud.stream_audio(
            job.text, voice_id=mm.voice_id, model=mm.model, speed=mm.speed,
            vol=mm.vol, pitch=mm.pitch, emotion=mm.emotion, language_boost=mm.language_boost,
        )
        try:
            first_chunk = await asyncio.wait_for(
                agen.__anext__(), timeout=config.TTS_STREAM_TTFA_TIMEOUT
            )
        except StopAsyncIteration:
            await agen.aclose()
            log.warning("Stream produced no audio; Piper fallback")
            return ("pre_audio", 0)
        except asyncio.TimeoutError:
            await agen.aclose()
            log.warning("Stream TTFA exceeded %.2fs; Piper fallback", config.TTS_STREAM_TTFA_TIMEOUT)
            return ("pre_audio", 0)
        except Exception as exc:
            await agen.aclose()
            log.warning("Stream failed before first audio (%s: %s); Piper fallback",
                        type(exc).__name__, exc)
            return ("pre_audio", 0)
        if prepared.cancelled:
            await agen.aclose()
            return ("cancelled", 0)

        proc = await asyncio.create_subprocess_exec(
            *build_tts_stream_pcm_command(),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        mid_error: list[BaseException] = []
        cache = self.tts_dispatcher.cache
        cache_final: Path | None = None
        cache_part = None
        if cache is not None:
            try:
                cache_final = cache.cache_path_for(job.text, voice.name)
                cache_final.parent.mkdir(parents=True, exist_ok=True)
                cache_part = open(str(cache_final) + ".part", "wb")
            except OSError:
                cache_final = None
                cache_part = None

        async def _feed() -> None:
            try:
                proc.stdin.write(first_chunk)
                await proc.stdin.drain()
                if cache_part is not None:
                    cache_part.write(first_chunk)
                async for chunk in agen:
                    if prepared.cancelled:
                        break
                    proc.stdin.write(chunk)
                    await proc.stdin.drain()
                    if cache_part is not None:
                        cache_part.write(chunk)
            except Exception as exc:
                mid_error.append(exc)
            finally:
                try:
                    proc.stdin.close()
                except Exception:
                    pass
                if cache_part is not None:
                    try:
                        cache_part.close()
                    except Exception:
                        pass

        feeder = asyncio.create_task(_feed())
        frames_count = 0
        leftover = b""
        try:
            while True:
                data = await proc.stdout.read(PCM_FRAME_BYTES * 16)
                if not data:
                    break
                buf = leftover + data
                n = len(buf) - (len(buf) % PCM_FRAME_BYTES)
                if n and not prepared.cancelled:
                    out_frames = [buf[i:i + PCM_FRAME_BYTES] for i in range(0, n, PCM_FRAME_BYTES)]
                    await prepared.channel.put(out_frames)
                    frames_count += len(out_frames)
                leftover = buf[n:]
        finally:
            await feeder
            if leftover and not prepared.cancelled:
                await prepared.channel.put(
                    [leftover + b"\x00" * (PCM_FRAME_BYTES - len(leftover))]
                )
                frames_count += 1
            try:
                await proc.wait()
            except Exception:
                pass

        part_path = (str(cache_final) + ".part") if cache_final is not None else None

        def _discard() -> None:
            if part_path:
                try:
                    os.unlink(part_path)
                except OSError:
                    pass

        if prepared.cancelled:
            _discard()
            return ("cancelled", frames_count)
        if mid_error:
            _discard()
            log.warning("Stream failed mid-stream after %d frames (%s); truncated",
                        frames_count, mid_error[0])
            return ("truncated", frames_count)
        if frames_count == 0:
            _discard()
            return ("pre_audio", 0)
        if cache is not None and cache_final is not None and part_path:
            try:
                os.replace(part_path, cache_final)
                cache.commit_file(job.text, cache_final, voice.name)
            except OSError:
                _discard()
        return ("ok", frames_count)

    async def _playback_worker(self) -> None:
        await self.wait_until_ready()
        log.info("TTS playback worker started")
        while not self.is_closed():
            prepared = await self.ready_queue.get()
            try:
                await self._play_prepared(prepared)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("TTS playback failed; disconnecting voice")
                await self.disconnect_guild_voice(prepared.job.voice_channel.guild)
            finally:
                self.active_prepared.discard(prepared)
                self.ready_queue.task_done()

    async def _drain_channel(self, prepared: PreparedAudio) -> None:
        """Consume a prepared channel to its sentinel without playing."""
        while True:
            batch = await prepared.channel.get()
            if batch is None:
                return

    async def _play_prepared(self, prepared: PreparedAudio) -> None:
        job = prepared.job
        pickup_ts = time.perf_counter()
        if prepared.cancelled:
            await self._drain_channel(prepared)
            log.info("Skipped cancelled job guild=%s channel=%s",
                     job.voice_channel.guild.id, job.voice_channel.id)
            return
        try:
            vc = await self.ensure_voice(job.voice_channel)
        except Exception:
            log.exception("Voice prepare failed (playback)")
            existing = discord.utils.get(self.voice_clients, guild=job.voice_channel.guild)
            if existing and existing.is_connected():
                await self.disconnect_guild_voice(job.voice_channel.guild)
            await self._drain_channel(prepared)
            return

        source = self.ensure_continuous_player(vc)
        first_ts: float | None = None
        total = 0
        while True:
            batch = await prepared.channel.get()
            if batch is None:
                break
            if prepared.cancelled:
                continue  # stop feeding but drain to the sentinel
            source.enqueue_frames(batch)
            total += len(batch)
            if first_ts is None:
                first_ts = time.perf_counter()
                log.info(
                    "Audio start guild=%s channel=%s provider=%s queue_wait=%.3fs "
                    "message_to_audio_s=%.3f queue_to_audio_s=%.3f",
                    job.voice_channel.guild.id, job.voice_channel.id,
                    prepared.provider or "?", pickup_ts - job.queued_at,
                    first_ts - job.message_ts, first_ts - job.queued_at,
                )
        if total == 0:
            return
        await source.wait_until_drained()
        log.info(
            "Playback finished guild=%s channel=%s frames=%d total_since_queue=%.3fs",
            job.voice_channel.guild.id, job.voice_channel.id, total,
            time.perf_counter() - job.queued_at,
        )
        self.schedule_continuous_idle_stop(job.voice_channel.guild)
        self.schedule_idle_disconnect(job.voice_channel.guild)

    async def tts_worker(self) -> None:
        await self.wait_until_ready()
        await self.warmup_tts()
        log.info("TTS worker started")

        while not self.is_closed():
            job = await self.message_queue.get()
            filename = config.TMP_DIR / f"tts_{uuid.uuid4().hex}.wav"

            try:
                worker_started = time.perf_counter()
                voice = (
                    self.voice_registry.get(job.voice_profile)
                    or self.voice_registry.fallback_record()
                )

                # Streaming fast path: a MiniMax voice over the continuous
                # stream plays chunks as they arrive (lower Time-To-First-
                # Audio). On a pre-audio failure or an open breaker it returns
                # "fallback" and we drop to the Piper file path below.
                file_voice_name = job.voice_profile
                if self._should_attempt_stream(voice):
                    outcome = await self._run_streaming_job(job, voice, worker_started)
                    if outcome == "done":
                        continue
                    # Pre-audio fallback: use Piper directly, never re-hit cloud.
                    file_voice_name = self.voice_registry.fallback_profile

                connect_task = asyncio.create_task(self.ensure_voice(job.voice_channel))
                tts_task = asyncio.create_task(self.generate_tts_file(job.text, filename, file_voice_name))

                try:
                    vc, _ = await asyncio.gather(connect_task, tts_task)
                except Exception:
                    connect_error = connect_task.exception() if connect_task.done() else None
                    tts_error = tts_task.exception() if tts_task.done() else None
                    if connect_error:
                        log.exception("Voice prepare failed", exc_info=connect_error)
                        if not tts_task.done():
                            tts_task.cancel()
                        vc = discord.utils.get(self.voice_clients, guild=job.voice_channel.guild)
                        if vc and vc.is_connected():
                            await self.disconnect_guild_voice(job.voice_channel.guild)
                        else:
                            log.info(
                                "Skip disconnect cleanup guild=%s reason=voice_not_connected",
                                job.voice_channel.guild.id,
                            )
                    elif tts_error:
                        log.exception("TTS generation failed; keeping voice session", exc_info=tts_error)
                    else:
                        log.exception("TTS processing failed before playback")
                    continue

                log.info(
                    "Ready to play guild=%s channel=%s queue_wait=%.3fs prep_total=%.3fs",
                    job.voice_channel.guild.id,
                    job.voice_channel.id,
                    worker_started - job.queued_at,
                    time.perf_counter() - worker_started,
                )

                try:
                    if config.TTS_CONTINUOUS_STREAM:
                        source = self.ensure_continuous_player(vc)
                        frames = await self.prepare_tts_pcm_frames(filename)
                        audio_enqueue_ts = time.perf_counter()
                        source.enqueue_frames(frames)
                        log.info(
                            "Queued continuous playback guild=%s channel=%s frames=%s duration=%.3fs message_to_audio_enqueue_s=%.3f queue_to_audio_enqueue_s=%.3f",
                            job.voice_channel.guild.id,
                            job.voice_channel.id,
                            len(frames),
                            len(frames) * PCM_FRAME_MS / 1000,
                            audio_enqueue_ts - job.message_ts,
                            audio_enqueue_ts - job.queued_at,
                        )
                        await source.wait_until_drained()
                    else:
                        playback_request_ts = time.perf_counter()
                        log.info(
                            "Starting non-continuous playback metrics message_to_playback_request_s=%.3f queue_to_playback_request_s=%.3f",
                            playback_request_ts - job.message_ts,
                            playback_request_ts - job.queued_at,
                        )
                        await self.play_file(vc, filename)
                except Exception:
                    log.exception("Playback failed; disconnecting voice")
                    await self.disconnect_guild_voice(job.voice_channel.guild)
                    continue

                log.info(
                    "Playback finished guild=%s channel=%s total_since_queue=%.3fs",
                    job.voice_channel.guild.id,
                    job.voice_channel.id,
                    time.perf_counter() - job.queued_at,
                )

                self.schedule_continuous_idle_stop(job.voice_channel.guild)
                self.schedule_idle_disconnect(job.voice_channel.guild)

            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("TTS processing failed")
                await self.disconnect_guild_voice(job.voice_channel.guild)
            finally:
                if filename.exists():
                    try:
                        filename.unlink()
                    except OSError:
                        log.exception("Failed to remove temp file: %s", filename)
                self.message_queue.task_done()

    def ensure_continuous_player(self, vc: discord.VoiceClient) -> ContinuousTTSAudioSource:
        guild_id = vc.guild.id
        self.cancel_continuous_idle_stop(guild_id)
        source = self.continuous_sources.get(guild_id)
        source_created = False
        if source is None or source.stopped:
            source = ContinuousTTSAudioSource(build_idle_pcm_frame(config.TTS_IDLE_FRAME_MODE, config.TTS_IDLE_VOLUME_DB))
            self.continuous_sources[guild_id] = source
            source_created = True

        if source_created and (vc.is_playing() or vc.is_paused()):
            log.warning("Stopping previous voice source before continuous stream guild=%s", guild_id)
            vc.stop()

        if source_created or (not vc.is_playing() and not vc.is_paused()):
            vc.play(source)
            log.info(
                "Continuous TTS stream started guild=%s mode=%s idle_volume_db=%s max_idle_seconds=%s",
                guild_id,
                config.TTS_IDLE_FRAME_MODE,
                config.TTS_IDLE_VOLUME_DB,
                config.TTS_MAX_CONTINUOUS_IDLE_SECONDS,
            )
        return source

    async def prepare_tts_pcm_frames(self, source: Path) -> list[bytes]:
        started = time.perf_counter()
        cmd = build_tts_pcm_command(source)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            stderr_text = stderr.decode("utf-8", errors="ignore").strip()
            raise RuntimeError(f"ffmpeg PCM preparation failed rc={proc.returncode} stderr={stderr_text}")

        frames = split_pcm_frames(stdout, config.TTS_STREAM_TAIL_MS)
        if not frames:
            raise RuntimeError("ffmpeg PCM preparation produced no audio frames")

        log.info(
            "PCM preparation took=%.3fs source_size=%s pcm_bytes=%s frames=%s tail_ms=%s",
            time.perf_counter() - started,
            source.stat().st_size if source.exists() else "unknown",
            len(stdout),
            len(frames),
            config.TTS_STREAM_TAIL_MS,
        )
        return frames

    async def prepare_playback_file(self, source: Path, prepared: Path) -> Path:
        started = time.perf_counter()
        cmd = build_playback_prepare_command(source, prepared)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            stdout_text = stdout.decode("utf-8", errors="ignore").strip()
            stderr_text = stderr.decode("utf-8", errors="ignore").strip()
            raise RuntimeError(
                f"ffmpeg playback preparation failed rc={proc.returncode} "
                f"stdout={stdout_text} stderr={stderr_text}"
            )
        if not prepared.exists() or prepared.stat().st_size == 0:
            raise RuntimeError("ffmpeg playback preparation produced empty output")

        log.info(
            "Playback preparation took=%.3fs source_size=%s prepared_size=%s "
            "preroll_ms=%s preroll_mode=%s preroll_volume_db=%s tail_ms=%s",
            time.perf_counter() - started,
            source.stat().st_size if source.exists() else "unknown",
            prepared.stat().st_size,
            config.TTS_PREROLL_MS,
            config.TTS_PREROLL_MODE,
            config.TTS_PREROLL_VOLUME_DB,
            config.TTS_SILENCE_TAIL_MS,
        )
        return prepared

    async def play_file(self, vc: discord.VoiceClient, filename: Path) -> None:
        if vc.is_playing() or vc.is_paused():
            log.warning("Voice client was already playing; stopping previous source")
            vc.stop()

        finished = asyncio.Event()
        loop = asyncio.get_running_loop()
        started = time.perf_counter()

        def after(error: Exception | None) -> None:
            if error:
                log.exception("Playback callback error", exc_info=error)
            loop.call_soon_threadsafe(finished.set)

        before_options = "-hide_banner -loglevel warning"
        if config.FFMPEG_LOW_DELAY:
            before_options = (
                f"{before_options} "
                "-fflags nobuffer -flags low_delay -probesize 32 -analyzeduration 0"
            )

        prepared = config.TMP_DIR / f"playback_{uuid.uuid4().hex}.wav"
        playback_file = filename
        try:
            try:
                playback_file = await self.prepare_playback_file(filename, prepared)
            except Exception:
                log.exception("Playback preparation failed; using source file")

            audio = discord.FFmpegPCMAudio(
                str(playback_file),
                before_options=before_options,
                options="-vn",
            )

            log.info("Starting playback file=%s size=%s", playback_file, playback_file.stat().st_size)
            vc.play(audio, after=after)
            await finished.wait()

            log.info("Playback duration took=%.3fs", time.perf_counter() - started)
        finally:
            if prepared.exists():
                try:
                    prepared.unlink()
                except OSError:
                    log.exception("Failed to remove prepared playback file: %s", prepared)

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
