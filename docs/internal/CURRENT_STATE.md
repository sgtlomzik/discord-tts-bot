# TTS Bot Current State

Updated: 2026-06-09

## Summary

This repository contains the Discord TTS bot running as Docker container `discord_tts_bot`.
The current runtime uses local Piper TTS with two Russian voice profiles.
RHVoice, eSpeak, Lavalink integration, and other extra voice paths are not part of the active bot.

## Current Runtime

- Container: `discord_tts_bot`
- Image: `custom-tts-bot:latest`
- Compose file: `docker-compose.yml`
- Network: `container:gluetun`
- Restart policy: `unless-stopped`
- Runtime config: `.env` (ignored by git)
- Persistent bot config: `data/config.json` (ignored by git)
- Local model mount: `models:/app/models:ro` (ignored by git)

Current model files expected on the server:

- `models/ru_RU-ruslan-medium.onnx`
- `models/ru_RU-ruslan-medium.onnx.json`
- `models/ru_RU-irina-medium.onnx`
- `models/ru_RU-irina-medium.onnx.json`

## Voice And Audio Decisions

- Default voice profile: `piper-ruslan`
- Available profiles: `piper-ruslan`, `piper-irina`
- Default engine: Piper ONNX
- `PIPER_LENGTH_SCALE` is configured from `.env`; current logs showed `0.90`.
- Preroll mode: silence, not audible hiss.
- Idle frame mode: silence.
- Continuous stream mode is enabled to avoid clipped phrase starts.
- Startup warmup generates a short Piper phrase before normal queue processing.

Piper remains the best runtime fit for this weak server: low memory usage, predictable CPU load,
and no GPU requirement. RVC/XTTS-style experiments should be prepared on a stronger personal PC,
then only lightweight deployable artifacts should be copied here.

## Commands And Behavior

Slash command group: `/voicebot`

Important commands:

- `/voicebot on` and `/voicebot off`: enable or disable TTS for the server.
- `/voicebot allow` and `/voicebot deny`: manage users whose messages are voiced.
- `/voicebot voices`: list available voice profiles.
- `/voicebot voice-set`: change the server default voice.
- `/voicebot voice-user`: assign a voice to one user.
- `/voicebot voice-clear`: clear a user's voice override.
- `/voicebot status`: show bot state.
- `/voicebot queue-clear`: clear queued TTS jobs.
- `/voicebot test`: play a test phrase.

`/voicebot test` supports remote playback:

1. If `voice_channel` is provided, the bot uses that channel.
2. Otherwise, if the bot is already connected, it uses the bot's current voice channel.
3. Otherwise, it falls back to the caller's current voice channel.
4. If no channel can be resolved, it asks the caller to choose a voice channel.

The command responds to Discord before queueing audio. This avoids `Unknown interaction`
errors when voice connect or TTS preparation takes too long.

## Idle And Disconnect Behavior

- The bot schedules idle disconnect after playback.
- Idle disconnect works even if an allowed user remains in the voice channel.
- After explicit or idle disconnect, auto-connect is suppressed briefly to avoid immediate reconnect loops.
- Voice connect failures set a short cooldown before retrying.

## Current Server State Checked On 2026-04-26

Memory snapshot:

- Total RAM: `5.6GiB`
- Used RAM: about `1.9GiB`
- Available RAM: about `3.7GiB`
- Swap used: about `468MiB`

TTS container snapshot:

- `discord_tts_bot`: about `175MiB` RAM after restart and warmup.

Disabled containers:

- `dota_profiler`
- `lavalink-unmanaged`

They can be started again with:

```bash
docker start dota_profiler lavalink-unmanaged
```

## Git State And Recent Commits

Recent commits:

- `386336e feat: allow remote tts test playback`
- `1d753d8 fix: simplify tts bot to piper and idle disconnect cleanly`
- `2c822a9 chore: checkpoint current beta tts bot state`

Ignored local/runtime paths:

- `.env`
- `data/*.json`
- `.venv/`
- `models/`
- `.codex`
- `__pycache__/`

## Verification Commands

Run tests:

```bash
.venv/bin/python -m unittest -v test_bot.py
```

Build and run:

```bash
docker compose build
docker compose up -d
docker logs --tail 120 discord_tts_bot
```

Check resources:

```bash
docker stats --no-stream discord_tts_bot
free -h
```

## Notes For Future Voice Work

For custom voices, the practical deployment path is:

1. Train or prepare the voice model on a personal PC.
2. Export a Piper-compatible ONNX voice if possible.
3. Copy the `.onnx` and `.onnx.json` files into `models/`.
4. Add a new `VoiceProfile` in `bot.py`.
5. Add tests for voice selection and config loading.

RVC is voice conversion, not standalone TTS. It needs a source TTS voice first and adds CPU/RAM
load and latency. On this server it should not be placed in the live Discord path unless proven
with measurements.
