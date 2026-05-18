import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from voice_receiver.config import VoiceRecorderConfig
from voice_receiver.metrics import RecorderMetrics
from voice_receiver.models import NormalizedPcmFrame, RawAudioFrame, SpeechSegment
from voice_receiver.receiver import VoiceRecorderSession
from voice_receiver.segmenter import RingBuffer, RollingBoolWindow, UserSegmenter
from voice_receiver.storage import SegmentStorage


UTC = timezone.utc


def frame_at(offset_ms: int, value: bytes = b"\x01\x00") -> NormalizedPcmFrame:
    return NormalizedPcmFrame(
        user_id=10,
        timestamp=datetime(2026, 5, 18, tzinfo=UTC) + timedelta(milliseconds=offset_ms),
        pcm=value * 320,
    )


class ScriptedVad:
    def __init__(self, answers):
        self.answers = list(answers)

    def is_speech(self, pcm: bytes) -> bool:
        return self.answers.pop(0)


class VoiceReceiverCoreTests(unittest.TestCase):
    def test_ring_buffer_keeps_only_configured_duration(self):
        buf = RingBuffer(max_frames=2)

        buf.append(frame_at(0))
        buf.append(frame_at(20))
        buf.append(frame_at(40))

        self.assertEqual([item.timestamp for item in buf.frames()], [frame_at(20).timestamp, frame_at(40).timestamp])

    def test_rolling_bool_window_counts_recent_speech(self):
        window = RollingBoolWindow(max_frames=3)

        for item in [True, False, True, True]:
            window.append(item)

        self.assertEqual(window.speech_count, 2)

    def test_segmenter_starts_and_closes_after_silence(self):
        config = VoiceRecorderConfig(
            pre_roll_ms=40,
            start_window_ms=60,
            start_min_speech_frames=2,
            end_silence_ms=40,
            min_segment_ms=40,
            post_roll_ms=20,
        )
        segmenter = UserSegmenter(config, ScriptedVad([False, True, True, True, False, False]))

        saved = []
        for idx in range(6):
            saved.extend(segmenter.process_frame(frame_at(idx * 20)))
        saved.extend(segmenter.flush())

        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0].duration_ms, 100)

    def test_segmenter_discards_short_segment(self):
        config = VoiceRecorderConfig(
            pre_roll_ms=0,
            start_window_ms=40,
            start_min_speech_frames=1,
            end_silence_ms=20,
            min_segment_ms=100,
            post_roll_ms=0,
        )
        segmenter = UserSegmenter(config, ScriptedVad([True, False]))

        discarded = []
        for idx in range(2):
            discarded.extend(segmenter.process_frame(frame_at(idx * 20)))

        self.assertEqual(discarded, [])
        self.assertEqual(segmenter.discarded_segments, 1)

    def test_segmenter_force_closes_at_max_duration(self):
        config = VoiceRecorderConfig(
            pre_roll_ms=0,
            start_window_ms=20,
            start_min_speech_frames=1,
            end_silence_ms=200,
            min_segment_ms=20,
            max_segment_ms=60,
            post_roll_ms=0,
        )
        segmenter = UserSegmenter(config, ScriptedVad([True, True, True]))

        saved = []
        for idx in range(3):
            saved.extend(segmenter.process_frame(frame_at(idx * 20)))
        saved.extend(segmenter.flush())

        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0].duration_ms, 60)

    def test_segmenter_merges_segments_inside_gap(self):
        config = VoiceRecorderConfig(
            pre_roll_ms=0,
            start_window_ms=20,
            start_min_speech_frames=1,
            end_silence_ms=20,
            min_segment_ms=20,
            merge_gap_ms=40,
            post_roll_ms=0,
        )
        segmenter = UserSegmenter(config, ScriptedVad([True, False, True, False]))

        saved = []
        for idx in range(4):
            saved.extend(segmenter.process_frame(frame_at(idx * 20)))
        saved.extend(segmenter.flush())

        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0].duration_ms, 80)


class VoiceReceiverStorageTests(unittest.IsolatedAsyncioTestCase):
    async def test_storage_writes_metadata_and_stable_path(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config = VoiceRecorderConfig(storage_root=Path(tmp_dir))
            storage = SegmentStorage(config, encoder=lambda pcm, target: target.write_bytes(b"flac"))
            segment = SpeechSegment(
                user_id=789,
                username_snapshot="UserName",
                start_utc=datetime(2026, 5, 18, 18, 32, 10, 420000, tzinfo=UTC),
                end_utc=datetime(2026, 5, 18, 18, 32, 14, 880000, tzinfo=UTC),
                pcm=b"\x00\x00" * 320,
            )

            session = await storage.start_session(guild_id=123, channel_id=456, session_id="abc")
            metadata_path = await storage.save_segment(session, segment)

            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            self.assertEqual(metadata["file"], "2026-05-18T18-32-10.420Z__seg_000001.flac")
            self.assertEqual(metadata["duration_ms"], 4460)
            self.assertTrue((metadata_path.parent / metadata["file"]).exists())
            self.assertTrue((session.path / "session.jsonl").exists())

    async def test_storage_error_does_not_raise(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config = VoiceRecorderConfig(storage_root=Path(tmp_dir))
            storage = SegmentStorage(config, encoder=lambda pcm, target: (_ for _ in ()).throw(OSError("boom")))
            segment = SpeechSegment(
                user_id=789,
                username_snapshot="UserName",
                start_utc=datetime(2026, 5, 18, 18, 32, 10, tzinfo=UTC),
                end_utc=datetime(2026, 5, 18, 18, 32, 11, tzinfo=UTC),
                pcm=b"\x00\x00" * 320,
            )
            session = await storage.start_session(guild_id=123, channel_id=456, session_id="abc")

            metadata_path = await storage.save_segment(session, segment)
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))

            self.assertEqual(metadata["status"], "storage_error")
            self.assertEqual(session.metrics.storage_errors, 1)


class VoiceRecorderSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_handle_frame_drops_when_queue_is_full(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config = VoiceRecorderConfig(storage_root=Path(tmp_dir), frame_queue_maxsize=1)
            session = VoiceRecorderSession(config=config, vad_factory=lambda: ScriptedVad([False]))
            await session.start(guild_id=1, channel_id=2, session_id="abc")

            raw = RawAudioFrame(
                user_id=1,
                username_snapshot="u",
                pcm=b"\x00\x00" * 320,
                sample_rate=16000,
                channels=1,
            )
            accepted = session.handle_frame(raw)
            dropped = session.handle_frame(raw)
            await asyncio.sleep(0.01)
            await session.stop()

            self.assertTrue(accepted)
            self.assertFalse(dropped)
            self.assertEqual(session.metrics.frames_received, 2)
            self.assertEqual(session.metrics.frames_dropped, 1)
            events = [
                json.loads(line)
                for line in (session.storage_session.path / "session.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertIn("queue_overflow", [event["type"] for event in events])

    async def test_stop_flushes_pipeline_and_updates_session(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config = VoiceRecorderConfig(
                storage_root=Path(tmp_dir),
                pre_roll_ms=0,
                start_window_ms=20,
                start_min_speech_frames=1,
                end_silence_ms=20,
                min_segment_ms=20,
                post_roll_ms=0,
            )
            storage = SegmentStorage(config, encoder=lambda pcm, target: target.write_bytes(b"flac"))
            session = VoiceRecorderSession(
                config=config,
                vad_factory=lambda: ScriptedVad([True, False]),
                storage=storage,
            )
            await session.start(guild_id=123, channel_id=456, session_id="abc")

            session.handle_frame(
                RawAudioFrame(
                    user_id=1,
                    username_snapshot="u",
                    pcm=b"\x00\x00" * 320,
                    sample_rate=16000,
                    channels=1,
                )
            )
            session.handle_frame(
                RawAudioFrame(
                    user_id=1,
                    username_snapshot="u",
                    pcm=b"\x00\x00" * 320,
                    sample_rate=16000,
                    channels=1,
                )
            )
            await asyncio.sleep(0.05)
            await session.stop()

            summary = json.loads((session.storage_session.path / "session.json").read_text(encoding="utf-8"))
            self.assertIsNotNone(summary["ended_at_utc"])
            self.assertEqual(summary["stats"]["segments_saved"], 1)
            events = [
                json.loads(line)
                for line in (session.storage_session.path / "session.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertIn("user_seen", [event["type"] for event in events])


if __name__ == "__main__":
    unittest.main()
