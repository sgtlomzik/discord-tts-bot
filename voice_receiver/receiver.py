import asyncio
from uuid import uuid4

from .config import VoiceRecorderConfig
from .metrics import RecorderMetrics
from .models import RawAudioFrame
from .resampler import PcmResampler
from .segmenter import UserSegmenter
from .storage import SegmentStorage, StorageSession
from .vad import WebRtcVad


class VoiceRecorderSession:
    def __init__(
        self,
        config: VoiceRecorderConfig | None = None,
        *,
        vad_factory=None,
        storage: SegmentStorage | None = None,
    ) -> None:
        self.config = config or VoiceRecorderConfig.from_env()
        self.metrics = RecorderMetrics()
        self.frame_queue: asyncio.Queue[RawAudioFrame] = asyncio.Queue(maxsize=self.config.frame_queue_maxsize)
        self.storage = storage or SegmentStorage(self.config)
        self.storage_session: StorageSession | None = None
        self._vad_factory = vad_factory or (lambda: WebRtcVad(self.config))
        self._segmenter: UserSegmenter | None = None
        self._resampler = PcmResampler(self.config)
        self._worker_task: asyncio.Task[None] | None = None
        self._stopping = False
        self._seen_users: set[int] = set()

    async def start(self, guild_id: int, channel_id: int, session_id: str | None = None) -> None:
        self.storage_session = await self.storage.start_session(guild_id, channel_id, session_id or str(uuid4()))
        self.storage_session.metrics = self.metrics
        self._segmenter = UserSegmenter(self.config, self._vad_factory())
        self._worker_task = asyncio.create_task(self._worker(), name=f"voice-recorder-{self.storage_session.session_id}")

    def handle_frame(self, frame: RawAudioFrame) -> bool:
        self.metrics.frames_received += 1
        try:
            self.frame_queue.put_nowait(frame)
            return True
        except asyncio.QueueFull:
            self.metrics.frames_dropped += 1
            self._schedule_event({"type": "queue_overflow"})
            return False

    async def stop(self, interrupted: bool = False) -> None:
        self._stopping = True
        if self._worker_task is not None:
            await self.frame_queue.join()
            await self._worker_task
        if self.storage_session is not None:
            assert self._segmenter is not None
            for segment in self._segmenter.flush():
                await self.storage.save_segment(self.storage_session, segment)
            self.metrics.segments_discarded += self._segmenter.discarded_segments
            await self.storage.stop_session(self.storage_session, interrupted=interrupted)

    def snapshot_status(self) -> dict:
        return {
            "segments_saved": self.metrics.segments_saved,
            "segments_discarded": self.metrics.segments_discarded,
            "users_seen": self.metrics.users_seen,
            "frames_received": self.metrics.frames_received,
            "frames_dropped": self.metrics.frames_dropped,
            "storage_errors": self.metrics.storage_errors,
            "storage_queue_size": self.frame_queue.qsize(),
        }

    async def _worker(self) -> None:
        while not self._stopping or not self.frame_queue.empty():
            try:
                frame = await asyncio.wait_for(self.frame_queue.get(), timeout=0.05)
            except TimeoutError:
                continue
            try:
                if frame.user_id is not None and frame.user_id not in self._seen_users:
                    self._seen_users.add(frame.user_id)
                    self.metrics.users_seen = len(self._seen_users)
                    await self._append_event({"type": "user_seen", "user_id": str(frame.user_id)})
                assert self._segmenter is not None
                for normalized in self._resampler.normalize(frame):
                    for segment in self._segmenter.process_frame(normalized):
                        if self.storage_session is not None:
                            await self.storage.save_segment(self.storage_session, segment)
            finally:
                self.frame_queue.task_done()

    async def _append_event(self, payload: dict) -> None:
        if self.storage_session is None:
            return
        await self.storage.append_event(self.storage_session, payload)

    def _schedule_event(self, payload: dict) -> None:
        if self.storage_session is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self._append_event(payload))
