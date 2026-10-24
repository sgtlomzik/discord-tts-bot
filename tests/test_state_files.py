"""A damaged JSON state file stops startup instead of being replaced.

voices.json used to be reseeded (and overwritten) when it could not be
read, losing every cloud voice and the fallback chain; config.json was
silently ignored and then overwritten by the next save. The container
restarts on failure, so the file must stay exactly as it was. The files
are now only imported into data/state.db (tests/test_statedb.py covers
damaged files and databases there); read_json_object still reads them.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ttsbot.statefile import StateFileError, read_json_object


class ReadJsonObjectTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "state.json"

    def tearDown(self):
        self.dir.cleanup()

    def test_missing_file_is_none(self):
        self.assertIsNone(read_json_object(self.path))

    def test_object_is_returned(self):
        self.path.write_text('{"a": 1}', encoding="utf-8")
        self.assertEqual(read_json_object(self.path), {"a": 1})

    def test_broken_json_raises_with_the_path(self):
        self.path.write_text('{"a": ', encoding="utf-8")
        with self.assertRaises(StateFileError) as ctx:
            read_json_object(self.path)
        self.assertIn(str(self.path), str(ctx.exception))

    def test_non_object_raises(self):
        self.path.write_text("[1, 2]", encoding="utf-8")
        with self.assertRaises(StateFileError):
            read_json_object(self.path)


if __name__ == "__main__":
    unittest.main()
