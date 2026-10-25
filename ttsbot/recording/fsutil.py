"""Small file helpers shared by the recorder, the worker and their readers."""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path

log = logging.getLogger("tts_bot")

_reported: set[tuple[str, int]] = set()


def report_once(key: tuple[str, int], message: str, *args: object) -> None:
    """Log a warning about a damaged file once per process, not every scan."""
    if key not in _reported:
        _reported.add(key)
        log.warning(message, *args)


def read_jsonl(path: Path, fields: dict[str, type | tuple[type, ...]] | None = None) -> list[dict]:
    """The objects of a JSON-lines file, [] when there is none.

    A damaged line (the process died mid-append, a bad hand edit), or one
    without the ``fields`` its readers use, is skipped and reported once:
    one cut-off line must never stop everything that reads the session.
    """
    if not path.exists():
        return []
    rows = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line:
            continue
        try:
            row = json.loads(line)
            if not isinstance(row, dict) or any(
                not isinstance(row.get(name), kind) for name, kind in (fields or {}).items()
            ):
                raise ValueError("not a valid entry")
        except ValueError as exc:
            report_once((str(path), number), "Skipping damaged line %s:%d: %s", path, number, exc)
            continue
        rows.append(row)
    return rows


def write_atomic(path: Path, text: str) -> None:
    """Replace ``path`` with ``text`` (UTF-8) so a concurrent reader (the
    recordings website, a crash recovery) sees the old file or the new
    one, never half of it."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}-{threading.get_ident()}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8", newline="\n")
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
