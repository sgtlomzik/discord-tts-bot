"""Write Opus packets into an Ogg Opus file (RFC 7845) without re-encoding.

Pages are written as they fill (about one per second), so a crash loses
at most the last second and the file up to it stays playable.
"""

from __future__ import annotations

import struct
from pathlib import Path

from ttsbot.ogg_opus import opus_packet_samples
from ttsbot.recording.rtp import OPUS_SILENCE


def _crc_table() -> list[int]:
    table = []
    for i in range(256):
        crc = i << 24
        for _ in range(8):
            crc = ((crc << 1) ^ 0x04C11DB7) if crc & 0x80000000 else crc << 1
        table.append(crc & 0xFFFFFFFF)
    return table


_CRC = _crc_table()


def ogg_crc(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc = ((crc << 8) & 0xFFFFFFFF) ^ _CRC[(crc >> 24) ^ byte]
    return crc


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
