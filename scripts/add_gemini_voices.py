#!/usr/bin/env python3
"""Register every prebuilt Gemini voice as ``gemini-<name>`` in voices.json.

Existing records are left untouched, so the script is safe to re-run. Stop
the bot first: it keeps the catalog in memory and would overwrite the file
on its next save.

    docker compose stop tts_bot
    docker compose run --rm --no-deps --entrypoint python tts_bot scripts/add_gemini_voices.py
    docker compose up -d tts_bot
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ttsbot import config  # noqa: E402
from ttsbot.gemini import GEMINI_VOICE_STYLES  # noqa: E402
from ttsbot.voice_registry import (  # noqa: E402
    PROVIDER_GEMINI, GeminiParams, VoiceRecord, load_registry, save_registry,
)


def main() -> int:
    path = config.VOICES_REGISTRY_PATH
    registry = load_registry(path)
    if registry is None:
        print(f"No readable voice registry at {path}", file=sys.stderr)
        return 1
    added = []
    for voice, style in sorted(GEMINI_VOICE_STYLES.items()):
        name = f"gemini-{voice.lower()}"
        if name in registry:
            continue
        registry.add(VoiceRecord(
            name=name, label=f"Gemini {voice}", description=style,
            provider=PROVIDER_GEMINI, gemini=GeminiParams(voice=voice),
        ))
        added.append(name)
    if added:
        save_registry(path, registry)
    print(f"added {len(added)}, total voices {len(registry.voices)}: {' '.join(added) or '-'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
