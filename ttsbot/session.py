"""One guild's TTS session: its queue, prefetch window, player and voice state.

Each guild gets its own pair of workers (generation and playback, under
the bot's supervisor), so a long message or a stuck player in one guild
never holds up another. Within a guild, messages play strictly in order;
the generation worker starts generating the next message while the
current one is still downloading or playing, up to the prefetch window
(``TTS_PREFETCH_LOOKAHEAD`` messages ready ahead of playback).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from ttsbot import config


@dataclass(eq=False)
class GuildSession:
    guild_id: int
    queue: asyncio.Queue  # TTSJob, in arrival order
    ready: asyncio.Queue  # PreparedAudio, in play order; bounds the prefetch
    active: set = field(default_factory=set)  # PreparedAudio generated or played now
    generating: set = field(default_factory=set)  # tasks generating audio
    workers_started: bool = False
    # Voice state (ttsbot.voice_lifecycle, ttsbot.playback).
    player: object | None = None  # the guild's ContinuousTTSAudioSource
    voice_lock: asyncio.Lock = field(default_factory=asyncio.Lock)  # connect/move
    connect_cooldown_until: float = 0.0  # time.monotonic(); after a failed connect
    auto_connect_suppressed_until: float = 0.0  # after an explicit or idle disconnect
    idle_disconnect_task: asyncio.Task | None = None
    idle_stop_task: asyncio.Task | None = None  # stops the idle continuous player

    def take_player(self):
        """The player, detached from the session (None if there is none)."""
        player, self.player = self.player, None
        return player

    @classmethod
    def create(cls, guild_id: int) -> "GuildSession":
        return cls(
            guild_id=guild_id,
            queue=asyncio.Queue(maxsize=config.QUEUE_MAXSIZE),
            ready=asyncio.Queue(maxsize=config.TTS_PREFETCH_LOOKAHEAD),
        )

    def cancel_all(self) -> int:
        """Drop queued jobs and cancel the ones in flight; return how many."""
        removed = 0
        while True:
            try:
                self.queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            self.queue.task_done()
            removed += 1
        for prepared in self.active:
            if not prepared.cancelled:
                prepared.cancelled = True
                removed += 1
        return removed
