"""Named long-running tasks that are restarted when they crash.

The bot's workers (generation, playback, keep-alive, provider health,
transcription) are endless loops. Without supervision an exception that
escapes one of them ends the task silently and the bot stays online but
mute. TaskSupervisor logs the crash, restarts the worker after a growing
delay and keeps a per-worker state for /voicebot status.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

log = logging.getLogger("tts_bot")

# A run this long counts as healthy: the next crash restarts after the
# base delay again instead of the grown one.
HEALTHY_RUN_SECONDS = 300.0


@dataclass
class WorkerState:
    running: bool = False
    restarts: int = 0
    last_error: str | None = None


class TaskSupervisor:
    def __init__(self, *, base_delay: float = 1.0, max_delay: float = 60.0) -> None:
        self.base_delay = base_delay
        self.max_delay = max_delay
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._states: dict[str, WorkerState] = {}

    def next_delay(self, failures: int) -> float:
        """Delay before restart number *failures* (1-based) in a row."""
        # Clamp the exponent first: 2 ** 1024 does not fit in a float, and an
        # OverflowError here would end the supervisor loop itself.
        return min(self.max_delay, self.base_delay * 2 ** min(failures - 1, 32))

    def start(self, name: str, factory: Callable[[], Awaitable[None]]) -> asyncio.Task[None]:
        state = self._states[name] = WorkerState(running=True)
        task = asyncio.create_task(self._run(name, factory, state), name=name)
        self._tasks[name] = task
        return task

    async def _run(self, name: str, factory: Callable[[], Awaitable[None]], state: WorkerState) -> None:
        failures = 0
        try:
            while True:
                started = time.monotonic()
                try:
                    await factory()
                    return  # a worker that returns has finished its job
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if time.monotonic() - started >= HEALTHY_RUN_SECONDS:
                        failures = 0
                    failures += 1
                    state.restarts += 1
                    state.last_error = f"{type(exc).__name__}: {exc}"[:200]
                    delay = self.next_delay(failures)
                    log.exception("Worker %s crashed; restart #%d in %.0fs", name, state.restarts, delay)
                    await asyncio.sleep(delay)
        finally:
            state.running = False

    def status(self) -> dict[str, WorkerState]:
        return dict(self._states)

    async def stop_all(self) -> None:
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
