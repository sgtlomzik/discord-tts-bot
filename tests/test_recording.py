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
from ttsbot.recording.vad import GateSettings, OpusSpeechDetector, SpeechGate

KEY = bytes(range(32))
# A 20 ms CELT fullband Opus packet (TOC 0xFC: config 31, stereo, one frame).
FRAME = b"\xfc" + bytes(40)


def encrypt(payload: bytes, *, ssrc=7, sequence=1, timestamp=960, counter=5, extension: bytes | None = None,
            padding=0, key=KEY) -> bytes:
    """Build a packet the way Discord sends it (aead_xchacha20_poly1305_rtpsize)."""
    header = bytearray(12)
    header[0] = 0x80 | (0x10 if extension is not None else 0) | (0x20 if padding else 0)
    if padding:
        payload = payload + bytes([padding]) * padding
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

    def test_strips_rtp_padding(self):
        # Shapes seen live: a frame padded to a bigger packet, and a
        # padding-only bandwidth probe of 255 bytes.
        data = encrypt(FRAME, extension=b"\x10\xff\x00\x00", padding=74)
        self.assertEqual(rtp.decrypt_transport(data, rtp.SUPPORTED_MODE, KEY).payload, FRAME)
        self.assertEqual(rtp.decrypt_transport(encrypt(b"", padding=255), rtp.SUPPORTED_MODE, KEY).payload, b"")

    def test_bad_rtp_padding_is_rejected(self):
        header = bytearray(encrypt(FRAME, padding=1)[:12])
        nonce = struct.pack(">I", 5) + bytes(20)
        cipher = nacl.bindings.crypto_aead_xchacha20poly1305_ietf_encrypt(FRAME + b"\x63", bytes(header), nonce, KEY)
        with self.assertRaises(rtp.PacketError):
            rtp.decrypt_transport(bytes(header) + cipher + nonce[:4], rtp.SUPPORTED_MODE, KEY)

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
        settings = RecorderSettings(root=self.dir, pause_ms=500, **kw)
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


    def test_speech_filter_drops_breathing_and_cuts_long_gaps(self):
        # Packets carry their "speech" flag in the second byte.
        speech = lambda: (lambda packet: packet[1] == 1)
        rec = self.recorder(detector_factory=speech)
        voice, breath = b"\xfc\x01" + bytes(39), b"\xfc\x00" + bytes(39)
        pattern = [voice] * 50 + [breath] * 100 + [voice] * 50 + [breath] * 10 + [voice] * 20
        for i, packet in enumerate(pattern):
            rec.submit(5, 960 * i, packet, 1000.0 + i * 0.02)
        rec.stop()
        [chunk] = self.chunks(rec)
        # Run 1: 50 speech + 13 hangover packets. Run 2 starts at the 6th
        # speech packet with a 10-packet pre-roll (4 of them breathing),
        # keeps the 200 ms of breathing inside it and runs to the end. The
        # 1.7 s between the runs becomes a 0.5 s pause and a new piece.
        self.assertEqual(len(chunk["pieces"]), 2)
        self.assertAlmostEqual(chunk["pieces"][1][1], 1000.0 + 146 * 0.02, places=2)
        self.assertEqual(rec.counters["packets"], 230)
        self.assertEqual(rec.counters["heard_ms"], 230 * 20)
        self.assertEqual(rec.counters["written_ms"], (63 + 84) * 20)
        self.assertAlmostEqual(chunk["audio_seconds"], (63 + 84) * 0.02 + 0.5)


class SpeechGateTests(unittest.TestCase):
    def feed(self, gate, flags):
        out = []
        for i, flag in enumerate(flags):
            out.extend(gate.feed(i, flag))
        return out

    def test_needs_six_of_ten_to_start_and_brings_preroll(self):
        gate = SpeechGate(GateSettings())
        self.assertEqual(self.feed(gate, [False] * 20 + [True] * 5), [])  # 5 of 10: not yet
        self.assertEqual(gate.feed(25, True), list(range(16, 26)))  # 6th: pre-roll of 10
        self.assertTrue(gate.speaking)

    def test_isolated_clicks_never_start_and_hangover_ends_run(self):
        gate = SpeechGate(GateSettings())
        self.assertEqual(self.feed(gate, [True, False, False] * 20), [])
        gate.reset()
        kept = self.feed(gate, [True] * 10 + [False] * 30)
        self.assertEqual(len(kept), 10 + 13)  # ends once <= 2 of the last 15 are speech
        self.assertFalse(gate.speaking)
        gate.feed(0, True)
        gate.reset()
        self.assertEqual(self.feed(gate, [True] * 5), [])  # the reset forgot the context


class OpusDetectorTests(unittest.TestCase):
    def test_silent_frame_is_not_speech(self):
        detector = OpusSpeechDetector(3)
        self.assertFalse(detector.is_speech(rtp.OPUS_SILENCE))
        self.assertFalse(detector.is_speech(FRAME))


class FakeDave:
    def __init__(self, fail=False):
        self.fail = fail
        self.error = ValueError("no key")
        self.ready = True
        self.calls = []

    def decrypt(self, user_id, media_type, packet):
        self.calls.append(user_id)
        if self.fail:
            raise self.error
        return packet[::-1]  # "decrypted"


class ClientTests(unittest.TestCase):
    def make_client(self, dave=None, bots=()):
        client = RecordingVoiceClient.__new__(RecordingVoiceClient)
        client._ssrc_users = {}
        client._ignored_ssrcs = set()
        client._pending = {}
        client._dave_warned = set()
        client._dave_held = {}
        client._named = set()
        names = {}
        client._listening = True
        client._connection = types.SimpleNamespace(
            mode=rtp.SUPPORTED_MODE, secret_key=list(KEY), dave_session=dave,
            dave_protocol_version=1 if dave else 0,
        )
        members = {uid: types.SimpleNamespace(bot=uid in bots, display_name=f"n{uid}") for uid in (5, 6, 99)}
        client.channel = types.SimpleNamespace(guild=types.SimpleNamespace(id=1, get_member=members.get))
        submitted = []
        client._recorder = types.SimpleNamespace(
            submit=lambda *args, **kw: submitted.append(args),
            counters={"transport_errors": 0},
        )
        client._recorder.note_name = names.__setitem__
        client._recorder.names = names
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
        self.assertEqual(client._recorder.names, {5: "n5"})

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

    def test_missing_dave_key_is_counted_quietly(self):
        dave = FakeDave(fail=True)
        dave.error = ValueError("Failed to decrypt: DecryptionFailed(NoValidCryptorFound { media_type: AUDIO })")
        client, submitted = self.make_client(dave)
        self.hook(client, 5, ssrc=7, user_id="5", speaking=1)
        with self.assertNoLogs("tts_bot", "INFO"):
            for i in range(3):
                client._on_packet(encrypt(FRAME, ssrc=7, timestamp=960 * i))
        counters = client._recorder.counters
        self.assertEqual(counters["dave_nokey"], 3)
        self.assertNotIn("dave_errors", counters)
        self.assertEqual(submitted, [])

    def test_dave_failure_streaks_warn_once_per_user(self):
        # Unencrypted frames arrive interleaved with good ones for hours; a
        # success between them must not re-arm the warning.
        dave = FakeDave(fail=True)
        client, submitted = self.make_client(dave)
        self.hook(client, 5, ssrc=7, user_id="5", speaking=1)
        with self.assertLogs("tts_bot", "WARNING") as logs:
            for i in range(4):
                dave.fail = True
                client._on_packet(encrypt(FRAME, ssrc=7, timestamp=1920 * i))
                dave.fail = False
                client._on_packet(encrypt(FRAME, ssrc=7, timestamp=1920 * i + 960))
        self.assertEqual(len(logs.output), 1)
        self.assertEqual(client._recorder.counters["dave_errors"], 4)
        client._reset_streams()
        self.assertEqual(client._dave_warned, set())

    def test_padded_frames_reach_dave_and_padding_only_packets_are_dropped(self):
        dave = FakeDave()
        client, submitted = self.make_client(dave)
        self.hook(client, 5, ssrc=7, user_id="5", speaking=1)
        client._on_packet(encrypt(FRAME, ssrc=7, timestamp=960, padding=17))
        client._on_packet(encrypt(b"", ssrc=7, timestamp=1920, padding=255))
        client._on_packet(encrypt(rtp.OPUS_SILENCE, ssrc=7, timestamp=2880, padding=47))
        self.assertEqual([(s[1], s[2]) for s in submitted], [(960, FRAME[::-1])])
        self.assertEqual(dave.calls, [5])
        self.assertEqual(client._recorder.counters["padding_packets"], 1)

    def test_packets_held_until_the_key_arrives_are_recovered_in_order(self):
        dave = FakeDave(fail=True)
        client, submitted = self.make_client(dave)
        self.hook(client, 5, ssrc=7, user_id="5", speaking=1)
        with self.assertLogs("tts_bot", "WARNING"):
            for i in range(3):
                client._on_packet(encrypt(FRAME, ssrc=7, timestamp=960 * i))
        dave.fail = False
        client._on_packet(encrypt(FRAME, ssrc=7, timestamp=960 * 3))
        self.assertEqual([s[1] for s in submitted], [0, 960, 1920, 2880])
        counters = client._recorder.counters
        self.assertEqual((counters["dave_errors"], counters["dave_recovered"]), (3, 3))
        self.assertEqual(client._dave_held, {})

    def test_without_dave_payload_is_passed_through(self):
        client, submitted = self.make_client(None)
        self.hook(client, 5, ssrc=7, user_id="6", speaking=1)
        client._on_packet(encrypt(FRAME, ssrc=7))
        self.assertEqual(submitted[0][:3], (6, 960, FRAME))


if __name__ == "__main__":
    unittest.main()
