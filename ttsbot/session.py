"""One guild's TTS session: its queue, prefetch window and player loop.

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
