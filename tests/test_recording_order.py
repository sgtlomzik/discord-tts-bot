"""Received speech is kept when packets arrive out of order or per SSRC.

Every packet whose RTP timestamp was not newer than the last one written
for that *user* was dropped as "late". Packets the VPN delivered out of
order were lost, and a user with two RTP streams (microphone and Go-Live
sound) lost up to half of them: 15,992 packets (5.3 min of speech) in one
session.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from test_recording import FRAME, KEY, encrypt
from ttsbot.recording import rtp
from ttsbot.recording.client import RecordingVoiceClient
from ttsbot.recording.recorder import ChannelRecorder, RecorderSettings


class ReorderTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)

    def recorder(self):
        rec = ChannelRecorder(RecorderSettings(root=self.dir, pause_ms=500), guild_id=1, channel_id=2)
        rec.start()
        return rec

    def chunks(self, rec):
        return [json.loads(line) for line in (rec.path / "chunks.jsonl").read_text().splitlines()]

    def test_swapped_neighbours_are_both_kept(self):
        rec = self.recorder()
        order = list(range(40))
        for i in range(0, 40, 4):
            order[i], order[i + 1] = order[i + 1], order[i]  # the VPN swaps pairs
        for i in order:
            rec.submit(5, 960 * i, FRAME, 1000.0 + i * 0.02)
        rec.stop()
        self.assertEqual(rec.counters["late_packets"], 0)
        self.assertEqual(rec.counters["written_ms"], 40 * 20)
        [chunk] = self.chunks(rec)
        self.assertAlmostEqual(chunk["audio_seconds"], 40 * 0.02)

    def test_duplicates_and_very_late_packets_are_still_dropped(self):
        rec = self.recorder()
        for i in range(30):
            rec.submit(5, 960 * i, FRAME, 1000.0 + i * 0.02)
        rec.submit(5, 960 * 29, FRAME, 1000.6)  # duplicate
        rec.submit(5, 960 * 2, FRAME, 1000.62)  # far behind the window
        rec.stop()
        self.assertEqual(rec.counters["late_packets"], 2)
        self.assertEqual(rec.counters["written_ms"], 30 * 20)

    def test_a_clock_that_jumps_back_after_a_pause_keeps_being_recorded(self):
        # A client that restarts its audio (mute/unmute) may keep the SSRC
        # and start a new RTP clock; that is a new utterance, not late audio.
        rec = self.recorder()
        for i in range(20):
            rec.submit(5, 1_000_000 + 960 * i, FRAME, 1000.0 + i * 0.02)
        for i in range(20):
            rec.submit(5, 960 * i, FRAME, 1002.0 + i * 0.02)
        rec.stop()
        self.assertEqual(rec.counters["late_packets"], 0)
        self.assertEqual(rec.counters["written_ms"], 40 * 20)

    def test_a_straggler_after_a_short_gap_is_still_late(self):
        rec = self.recorder()
        for i in range(20):
            rec.submit(5, 960 * i, FRAME, 1000.0 + i * 0.02)
        rec.submit(5, 960 * 10, FRAME, 1000.7)  # jitter, then an old packet
        for i in range(20, 30):
            rec.submit(5, 960 * i, FRAME, 1000.72 + (i - 20) * 0.02)
        rec.stop()
        self.assertEqual(rec.counters["late_packets"], 1)
        self.assertEqual(rec.counters["written_ms"], 30 * 20)

    def test_a_clock_that_jumps_by_half_its_range_keeps_being_recorded(self):
        rec = self.recorder()
        for i in range(20):
            rec.submit(5, 960 * i, FRAME, 1000.0 + i * 0.02)
        for i in range(20):  # 2**31 or more ahead reads as "behind"
            rec.submit(5, 960 * 20 + 2**31 + 960 * i, FRAME, 1003.0 + i * 0.02)
        rec.stop()
        self.assertEqual(rec.counters["late_packets"], 0)
        self.assertEqual(rec.counters["written_ms"], 40 * 20)

    def test_two_streams_of_one_user_are_both_kept(self):
        rec = self.recorder()
        for i in range(50):  # two RTP clocks, interleaved
            rec.submit(5, 960 * i, FRAME, 1000.0 + i * 0.02, ssrc=7)
            rec.submit(5, 3_000_000 + 960 * i, FRAME, 1000.0 + i * 0.02, ssrc=8)
        rec.stop()
        self.assertEqual(rec.counters["late_packets"], 0)
        self.assertEqual(rec.counters["written_ms"], 100 * 20)
        files = sorted(chunk["file"] for chunk in self.chunks(rec))
        self.assertEqual(files, ["5/0001.ogg", "5/0002.ogg"])  # one numbering per user

    def test_names_and_tts_notes_are_written_by_the_writer_thread(self):
        rec = self.recorder()
        writers = []
        original = rec._write_name

        def spy(user_id, name):
            writers.append(threading.current_thread().name)
            original(user_id, name)

        rec._write_name = spy
        rec.note_name(5, "Аня")
        rec.note_text(5, "привет", 1005.0)
        rec.stop()
        self.assertEqual(writers, [rec._thread.name])
        self.assertEqual(json.loads((rec.path / "names.json").read_text(encoding="utf-8")), {"5": "Аня"})
        self.assertIn("привет", (rec.path / "tts.jsonl").read_text(encoding="utf-8"))


class SoundshareTests(unittest.TestCase):
    def make_client(self):
        client = RecordingVoiceClient.__new__(RecordingVoiceClient)
        client._ssrc_users = {}
        client._ignored_ssrcs = set()
        client._pending = {}
        client._dave_warned = set()
        client._dave_held = {}
        client._named = set()
        client._listening = True
        client._connection = types.SimpleNamespace(
            mode=rtp.SUPPORTED_MODE, secret_key=list(KEY), dave_session=None, dave_protocol_version=0,
        )
        members = {5: types.SimpleNamespace(bot=False, display_name="n5")}
        client.channel = types.SimpleNamespace(guild=types.SimpleNamespace(id=1, get_member=members.get))
        submitted = []
        counters = {}
        client._recorder = types.SimpleNamespace(
            submit=lambda *args, **kw: submitted.append((args, kw)),
            counters=counters,
            count=lambda name, n=1: counters.__setitem__(name, counters.get(name, 0) + n),
            note_name=MagicMock(),
        )
        return client, submitted

    def hook(self, client, **data):
        asyncio.run(client._ws_hook(None, {"op": 5, "d": data}))

    def test_go_live_sound_is_not_recorded_but_the_microphone_is(self):
        client, submitted = self.make_client()
        self.hook(client, ssrc=7, user_id="5", speaking=1)  # microphone
        self.hook(client, ssrc=8, user_id="5", speaking=2)  # Go-Live sound
        client._on_packet(encrypt(FRAME, ssrc=7, timestamp=960))
        client._on_packet(encrypt(FRAME, ssrc=8, timestamp=960))
        self.assertEqual(len(submitted), 1)
        self.assertEqual(submitted[0][1].get("ssrc"), 7)
        self.assertEqual(client._recorder.counters["soundshare_packets"], 1)


if __name__ == "__main__":
    unittest.main()
