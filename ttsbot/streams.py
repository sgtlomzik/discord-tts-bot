"""What a cloud provider tells the pipeline about one message's audio.

A provider plugs into the streaming pipeline by answering
``stream_spec(text, voice)`` with a :class:`StreamSpec`; the pipeline
handles the cache, the circuit breaker, the first-audio budget, framing
and the fallback chain the same way for every provider.

Kinds (how the bytes become 20 ms Discord frames):

- ``opus``: Ogg/Opus with 20 ms packets, sent to Discord as-is (cached as
  ``.dopus`` frames); a stream whose packets are not 20 ms is decoded by
  ffmpeg instead;
- ``ogg``: Ogg/Opus decoded by ffmpeg (``pitch`` applied there);
- ``mp3``: MP3 decoded by ffmpeg;
- ``pcm``: raw PCM resampled and framed in-process (``volume_db`` gain).

``open`` starts the request: for ``pcm`` an async callable returning a
stream with ``rate``, ``channels``, ``chunks()`` and ``aclose()``; for
the others a callable returning an async iterator of bytes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Literal

StreamKind = Literal["opus", "ogg", "mp3", "pcm"]


@dataclass(frozen=True)
class StreamSpec:
    kind: StreamKind
    cache_key: str
    ttfa: float  # seconds to the first audio before the next voice speaks
    label: str  # provider name for log lines ("Fish")
    open: Callable[[], Any]
    pitch: int = 0  # semitones, applied by ffmpeg (ogg/mp3 and their cache)
    volume_db: float = 0.0  # gain for pcm
