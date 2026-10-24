"""The TTSBot Discord client: composition of the runtime concerns.

The behavior lives in four concern mixins; this module owns construction
(queues, dispatcher, registry, config store), the task lifecycle
(setup_hook/close) and nothing else. Slash commands and event handlers
are attached from the outside by the composition root (bot.py).
"""

import asyncio
import logging
import time
import zoneinfo
from functools import partial
from dataclasses import replace
from pathlib import Path

import discord
from discord.ext import commands

from ttsbot import voice_registry
from ttsbot import plugins
from ttsbot.fish import FishConfig
from ttsbot.health import HealthStore
from ttsbot.providers import (
    LocalProvider,
    TTSDispatcher,
    TTSPhraseCache,
    load_cache_config_from_env,
    load_dispatcher_config_from_env,
)
from ttsbot.minimax import load_minimax_config_from_env
from ttsbot import config
from ttsbot.audio import ContinuousTTSAudioSource
from ttsbot.intake import MessageQueueMixin
from ttsbot.models import VOICE_PROFILES
from ttsbot.pipeline import SynthesisPipelineMixin
from ttsbot.playback import PlaybackMixin
from ttsbot.recording.groq import AudioBudget, GroqTranscriber
from ttsbot.recording.worker import TranscriptionWorker
from ttsbot.store import BotConfigStore
from ttsbot.session import GuildSession
from ttsbot.supervisor import TaskSupervisor
from ttsbot.voice_lifecycle import VoiceLifecycleMixin

log = logging.getLogger("tts_bot")

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
intents.guilds = True


class TTSBot(
    VoiceLifecycleMixin,
    MessageQueueMixin,
    SynthesisPipelineMixin,
    PlaybackMixin,
    commands.Bot,
):
    def __init__(self) -> None:
        super().__init__(command_prefix=("!tts ", "!tts"), intents=intents)
        self.started_at = time.time()  # process start, for the stats uptime
        # The /voicebot command group; attached by the composition root
        # (bot.py) before the bot starts, added to the tree in setup_hook.
        self.tts_group: discord.app_commands.Group | None = None
        # More top-level groups (/fallback), attached the same way.
        self.extra_groups: list[discord.app_commands.Group] = []
        # Every long-running worker runs under the supervisor: a crash is
        # logged and the worker restarted, and /voicebot status shows it.
        self.supervisor = TaskSupervisor()
        # One session per guild (its queue, prefetch and player loop),
        # created with its workers on the guild's first message.
        self.sessions: dict[int, GuildSession] = {}
        self.idle_disconnect_tasks: dict[int, asyncio.Task[None]] = {}
        self.continuous_idle_stop_tasks: dict[int, asyncio.Task[None]] = {}
        self.voice_connect_locks: dict[int, asyncio.Lock] = {}
        self.voice_connect_cooldown_until: dict[int, float] = {}
        self.suppress_auto_connect_until: dict[int, float] = {}
        # Unified voice catalog: data/voices.json, seeded on first start
        # from the Piper profiles (and MINIMAX_VOICE_ID); then each plugin
        # points its default voice at its env setting (fish-default, ...).
        # Selection state stays separate in BotConfigStore.
        _mm_seed = load_minimax_config_from_env()
        self.voice_registry = voice_registry.load_or_seed(
            config.VOICES_REGISTRY_PATH,
            VOICE_PROFILES,
            fallback_profile=config.DEFAULT_VOICE_PROFILE,
            minimax_voice_id=_mm_seed.voice_id,
            minimax_model=_mm_seed.model,
            minimax_language_boost=_mm_seed.language_boost,
        )
        if plugins.seed_voices(self.voice_registry):
            try:
                voice_registry.save_registry(config.VOICES_REGISTRY_PATH, self.voice_registry)
            except OSError:
                log.exception("Could not save the seeded voices to %s; keeping them in memory",
                              config.VOICES_REGISTRY_PATH)
        self.config_store = BotConfigStore(
            config.BOT_CONFIG_PATH, config.WHITELIST_USERS, voice_registry=self.voice_registry
        )
        self.piper_voices: dict[tuple[str, str], object] = {}
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
            providers=plugins.create_providers(self.config_store),
            config=load_dispatcher_config_from_env(),
            cache=self.tts_cache,
        )
        # Provider pauses survive restarts: a provider out of quota stays
        # paused until its check, instead of failing the first messages.
        if config.PROVIDER_HEALTH_PATH.parent.exists():
            self.tts_dispatcher.attach_health_store(HealthStore(config.PROVIDER_HEALTH_PATH))
        self.continuous_sources: dict[int, ContinuousTTSAudioSource] = {}
        # The catalog as saved; persist_voice_registry() rolls back to it.
        self._saved_registry = self.voice_registry.snapshot()

    @property
    def fish_latency(self) -> str:
        fish = self.tts_dispatcher.provider_for("fish")
        if fish is not None:
            return fish.config.latency
        saved = self.config_store.settings.get("fish_latency")
        return saved if isinstance(saved, str) else FishConfig(api_key="").latency

    def set_fish_latency(self, mode: str) -> None:
        """Persist the global Fish mode and apply it to future requests."""
        self.config_store.set_fish_latency(mode)
        fish = self.tts_dispatcher.provider_for("fish")
        if fish is not None:
            fish.config = replace(fish.config, latency=mode)

    async def setup_hook(self) -> None:
        if self.tts_group is not None:
            self.tree.add_command(self.tts_group)
        for group in self.extra_groups:
            self.tree.add_command(group)
        try:
            synced = await self.tree.sync()
            log.info(
                "Slash commands synced count=%s group=/%s",
                len(synced),
                self.tts_group.name if self.tts_group is not None else "?",
            )
        except Exception:
            log.exception("Failed to sync slash commands")
        sup = self.supervisor
        sup.start("tts-warmup", self._warmup_when_ready)
        if config.TTS_CONNECTION_KEEPALIVE_SECONDS > 0:
            sup.start("tts-connection-keepalive", self._connection_keepalive_worker)
        if self.tts_dispatcher.provider_names():
            sup.start("tts-provider-health", self._provider_health_worker)
        if config.RECORDING_ENABLED and config.STT_ENABLED:
            sup.start("stt-worker", self._make_transcription_worker().run)

    def session_for(self, guild_id: int) -> GuildSession:
        """The guild's session, created (with its workers) on first use."""
        session = self.sessions.get(guild_id)
        if session is None:
            session = self.sessions[guild_id] = GuildSession.create(guild_id)
            self.supervisor.start(f"tts-generation:{guild_id}", partial(self._generation_worker, session))
            self.supervisor.start(f"tts-playback:{guild_id}", partial(self._playback_worker, session))
        return session

    def queue_size(self, guild_id: int | None = None) -> int:
        """Messages waiting to be generated, in one guild or in all of them."""
        sessions = [self.sessions.get(guild_id)] if guild_id is not None else list(self.sessions.values())
        return sum(session.queue.qsize() for session in sessions if session is not None)

    async def _warmup_when_ready(self) -> None:
        await self.wait_until_ready()
        await self.warmup_tts()

    def _make_transcription_worker(self) -> TranscriptionWorker:
        config.RECORDING_DIR.mkdir(parents=True, exist_ok=True)
        self.transcriber = GroqTranscriber(
            config.GROQ_API_KEY, model=config.STT_MODEL, language=config.STT_LANGUAGE,
            prompt=config.STT_PROMPT,
        )
        budget = AudioBudget(
            config.RECORDING_DIR / "groq_ledger.jsonl",
            per_hour=config.STT_AUDIO_SECONDS_PER_HOUR, per_day=config.STT_AUDIO_SECONDS_PER_DAY,
            min_billed=config.STT_MIN_BILLED_SECONDS, requests_per_minute=config.STT_REQUESTS_PER_MINUTE,
        )
        try:
            tz = zoneinfo.ZoneInfo(config.TRANSCRIPT_TIMEZONE)
        except (zoneinfo.ZoneInfoNotFoundError, ValueError):
            log.warning("Unknown TRANSCRIPT_TIMEZONE=%s; using UTC", config.TRANSCRIPT_TIMEZONE)
            tz = zoneinfo.ZoneInfo("UTC")
        return TranscriptionWorker(config.RECORDING_DIR, self.transcriber, budget,
                                   name_for=self.member_display_name, tz=tz, ready=self.wait_until_ready,
                                   keep_audio_days=config.RECORDING_AUDIO_KEEP_DAYS,
                                   keep_transcript_days=config.RECORDING_TRANSCRIPT_KEEP_DAYS,
                                   prune_seconds=config.RECORDING_PRUNE_INTERVAL_HOURS * 3600)

    async def member_display_name(self, guild_id: int, user_id: int) -> str | None:
        """Server nickname or global name, from the cache or Discord's API
        (the bot has no members intent, so people who left voice are not
        cached)."""
        guild = self.get_guild(guild_id)
        member = guild.get_member(user_id) if guild else None
        if member is not None:
            return member.display_name
        try:
            if guild is not None:
                return (await guild.fetch_member(user_id)).display_name
        except discord.HTTPException:
            pass  # left the server
        try:
            return (await self.fetch_user(user_id)).display_name
        except discord.HTTPException:
            return None

    async def close(self) -> None:
        await self.supervisor.stop_all()
        for session in self.sessions.values():
            for task in list(session.generating):
                task.cancel()
        transcriber = getattr(self, "transcriber", None)
        if transcriber is not None:
            await transcriber.aclose()

        for task in self.idle_disconnect_tasks.values():
            task.cancel()
        for task in self.continuous_idle_stop_tasks.values():
            task.cancel()
        for source in self.continuous_sources.values():
            source.stop()

        # Close every provider's HTTP keep-alive pool.
        for name in self.tts_dispatcher.provider_names():
            aclose = getattr(self.tts_dispatcher.provider_for(name), "aclose", None)
            if aclose is None:
                continue
            try:
                await aclose()
            except Exception:
                log.exception("Failed to close the %s provider cleanly", name)

        await super().close()
