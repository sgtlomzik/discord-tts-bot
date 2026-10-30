"""Where the bot records, and the recording file-safety fixes.

- RECORDING_GUILD_IDS limits recording to the listed guilds; elsewhere the
  bot joins deafened with the plain voice client and only speaks.
- write_atomic fsyncs before the rename; the Groq ledger is rewritten
  atomically.
- a hung ffmpeg in _shrink is killed after SHRINK_TIMEOUT_SECONDS.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from test_bot import load_bot_module
from ttsbot.recording import fsutil, worker
from ttsbot.recording.client import RecordingVoiceClient
from ttsbot.recording.groq import AudioBudget, TranscriptionError
from ttsbot.voice_lifecycle import records_guild


class RecordsGuildTests(unittest.TestCase):
    def test_recording_off_records_nowhere(self):
        bot_mod = load_bot_module()
        with patch.object(bot_mod.config, "RECORDING_ENABLED", False), \
                patch.object(bot_mod.config, "RECORDING_GUILD_IDS", set()):
            self.assertFalse(records_guild(1))

    def test_an_empty_list_records_every_guild(self):
        bot_mod = load_bot_module()
        with patch.object(bot_mod.config, "RECORDING_ENABLED", True), \
                patch.object(bot_mod.config, "RECORDING_GUILD_IDS", set()):
            self.assertTrue(records_guild(1))

    def test_only_listed_guilds_are_recorded(self):
        bot_mod = load_bot_module()
        with patch.object(bot_mod.config, "RECORDING_ENABLED", True), \
                patch.object(bot_mod.config, "RECORDING_GUILD_IDS", {1030870431181312060}):
            self.assertTrue(records_guild(1030870431181312060))
            self.assertFalse(records_guild(2))

    def test_the_env_list_is_parsed(self):
        bot_mod = load_bot_module()
        with patch.dict(os.environ, {"RECORDING_GUILD_IDS": "1, 2;x"}), self.assertLogs("tts_bot", "WARNING"):
            bot_mod.config.reload()
        try:
            self.assertEqual(bot_mod.config.RECORDING_GUILD_IDS, {1, 2})
        finally:
            bot_mod.config.reload()


class ConnectClientTests(unittest.IsolatedAsyncioTestCase):
    async def _connect(self, guild_id):
        bot_mod = load_bot_module()
        tts_bot = bot_mod.TTSBot()
        guild = types.SimpleNamespace(id=guild_id, voice_client=None)
        channel = MagicMock()
        channel.id = 10
        channel.guild = guild
        channel.connect = AsyncMock(return_value=MagicMock(spec=discord.VoiceClient))
        with patch.object(bot_mod.config, "RECORDING_ENABLED", True), \
                patch.object(bot_mod.config, "RECORDING_GUILD_IDS", {1}), \
                patch.object(bot_mod.discord.utils, "get", MagicMock(return_value=None)):
            await tts_bot.ensure_voice(channel)
        return channel.connect.await_args.kwargs

    async def test_a_listed_guild_connects_the_recording_client(self):
        kwargs = await self._connect(1)
        self.assertIs(kwargs.get("cls"), RecordingVoiceClient)
        self.assertFalse(kwargs["self_deaf"])

    async def test_another_guild_joins_deafened_without_recording(self):
        kwargs = await self._connect(2)
        self.assertNotIn("cls", kwargs)
        self.assertTrue(kwargs["self_deaf"])


class FileSafetyTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)

    def test_write_atomic_fsyncs_before_the_rename(self):
        order = []
        real_fsync, real_replace = os.fsync, os.replace
        with patch.object(fsutil.os, "fsync", side_effect=lambda fd: (order.append("fsync"), real_fsync(fd))), \
                patch.object(fsutil.os, "replace", side_effect=lambda a, b: (order.append("replace"), real_replace(a, b))):
            fsutil.write_atomic(self.dir / "f.json", "{}")
        self.assertEqual(order, ["fsync", "replace"])
        self.assertEqual((self.dir / "f.json").read_text(encoding="utf-8"), "{}")
        self.assertEqual(list(self.dir.iterdir()), [self.dir / "f.json"])  # no temp file left

    def test_the_ledger_is_rewritten_atomically(self):
        ledger = self.dir / "groq_ledger.jsonl"
        ledger.write_text(json.dumps([1.0, 30.0]) + "\n" + "garbage\n", encoding="utf-8")
        with patch("ttsbot.recording.groq.write_atomic", wraps=fsutil.write_atomic) as atomic:
            AudioBudget(ledger, per_hour=7200, per_day=28800)
        atomic.assert_called_once()
        self.assertNotIn("garbage", ledger.read_text(encoding="utf-8"))


class ShrinkTimeoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_hung_ffmpeg_is_killed(self):
        process = MagicMock()
        async def hang():
            await asyncio.sleep(3600)

        process.communicate = hang
        process.wait = AsyncMock(return_value=-9)
        source = Path(tempfile.mkdtemp()) / "big.ogg"
        self.addCleanup(shutil.rmtree, source.parent, True)
        source.write_bytes(b"x")
        with patch.object(worker, "SHRINK_TIMEOUT_SECONDS", 0.05), \
                patch.object(worker.asyncio, "create_subprocess_exec", AsyncMock(return_value=process)) as spawn:
            with self.assertRaises(TranscriptionError) as caught:
                await worker.TranscriptionWorker._shrink(source)
        process.kill.assert_called_once()
        self.assertTrue(caught.exception.retry)
        self.assertIn("-nostdin", spawn.await_args.args)


if __name__ == "__main__":
    unittest.main()
