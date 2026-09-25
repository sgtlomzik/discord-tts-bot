"""The TTSBot Discord client: composition of the runtime concerns.

The behavior lives in four concern mixins; this module owns construction
(queues, dispatcher, registry, config store), the task lifecycle
(setup_hook/close) and nothing else. Slash commands and event handlers
are attached from the outside by the composition root (bot.py).
"""

import asyncio
import logging
import os
import time
from dataclasses import replace
from pathlib import Path

import discord
from discord.ext import commands

from ttsbot import voice_registry
from ttsbot.fish import FishConfig, FishProvider
from ttsbot.providers import (
    LocalProvider,
    TTSDispatcher,
    TTSPhraseCache,
    load_cache_config_from_env,
    load_circuit_breaker_from_env,
    load_dispatcher_config_from_env,
    load_minimax_config_from_env,
)
from ttsbot import config
from ttsbot.audio import ContinuousTTSAudioSource
from ttsbot.merge import MergeQueueMixin
from ttsbot.messages import MergeBufferState
from ttsbot.models import PreparedAudio, TTSJob, VOICE_PROFILES
from ttsbot.pipeline import SynthesisPipelineMixin
from ttsbot.playback import PlaybackMixin
from ttsbot.store import BotConfigStore
from ttsbot.voice_lifecycle import VoiceLifecycleMixin

log = logging.getLogger("tts_bot")

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
intents.guilds = True


class TTSBot(
    VoiceLifecycleMixin,
    MergeQueueMixin,
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
        try:
            fish_cfg = FishConfig.from_env()
        except ValueError as exc:
            if os.getenv("FISH_API_KEY", "").strip():
                raise  # Fish is wanted, so a bad setting must stop startup
            log.warning("Ignoring invalid FISH_* settings; Fish is disabled: %s", exc)
            fish_cfg = FishConfig(api_key="")
        self.fish_provider = FishProvider(fish_cfg) if fish_cfg.api_key else None
        if fish_cfg.reference_id:
            current = self.voice_registry.get("fish-default")
            if current is not None and current.is_fish and current.fish is not None:
                fish_record = replace(current, fish=replace(current.fish, reference_id=fish_cfg.reference_id))
            else:
                fish_record = voice_registry.VoiceRecord(
                    name="fish-default", label="Fish Audio", description="Fish Audio reference voice",
                    provider=voice_registry.PROVIDER_FISH,
                    fish=voice_registry.FishParams(reference_id=fish_cfg.reference_id),
                )
            if current != fish_record:
                self.voice_registry.add(fish_record)
                try:
                    voice_registry.save_registry(config.VOICES_REGISTRY_PATH, self.voice_registry)
                except OSError:
                    log.exception("Could not save fish-default to %s; keeping it in memory",
                                  config.VOICES_REGISTRY_PATH)
        self.config_store = BotConfigStore(
            config.BOT_CONFIG_PATH, config.WHITELIST_USERS, voice_registry=self.voice_registry
        )
        saved_latency = self.config_store.settings.get("fish_latency")
        self.fish_config = replace(fish_cfg, latency=saved_latency) if isinstance(saved_latency, str) else fish_cfg
        if self.fish_provider is not None:
            self.fish_provider.config = self.fish_config
        self.piper_voices: dict[tuple[str, str], object] = {}
        # TTS provider abstraction (see ttsbot/providers.py). Skeleton
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
            fish=self.fish_provider,
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

    @property
    def fish_latency(self) -> str:
        return self.fish_config.latency

    def set_fish_latency(self, mode: str) -> None:
        """Persist the global Fish mode and apply it to future requests."""
        self.config_store.set_fish_latency(mode)
        self.fish_config = replace(self.fish_config, latency=mode)
        if self.fish_provider is not None:
            self.fish_provider.config = self.fish_config

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

        if self.fish_provider is not None:
            await self.fish_provider.aclose()

        await super().close()
