"""Move all configured Discord guilds to the fish-default voice.

Only voice selections change. Allowed users, enable flags, fixed phrases,
emoji aliases, and other settings remain intact. A timestamped backup is
written next to config.json before the atomic replacement.
"""

import argparse
import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path


def migrate(path: Path) -> tuple[int, int, Path]:
    data = json.loads(path.read_text(encoding="utf-8"))
    guilds = data.get("guilds", {})
    if not isinstance(guilds, dict):
        raise ValueError("config.json has no guild map")
    changed = 0
    cleared = 0
    for guild in guilds.values():
        if not isinstance(guild, dict):
            continue
        if guild.get("default_voice") != "fish-default" or guild.get("user_voices"):
            changed += 1
        cleared += len(guild.get("user_voices", {}))
        guild["default_voice"] = "fish-default"
        guild["user_voices"] = {}

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = path.with_name(f"{path.name}.pre-fish-{stamp}")
    shutil.copy2(path, backup)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as tmp:
        json.dump(data, tmp, ensure_ascii=False, indent=2)
        tmp.write("\n")
        temp_path = Path(tmp.name)
    try:
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)
    return changed, cleared, backup


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path)
    args = parser.parse_args()
    changed, cleared, backup = migrate(args.config)
    print(f"Updated guilds: {changed}; cleared user voice overrides: {cleared}; backup: {backup}")
