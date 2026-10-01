"""Transcription: Groq client, audio budget, transcript assembly, worker."""

import asyncio
import datetime as dt
import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import httpx

from ttsbot.recording import recorder as recorder_module
from ttsbot.recording.groq import AudioBudget, GroqTranscriber, Transcription, TranscriptionError, parse_duration
from ttsbot.recording.ogg import OggOpusWriter, ogg_duration
from ttsbot.recording.recorder import ChannelRecorder, RecorderSettings, pieces_path
from ttsbot.recording.transcript import build_session_transcript, chunk_result, drop_reason, wall_time
from ttsbot.recording.worker import TranscriptionWorker, error_path

FRAME = b"\xfc" + bytes(40)
UTC = dt.timezone.utc
DAY = 86400
CHUNK_END = 1005.0 + 2.0  # the recorded test chunk ends about here (a 1970 timestamp)


def groq_json(*segments):
    return {"language": "Russian", "text": " ".join(t for _, _, t in segments), "segments": [
        {"start": a, "end": b, "text": " " + t, "no_speech_prob": 0.01, "avg_logprob": -0.3,
         "compression_ratio": 1.2} for a, b, t in segments]}


class ParseTests(unittest.TestCase):
    def test_durations(self):
        self.assertEqual(parse_duration("4.5s"), 4.5)
        self.assertEqual(parse_duration("1m2.5s"), 62.5)
        self.assertEqual(parse_duration("2h0m3s"), 7203)
        self.assertAlmostEqual(parse_duration("120ms"), 0.12)
        self.assertIsNone(parse_duration(""))


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        self.ledger = self.dir / "ledger.jsonl"

    def budget(self, **kw):
        kw = {"per_hour": 7200, "per_day": 28800, "min_billed": 10, **kw}
        return AudioBudget(self.ledger, **kw)

    def test_bucket_from_headers_refills_two_seconds_per_second(self):
        b = self.budget()
        now = 1_000_000.0
        b.record(600, remaining=100, now=now)
        self.assertAlmostEqual(b.wait_for(200, now=now + 3), (200 - 106) / 2)
        self.assertEqual(b.wait_for(200, now=now + 60), 0)

    def test_min_billed_and_request_spacing(self):
        b = self.budget()
        now = time.time()
        b.record(0.5, remaining=7190, now=now)
        self.assertEqual(json.loads(self.ledger.read_text().splitlines()[0])[1], 10)
        self.assertAlmostEqual(b.wait_for(1, now=now + 1), 2.0)  # 20 per minute

    def test_ledger_hour_and_day_after_restart(self):
        now = time.time()
        lines = [[now - 90000, 5000], [now - 3000, 3000], [now - 1000, 4000], [now - 50000, 20000]]
        self.ledger.write_text("".join(json.dumps(l) + "\n" for l in lines))
        b = self.budget()
        self.assertEqual(len(self.ledger.read_text().splitlines()), 3)  # >1 day dropped
        # Hour: 7000 used; 300 more fits once the entry from 3000 s ago leaves.
        self.assertAlmostEqual(b.wait_for(300, now=now), 600, delta=1)
        # Day: 27000 used; 2000 more waits for the 20000 s entry to leave.
        self.assertAlmostEqual(b.wait_for(2000, now=now + 700), 86400 - 50000 - 700, delta=1)

    def test_block(self):
        b = self.budget()
        b.block(30, now=100.0)
        self.assertAlmostEqual(b.wait_for(5, now=110.0), 20)


class GroqClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        self.audio = self.dir / "a.ogg"
        self.audio.write_bytes(b"OggS...")
        self.requests = []

    async def client(self, response):
        def handler(request):
            request.read()
            self.requests.append(request)
            return response

        c = GroqTranscriber("key", model="whisper-large-v3", language="ru", prompt="Мида",
                            transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(c.aclose)
        return c

    async def test_success_reads_budget_headers(self):
        c = await self.client(httpx.Response(200, json=groq_json((0, 1, "привет")), headers={
            "x-ratelimit-remaining-audio-seconds": "7191", "x-ratelimit-reset-audio-seconds": "4.5s"}))
        result = await c.transcribe(self.audio)
        self.assertEqual(result.data["language"], "Russian")
        self.assertEqual((result.remaining_audio_seconds, result.reset_audio_seconds), (7191, 4.5))
        body = self.requests[0].read().decode("utf-8", "replace")
        for field in ("whisper-large-v3", "verbose_json", 'name="language"', "Мида", "Bearer key"):
            self.assertIn(field, body + self.requests[0].headers["authorization"])

    async def test_429_is_retried_after_the_given_time(self):
        c = await self.client(httpx.Response(429, text="slow down", headers={"retry-after": "37"}))
        with self.assertRaises(TranscriptionError) as ctx:
            await c.transcribe(self.audio)
        self.assertTrue(ctx.exception.retry)
        self.assertEqual(ctx.exception.wait, 37)

    async def test_400_is_final(self):
        c = await self.client(httpx.Response(400, text="bad file"))
        with self.assertRaises(TranscriptionError) as ctx:
            await c.transcribe(self.audio)
        self.assertFalse(ctx.exception.retry)


class TranscriptTests(unittest.TestCase):
    def test_wall_time_follows_pieces(self):
        pieces = [[0.0, 1000.0], [5.5, 2000.0]]
        self.assertEqual(wall_time(pieces, 2.0), 1002.0)
        self.assertEqual(wall_time(pieces, 6.0), 2000.5)
        self.assertEqual(wall_time(pieces, 5.2), 1005.2)  # inside the pause: still piece 1

    def test_drop_reasons(self):
        ok = {"text": "привет", "no_speech_prob": 0.1, "avg_logprob": -0.3, "compression_ratio": 1.1}
        self.assertIsNone(drop_reason(ok))
        self.assertEqual(drop_reason({**ok, "text": " Субтитры сделал DimaTorzok"}), "hallucination")
        self.assertEqual(drop_reason({**ok, "text": "Продолжение следует..."}), "hallucination")
        self.assertEqual(drop_reason({**ok, "no_speech_prob": 0.9, "avg_logprob": -1.5}), "no_speech")
        self.assertEqual(drop_reason({**ok, "compression_ratio": 3.0}), "repetition")

    def test_session_transcript_merges_speakers_and_tts(self):
        session = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, session)
        (session / "session.json").write_text(json.dumps(
            {"guild_id": 1, "channel_id": 2, "started_at": 3600.0, "ended_at": 7200.0}))
        entries = [
            {"user_id": 5, "file": "5/0001.ogg", "audio_seconds": 9, "started_at": 3600.0,
             "pieces": [[0.0, 3600.0], [4.5, 3700.0]]},
            {"user_id": 6, "file": "6/0001.ogg", "audio_seconds": 3, "started_at": 3650.0,
             "pieces": [[0.0, 3650.0]]},
            {"user_id": 7, "file": "7/0001.ogg", "audio_seconds": 3, "started_at": 3660.0,
             "pieces": [[0.0, 3660.0]]},
        ]
        (session / "chunks.jsonl").write_text("".join(json.dumps(e) + "\n" for e in entries))
        results = {
            "5/0001.ogg": groq_json((0, 2, "Привет."), (2.5, 4, "Как дела?"), (4.5, 6, "Я вернулся.")),
            "6/0001.ogg": groq_json((0, 1, "Норм."), (1.5, 2.5, "Субтитры сделал DimaTorzok")),
        }
        for entry in entries[:2]:
            (session / entry["user_id"].__str__()).mkdir()
            stored = chunk_result(entry, results[entry["file"]], model="m", took=0.1)
            (session / entry["file"]).with_suffix(".json").write_text(json.dumps(stored))
        (session / "tts.jsonl").write_text(json.dumps({"user_id": 8, "at": 3655.0, "text": "го"}) + "\n")
        (session / "names.json").write_text(json.dumps({"5": "Аня"}))  # noted while recording
        summary = build_session_transcript(session, {5: "не это", 6: "Боб", 8: "Вика"}.get, UTC)
        self.assertEqual(summary, {"lines": 4, "pending": 1, "files": 3})
        text = (session / "transcript.md").read_text(encoding="utf-8")
        self.assertIn("Ещё не расшифровано файлов: 1", text)
        body = text.split("\n\n", 2)[-1].strip().splitlines()
        self.assertEqual(body, [
            "[01:00:00] Аня: Привет. Как дела?",
            "[01:00:50] Боб: Норм.",
            "[01:00:55] Вика (TTS): го",
            "[01:01:40] Аня: Я вернулся.",
        ])
        jsonl = [json.loads(l) for l in (session / "transcript.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(jsonl[0]["name"], "Аня")


class FakeTranscriber:
    model = "fake"

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.sent = []

    async def transcribe(self, path):
        self.sent.append(path.name)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return Transcription(data=outcome, took=0.2, remaining_audio_seconds=7000, reset_audio_seconds=1)


class WorkerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)

    def record_session(self, users=(5,), end=True):
        rec = ChannelRecorder(RecorderSettings(root=self.root), guild_id=1, channel_id=2)
        rec.start()
        for user in users:
            for i in range(100):
                rec.submit(user, 960 * i, FRAME, 1000.0 + user + i * 0.02)
        if end:
            rec.stop()
        return rec

    def worker(self, transcriber, **kw):
        budget = AudioBudget(self.root / "ledger.jsonl", per_hour=7200, per_day=28800, requests_per_minute=10**6)
        async def name_for(guild_id, user_id):
            return f"user{user_id}" if user_id != 404 else None

        return TranscriptionWorker(self.root, transcriber, budget, name_for=name_for, tz=UTC, **kw)

    async def test_transcribes_oldest_first_and_writes_transcript(self):
        rec = self.record_session(users=(6, 5))
        fake = FakeTranscriber([groq_json((0, 1, "первый")), groq_json((0, 1, "второй"))])
        worker = self.worker(fake)
        self.assertEqual(await worker.step(), 0.0)
        self.assertEqual(await worker.step(), 0.0)
        self.assertEqual(await worker.step(), worker.scan_seconds)  # nothing left
        self.assertEqual(fake.sent, ["0001.ogg", "0001.ogg"])
        stored = json.loads((rec.path / "5/0001.json").read_text(encoding="utf-8"))
        self.assertEqual((stored["text"], stored["segments"][0]["at"]), ("первый", 1005.0))
        text = (rec.path / "transcript.md").read_text(encoding="utf-8")
        self.assertIn("user5: первый", text)
        self.assertIn("user6: второй", text)
        self.assertEqual(await worker.step(), worker.scan_seconds)
        self.assertIn(rec.path, worker._finished)
        names = json.loads((rec.path / "names.json").read_text(encoding="utf-8"))
        self.assertEqual(names, {"5": "user5", "6": "user6"})

    async def test_rejected_file_is_retried_then_given_up(self):
        rec = self.record_session()
        fake = FakeTranscriber([TranscriptionError("HTTP 400", retry=False)] * 3)
        worker = self.worker(fake, max_attempts=3)
        worker.budget.block = lambda seconds, now=None: None
        with self.assertLogs("tts_bot", "ERROR"):
            for _ in range(3):
                await worker.step()
        self.assertEqual(json.loads(error_path(rec.path, "5/0001.ogg").read_text())["attempts"], 3)
        self.assertEqual(worker.scan(), ([], [rec.path]))
        self.assertEqual(len(fake.sent), 3)

    async def test_retryable_error_blocks_the_budget(self):
        self.record_session()
        worker = self.worker(FakeTranscriber([TranscriptionError("HTTP 429", retry=True, wait=40)]))
        with self.assertLogs("tts_bot", "WARNING"):
            await worker.step()
        self.assertEqual(len(worker.scan()[0]), 1)
        self.assertGreater(await worker.step(), 29)  # waits (capped at scan_seconds)

    async def test_crashed_session_is_recovered(self):
        # What a crash leaves: an open file, its pieces, no manifest line.
        session = self.root / "1" / "20260101-000000Z-2"
        (session / "5").mkdir(parents=True)
        (session / "session.json").write_text(json.dumps(
            {"guild_id": 1, "channel_id": 2, "started_at": 1000.0, "ended_at": None}))
        writer = OggOpusWriter(session / "5" / "0001.ogg", 1)
        self.addCleanup(writer._file.close)
        for _ in range(100):
            writer.write(FRAME)
        pieces_path(writer.path).write_text(json.dumps([0.0, 1005.0]) + "\n")
        worker = self.worker(FakeTranscriber([groq_json((0, 1, "спасён"))]))
        [chunk], _ = worker.scan()
        self.assertEqual(chunk.entry["reason"], "recovered")
        self.assertEqual(chunk.entry["pieces"], [[0.0, 1005.0]])
        self.assertAlmostEqual(chunk.entry["audio_seconds"], 2.0)
        self.assertIsNotNone(json.loads((session / "session.json").read_text())["ended_at"])
        self.assertFalse(pieces_path(writer.path).exists())

    async def test_active_session_is_left_alone(self):
        rec = self.record_session(end=False)
        self.addCleanup(rec.stop)
        worker = self.worker(FakeTranscriber([]))
        self.assertEqual(worker.scan(), ([], []))
        self.assertIsNone(json.loads((rec.path / "session.json").read_text())["ended_at"])


    # -- audio retention: old ogg files go once transcribed, text stays --

    async def transcribed_session(self, **kw):
        rec = self.record_session()
        worker = self.worker(FakeTranscriber([groq_json((0, 1, "слово"))]), **kw)
        await worker.step()
        self.ogg = rec.path / "5" / "0001.ogg"
        self.assertTrue((rec.path / "5" / "0001.json").exists())
        self.assertTrue(self.ogg.exists())
        return rec, worker

    async def test_old_transcribed_audio_is_deleted_and_text_kept(self):
        rec, worker = await self.transcribed_session(keep_audio_days=90)
        size = self.ogg.stat().st_size
        sidecar = pieces_path(self.ogg)
        sidecar.write_text("[0.0, 1005.0]\n")
        self.assertEqual(worker.prune_audio(now=CHUNK_END + 91 * DAY), (1, size))
        self.assertFalse(self.ogg.exists())
        self.assertFalse(sidecar.exists())
        for kept in ("5/0001.json", "chunks.jsonl", "session.json", "transcript.md"):
            self.assertTrue((rec.path / kept).exists(), kept)
        self.assertIn("слово", (rec.path / "transcript.md").read_text(encoding="utf-8"))

    async def test_audio_younger_than_the_limit_is_kept(self):
        _, worker = await self.transcribed_session(keep_audio_days=90)
        self.assertEqual(worker.prune_audio(now=CHUNK_END + 89 * DAY), (0, 0))
        self.assertTrue(self.ogg.exists())

    async def test_untranscribed_audio_is_never_deleted(self):
        rec = self.record_session()
        worker = self.worker(FakeTranscriber([]), keep_audio_days=90)
        self.assertEqual(worker.prune_audio(now=CHUNK_END + 400 * DAY), (0, 0))
        self.assertTrue((rec.path / "5" / "0001.ogg").exists())

    async def test_active_session_is_not_pruned(self):
        rec, worker = await self.transcribed_session(keep_audio_days=90)
        recorder_module.ACTIVE_SESSIONS.add(rec.path)
        self.addCleanup(recorder_module.ACTIVE_SESSIONS.discard, rec.path)
        self.assertEqual(worker.prune_audio(now=CHUNK_END + 400 * DAY), (0, 0))
        self.assertTrue(self.ogg.exists())

    async def test_zero_days_keeps_audio_forever(self):
        _, worker = await self.transcribed_session(keep_audio_days=0)
        self.assertEqual(worker.prune_audio(now=CHUNK_END + 10_000 * DAY), (0, 0))
        self.assertTrue(self.ogg.exists())

    async def test_already_deleted_audio_is_skipped(self):
        _, worker = await self.transcribed_session(keep_audio_days=90)
        later = CHUNK_END + 91 * DAY
        self.assertEqual(worker.prune_audio(now=later)[0], 1)
        self.assertEqual(worker.prune_audio(now=later), (0, 0))

    # -- when the sweep runs: once, as the worker starts --

    async def run_until(self, worker, condition, timeout=3.0):
        """Run the worker's loop until ``condition()`` holds (or the timeout), then stop it."""
        task = asyncio.create_task(worker.run())
        deadline = time.monotonic() + timeout
        while not condition() and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def test_step_never_prunes(self):
        self.record_session()
        worker = self.worker(FakeTranscriber([groq_json((0, 1, "слово"))]),
                             keep_audio_days=90, keep_transcript_days=30)
        calls = []
        for name in ("prune", "prune_audio", "prune_transcripts"):
            setattr(worker, name, lambda *a, _name=name, **kw: calls.append(_name) or (0, 0))
        for _ in range(3):  # transcribe, rebuild the transcript, idle
            await worker.step()
        self.assertEqual(calls, [])

    async def test_run_prunes_audio_at_startup(self):
        rec, worker = await self.transcribed_session(keep_audio_days=90)
        await self.run_until(worker, lambda: not self.ogg.exists())
        self.assertFalse(self.ogg.exists())
        self.assertTrue((rec.path / "5" / "0001.json").exists())
        self.assertTrue((rec.path / "transcript.md").exists())

    async def test_run_prunes_transcripts_at_startup(self):
        old = self.make_session("20260101-000000Z-2", ended_at=1000.0, chunks=[("5/0001.ogg", True)])
        worker = self.worker(FakeTranscriber([]), keep_transcript_days=30)
        await self.run_until(worker, lambda: not old.exists())
        self.assertFalse(old.exists())

    async def test_run_prunes_only_once(self):
        old = self.make_session("20260101-000000Z-2", ended_at=1000.0, chunks=[("5/0001.ogg", True)])
        worker = self.worker(FakeTranscriber([]), keep_transcript_days=30, keep_audio_days=90)
        calls = []
        worker.prune = lambda now=None: calls.append(now)
        task = asyncio.create_task(worker.run())
        await asyncio.sleep(0.3)  # several scan loops would pass if it ran on each
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(len(calls), 1)
        self.assertTrue(old.exists())  # the stubbed prune deleted nothing

    # -- transcript retention: a whole old session folder goes --

    def make_session(self, name, ended_at, chunks=(), guild="1", error_attempts=None):
        """A finished session folder; ``chunks`` is [(file, transcribed)]."""
        session = self.root / guild / name
        session.mkdir(parents=True)
        (session / "session.json").write_text(json.dumps(
            {"guild_id": int(guild), "channel_id": 2, "started_at": 1000.0, "ended_at": ended_at}))
        (session / "transcript.md").write_text("[10:00:00] Аня: привет\n", encoding="utf-8")
        manifest = []
        for file, transcribed in chunks:
            (session / file).parent.mkdir(exist_ok=True)
            (session / file).write_bytes(b"OggS" + bytes(500))
            if transcribed:
                (session / file).with_suffix(".json").write_text(json.dumps({"segments": []}))
            elif error_attempts is not None:
                (session / file).with_suffix(".error.json").write_text(json.dumps({"attempts": error_attempts}))
            manifest.append(json.dumps({"user_id": 5, "file": file, "started_at": 1000.0, "ended_at": 1002.0,
                                        "audio_seconds": 2.0, "pieces": [[0.0, 1000.0]], "reason": "idle"}))
        (session / "chunks.jsonl").write_text("".join(line + "\n" for line in manifest))
        return session

    async def test_old_session_is_deleted_whole(self):
        old = self.make_session("20260101-000000Z-2", ended_at=1000.0, chunks=[("5/0001.ogg", True)])
        worker = self.worker(FakeTranscriber([]), keep_transcript_days=30)
        sessions, freed = worker.prune_transcripts(now=1000.0 + 31 * DAY)
        self.assertEqual(sessions, 1)
        self.assertGreater(freed, 500)
        self.assertFalse(old.exists())
        self.assertFalse(old.parent.exists())  # the guild folder it emptied goes too

    async def test_session_younger_than_the_limit_is_kept(self):
        old = self.make_session("20260101-000000Z-2", ended_at=1000.0, chunks=[("5/0001.ogg", True)])
        worker = self.worker(FakeTranscriber([]), keep_transcript_days=30)
        self.assertEqual(worker.prune_transcripts(now=1000.0 + 29 * DAY), (0, 0))
        self.assertTrue((old / "transcript.md").exists())

    async def test_only_old_sessions_go(self):
        old = self.make_session("20260101-000000Z-2", ended_at=1000.0)
        new = self.make_session("20260131-000000Z-2", ended_at=1000.0 + 30 * DAY)
        worker = self.worker(FakeTranscriber([]), keep_transcript_days=30)
        self.assertEqual(worker.prune_transcripts(now=1000.0 + 31 * DAY)[0], 1)
        self.assertFalse(old.exists())
        self.assertTrue((new / "transcript.md").exists())

    async def test_session_with_a_chunk_still_to_transcribe_is_kept(self):
        old = self.make_session("20260101-000000Z-2", ended_at=1000.0,
                                chunks=[("5/0001.ogg", True), ("5/0002.ogg", False)])
        worker = self.worker(FakeTranscriber([]), keep_transcript_days=30)
        self.assertEqual(worker.prune_transcripts(now=1000.0 + 400 * DAY), (0, 0))
        self.assertTrue((old / "5" / "0002.ogg").exists())

    async def test_chunk_given_up_on_does_not_keep_the_session(self):
        old = self.make_session("20260101-000000Z-2", ended_at=1000.0,
                                chunks=[("5/0001.ogg", False)], error_attempts=3)
        worker = self.worker(FakeTranscriber([]), keep_transcript_days=30)
        self.assertEqual(worker.prune_transcripts(now=1000.0 + 31 * DAY)[0], 1)
        self.assertFalse(old.exists())

    async def test_unfinished_session_is_kept(self):
        old = self.make_session("20260101-000000Z-2", ended_at=None, chunks=[("5/0001.ogg", True)])
        worker = self.worker(FakeTranscriber([]), keep_transcript_days=30)
        self.assertEqual(worker.prune_transcripts(now=1000.0 + 400 * DAY), (0, 0))
        self.assertTrue(old.exists())

    async def test_active_session_transcript_is_kept(self):
        old = self.make_session("20260101-000000Z-2", ended_at=1000.0)
        recorder_module.ACTIVE_SESSIONS.add(old)
        self.addCleanup(recorder_module.ACTIVE_SESSIONS.discard, old)
        worker = self.worker(FakeTranscriber([]), keep_transcript_days=30)
        self.assertEqual(worker.prune_transcripts(now=1000.0 + 400 * DAY), (0, 0))
        self.assertTrue(old.exists())

    async def test_zero_transcript_days_keeps_everything(self):
        old = self.make_session("20260101-000000Z-2", ended_at=1000.0, chunks=[("5/0001.ogg", True)])
        worker = self.worker(FakeTranscriber([]))  # default: keep forever
        self.assertEqual(worker.keep_transcript_days, 0)
        self.assertEqual(worker.prune_transcripts(now=1000.0 + 10_000 * DAY), (0, 0))
        self.assertTrue(old.exists())

    async def test_non_session_files_in_the_root_are_left_alone(self):
        ledger = self.root / "groq_ledger.jsonl"
        ledger.write_text("{}\n")
        (self.root / "1").mkdir()
        (self.root / "1" / "stray.txt").write_text("x")  # not a session folder
        worker = self.worker(FakeTranscriber([]), keep_transcript_days=30)
        worker.prune_transcripts(now=1000.0 + 400 * DAY)
        self.assertTrue(ledger.exists())
        self.assertTrue((self.root / "1" / "stray.txt").exists())

    async def test_audio_and_transcripts_have_separate_limits(self):
        session = self.make_session("20260101-000000Z-2", ended_at=1000.0, chunks=[("5/0001.ogg", True)])
        worker = self.worker(FakeTranscriber([]), keep_audio_days=90, keep_transcript_days=365)
        worker.prune(now=1000.0 + 100 * DAY)  # past the audio limit only
        self.assertFalse((session / "5" / "0001.ogg").exists())
        self.assertTrue((session / "transcript.md").exists())
        self.assertTrue((session / "5" / "0001.json").exists())
        worker.prune(now=1000.0 + 366 * DAY)  # past the transcript limit too
        self.assertFalse(session.exists())


class RecordingConfigTests(unittest.TestCase):
    def test_audio_keep_days_defaults_to_three_months(self):
        from ttsbot import config
        env = {k: v for k, v in os.environ.items() if k != "RECORDING_AUDIO_KEEP_DAYS"}
        with mock.patch.dict(os.environ, env, clear=True):
            config.reload()
            self.assertEqual(config.RECORDING_AUDIO_KEEP_DAYS, 90)
        self.addCleanup(config.reload)

    def test_audio_keep_days_from_env_and_clamped(self):
        from ttsbot import config
        self.addCleanup(config.reload)
        for raw, expected in (("30", 30), ("0", 0), ("-5", 0)):
            with mock.patch.dict(os.environ, {"RECORDING_AUDIO_KEEP_DAYS": raw}):
                config.reload()
                self.assertEqual(config.RECORDING_AUDIO_KEEP_DAYS, expected, raw)

    def test_transcript_keep_days_defaults_to_forever(self):
        from ttsbot import config
        env = {k: v for k, v in os.environ.items() if k != "RECORDING_TRANSCRIPT_KEEP_DAYS"}
        with mock.patch.dict(os.environ, env, clear=True):
            config.reload()
            self.assertEqual(config.RECORDING_TRANSCRIPT_KEEP_DAYS, 0)
        self.addCleanup(config.reload)

    def test_transcript_keep_days_from_env_and_clamped(self):
        from ttsbot import config
        self.addCleanup(config.reload)
        for raw, expected in (("365", 365), ("0", 0), ("-5", 0)):
            with mock.patch.dict(os.environ, {"RECORDING_TRANSCRIPT_KEEP_DAYS": raw}):
                config.reload()
                self.assertEqual(config.RECORDING_TRANSCRIPT_KEEP_DAYS, expected, raw)


class RecorderSidecarTests(unittest.TestCase):
    def test_pieces_sidecar_and_tts_notes(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        rec = ChannelRecorder(RecorderSettings(root=root), 1, 2)
        rec.start()
        self.assertIn(rec.path, recorder_module.ACTIVE_SESSIONS)
        rec.submit(5, 0, FRAME, 1000.0)
        rec.submit(5, 10**6, FRAME, 1010.0)
        deadline = time.time() + 2
        sidecar = pieces_path(rec.path / "5" / "0001.ogg")
        while time.time() < deadline and len(sidecar.read_text().splitlines() if sidecar.exists() else []) < 2:
            time.sleep(0.02)
        self.assertEqual([json.loads(l) for l in sidecar.read_text().splitlines()], [[0.0, 1000.0], [0.52, 1010.0]])
        rec.note_text(5, "привет", 1005.0)
        rec.note_name(5, "Аня")
        rec.note_name(6, "Боб")
        rec.stop()
        self.assertEqual(json.loads((rec.path / "names.json").read_text(encoding="utf-8")), {"5": "Аня", "6": "Боб"})
        self.assertFalse(sidecar.exists())
        self.assertNotIn(rec.path, recorder_module.ACTIVE_SESSIONS)
        self.assertEqual(json.loads((rec.path / "tts.jsonl").read_text(encoding="utf-8")),
                         {"user_id": 5, "at": 1005.0, "text": "привет"})

    def test_ogg_duration_of_a_cut_file(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        writer = OggOpusWriter(root / "a.ogg", 1)
        self.addCleanup(writer._file.close)
        for _ in range(120):
            writer.write(FRAME)
        writer._file.flush()
        self.assertAlmostEqual(ogg_duration(root / "a.ogg"), 2.0)  # the unflushed 20 packets are not there
        data = (root / "a.ogg").read_bytes()
        (root / "b.ogg").write_bytes(data[:-10])  # a torn last page
        self.assertAlmostEqual(ogg_duration(root / "b.ogg"), 1.0)


if __name__ == "__main__":
    unittest.main()
