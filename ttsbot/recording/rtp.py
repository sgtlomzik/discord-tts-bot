"""Parse and decrypt received Discord voice packets (transport layer).

Discord wraps each Opus packet twice: DAVE end-to-end encryption inside,
then the transport encryption negotiated for the UDP connection. This
module removes the transport layer only; the DAVE layer needs the
sender's user id and is removed by the voice client.

Only ``aead_xchacha20_poly1305_rtpsize`` is handled: it is the first mode
discord.py offers and Discord has retired the older xsalsa20 modes.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

import nacl.bindings
import nacl.exceptions

SUPPORTED_MODE = "aead_xchacha20_poly1305_rtpsize"

# Opus packet a Discord client sends five times after the user stops
# talking; DAVE leaves it unencrypted.
OPUS_SILENCE = b"\xf8\xff\xfe"

_RTP_VERSION = 2
_OPUS_PAYLOAD_TYPE = 0x78


class PacketError(ValueError):
    """The packet is not a decryptable Opus RTP packet."""


@dataclass(frozen=True, slots=True)
class RtpPacket:
    ssrc: int
    sequence: int
    timestamp: int
    payload: bytes  # still DAVE-encrypted unless DAVE is off


def is_rtp_audio(data: bytes) -> bool:
    """True for an RTP Opus packet; False for RTCP and anything else."""
    return (
        len(data) >= 12
        and data[0] >> 6 == _RTP_VERSION
        and data[1] & 0x7F == _OPUS_PAYLOAD_TYPE
    )


def decrypt_transport(data: bytes, mode: str, secret_key: bytes) -> RtpPacket:
    """Return the packet's header fields and its transport-decrypted payload.

    rtpsize layout: 12-byte header, 4 bytes per CSRC and, if the X bit is
    set, the 4-byte extension header are plain text (and the AEAD's
    additional data); the extension body and the Opus payload are
    encrypted; the last 4 bytes are the nonce counter. If the P bit is set
    the decrypted payload ends in RTP padding whose last byte is its length
    (RFC 3550 5.1): WebRTC pads real frames and sends padding-only packets
    to probe bandwidth. The padding is removed, so a padding-only packet
    has an empty payload.
    """
    if mode != SUPPORTED_MODE:
        raise PacketError(f"unsupported voice encryption mode {mode}")
    if not is_rtp_audio(data):
        raise PacketError("not an RTP Opus packet")
    header_len = 12 + 4 * (data[0] & 0x0F)
    has_extension = bool(data[0] & 0x10)
    if has_extension:
        header_len += 4
    if len(data) < header_len + 16 + 4:  # header + Poly1305 tag + nonce
        raise PacketError("truncated packet")
    nonce = data[-4:] + bytes(20)
    try:
        plain = nacl.bindings.crypto_aead_xchacha20poly1305_ietf_decrypt(
            bytes(data[header_len:-4]), bytes(data[:header_len]), nonce, secret_key
        )
    except nacl.exceptions.CryptoError as exc:
        raise PacketError("transport decryption failed") from exc
    if has_extension:
        words = struct.unpack_from(">H", data, header_len - 2)[0]
        plain = plain[4 * words:]
    if data[0] & 0x20:
        if not plain or not 0 < plain[-1] <= len(plain):
            raise PacketError("bad RTP padding")
        plain = plain[:-plain[-1]]
    sequence, timestamp, ssrc = struct.unpack_from(">HII", data, 2)
    return RtpPacket(ssrc=ssrc, sequence=sequence, timestamp=timestamp, payload=plain)
