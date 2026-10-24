"""One bad file must not stall transcription or burn the Groq quota.

A truncated chunks.jsonl line (a crash mid-append) raised in every scan,
so nothing was transcribed any more; an error after Groq had answered
retried the same chunk forever and re-sent its audio each time; a revoked
key marked the whole backlog as failed for good.
"""

from __future__ import annotations

import datetime as dt
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx

from test_transcription import FRAME, FakeTranscriber, groq_json
from ttsbot.recording.groq import AudioBudget, GroqTranscriber, TranscriptionError
from ttsbot.recording.recorder import ChannelRecorder, RecorderSettings
from ttsbot.recording.worker import TranscriptionWorker, error_path

UTC = dt.timezone.utc


class WorkerRobustnessTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)

    def record_session(self, users=(5,), guild_id=1):
        rec = ChannelRecorder(RecorderSettings(root=self.root), guild_id=guild_id, channel_id=2)
        rec.start()
        for user in users:
            for i in range(100):
                rec.submit(user, 960 * i, FRAME, 1000.0 + user + i * 0.02)
        rec.stop()
        return rec

    def worker(self, transcriber, **kw):
        budget = AudioBudget(self.root / "ledger.jsonl", per_hour=7200, per_day=28800, requests_per_minute=10**6)
        budget.block = lambda seconds, now=None: None

        async def name_for(guild_id, user_id):
            return f"user{user_id}"

        return TranscriptionWorker(self.root, transcriber, budget, name_for=name_for, tz=UTC, **kw)

    async def test_a_truncated_manifest_line_is_skipped(self):
        rec = self.record_session(users=(5, 6))
        with (rec.path / "chunks.jsonl").open("a", encoding="utf-8") as manifest:
            manifest.write('{"file": "6/00')
        worker = self.worker(FakeTranscriber([]))
        with self.assertLogs("tts_bot", "WARNING"):
            pending, _ = worker.scan()
        self.assertEqual(len(pending), 2)

    async def test_a_broken_session_does_not_stop_the_others(self):
        broken = self.record_session(guild_id=1)
        self.record_session(guild_id=3)
        (broken.path / "session.json").write_text("{", encoding="utf-8")
        worker = self.worker(FakeTranscriber([]))
        with self.assertLogs("tts_bot", "WARNING"):
            pending, _ = worker.scan()
        self.assertEqual([chunk.session.parent.name for chunk in pending], ["3"])

    async def test_missing_audio_is_given_up_without_a_request(self):
        rec = self.record_session()
        (rec.path / "5" / "0001.ogg").unlink()
        fake = FakeTranscriber([])
        worker = self.worker(fake, max_attempts=3)
        with self.assertLogs("tts_bot", "ERROR"):
            await worker.step()
        self.assertEqual(fake.sent, [])
        self.assertEqual(json.loads(error_path(rec.path, "5/0001.ogg").read_text())["attempts"], 3)
        self.assertEqual(worker.scan()[0], [])

    async def test_an_error_after_groq_answered_is_bounded(self):
        rec = self.record_session()
        fake = FakeTranscriber([groq_json((0, 1, "текст"))] * 3)
        worker = self.worker(fake, max_attempts=3)
        with mock.patch("ttsbot.recording.worker.chunk_result", side_effect=KeyError("segments")):
            with self.assertLogs("tts_bot", "ERROR"):
                for _ in range(5):
                    await worker.step()
        self.assertEqual(len(fake.sent), 3)  # given up after max_attempts, not forever
        self.assertEqual(json.loads(error_path(rec.path, "5/0001.ogg").read_text())["attempts"], 3)

    async def test_an_unreadable_error_file_is_retried(self):
        rec = self.record_session()
        error_path(rec.path, "5/0001.ogg").write_text("{", encoding="utf-8")
        worker = self.worker(FakeTranscriber([]))
        self.assertEqual(len(worker.scan()[0]), 1)


class ActiveSessionNamesTests(unittest.IsolatedAsyncioTestCase):
    async def test_rebuild_leaves_names_to_the_recorder_while_it_records(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        rec = ChannelRecorder(RecorderSettings(root=root), guild_id=1, channel_id=2)
        rec.start()
        self.addCleanup(rec.stop)
        rec.submit(5, 0, FRAME, 1000.0)
        budget = AudioBudget(root / "ledger.jsonl", per_hour=7200, per_day=28800)

        async def name_for(guild_id, user_id):
            return "from-api"

        worker = TranscriptionWorker(root, FakeTranscriber([]), budget, name_for=name_for, tz=UTC)
        (rec.path / "tts.jsonl").write_text('{"user_id": 9, "at": 1.0, "text": "x"}\n', encoding="utf-8")
        await worker.rebuild(rec.path)
        self.assertFalse((rec.path / "names.json").exists())  # the recorder's writer owns it


class GroqAuthTests(unittest.IsolatedAsyncioTestCase):
    async def test_rejected_key_pauses_instead_of_failing_the_file(self):
        audio = Path(tempfile.mkdtemp()) / "a.ogg"
        self.addCleanup(shutil.rmtree, audio.parent)
        audio.write_bytes(b"OggS")
        for status in (401, 403):
            client = GroqTranscriber("key", model="m", language="ru",
                                     transport=httpx.MockTransport(lambda r, s=status: httpx.Response(s, text="no")))
            with self.assertRaises(TranscriptionError) as ctx:
                await client.transcribe(audio)
            await client.aclose()
            self.assertTrue(ctx.exception.retry)
            self.assertGreaterEqual(ctx.exception.wait, 600)


if __name__ == "__main__":
    unittest.main()
