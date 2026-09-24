"""Incremental Ogg/Opus demuxing for 20 ms Discord voice packets.

This module only parses container and packet headers. It never decodes or
encodes audio and does not load libopus or start ffmpeg.
"""

from __future__ import annotations

import struct
from pathlib import Path
from typing import BinaryIO, Iterator


FRAME_CACHE_MAGIC = b"DOPUS1\x00"
MAX_OPUS_PACKET = 1275


class UnsupportedOpusStream(ValueError):
    """The stream cannot be sent as one 20 ms packet per Discord read."""


def opus_packet_samples(packet: bytes) -> int:
    """Return duration in 48 kHz samples from the Opus TOC (RFC 6716)."""
    if not packet or len(packet) > MAX_OPUS_PACKET:
        raise UnsupportedOpusStream("empty or oversized Opus packet")
    toc = packet[0]
    if toc & 0x80:
        per_frame = (48000 << ((toc >> 3) & 3)) // 400
    elif (toc & 0x60) == 0x60:
        per_frame = 48000 // (50 if toc & 0x08 else 100)
    else:
        size_code = (toc >> 3) & 3
        per_frame = 2880 if size_code == 3 else (48000 << size_code) // 100
    count_code = toc & 3
    if count_code == 0:
        count = 1
    elif count_code in (1, 2):
        count = 2
    else:
        if len(packet) < 2:
            raise UnsupportedOpusStream("missing Opus frame count")
        count = packet[1] & 0x3F
        if not count:
            raise UnsupportedOpusStream("invalid Opus frame count")
    samples = per_frame * count
    if samples > 5760:
        raise UnsupportedOpusStream("Opus packet exceeds 120 ms")
    return samples


def require_discord_frame(packet: bytes) -> bytes:
    if opus_packet_samples(packet) != 960:
        raise UnsupportedOpusStream("Opus packet duration is not 20 ms")
    return packet


class OggOpusDemuxer:
    """Consume arbitrary HTTP chunks and emit complete 20 ms Opus packets."""

    def __init__(self) -> None:
        self._buffer = bytearray()
        self._packet = bytearray()
        self._serial: int | None = None
        self._sequence: int | None = None
        self._headers = 0
        self.packet_count = 0
        self._eos = False

    def feed(self, chunk: bytes) -> list[bytes]:
        if self._eos and chunk:
            raise UnsupportedOpusStream("data after Ogg end-of-stream")
        self._buffer.extend(chunk)
        output: list[bytes] = []
        while True:
            if len(self._buffer) < 27:
                break
            if self._buffer[:4] != b"OggS" or self._buffer[4] != 0:
                raise UnsupportedOpusStream("invalid Ogg page header")
            segment_count = self._buffer[26]
            header_end = 27 + segment_count
            if len(self._buffer) < header_end:
                break
            sizes = self._buffer[27:header_end]
            page_end = header_end + sum(sizes)
            if len(self._buffer) < page_end:
                break
            flags = self._buffer[5]
            serial = int.from_bytes(self._buffer[14:18], "little")
            sequence = int.from_bytes(self._buffer[18:22], "little")
            if self._serial is None:
                if not flags & 0x02:
                    raise UnsupportedOpusStream("missing Ogg beginning-of-stream")
                self._serial = serial
            elif serial != self._serial or sequence != (self._sequence + 1) & 0xFFFFFFFF:
                raise UnsupportedOpusStream("Ogg stream or page sequence changed")
            if bool(flags & 0x01) != bool(self._packet):
                raise UnsupportedOpusStream("invalid Ogg packet continuation")
            self._sequence = sequence
            cursor = header_end
            for size in sizes:
                self._packet.extend(self._buffer[cursor:cursor + size])
                cursor += size
                if size < 255:
                    packet = bytes(self._packet)
                    self._packet.clear()
                    if self._headers == 0:
                        if len(packet) < 19 or not packet.startswith(b"OpusHead") or packet[9] not in (1, 2):
                            raise UnsupportedOpusStream("unsupported OpusHead")
                        self._headers = 1
                    elif self._headers == 1:
                        if not packet.startswith(b"OpusTags"):
                            raise UnsupportedOpusStream("missing OpusTags")
                        self._headers = 2
                    else:
                        output.append(require_discord_frame(packet))
                        self.packet_count += 1
            del self._buffer[:page_end]
            if flags & 0x04:
                self._eos = True
                if self._buffer:
                    raise UnsupportedOpusStream("chained Ogg streams are unsupported")
                break
        return output

    def finish(self) -> None:
        if self._buffer or self._packet or self._headers != 2 or not self.packet_count or not self._eos:
            raise UnsupportedOpusStream("incomplete Ogg/Opus stream")


def read_ogg_frames(path: Path) -> list[bytes]:
    demuxer = OggOpusDemuxer()
    frames: list[bytes] = []
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(65536), b""):
            frames.extend(demuxer.feed(chunk))
    demuxer.finish()
    return frames


def write_frame(output: BinaryIO, packet: bytes) -> None:
    require_discord_frame(packet)
    output.write(struct.pack(">H", len(packet)))
    output.write(packet)


def read_frame_cache(path: Path) -> Iterator[bytes]:
    with path.open("rb") as source:
        if source.read(len(FRAME_CACHE_MAGIC)) != FRAME_CACHE_MAGIC:
            raise UnsupportedOpusStream("invalid Discord Opus frame cache")
        count = 0
        while prefix := source.read(2):
            if len(prefix) != 2:
                raise UnsupportedOpusStream("truncated frame length")
            size = struct.unpack(">H", prefix)[0]
            packet = source.read(size)
            if len(packet) != size:
                raise UnsupportedOpusStream("truncated frame")
            count += 1
            yield require_discord_frame(packet)
        if not count:
            raise UnsupportedOpusStream("empty frame cache")
