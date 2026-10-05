"""Write Opus packets into an Ogg Opus file (RFC 7845) without re-encoding.

Pages are written as they fill (about one per second), so a crash loses
at most the last second and the file up to it stays playable.
``OggPacketIndex`` reads such a file back packet by packet.
"""

from __future__ import annotations

import array
import bisect
import mmap
import os
import struct
import zlib
from pathlib import Path

from ttsbot.ogg_opus import UnsupportedOpusStream, opus_packet_samples
from ttsbot.recording.rtp import OPUS_SILENCE


_REVERSED_BITS = bytes(int(f"{i:08b}"[::-1], 2) for i in range(256))


def ogg_crc(data: bytes) -> int:
    """Ogg's CRC-32 (polynomial 0x04C11DB7, MSB first, init 0, no final xor).

    zlib's reflected CRC-32 over bit-reversed bytes, with its init and
    final xor undone and the result bit-reversed back, is the same value
    at C speed (a long session mix has tens of thousands of pages).
    """
    raw = zlib.crc32(bytes(data).translate(_REVERSED_BITS), 0xFFFFFFFF) ^ 0xFFFFFFFF
    return int.from_bytes(raw.to_bytes(4, "little").translate(_REVERSED_BITS), "big")


class OggOpusWriter:
    """One logical Ogg Opus stream, 48 kHz stereo, in one file."""

    PACKETS_PER_PAGE = 50  # 1 s of 20 ms packets

    def __init__(self, path: Path, serial: int) -> None:
        self.path = path
        self._file = path.open("wb")
        self._serial = serial & 0xFFFFFFFF
        self._sequence = 0
        self._packets: list[bytes] = []
        self.samples = 0  # granule position: 48 kHz samples written
        self.bytes_written = 0
        head = b"OpusHead" + struct.pack("<BBHIhB", 1, 2, 0, 48000, 0, 0)
        vendor = b"ttsbot"
        tags = b"OpusTags" + struct.pack("<I", len(vendor)) + vendor + struct.pack("<I", 0)
        self._write_page([head], granule=0, flags=0x02)
        self._write_page([tags], granule=0, flags=0)

    @property
    def seconds(self) -> float:
        return self.samples / 48000

    def write(self, packet: bytes) -> None:
        self.samples += opus_packet_samples(packet)
        self._packets.append(packet)
        if len(self._packets) >= self.PACKETS_PER_PAGE:
            self._flush(flags=0)

    def write_silence(self, samples: int) -> None:
        """Append ``samples`` of silence, rounded down to 20 ms packets."""
        for _ in range(samples // 960):
            self.write(OPUS_SILENCE)

    def close(self) -> None:
        if not self._packets:
            self.write(OPUS_SILENCE)  # the end-of-stream page needs a packet
        self._flush(flags=0x04)
        self._file.close()

    def _flush(self, flags: int) -> None:
        self._write_page(self._packets, granule=self.samples, flags=flags)
        self._packets = []

    def _write_page(self, packets: list[bytes], granule: int, flags: int) -> None:
        lacing = bytearray()
        for packet in packets:
            lacing.extend(b"\xff" * (len(packet) // 255))
            lacing.append(len(packet) % 255)
        if len(lacing) > 255:
            raise ValueError("too many Ogg segments for one page")
        header = bytearray(struct.pack(
            "<4sBBqIIIB", b"OggS", 0, flags, granule, self._serial, self._sequence, 0, len(lacing)
        ))
        page = header + lacing + b"".join(packets)
        struct.pack_into("<I", page, 22, ogg_crc(bytes(page)))
        self._file.write(page)
        self._file.flush()
        self._sequence += 1
        self.bytes_written += len(page)


def ogg_duration(path: Path) -> float:
    """Seconds of audio in an Ogg Opus file: the last page's granule position.

    Stops at the first damaged page, so a file cut off by a crash reports
    what can be played.
    """
    data = path.read_bytes()
    pos, granule = 0, 0
    while pos + 27 <= len(data) and data[pos:pos + 4] == b"OggS":
        segments = data[pos + 26]
        end = pos + 27 + segments + sum(data[pos + 27:pos + 27 + segments])
        if end > len(data):
            break
        granule = max(granule, struct.unpack_from("<q", data, pos + 6)[0])
        pos = end
    return granule / 48000


class OggPacketIndex:
    """Where every audio packet of an Ogg Opus file is, without loading it.

    The file is memory-mapped and only packet offsets, sizes and start
    times are kept (about 18 bytes a packet), so a long session can be
    cut and re-joined packet by packet in little memory. Like
    ``ogg_duration`` it stops at the first damaged page (bad structure or
    CRC) and keeps everything before it.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._starts = array.array("Q")
        self._sizes = array.array("I")
        self._samples = array.array("Q")  # start sample of each packet; one more at the end
        self._split: dict[int, bytes] = {}  # packets continued across pages (never written by us)
        self.damaged = False
        self._data: mmap.mmap | bytes = b""
        self._file = path.open("rb")
        try:
            size = os.fstat(self._file.fileno()).st_size
            self._data = mmap.mmap(self._file.fileno(), 0, access=mmap.ACCESS_READ) if size else b""
            self._scan()
        except BaseException:
            self.close()
            raise

    def __len__(self) -> int:
        return len(self._sizes)

    def __enter__(self) -> "OggPacketIndex":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @property
    def seconds(self) -> float:
        return self._samples[-1] / 48000

    def packet(self, i: int) -> bytes:
        split = self._split.get(i)
        if split is not None:
            return split
        start = self._starts[i]
        return self._data[start:start + self._sizes[i]]

    def start_sample(self, i: int) -> int:
        """48 kHz samples before packet ``i`` (``len(self)`` gives the total)."""
        return self._samples[i]

    def index_at(self, seconds: float) -> int:
        """The first packet that starts at or after ``seconds``."""
        return bisect.bisect_left(self._samples, round(seconds * 48000), 0, len(self))

    def close(self) -> None:
        if isinstance(self._data, mmap.mmap):
            self._data.close()
        self._file.close()

    def _scan(self) -> None:
        data = self._data
        pos, headers, total = 0, 0, 0
        partial: list[bytes] = []  # a packet continued from the previous page
        self._samples.append(0)
        while pos + 27 <= len(data):
            if data[pos:pos + 4] != b"OggS":
                self.damaged = True
                break
            segments = data[pos + 26]
            body = pos + 27 + segments
            lacing = data[pos + 27:body]
            end = body + sum(lacing)
            if end > len(data):
                self.damaged = True  # cut off mid-page by a crash
                break
            page = bytearray(data[pos:end])
            struct.pack_into("<I", page, 22, 0)
            if ogg_crc(page) != struct.unpack_from("<I", data, pos + 22)[0]:
                self.damaged = True
                break
            cursor, start = body, body
            for size in lacing:
                cursor += size
                if size == 255:
                    continue
                if partial:
                    partial.append(data[start:cursor])
                    packet, start_offset = b"".join(partial), None
                    partial = []
                else:
                    packet, start_offset = None, start
                length = cursor - start if packet is None else len(packet)
                if headers < 2:
                    head = data[start:cursor] if packet is None else packet
                    expected = b"OpusHead" if headers == 0 else b"OpusTags"
                    if not head.startswith(expected):
                        raise UnsupportedOpusStream(f"{self.path.name}: missing {expected.decode()}")
                    headers += 1
                elif length:
                    try:
                        samples = opus_packet_samples(data[start:cursor] if packet is None else packet)
                    except UnsupportedOpusStream:
                        samples = 0  # undecodable: keep its place, it plays as nothing
                    if packet is not None:
                        self._split[len(self._sizes)] = packet
                    self._starts.append(start_offset or 0)
                    self._sizes.append(length)
                    total += samples
                    self._samples.append(total)
                start = cursor
            if start < end:
                partial.append(data[start:end])  # continues on the next page
            pos = end
        else:
            self.damaged = pos != len(data)
        if headers < 2:
            raise UnsupportedOpusStream(f"{self.path.name}: not an Ogg Opus file")
