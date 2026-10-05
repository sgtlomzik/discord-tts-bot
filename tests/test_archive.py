"""Recordings archive: store, mix, retention, media cache and service."""

import asyncio
import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from ttsbot.archive import mix as mixing
from ttsbot.archive.media import BuildFailed, MediaCache
from ttsbot.archive.retention import DAY, RetentionPolicy
from ttsbot.archive.service import ArchiveService, AudioUnavailable
from ttsbot.archive.store import RecordingStore, SessionKey, SessionNotFound
from ttsbot.ogg_opus import UnsupportedOpusStream
from ttsbot.recording.ogg import OggOpusWriter, OggPacketIndex, ogg_crc
from ttsbot.recording.recorder import ChannelRecorder, RecorderSettings
from ttsbot.recording.rtp import OPUS_SILENCE
from ttsbot.recording.transcript import chunk_result

GUILD = "111"
NAME = "20261004-095316Z-222"


def frame(marker: int) -> bytes:
    """A 20 ms Opus packet whose payload says which piece it belongs to."""
    return b"\xfc" + bytes([marker]) * 40


def bitwise_crc(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc ^= byte << 24
        for _ in range(8):
            crc = ((crc << 1) ^ 0x04C11DB7) if crc & 0x80000000 else crc << 1
        crc &= 0xFFFFFFFF
    return crc


def write_ogg(path: Path, runs) -> None:
    """``runs``: (marker or None for silence, packets) in order."""
    path.parent.mkdir(parents=True, exist_ok=True)
    writer = OggOpusWriter(path, serial=1)
    for marker, count in runs:
        for _ in range(count):
            writer.write(OPUS_SILENCE if marker is None else frame(marker))
    writer.close()


def groq(*segments):
    return {"language": "ru", "segments": [
        {"start": a, "end": b, "text": t, "no_speech_prob": 0.0, "avg_logprob": -0.1, "compression_ratio": 1.0}
        for a, b, t in segments]}


class SessionFixture:
    """A session folder like the bot writes: user 5 speaks twice, user 6 once
    in between, user 7 only through TTS.

    Wall clock: A (user 5) 1000-1001, C (user 6) 1005-1005.5, B (user 5)
    1010-1011. Mix with a 0.5 s gap: A 0-1, C 1.5-2, B 2.5-3.5.
    """

    def __init__(self, root: Path, *, ended=True, transcribe=True, name=NAME, info_extra=None):
        self.path = root / GUILD / name
        self.path.mkdir(parents=True)
        info = {"guild_id": int(GUILD), "channel_id": 222, "started_at": 990.0,
                "ended_at": 1020.0 if ended else None, "guild_name": "Гильдия", "channel_name": "голос",
                **(info_extra or {})}
        (self.path / "session.json").write_text(json.dumps(info), encoding="utf-8")
        write_ogg(self.path / "5/0001.ogg", [(1, 50), (None, 25), (2, 50)])
        write_ogg(self.path / "6/0001.ogg", [(3, 25)])
        self.entries = [
            {"user_id": 5, "file": "5/0001.ogg", "started_at": 1000.0, "ended_at": 1011.0, "audio_seconds": 2.5,
             "pieces": [[0.0, 1000.0], [1.5, 1010.0]]},
            {"user_id": 6, "file": "6/0001.ogg", "started_at": 1005.0, "ended_at": 1005.5, "audio_seconds": 0.5,
             "pieces": [[0.0, 1005.0]]},
        ]
        (self.path / "chunks.jsonl").write_text("".join(json.dumps(e) + "\n" for e in self.entries))
        (self.path / "names.json").write_text(json.dumps({"5": "Аня", "6": "Боб", "7": "Вика"}))
        (self.path / "tts.jsonl").write_text(json.dumps({"user_id": 7, "at": 1005.2, "text": "го"}) + "\n")
        if transcribe:
            self.transcribe(0, groq((0.0, 0.9, "Привет."), (1.6, 2.4, "Я вернулся.")))
            self.transcribe(1, groq((0.1, 0.4, "Норм.")))

    def transcribe(self, i, result):
        entry = self.entries[i]
        stored = chunk_result(entry, result, model="m", took=0.1)
        (self.path / entry["file"]).with_suffix(".json").write_text(json.dumps(stored, ensure_ascii=False), encoding="utf-8")


class TempDirCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.root = self.dir / "recordings"
        self.root.mkdir()


class OggTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)

    def test_crc_matches_the_bitwise_definition(self):
        for data in (b"", b"OggS", bytes(range(256)) * 3, os.urandom(5000)):
            self.assertEqual(ogg_crc(data), bitwise_crc(data))

    def test_index_reads_back_what_was_written(self):
        path = self.dir / "a.ogg"
        write_ogg(path, [(1, 60), (None, 5), (2, 70)])
        with OggPacketIndex(path) as index:
            self.assertEqual(len(index), 135)
            self.assertFalse(index.damaged)
            self.assertAlmostEqual(index.seconds, 2.7)
            self.assertEqual(index.packet(0), frame(1))
            self.assertEqual(index.packet(60), OPUS_SILENCE)
            self.assertEqual(index.packet(134), frame(2))
            self.assertEqual(index.index_at(1.3), 65)
            self.assertEqual(index.start_sample(65), 65 * 960)

    def test_cut_or_corrupted_file_keeps_the_good_part(self):
        path = self.dir / "a.ogg"
        write_ogg(path, [(1, 200)])
        data = path.read_bytes()
        path.write_bytes(data[:len(data) // 2])  # a crash mid-page
        with OggPacketIndex(path) as index:
            self.assertTrue(index.damaged)
            self.assertGreater(len(index), 0)
            self.assertTrue(all(index.packet(i) == frame(1) for i in range(len(index))))
        corrupt = bytearray(data)
        corrupt[len(data) // 2] ^= 0xFF  # a flipped bit: the CRC catches it
        path.write_bytes(bytes(corrupt))
        with OggPacketIndex(path) as index:
            self.assertTrue(index.damaged)
            self.assertLess(len(index), 200)

    def test_not_ogg_is_rejected(self):
        path = self.dir / "a.ogg"
        path.write_bytes(b"not an ogg file at all, just text" * 3)
        with self.assertRaises(UnsupportedOpusStream):
            OggPacketIndex(path)
        path.write_bytes(b"")
        with self.assertRaises(UnsupportedOpusStream):
            OggPacketIndex(path)


class StoreTests(TempDirCase):
    def test_reads_a_session(self):
        SessionFixture(self.root)
        store = RecordingStore(self.root)
        self.assertEqual(store.keys(), [SessionKey(GUILD, NAME)])
        data = store.read(SessionKey(GUILD, NAME))
        self.assertEqual([c.status for c in data.chunks], ["done", "done"])
        self.assertEqual(data.names[6], "Боб")
        self.assertEqual(len(data.tts), 1)
        self.assertEqual(data.problems, [])

    def test_keys_ignore_strangers_and_sort_newest_first(self):
        SessionFixture(self.root, name="20261001-000000Z-222")
        SessionFixture(self.root, name="20261003-000000Z-222")
        (self.root / GUILD / "notes").mkdir()
        (self.root / GUILD / "20261005-000000Z-222").mkdir()  # no session.json yet
        (self.root / "groq_ledger.jsonl").write_text("")
        names = [k.name for k in RecordingStore(self.root).keys()]
        self.assertEqual(names, ["20261003-000000Z-222", "20261001-000000Z-222"])

    def test_ids_are_validated_before_touching_the_disk(self):
        for guild, name in [("..", NAME), (GUILD, "../../etc"), (GUILD, NAME + "/.."), ("1a", NAME), ("", "")]:
            with self.assertRaises(SessionNotFound):
                SessionKey.parse(guild, name)
        self.assertEqual(SessionKey.from_id(f"{GUILD}/{NAME}"), SessionKey(GUILD, NAME))

    def test_damaged_files_cost_only_their_own_data(self):
        fx = SessionFixture(self.root)
        with (fx.path / "chunks.jsonl").open("a") as manifest:
            manifest.write("{broken\n")
            manifest.write(json.dumps({**fx.entries[0], "file": "../../evil.ogg"}) + "\n")
            manifest.write(json.dumps({**fx.entries[0], "file": "9/0001.ogg"}) + "\n")  # user mismatch
            manifest.write('{"half": ')  # an append in progress: not a problem
        (fx.path / "6/0001.json").write_text("{not json")
        (fx.path / "names.json").write_text("[]")
        data = RecordingStore(self.root).read(SessionKey(GUILD, NAME))
        self.assertEqual([c.file for c in data.chunks], ["5/0001.ogg", "6/0001.ogg"])
        self.assertEqual([c.status for c in data.chunks], ["done", "pending"])
        self.assertEqual(data.names, {})
        self.assertEqual(len(data.problems), 5, data.problems)

    def test_unreadable_session_json_hides_only_that_session(self):
        fx = SessionFixture(self.root)
        (fx.path / "session.json").write_text("{")
        with self.assertRaises(SessionNotFound):
            RecordingStore(self.root).read(SessionKey(GUILD, NAME))

    def test_signature_follows_the_bots_writes(self):
        fx = SessionFixture(self.root, transcribe=False)
        store = RecordingStore(self.root)
        key = SessionKey(GUILD, NAME)
        first = store.signature(key)
        time.sleep(0.01)
        fx.transcribe(0, groq((0.0, 1.0, "Привет.")))
        second = store.signature(key)
        self.assertNotEqual(first, second)
        error = fx.path / "6/0001.error.json"
        error.write_text(json.dumps({"attempts": 1}))
        data = store.read(key)
        self.assertEqual(data.error_files, ("6/0001.error.json",))
        before = store.signature(key, data.error_files)
        time.sleep(0.01)
        error.write_text(json.dumps({"attempts": 3, "error": "rejected"}))  # rewritten in place
        self.assertNotEqual(before, store.signature(key, data.error_files))
        self.assertEqual(store.read(key).chunks[1].status, "failed")


class MixTests(TempDirCase):
    def setUp(self):
        super().setUp()
        self.fx = SessionFixture(self.root)
        self.data = RecordingStore(self.root).read(SessionKey(GUILD, NAME))
        self.out = self.dir / "mix.ogg"

    def markers(self, path):
        with OggPacketIndex(path) as index:
            return [index.packet(i)[1] if index.packet(i) != OPUS_SILENCE else None for i in range(len(index))]

    def test_pieces_in_spoken_order_with_one_gap_between(self):
        plan = mixing.build_mix(self.fx.path, self.data.chunks, self.out, gap_ms=500)
        self.assertEqual(plan.duration, 3.5)
        self.assertEqual([(p.user_id, p.start, p.duration) for p in plan.pieces],
                         [(5, 0.0, 1.0), (6, 1.5, 0.5), (5, 2.5, 1.0)])
        expected = [1] * 50 + [None] * 25 + [3] * 25 + [None] * 25 + [2] * 50
        self.assertEqual(self.markers(self.out), expected)  # the recorder's pause is not doubled

    def test_positions_land_on_the_right_speech(self):
        plan = mixing.build_mix(self.fx.path, self.data.chunks, self.out, gap_ms=500)
        markers = self.markers(self.out)

        def heard(position):
            return markers[int(round(position * 50))]

        self.assertEqual(plan.position_in_file("5/0001.ogg", 0.0), 0.0)
        self.assertEqual(heard(plan.position_in_file("5/0001.ogg", 1.6)), 2)  # "Я вернулся."
        self.assertEqual(plan.position_in_file("5/0001.ogg", 1.6), 2.6)
        self.assertEqual(heard(plan.position_in_file("6/0001.ogg", 0.2)), 3)
        self.assertEqual(plan.position_in_file("5/0001.ogg", 1.2), 1.0)  # in the cut pause: end of piece
        self.assertIsNone(plan.position_in_file("8/0001.ogg", 0.0))
        self.assertEqual(plan.position_at(1005.2), 1.7)  # during C
        self.assertEqual(plan.position_at(1003.0), 1.5)  # silence: what was said next
        self.assertEqual(plan.position_at(900.0), 0.0)
        self.assertEqual(plan.position_at(5000.0), 3.5)

    def test_plan_round_trips_through_json(self):
        plan = mixing.build_mix(self.fx.path, self.data.chunks, self.out, gap_ms=0)
        path = self.dir / "plan.json"
        mixing.save_plan(plan, path)
        again = mixing.load_plan(path)
        self.assertEqual(again.pieces, plan.pieces)
        self.assertEqual(again.position_at(1005.2), plan.position_at(1005.2))
        self.assertEqual(again.duration, 2.5)

    def test_deleted_and_damaged_audio_are_left_out(self):
        (self.fx.path / "6/0001.ogg").unlink()
        data = RecordingStore(self.root).read(SessionKey(GUILD, NAME))
        (self.fx.path / "5/0001.ogg").write_bytes(b"garbage")
        with self.assertLogs("tts_web", "WARNING"):
            plan = mixing.build_mix(self.fx.path, data.chunks, self.out, gap_ms=500)
        self.assertEqual(plan.missing_files, ["6/0001.ogg"])
        self.assertEqual(plan.damaged_files, ["5/0001.ogg"])
        self.assertTrue(plan.empty)
        self.assertFalse(self.out.exists())

    def test_fingerprint_changes_with_audio_and_settings(self):
        base = mixing.fingerprint(self.data.chunks, 500)
        self.assertEqual(base, mixing.fingerprint(list(reversed(self.data.chunks)), 500))
        self.assertNotEqual(base, mixing.fingerprint(self.data.chunks, 300))
        self.data.chunks[1].audio = None
        self.assertNotEqual(base, mixing.fingerprint(self.data.chunks, 500))


class RetentionTests(TempDirCase):
    def test_dates_follow_the_bots_rules(self):
        fx = SessionFixture(self.root)
        fx.entries[1]  # user 6's file stays untranscribed
        (fx.path / "6/0001.json").unlink()
        data = RecordingStore(self.root).read(SessionKey(GUILD, NAME))
        policy = RetentionPolicy(audio_days=90, transcript_days=365)
        doc = policy.describe(policy.facts(data), now=1011.0 + DAY)
        self.assertEqual(doc["audio"]["state"], "kept")
        self.assertEqual(doc["audio"]["expires_at"], 1011.0 + 90 * DAY)
        self.assertEqual(doc["audio"]["seconds_left"], 89 * DAY)
        self.assertEqual(doc["audio"]["kept_until_transcribed"], 1)
        self.assertEqual(doc["transcript"]["expires_at"], 1020.0 + 365 * DAY)
        self.assertTrue(doc["transcript"]["held"])  # a file still waits for Groq

    def test_forever_and_overdue(self):
        fx = SessionFixture(self.root)
        (fx.path / "6/0001.ogg").unlink()
        data = RecordingStore(self.root).read(SessionKey(GUILD, NAME))
        forever = RetentionPolicy().describe(RetentionPolicy().facts(data), now=10**10)
        self.assertIsNone(forever["audio"]["expires_at"])
        self.assertIsNone(forever["transcript"]["expires_at"])
        self.assertEqual(forever["audio"]["state"], "partial")
        policy = RetentionPolicy(audio_days=1)
        overdue = policy.describe(policy.facts(data), now=10**10)
        self.assertEqual(overdue["audio"]["seconds_left"], 0)  # waits for the next sweep


class MediaCacheTests(TempDirCase):
    def setUp(self):
        super().setUp()
        self.now = 0.0
        self.cache = MediaCache(self.dir / "cache", max_bytes=10_000, clock=lambda: self.now)
        self.cache.setup()
        self.builds = 0

    def builder(self, size=100, fail=False):
        def build(folder):
            self.builds += 1
            (folder / "file.bin").write_bytes(b"x" * size)
            if fail:
                raise RuntimeError("broken input")
        return build

    async def test_builds_once_and_serves_from_disk(self):
        first = await self.cache.ensure("1/s", "mix", "aaa", self.builder())
        again = await self.cache.ensure("1/s", "mix", "aaa", self.builder())
        self.assertEqual(first, again)
        self.assertEqual(self.builds, 1)
        self.assertEqual((first / "file.bin").stat().st_size, 100)

    async def test_concurrent_requests_share_one_build(self):
        paths = await asyncio.gather(*[self.cache.ensure("1/s", "mix", "aaa", self.builder()) for _ in range(5)])
        self.assertEqual(len(set(paths)), 1)
        self.assertEqual(self.builds, 1)

    async def test_failed_build_leaves_nothing_and_waits_before_retrying(self):
        with self.assertRaises(BuildFailed), self.assertLogs("tts_web", "ERROR"):
            await self.cache.ensure("1/s", "mix", "aaa", self.builder(fail=True))
        self.assertEqual(list((self.dir / "cache" / "1" / "s").iterdir()), [])
        with self.assertRaises(BuildFailed):
            await self.cache.ensure("1/s", "mix", "aaa", self.builder())
        self.assertEqual(self.builds, 1)
        self.now += 301
        await self.cache.ensure("1/s", "mix", "aaa", self.builder())
        self.assertEqual(self.builds, 2)

    async def test_new_fingerprint_replaces_the_old_one(self):
        old = await self.cache.ensure("1/s", "mix", "aaa", self.builder())
        new = await self.cache.ensure("1/s", "mix", "bbb", self.builder())
        self.assertFalse(old.exists())
        self.assertTrue(new.exists())

    async def test_collect_and_size_limit(self):
        a = await self.cache.ensure("1/a", "mix", "f1", self.builder(4000))
        b = await self.cache.ensure("1/b", "mix", "f1", self.builder(4000))
        os.utime(a, (1, 1))  # a is the least recently used
        c = await self.cache.ensure("1/c", "mix", "f1", self.builder(4000))  # over 10 000 bytes
        self.assertFalse(a.exists())
        self.assertTrue(b.exists() and c.exists())
        self.cache.collect({"1/b": {"mix-f1"}})  # c's session is gone
        self.assertTrue(b.exists())
        self.assertFalse((self.dir / "cache" / "1" / "c").exists())
        self.cache.collect({"1/b": set()})  # b has no audio any more
        self.assertFalse(b.exists())

    async def test_setup_drops_half_built_folders(self):
        tmp = self.dir / "cache" / "1" / "s" / ".tmp-mix-123"
        tmp.mkdir(parents=True)
        self.cache.setup()
        self.assertFalse(tmp.exists())

    def test_artifact_names_are_validated(self):
        for scope, kind, fp in [("1/../x", "mix", "a"), ("1", "mix", "a"), ("1/s", "../m", "a"), ("1/s", "mix", "")]:
            with self.assertRaises(ValueError):
                self.cache.path(scope, kind, fp)


class ServiceTests(TempDirCase):
    def setUp(self):
        super().setUp()
        self.now = 1020.0 + DAY
        self.cache = MediaCache(self.dir / "cache", max_bytes=10**8)
        self.cache.setup()
        self.service = ArchiveService(RecordingStore(self.root), self.cache,
                                      RetentionPolicy(audio_days=90), clock=lambda: self.now)

    async def test_listing(self):
        SessionFixture(self.root)
        SessionFixture(self.root, name="20261005-000000Z-333", ended=False, transcribe=False)
        page = await self.service.list_sessions()
        self.assertEqual(page["total"], 2)
        live, done = page["items"]
        self.assertEqual(live["status"], "recording")
        self.assertEqual(done["status"], "done")
        self.assertEqual(done["guild"], {"id": GUILD, "name": "Гильдия"})
        self.assertEqual(done["channel"], {"id": "222", "name": "голос"})
        self.assertEqual(done["duration_seconds"], 30.0)
        self.assertEqual(live["duration_seconds"], self.now - 990.0)
        self.assertEqual([(p["user_id"], p["name"]) for p in done["participants"]],
                         [("5", "Аня"), ("6", "Боб"), ("7", "Вика")])
        self.assertEqual(done["participants"][2]["tts_lines"], 1)
        self.assertEqual(done["line_count"], 4)
        # The first file to go is user 6's, which ended at 1005.5.
        self.assertEqual(done["retention"]["audio"]["expires_at"], 1005.5 + 90 * DAY)
        self.assertAlmostEqual(done["retention"]["audio"]["seconds_left"], 89 * DAY - 14.5, delta=0.5)
        self.assertEqual((await self.service.list_sessions(user="7"))["total"], 2)
        self.assertEqual((await self.service.list_sessions(user="8"))["total"], 0)
        self.assertEqual((await self.service.list_sessions(since=1000.0))["total"], 0)
        self.assertEqual(len((await self.service.list_sessions(limit=1, offset=1))["items"]), 1)
        guilds = await self.service.guilds()
        self.assertEqual(guilds["items"], [{"id": GUILD, "name": "Гильдия", "sessions": 2,
                                            "last_started_at": 990.0}])

    async def test_session_lines_point_into_the_mix(self):
        SessionFixture(self.root)
        doc = await self.service.session(f"{GUILD}/{NAME}")
        self.assertEqual(doc["audio"]["status"], "ready")
        self.assertEqual(doc["audio"]["duration"], 3.5)
        rows = [(l["name"], l["kind"], l["text"], l["position"], l["exact"]) for l in doc["lines"]]
        self.assertEqual(rows, [
            ("Аня", "voice", "Привет.", 0.0, True),
            ("Боб", "voice", "Норм.", 1.6, True),
            ("Вика", "tts", "го", 1.7, False),
            ("Аня", "voice", "Я вернулся.", 2.6, True),
        ])
        self.assertEqual(doc["lines"][0]["user_id"], "5")
        path = await self.service.audio_file(f"{GUILD}/{NAME}")
        self.assertEqual(path.name, "mix.ogg")

    async def test_document_follows_new_results(self):
        fx = SessionFixture(self.root, transcribe=False)
        doc = await self.service.session(f"{GUILD}/{NAME}")
        self.assertEqual(doc["status"], "transcribing")
        self.assertEqual(doc["lines"][0]["kind"], "tts")
        fx.transcribe(1, groq((0.1, 0.4, "Норм.")))
        doc = await self.service.session(f"{GUILD}/{NAME}")
        self.assertEqual([l["text"] for l in doc["lines"]], ["Норм.", "го"])
        self.assertEqual(doc["transcription"]["done"], 1)

    async def test_text_stays_when_the_audio_is_gone(self):
        fx = SessionFixture(self.root)
        for ogg in fx.path.glob("*/*.ogg"):
            ogg.unlink()
        doc = await self.service.session(f"{GUILD}/{NAME}")
        self.assertEqual(doc["audio"]["status"], "none")
        self.assertEqual(doc["audio"]["missing_files"], 2)
        self.assertEqual(len(doc["lines"]), 4)
        self.assertTrue(all(l["position"] is None for l in doc["lines"]))
        self.assertIsNone(doc["links"]["audio"])
        self.assertEqual(doc["retention"]["audio"]["state"], "deleted")
        with self.assertRaises(AudioUnavailable):
            await self.service.audio_file(f"{GUILD}/{NAME}")

    async def test_broken_audio_still_shows_the_transcript(self):
        fx = SessionFixture(self.root)
        for ogg in fx.path.glob("*/*.ogg"):
            ogg.write_bytes(b"garbage")
        with self.assertLogs("tts_web", "WARNING"):
            doc = await self.service.session(f"{GUILD}/{NAME}")
        self.assertEqual(len(doc["lines"]), 4)
        self.assertEqual(doc["audio"]["status"], "none")  # nothing playable

    async def test_missing_session(self):
        with self.assertRaises(SessionNotFound):
            await self.service.session(f"{GUILD}/{NAME}")
        with self.assertRaises(SessionNotFound):
            await self.service.session("../etc/passwd")

    async def test_deleted_session_leaves_index_and_cache(self):
        fx = SessionFixture(self.root)
        await self.service.session(f"{GUILD}/{NAME}")
        shutil.rmtree(fx.path)
        await self.service.maintain()
        self.assertEqual((await self.service.list_sessions())["total"], 0)
        self.assertEqual(self.cache.stats()["artifacts"], 0)

    async def test_maintenance_prebuilds_recent_mixes(self):
        SessionFixture(self.root)
        await self.service.maintain(prebuild_recent=1)
        self.assertEqual(self.cache.stats()["artifacts"], 1)

    async def test_mp3_is_made_in_the_background(self):
        SessionFixture(self.root)
        sid = f"{GUILD}/{NAME}"
        if shutil.which("ffmpeg") is None:
            self.skipTest("ffmpeg not installed")
        self.assertIsNone(await self.service.mp3_file(sid))
        for _ in range(200):
            path = await self.service.mp3_file(sid)
            if path is not None:
                break
            await asyncio.sleep(0.05)
        self.assertIsNotNone(path)
        self.assertGreater(path.stat().st_size, 0)


class RecorderNamesTests(unittest.TestCase):
    def test_session_json_keeps_guild_and_channel_names(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        rec = ChannelRecorder(RecorderSettings(root=root), 1, 2, guild_name="Сервер", channel_name="Общий")
        rec.start()
        rec.stop()
        info = json.loads((rec.path / "session.json").read_text(encoding="utf-8"))
        self.assertEqual((info["guild_name"], info["channel_name"]), ("Сервер", "Общий"))
        self.assertIsNotNone(info["ended_at"])
        self.assertEqual(sorted(p.name for p in rec.path.iterdir()), ["session.json"])


if __name__ == "__main__":
    unittest.main()
