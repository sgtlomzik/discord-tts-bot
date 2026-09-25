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
- **Low latency** — Fish streams Ogg/Opus over HTTP; the bot extracts 20 ms
  Opus packets and sends them straight to Discord without decoding or
  re-encoding. One continuous Opus player serves every engine (Piper and
  MiniMax PCM is encoded in place), so switching voices never restarts it.
  A keep-alive HTTP client is reused, and the next message is synthesized
  while the previous plays.
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
| `FISH_TTFA_TIMEOUT` | Seconds to wait for Fish's first audio before Piper (default 5) |
| `MINIMAX_QUOTA_COOLDOWN_SECONDS` | Pause MiniMax after a quota/balance error (default 1800) |
| `TTS_PRIMARY_PROVIDER` | `local`, `minimax`, or `fish` |
| `TTS_MERGE_ALGORITHM` | `selective_hold_v2`, `legacy` or `off` |

For Fish, put `FISH_API_KEY` and `FISH_REFERENCE_ID` in `.env`, then select
`/voicebot voice-set voice:fish-default`. Defaults are `s2.1-pro-free`,
`opus`, `low`, `chunk_length=150`, and `opus_bitrate=48000`. Existing per-user
voice assignments must be changed or cleared separately. The direct playback
cache stores Discord-ready packets in `.dopus`; older `.opus` files are
converted on read without ffmpeg. Cache keys include the text, `reference_id`,
model, and the settings sent to Fish (pitch is not among them).

If Fish sends no audio within `FISH_TTFA_TIMEOUT` seconds, that message is
spoken by the Piper fallback voice and a `Fish TTFA exceeded` warning is
logged. Repeated Fish or MiniMax failures open a circuit breaker for
`CB_COOLDOWN_SECONDS`. A MiniMax balance or plan limit (status 1008 or 2056)
pauses MiniMax for `MINIMAX_QUOTA_COOLDOWN_SECONDS` instead; rate limits
(HTTP 429, status 1039) count as ordinary failures.

To move an existing install to Fish in one step, stop the bot and run
`python scripts/migrate_fish_default.py data/config.json`. It sets every
guild's default voice to `fish-default`, clears per-user overrides and writes
a timestamped backup next to `config.json`.

`/voicebot voice-clone` now creates a private Fish voice from an attached
WAV/MP3/M4A/OGG/Opus sample (OGG covers Discord voice messages). The bot waits for training, checks a short TTS
generation, then saves its `reference_id` in `data/voices.json`. Assign it
with `voice-set` or `voice-user`.
Import a library voice with `/voicebot voice-fish-add name:my-voice
reference_id:YOUR_ID`, then assign it with `/voicebot voice-user`.
`/voicebot voice-fish-tune` configures speed, volume in dB, emotion,
pitch, model, temperature, and top_p for each Fish voice. Set the global
Fish latency mode in Discord with `/voicebot fish-latency mode:balanced`
(`low`, `balanced`, or `normal`). Omit `mode` to show the current setting.
The default is `low`; a Discord selection persists in `data/config.json`
across restarts and overrides `FISH_LATENCY` from `.env`. The mode is included
in TTS cache keys. Pitch is applied
locally by ffmpeg, so changing it reuses cached Fish audio, but it disables
direct Opus playback for that profile. Packets
with a non-20 ms duration also fall back to ffmpeg. The other controls use
Fish TTS parameters. Voice tuning survives restarts; every setting except
pitch changes the cache key.
`/voicebot stats` reports successful Fish requests and input characters for
the current session (this is not Fish billing usage) and the Fish and MiniMax
circuit-breaker states with the remaining cooldown.

## Commands

`/voicebot` group: `on`, `off`, `allow`, `deny`, `voices`, `voice-set`,
`voice-user`, `voice-clear`, `voice-add`, `voice-fish-add`, `voice-clone`,
`voice-tune`, `voice-fish-tune`, `fish-latency`,
`voice-describe`, `voice-say-set`, `voice-say-clear`, `emoji-alias`,
`emoji-aliases`, `emoji-alias-remove`, `status`, `stats`, `test`,
`limit`, `queue-clear` — plus legacy `!tts join` / `!tts stop` text commands.

## Development

```bash
python3.11 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python -m unittest discover -s tests -p 'test_*.py'  # ~340 tests, no network needed
```

The application lives in the `ttsbot/` package; `bot.py` is the entrypoint
and composition root. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for
the module map and runtime flow. Historical design notes are under
[docs/internal/](docs/internal/).

## License

[MIT](LICENSE)
