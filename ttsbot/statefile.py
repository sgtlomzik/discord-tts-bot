"""Reading the bot's JSON state files without ever losing them.

A file that exists but cannot be read is an operator problem (a bad hand
edit, a damaged disk), not a reason to start from defaults: the next save
would overwrite it, and the container restarts on failure. Readers raise
StateFileError instead, which stops startup with the path in the message
and leaves the file as it is.
"""

from __future__ import annotations

import json
from pathlib import Path


class StateFileError(RuntimeError):
    """A state file exists but cannot be used; fix or move it, then restart."""


def read_json_object(path: Path) -> dict | None:
    """The JSON object stored at *path*, or None when there is no file."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise StateFileError(f"Cannot read {path}: {exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise StateFileError(f"{path} is not valid JSON ({exc}); fix or move it, then restart") from exc
    if not isinstance(data, dict):
        raise StateFileError(f"{path} must hold a JSON object, not {type(data).__name__}")
    return data
