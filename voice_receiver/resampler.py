import audioop
from collections.abc import Iterable
from datetime import timedelta

from .config import VoiceRecorderConfig
from .models import NormalizedPcmFrame, RawAudioFrame


class PcmResampler:
    def __init__(self, config: VoiceRecorderConfig) -> None:
        self.config = config

    def normalize(self, raw: RawAudioFrame) -> Iterable[NormalizedPcmFrame]:
        stamped = raw.with_timestamp()
        if stamped.user_id is None:
            return []

        mono = audioop.tomono(stamped.pcm, 2, 0.5, 0.5) if stamped.channels == 2 else stamped.pcm
        pcm, _ = audioop.ratecv(mono, 2, 1, stamped.sample_rate, self.config.sample_rate, None)
        frame_size = self.config.bytes_per_frame
        frames = []
        for offset in range(0, len(pcm) - frame_size + 1, frame_size):
            frame_index = offset // frame_size
            frames.append(
                NormalizedPcmFrame(
                    user_id=stamped.user_id,
                    username_snapshot=stamped.username_snapshot,
                    timestamp=stamped.timestamp + timedelta(milliseconds=frame_index * self.config.frame_ms),
                    pcm=pcm[offset : offset + frame_size],
                )
            )
        return frames
