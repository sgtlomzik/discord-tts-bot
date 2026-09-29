"""Voice receive: packet decryption, Ogg writing, chunking and the client."""

import asyncio
import json
import shutil
import struct
import subprocess
import tempfile
import time
import types
import unittest
from pathlib import Path

import nacl.bindings
import nacl.utils

from ttsbot.ogg_opus import OggOpusDemuxer
from ttsbot.recording import rtp
from ttsbot.recording.client import RecordingVoiceClient
from ttsbot.recording.ogg import OggOpusWriter
from ttsbot.recording.recorder import ChannelRecorder, RecorderSettings

KEY = bytes(range(32))
# A 20 ms CELT fullband Opus packet (TOC 0xFC: config 31, stereo, one frame).
FRAME = b"\xfc" + bytes(40)


def encrypt(payload: bytes, *, ssrc=7, sequence=1, timestamp=960, counter=5, extension: bytes | None = None,
            key=KEY) -> bytes:
    """Build a packet the way Discord sends it (aead_xchacha20_poly1305_rtpsize)."""
    header = bytearray(12)
    header[0] = 0x80 | (0x10 if extension is not None else 0)
    header[1] = 0x78
    struct.pack_into(">HII", header, 2, sequence, timestamp, ssrc)
    plain = payload
    if extension is not None:
        header += b"\xbe\xde" + struct.pack(">H", len(extension) // 4)
        plain = extension + payload
    nonce = struct.pack(">I", counter) + bytes(20)
    cipher = nacl.bindings.crypto_aead_xchacha20poly1305_ietf_encrypt(plain, bytes(header), nonce, key)
    return bytes(header) + cipher + nonce[:4]


class RtpTests(unittest.TestCase):
    def test_decrypts_header_fields_and_payload(self):
        packet = rtp.decrypt_transport(encrypt(FRAME, ssrc=42, sequence=9, timestamp=123456), rtp.SUPPORTED_MODE, KEY)
        self.assertEqual((packet.ssrc, packet.sequence, packet.timestamp, packet.payload), (42, 9, 123456, FRAME))

    def test_strips_encrypted_header_extension(self):
        data = encrypt(FRAME, extension=b"\x10\xff\x00\x00" * 2)
        self.assertEqual(rtp.decrypt_transport(data, rtp.SUPPORTED_MODE, KEY).payload, FRAME)

    def test_wrong_key_mode_and_rtcp_are_rejected(self):
        with self.assertRaises(rtp.PacketError):
            rtp.decrypt_transport(encrypt(FRAME), rtp.SUPPORTED_MODE, bytes(32))
        with self.assertRaises(rtp.PacketError):
            rtp.decrypt_transport(encrypt(FRAME), "xsalsa20_poly1305", KEY)
        rtcp = b"\x81\xc9" + bytes(30)
        self.assertFalse(rtp.is_rtp_audio(rtcp))
        with self.assertRaises(rtp.PacketError):
            rtp.decrypt_transport(rtcp, rtp.SUPPORTED_MODE, KEY)


class OggWriterTests(unittest.TestCase):
    def test_file_is_valid_ogg_opus(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "a.ogg"
            writer = OggOpusWriter(path, serial=1)
            for _ in range(120):
                writer.write(FRAME)
            writer.write_silence(48000)
            writer.close()
            self.assertAlmostEqual(writer.seconds, 3.4)
            demuxer = OggOpusDemuxer()
            packets = demuxer.feed(path.read_bytes())
            demuxer.finish()  # headers, page sequence and end-of-stream are right
            self.assertEqual(len(packets), 170)
            if shutil.which("ffprobe"):
                probe = subprocess.run(
                    ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                    capture_output=True, text=True, check=True,
                )
                self.assertEqual(probe.stderr, "")  # no CRC or granule complaints
                self.assertAlmostEqual(float(probe.stdout), 3.4, places=2)


class RecorderTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)

    def recorder(self, **kw):
        settings = RecorderSettings(root=self.dir, piece_gap_seconds=2.0, piece_pause_ms=500, **kw)
        rec = ChannelRecorder(settings, guild_id=1, channel_id=2)
        rec.start()
        return rec

    def chunks(self, rec):
        return [json.loads(line) for line in (rec.path / "chunks.jsonl").read_text().splitlines()]

    def test_pieces_gaps_and_late_packets(self):
        rec = self.recorder()
        t0 = 1000.0
        for i in range(50):  # 1 s of speech
            rec.submit(5, 960 * i, FRAME, t0 + i * 0.02)
        rec.submit(5, 960 * 55, FRAME, t0 + 1.1)  # 5 lost packets -> 100 ms silence filled
        rec.submit(5, 960 * 10, FRAME, t0 + 1.12)  # late: dropped
        rec.submit(5, 960 * 55, FRAME, t0 + 1.13)  # duplicate: dropped
        rec.submit(5, 10**6, FRAME, t0 + 30.0)  # new utterance after 29 s of silence
        rec.stop()
        [chunk] = self.chunks(rec)
        self.assertEqual(chunk["user_id"], 5)
        self.assertEqual(chunk["file"], "5/0001.ogg")
        # 50 + 5 filled + 1 packets, 0.5 s pause, 1 packet
        self.assertAlmostEqual(chunk["audio_seconds"], 56 * 0.02 + 0.5 + 0.02)
        self.assertEqual(chunk["pieces"], [[0.0, t0], [1.62, t0 + 30.0]])
        self.assertEqual(chunk["reason"], "stop")
        self.assertEqual(rec.counters["late_packets"], 2)
        self.assertEqual(rec.counters["filled_ms"], 100)
        session = json.loads((rec.path / "session.json").read_text())
        self.assertEqual((session["guild_id"], session["channel_id"]), (1, 2))
        self.assertIsNotNone(session["ended_at"])

    def test_chunk_rotates_at_utterance_boundary_per_user(self):
        rec = self.recorder(chunk_seconds=10.0)
        ts, wall = 0, 0.0
        for _ in range(3):  # three 6 s utterances, 10 s apart
            for i in range(300):
                rec.submit(5, ts + 960 * i, FRAME, wall + i * 0.02)
            rec.submit(6, ts, FRAME, wall)
            ts += 10**6
            wall += 10.0
        rec.stop()
        chunks = self.chunks(rec)
        by_user = {(c["user_id"], c["file"]): c for c in chunks}
        self.assertEqual(sorted(by_user), [(5, "5/0001.ogg"), (5, "5/0002.ogg"), (6, "6/0001.ogg")])
        self.assertEqual(by_user[5, "5/0001.ogg"]["reason"], "full")
        self.assertEqual(len(by_user[5, "5/0001.ogg"]["pieces"]), 2)  # 12.5 s: closed at the 3rd utterance
        self.assertEqual(len(by_user[6, "6/0001.ogg"]["pieces"]), 3)

    def test_idle_user_file_is_closed(self):
        rec = self.recorder(idle_close_seconds=0.2)
        rec.submit(5, 0, FRAME, 1.0)
        deadline = 50
        while not (rec.path / "chunks.jsonl").exists() and deadline:
            time.sleep(0.05)
            deadline -= 1
        rec.stop()
        [chunk] = self.chunks(rec)
        self.assertEqual(chunk["reason"], "idle")


class FakeDave:
    def __init__(self, fail=False):
        self.fail = fail
        self.ready = True
        self.calls = []

    def decrypt(self, user_id, media_type, packet):
        self.calls.append(user_id)
        if self.fail:
            raise ValueError("no key")
        return packet[::-1]  # "decrypted"


class ClientTests(unittest.TestCase):
    def make_client(self, dave=None, bots=()):
        client = RecordingVoiceClient.__new__(RecordingVoiceClient)
        client._ssrc_users = {}
        client._pending = {}
        client._dave_failed_users = set()
        client._listening = True
        client._connection = types.SimpleNamespace(
            mode=rtp.SUPPORTED_MODE, secret_key=list(KEY), dave_session=dave,
            dave_protocol_version=1 if dave else 0,
        )
        members = {uid: types.SimpleNamespace(bot=uid in bots) for uid in (5, 6, 99)}
        client.channel = types.SimpleNamespace(guild=types.SimpleNamespace(id=1, get_member=members.get))
        submitted = []
        client._recorder = types.SimpleNamespace(
            submit=lambda *args: submitted.append(args),
            counters={"transport_errors": 0},
        )
        counters = client._recorder.counters
        client._recorder.count = lambda name, n=1: counters.__setitem__(name, counters.get(name, 0) + n)
        return client, submitted

    def hook(self, client, op, **data):
        asyncio.run(client._ws_hook(None, {"op": op, "d": data}))

    def test_packets_wait_for_speaking_then_are_decrypted_per_user(self):
        dave = FakeDave()
        client, submitted = self.make_client(dave)
        client._on_packet(encrypt(FRAME, ssrc=7, timestamp=100))
        self.assertEqual(submitted, [])
        self.hook(client, 5, ssrc=7, user_id="5", speaking=1)
        client._on_packet(encrypt(FRAME, ssrc=7, timestamp=1060))
        self.assertEqual([(s[0], s[1], s[2]) for s in submitted], [(5, 100, FRAME[::-1]), (5, 1060, FRAME[::-1])])
        self.assertEqual(dave.calls, [5, 5])

    def test_silence_bots_errors_and_disconnect(self):
        dave = FakeDave()
        client, submitted = self.make_client(dave, bots={99})
        self.hook(client, 5, ssrc=7, user_id="5", speaking=1)
        self.hook(client, 5, ssrc=8, user_id="99", speaking=1)
        client._on_packet(encrypt(rtp.OPUS_SILENCE, ssrc=7))
        client._on_packet(encrypt(FRAME, ssrc=8))
        client._on_packet(b"\x81\xc9" + bytes(30))  # RTCP
        client._on_packet(encrypt(FRAME, ssrc=7, key=bytes(32)))
        self.assertEqual(submitted, [])
        self.assertEqual(client._recorder.counters["bot_packets"], 1)
        self.assertEqual(client._recorder.counters["transport_errors"], 1)
        self.hook(client, 13, user_id="5")
        self.assertNotIn(7, client._ssrc_users)
        self.hook(client, 2, ssrc=1)
        self.assertEqual(client._ssrc_users, {})

    def test_dave_failure_is_counted_and_logged_once(self):
        client, submitted = self.make_client(FakeDave(fail=True))
        self.hook(client, 5, ssrc=7, user_id="5", speaking=1)
        with self.assertLogs("tts_bot", "WARNING") as logs:
            client._on_packet(encrypt(FRAME, ssrc=7))
            client._on_packet(encrypt(FRAME, ssrc=7))
        self.assertEqual(len(logs.output), 1)
        self.assertIn("DAVE decrypt failed", logs.output[0])
        self.assertEqual(client._recorder.counters["dave_errors"], 2)
        self.assertEqual(submitted, [])

    def test_without_dave_payload_is_passed_through(self):
        client, submitted = self.make_client(None)
        self.hook(client, 5, ssrc=7, user_id="6", speaking=1)
        client._on_packet(encrypt(FRAME, ssrc=7))
        self.assertEqual(submitted[0][:3], (6, 960, FRAME))


if __name__ == "__main__":
    unittest.main()
