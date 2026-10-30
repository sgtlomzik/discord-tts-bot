"""LOG_FILE keeps a rotating log; state.db runs in WAL mode."""

from __future__ import annotations

import logging
import logging.handlers
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ttsbot import config
from ttsbot.statedb import StateDB


class LogFileTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        root = logging.getLogger()
        before = list(root.handlers)

        def restore():
            for handler in list(root.handlers):
                if handler not in before:
                    root.removeHandler(handler)
                    handler.close()

        self.addCleanup(restore)

    def _file_handlers(self):
        return [h for h in logging.getLogger().handlers if isinstance(h, logging.handlers.RotatingFileHandler)]

    def test_log_file_gets_a_rotating_copy(self):
        path = self.dir / "logs" / "bot.log"
        env = {"LOG_FILE": str(path), "LOG_FILE_MAX_MB": "3", "LOG_FILE_BACKUPS": "2"}
        with patch.dict(os.environ, env):
            config.setup_logging()
        (handler,) = self._file_handlers()
        self.assertEqual(handler.maxBytes, 3 * 1024 * 1024)
        self.assertEqual(handler.backupCount, 2)
        logging.getLogger("tts_bot").warning("hello file")
        handler.flush()
        self.assertIn("hello file", path.read_text(encoding="utf-8"))

    def test_no_log_file_by_default(self):
        with patch.dict(os.environ, {"LOG_FILE": ""}):
            config.setup_logging()
        self.assertEqual(self._file_handlers(), [])

    def test_an_unwritable_log_file_falls_back_to_stdout(self):
        blocker = self.dir / "file"
        blocker.write_text("x")
        with patch.dict(os.environ, {"LOG_FILE": str(blocker / "bot.log")}), \
                self.assertLogs("tts_bot", "WARNING"):
            config.setup_logging()
        self.assertEqual(self._file_handlers(), [])


class StateDbJournalTests(unittest.TestCase):
    def test_a_file_database_uses_wal_and_normal_sync(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        db = StateDB(tmp / "state.db")
        self.assertEqual(db._conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual(db._conn.execute("PRAGMA synchronous").fetchone()[0], 1)  # NORMAL
        db._conn.close()

    def test_memory_database_still_works(self):
        db = StateDB(":memory:")
        self.assertEqual(db._conn.execute("PRAGMA synchronous").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
