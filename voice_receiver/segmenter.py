from collections import deque
from dataclasses import dataclass, field

from .config import VoiceRecorderConfig
from .models import NormalizedPcmFrame, SpeechSegment


class RingBuffer:
    def __init__(self, max_frames: int) -> None:
        self._frames = deque(maxlen=max_frames)

    def append(self, frame: NormalizedPcmFrame) -> None:
        self._frames.append(frame)

    def frames(self) -> list[NormalizedPcmFrame]:
        return list(self._frames)


class RollingBoolWindow:
    def __init__(self, max_frames: int) -> None:
        self._items = deque(maxlen=max_frames)

    def append(self, value: bool) -> None:
        self._items.append(value)

    @property
    def speech_count(self) -> int:
        return sum(1 for item in self._items if item)

    def clear(self) -> None:
        self._items.clear()


@dataclass
class _UserState:
    pre_roll: RingBuffer
    start_window: RollingBoolWindow
    current_frames: list[NormalizedPcmFrame] = field(default_factory=list)
    silence_ms: int = 0
    pending_segment: SpeechSegment | None = None

    @property
    def active(self) -> bool:
        return bool(self.current_frames)


class UserSegmenter:
    def __init__(self, config: VoiceRecorderConfig, vad) -> None:
        self.config = config
        self.vad = vad
        self.states: dict[int, _UserState] = {}
        self.discarded_segments = 0

    def _state(self, user_id: int) -> _UserState:
        state = self.states.get(user_id)
        if state is None:
            state = _UserState(
                pre_roll=RingBuffer(self.config.pre_roll_ms // self.config.frame_ms),
                start_window=RollingBoolWindow(self.config.start_window_ms // self.config.frame_ms),
            )
            self.states[user_id] = state
        return state

    def process_frame(self, frame: NormalizedPcmFrame) -> list[SpeechSegment]:
        state = self._state(frame.user_id)
        is_speech = self.vad.is_speech(frame.pcm)

        if not state.active:
            state.pre_roll.append(frame)
            state.start_window.append(is_speech)
            if state.start_window.speech_count >= self.config.start_min_speech_frames:
                state.current_frames = state.pre_roll.frames()
                if not state.current_frames or state.current_frames[-1] is not frame:
                    state.current_frames.append(frame)
                state.silence_ms = 0
            return []

        state.current_frames.append(frame)
        if is_speech:
            state.silence_ms = 0
        else:
            state.silence_ms += self.config.frame_ms

        if len(state.current_frames) * self.config.frame_ms >= self.config.max_segment_ms:
            return self._close(state)
        if state.silence_ms >= self.config.end_silence_ms:
            return self._close(state)
        return []

    def _close(self, state: _UserState) -> list[SpeechSegment]:
        frames = state.current_frames
        segment = SpeechSegment(
            user_id=frames[0].user_id,
            username_snapshot=frames[0].username_snapshot,
            start_utc=frames[0].timestamp,
            end_utc=frames[-1].timestamp + timedelta_ms(self.config.frame_ms),
            pcm=b"".join(frame.pcm for frame in frames),
        )
        state.current_frames = []
        state.start_window.clear()
        state.silence_ms = 0
        state.pre_roll = RingBuffer(self.config.pre_roll_ms // self.config.frame_ms)

        if segment.duration_ms < self.config.min_segment_ms:
            self.discarded_segments += 1
            return []

        pending = state.pending_segment
        if pending is not None:
            gap_ms = int((segment.start_utc - pending.end_utc).total_seconds() * 1000)
            if gap_ms <= self.config.merge_gap_ms:
                state.pending_segment = SpeechSegment(
                    user_id=segment.user_id,
                    username_snapshot=segment.username_snapshot,
                    start_utc=pending.start_utc,
                    end_utc=segment.end_utc,
                    pcm=pending.pcm + segment.pcm,
                )
                return []
            state.pending_segment = segment
            return [pending]

        state.pending_segment = segment
        return []

    def flush(self) -> list[SpeechSegment]:
        segments = []
        for state in self.states.values():
            if state.current_frames:
                segments.extend(self._close(state))
            if state.pending_segment is not None:
                segments.append(state.pending_segment)
                state.pending_segment = None
        return segments


def timedelta_ms(value: int):
    from datetime import timedelta

    return timedelta(milliseconds=value)
