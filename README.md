# Discord TTS Bot

**English** | [Русский](README.ru.md)

A self-hosted Discord bot that reads chat messages aloud in a voice channel.
Fish Audio streams Ogg/Opus speech for low-latency playback. Local
[Piper](https://github.com/OHF-Voice/piper1-gpl) voices work offline, and
MiniMax voices remain available. Cloud failures fall back to Piper.

[![CI](https://github.com/sgtlomzik/discord-tts-bot/actions/workflows/ci.yml/badge.svg)](https://github.com/sgtlomzik/discord-tts-bot/actions/workflows/ci.yml)
[![Docker](https://github.com/sgtlomzik/discord-tts-bot/actions/workflows/docker.yml/badge.svg)](https://github.com/sgtlomzik/discord-tts-bot/actions/workflows/docker.yml)

## Features

- **Reads chat into voice** — whitelisted users' messages are synthesized and
  played in their current voice channel; the bot auto-connects and
  auto-disconnects when idle.
- **Three TTS engines** — Fish Audio, MiniMax, and local Piper. Cloud failures
  fall back to Piper; repeated phrases use an on-disk LRU cache.
- **Low latency** — Fish streams Ogg/Opus over HTTP into ffmpeg and the
  existing PCM player before generation finishes. A keep-alive HTTP client
  is reused, and the next message is synthesized while the previous plays.
- **Smart message merging** — short bursts of messages from one user are
  merged into a single natural phrase (`selective_hold_v2`), while reactions,
  emoji and questions play immediately.
- **Speech-friendly text handling** — URLs and markup are stripped, unicode
  emoji are spoken by their Russian names, mentions are read as display
  names, custom server emoji get configurable pronunciations.
- **Managed entirely from Discord** — the `/voicebot` slash-command group
  covers enabling TTS, the user whitelist, voices, cloning, emoji aliases,
  stats and queue control.

## Quick start (Docker)

Docker is optional — see [Running without Docker](#running-without-docker).
Prerequisites: a Discord application with a bot token
(enable the **Message Content** intent), Docker with the compose plugin.

```bash
git clone https://github.com/sgtlomzik/discord-tts-bot.git
cd discord-tts-bot

# 1. Configure
cp .env.example .env          # then edit: DISCORD_TOKEN, WHITELIST_USERS, ...

# 2. Download the Piper voice models (~120 MB, one time)
./scripts/download_models.sh

# 3. Run (pulls the prebuilt image from GHCR)
docker compose pull && docker compose up -d
```

Or build the image locally instead of pulling: `docker compose up -d --build`.
Host-specific tweaks (custom networking, extra mounts) belong in an untracked
`docker-compose.override.yml`, which compose merges automatically.

Invite the bot to your server with the `bot` + `applications.commands` scopes
and voice permissions (Connect, Speak), then in Discord:

```
/voicebot on
/voicebot allow @user
/voicebot test text: привет
```

## Running without Docker

The bot is a single Python process; Docker only packages ffmpeg/libopus and
loads the env file for you.

```bash
sudo apt install ffmpeg libopus0          # system deps (Debian/Ubuntu)
python3.11 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
./scripts/download_models.sh

cp .env.example .env                      # edit DISCORD_TOKEN, WHITELIST_USERS
set -a; . ./.env; set +a                  # nothing loads .env for you outside Docker
export PIPER_MODELS_DIR=./models BOT_CONFIG_PATH=./data/config.json
python bot.py
```

For unattended runs wrap the same thing in a systemd unit with
`EnvironmentFile=/path/to/.env`.

## Configuration

Everything is configured through environment variables — see
[.env.example](.env.example) for the full annotated list. The essentials:

| Variable | Purpose |
|---|---|
| `DISCORD_TOKEN` | Bot token (**required**) |
| `WHITELIST_USERS` | Comma-separated Discord user IDs allowed to use TTS |
| `TTS_DEFAULT_VOICE_PROFILE` | Default voice (`piper-ruslan`, `piper-irina`, …) |
| `MINIMAX_API_KEY` | Enables MiniMax cloud voices (optional) |
| `FISH_API_KEY` | Fish Audio key; keep it in `.env` only |
| `FISH_REFERENCE_ID` | Fish voice ID; seeds the `fish-default` profile |
| `TTS_PRIMARY_PROVIDER` | `local`, `minimax`, or `fish` |
| `TTS_MERGE_ALGORITHM` | `selective_hold_v2`, `legacy` or `off` |

For Fish, put `FISH_API_KEY` and `FISH_REFERENCE_ID` in `.env`, then select
`/voicebot voice-set voice:fish-default`. Defaults are `s2.1-pro-free`,
`opus`, `low`, `chunk_length=150`, and `opus_bitrate=48000`. Existing per-user
voice assignments must be changed or cleared separately. Fish cache files
use `.opus`; cache keys include the text, `reference_id`, model, and TTS settings.

## Commands

`/voicebot` group: `on`, `off`, `allow`, `deny`, `voices`, `voice-set`,
`voice-user`, `voice-clear`, `voice-add`, `voice-clone`, `voice-tune`,
`voice-describe`, `voice-say-set`, `voice-say-clear`, `emoji-alias`,
`emoji-aliases`, `emoji-alias-remove`, `status`, `stats`, `test`,
`limit`, `queue-clear` — plus legacy `!tts join` / `!tts stop` text commands.

## Development

```bash
python3.11 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python -m unittest discover -s tests -p 'test_*.py'  # ~300 tests, no network needed
```

The application lives in the `ttsbot/` package; `bot.py` is the entrypoint
and composition root. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for
the module map and runtime flow. Historical design notes are under
[docs/internal/](docs/internal/).

## License

[MIT](LICENSE)
