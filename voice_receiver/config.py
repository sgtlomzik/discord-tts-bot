import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class VoiceRecorderConfig:
    enabled: bool = True
    storage_root: Path = Path("data/voice_recordings")
    sample_rate: int = 16000
    frame_ms: int = 20
    vad_engine: str = "webrtcvad"
    vad_mode: int = 2
    pre_roll_ms: int = 400
    post_roll_ms: int = 250
    start_window_ms: int = 240
    start_min_speech_frames: int = 3
    end_silence_ms: int = 700
    min_segment_ms: int = 500
    max_segment_ms: int = 45000
    merge_gap_ms: int = 350
    debug_save_discarded: bool = False
    debug_save_mixed_track: bool = False
    frame_queue_maxsize: int = 500
    storage_queue_maxsize: int = 100

    @classmethod
    def from_env(cls) -> "VoiceRecorderConfig":
        def env_bool(name: str, default: bool) -> bool:
            value = os.getenv(name)
            if value is None:
                return default
            return value.strip().lower() not in {"0", "false", "no"}

        return cls(
            enabled=env_bool("VOICE_RECORD_ENABLED", True),
            storage_root=Path(os.getenv("VOICE_RECORD_STORAGE_ROOT", "data/voice_recordings")),
            sample_rate=int(os.getenv("VOICE_RECORD_SAMPLE_RATE", "16000")),
            frame_ms=int(os.getenv("VOICE_RECORD_FRAME_MS", "20")),
            vad_engine=os.getenv("VOICE_RECORD_VAD_ENGINE", "webrtcvad"),
            vad_mode=int(os.getenv("VOICE_RECORD_VAD_MODE", "2")),
            pre_roll_ms=int(os.getenv("VOICE_RECORD_PRE_ROLL_MS", "400")),
            post_roll_ms=int(os.getenv("VOICE_RECORD_POST_ROLL_MS", "250")),
            start_window_ms=int(os.getenv("VOICE_RECORD_START_WINDOW_MS", "240")),
            start_min_speech_frames=int(os.getenv("VOICE_RECORD_START_MIN_SPEECH_FRAMES", "3")),
            end_silence_ms=int(os.getenv("VOICE_RECORD_END_SILENCE_MS", "700")),
            min_segment_ms=int(os.getenv("VOICE_RECORD_MIN_SEGMENT_MS", "500")),
            max_segment_ms=int(os.getenv("VOICE_RECORD_MAX_SEGMENT_MS", "45000")),
            merge_gap_ms=int(os.getenv("VOICE_RECORD_MERGE_GAP_MS", "350")),
            debug_save_discarded=env_bool("VOICE_RECORD_DEBUG_SAVE_DISCARDED", False),
            debug_save_mixed_track=env_bool("VOICE_RECORD_DEBUG_SAVE_MIXED_TRACK", False),
        )

    @property
    def samples_per_frame(self) -> int:
        return self.sample_rate * self.frame_ms // 1000

    @property
    def bytes_per_frame(self) -> int:
        return self.samples_per_frame * 2
