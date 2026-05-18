from dataclasses import dataclass


@dataclass
class RecorderMetrics:
    segments_saved: int = 0
    segments_discarded: int = 0
    users_seen: int = 0
    frames_received: int = 0
    frames_dropped: int = 0
    storage_errors: int = 0
