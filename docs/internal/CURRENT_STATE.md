# TTS Bot Current State

Updated: 2026-10-24

## Summary

The Discord TTS bot runs as two Docker services from one image
(`ghcr.io/sgtlomzik/discord-tts-bot`):

- `discord_tts_bot` (`tts_bot`): speaks allowed users' messages in voice,
  records the voice channel and transcribes it with Groq Whisper.
- `discord_tts_web` (`tts_web`): the read-only recordings website
  (`python -m ttsbot.web`, see [RECORDINGS_WEB.md](../RECORDINGS_WEB.md)).

How the code is organised is described in [ARCHITECTURE.md](../ARCHITECTURE.md).

## Runtime

- Compose files: `docker-compose.yml` plus an untracked
  `docker-compose.override.yml` that puts the bot on the gluetun VPN
  (`network_mode: container:gluetun`).
- Image tag: `TTS_IMAGE_TAG` in `.env`, used by both services. CI
  (`.github/workflows/docker.yml`) publishes `latest` and `sha-<short sha>`
  for every push to `main`; production pins a `sha-` tag.
- Runtime config: `.env` (untracked, mode 600).
- State in `data/` (untracked): `state.db` (SQLite: guild settings,
  voice catalog and fallback chain, provider pauses; the `config.json`,
  `voices.json` and `provider_health.json` of earlier versions were
  imported into it once and are no longer read),
  `tts_cache/`, `recordings/`.
- Models: `models/` (Piper ONNX voices, read-only mount).

## Voices and providers

- Providers: Fish Audio, ElevenLabs (several keys), Gemini through
  OpenRouter, MiniMax (only with `MINIMAX_API_KEY`) and local Piper.
- Guild default voice: `fish-default`; fallback chain
  `fish-default -> piper-ruslan`.
- Health: one circuit breaker per cloud provider, persisted in
  `data/state.db`; `/fallback status` shows it.

## Deploying

1. Merge to `main`; wait for the Docker workflow.
2. Set `TTS_IMAGE_TAG=sha-<short sha>` in `.env`.
3. `docker compose pull && docker compose up -d`.
4. Check `docker logs --tail 100 discord_tts_bot` and `docker ps`.

Rolling back is the same with the previous tag.

## Verification

Run the test suite inside the image, with the working tree mounted:

```bash
docker run --rm -v "$PWD:/app" -w /app --entrypoint python \
  ghcr.io/sgtlomzik/discord-tts-bot:latest -m unittest discover -s tests -p 'test_*.py'
node --test tests/web
```

Resources: `docker stats --no-stream discord_tts_bot discord_tts_web`
(the bot uses about 400 MiB with recording on).
