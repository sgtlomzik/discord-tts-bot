# Voice samples live here.

This directory holds audio samples used by `scripts/clone_voice.py` to
register voices on MiniMax. Files in this directory are NOT tracked
by git (see `.gitignore` at the repo root).

## Workflow

1. Drop a clean voice sample here (e.g. `bussshy.mp3`).
   - Format: mp3 / m4a / wav
   - Length: 10 seconds to 5 minutes
   - Size: up to 20 MB
   - Quality: clean voice, no music, no background noise

2. Run from the repo root (with venv active):
   ```
   python scripts/clone_voice.py scripts/voice_samples/bussshy.mp3 bussshy
   ```

3. The script will:
   - Upload the sample to MiniMax
   - Trigger the voice clone (model: speech-2.8-hd for preview)
   - Synthesize a short Russian phrase to verify it works
   - Save the preview to `_preview_bussshy.mp3` so you can listen

4. Copy the printed `MINIMAX_VOICE_ID` into the bot's `.env`.

## Why .gitignore?

Voice samples may contain personal recordings. They are kept locally
on the operator's machine and never committed.