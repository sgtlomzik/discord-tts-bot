from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(frozen=True)
class RawAudioFrame:
    user_id: int | None
    username_snapshot: str | None
    pcm: bytes
    sample_rate: int = 48000
    channels: int = 2
    timestamp: datetime | None = None

    def with_timestamp(self) -> "RawAudioFrame":
        if self.timestamp is not None:
            return self
        return RawAudioFrame(
            user_id=self.user_id,
            username_snapshot=self.username_snapshot,
            pcm=self.pcm,
            sample_rate=self.sample_rate,
            channels=self.channels,
            timestamp=datetime.now(timezone.utc),
        )


@dataclass(frozen=True)
class NormalizedPcmFrame:
    user_id: int
    timestamp: datetime
    pcm: bytes
    username_snapshot: str | None = None


@dataclass(frozen=True)
class SpeechSegment:
    user_id: int
    username_snapshot: str | None
    start_utc: datetime
    end_utc: datetime
    pcm: bytes

    @property
    def duration_ms(self) -> int:
        return int((self.end_utc - self.start_utc).total_seconds() * 1000)
