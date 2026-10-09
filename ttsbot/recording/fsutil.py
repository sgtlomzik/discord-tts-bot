"""Small file helpers shared by the recorder, the worker and their readers."""

from __future__ import annotations

import os
import threading
from pathlib import Path


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
