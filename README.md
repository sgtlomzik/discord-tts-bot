# Discord TTS Bot

**English** | [Русский](README.ru.md)

A self-hosted Discord bot that reads chat messages aloud in a voice channel.
Fish Audio and ElevenLabs stream Ogg/Opus speech for low-latency playback.
Local [Piper](https://github.com/OHF-Voice/piper1-gpl) voices work offline, and
MiniMax and Gemini (through OpenRouter) voices are available too. Cloud
failures fall back to Piper.

[![CI](https://github.com/sgtlomzik/discord-tts-bot/actions/workflows/ci.yml/badge.svg)](https://github.com/sgtlomzik/discord-tts-bot/actions/workflows/ci.yml)
[![Docker](https://github.com/sgtlomzik/discord-tts-bot/actions/workflows/docker.yml/badge.svg)](https://github.com/sgtlomzik/discord-tts-bot/actions/workflows/docker.yml)

## Features

- **Reads chat into voice** — whitelisted users' messages are synthesized and
  played in their current voice channel; the bot auto-connects and
  auto-disconnects when idle.
- **Five TTS engines** — Fish Audio, ElevenLabs, MiniMax, Gemini (through
  OpenRouter) and local Piper. Cloud failures fall back to Piper; repeated
  phrases use an on-disk LRU cache.
- **Low latency** — Fish and ElevenLabs stream Ogg/Opus over HTTP; the bot
  extracts 20 ms Opus packets and sends them straight to Discord without
  decoding or re-encoding. One continuous Opus player serves every engine
  (Piper, MiniMax and Gemini PCM is encoded in place), so switching voices
  never restarts it.
  Cloud connections stay open for 120 s and are warmed while an allowed
  user is typing, so a message after a pause skips the TLS handshake. The
  next message is synthesized while the previous plays.
- **Smart message merging** — short bursts of messages from one user are
  merged into a single natural phrase (`selective_hold_v2`), while reactions,
  emoji and questions play immediately.
- **Speech-friendly text handling** — URLs and markup are stripped, unicode
  emoji are spoken by their Russian names, mentions are read as display
  names, custom server emoji get configurable pronunciations (each server
  sets them only for its own emoji).
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
| `TTS_DEFAULT_VOICE_PROFILE` | Default voice for new servers (`piper-ruslan`, `fish-default`, …) |
| `MINIMAX_API_KEY` | Enables MiniMax cloud voices (optional) |
| `FISH_API_KEY` | Fish Audio key; keep it in `.env` only |
| `FISH_REFERENCE_ID` | Fish voice ID; seeds the `fish-default` profile |
| `FISH_TTFA_TIMEOUT` | Seconds to wait for Fish's first audio packet before Piper (default 5) |
| `ELEVENLABS_API_KEY` / `ELEVENLABS_API_KEYS` | Enables ElevenLabs; several keys form a ring (see below) |
| `OPENROUTER_API_KEY` | Enables Gemini voices through OpenRouter (optional) |
| `TTS_QUOTA_COOLDOWN_SECONDS` | Pause a cloud provider after a balance/plan error (default 1800) |
| `TTS_PRIMARY_PROVIDER` | `local`, `minimax`, `fish`, `gemini` or `elevenlabs` |
| `TTS_MERGE_ALGORITHM` | `selective_hold_v2`, `legacy` or `off` |

For Fish, put `FISH_API_KEY` and `FISH_REFERENCE_ID` in `.env`, then select
`/voicebot voice-set voice:fish-default`, or set
`TTS_DEFAULT_VOICE_PROFILE=fish-default` so new servers start with Fish. The
bot never switches the default to Fish on its own. Defaults are `s2.1-pro-free`,
`opus`, `low`, `chunk_length=150`, and `opus_bitrate=48000`. Existing per-user
voice assignments must be changed or cleared separately. The direct playback
cache stores Discord-ready packets in `.dopus`; voices with a pitch shift keep
the raw Ogg in `.opus`. Cache keys include the text, `reference_id`,
model, and the settings sent to Fish (pitch is not among them).

If Fish sends no audio packet within `FISH_TTFA_TIMEOUT` seconds (response
headers alone do not count), that message is spoken by the Piper fallback
voice and a `Fish first audio exceeded` warning is logged. Repeated Fish or
MiniMax failures open a circuit breaker for `CB_COOLDOWN_SECONDS`. An
exhausted balance or plan (Fish HTTP 402, MiniMax status 1008 or 2056)
pauses that provider for `TTS_QUOTA_COOLDOWN_SECONDS` instead; rate limits
(HTTP 429, MiniMax status 1039) count as ordinary failures.

To move an existing install to Fish in one step, stop the bot and run
`python scripts/migrate_fish_default.py data/config.json`. It sets every
guild's default voice to `fish-default`, clears per-user overrides and writes
a timestamped backup next to `config.json`.

`/voicebot voice-clone` creates a private Fish voice from an attached audio
sample (any `audio/*` upload, or WAV/MP3/M4A/OGG/Opus/FLAC by extension; OGG
covers Discord voice messages). The bot waits for training, checks a short TTS
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
with a non-20 ms duration also go through ffmpeg, reusing the bytes already
received instead of a second Fish request. The other controls use
Fish TTS parameters. Voice tuning survives restarts; every setting except
pitch changes the cache key.
`/voicebot stats` reports successful Fish requests and input characters for
the current session (this is not Fish billing usage) and the Fish and MiniMax
circuit-breaker states with the remaining cooldown.

### ElevenLabs

Set `ELEVENLABS_API_KEY` (the key needs the `text_to_speech` permission;
`voices_read` adds voice_id autocomplete) and add voices with
`/voicebot voice-add name:my-voice voice_id:<id> provider:ElevenLabs`; the
bot checks one short generation before saving. `ELEVENLABS_VOICE_ID`
optionally seeds an `eleven-default` profile. The default model is
`eleven_v4_turbo` (first audio ~0.2 s, 0.5 credit per character).

`ELEVENLABS_FORMAT=opus_48000_64` (the default) returns Ogg/Opus with 20 ms
packets, which take the same direct path as Fish. `pcm_48000` or
`pcm_24000` returns raw PCM, framed in-process without ffmpeg.
`/voicebot voice-tune` sets `stability`, `similarity` and the model of an
ElevenLabs voice (`reset` goes back to the voice's own settings). Voice
Library voices need a paid ElevenLabs plan; premade voices work on a free one.

`ELEVENLABS_API_KEYS=k1,k2,k3` spreads spending over several accounts. When
the active key is out of credits (under 50 left) or invalid, the same
message is retried with the next key before any audio plays, and after the
last key comes the first. A message that is only longer than a key's
remaining credits goes to the next key without moving the ring. When every
key is empty, Piper speaks and ElevenLabs pauses for
`TTS_QUOTA_COOLDOWN_SECONDS`. The active key survives restarts (its hash is
kept in `data/config.json`), and `/voicebot stats` shows session credits
per key. Cloned and Voice Library voices must exist in every account of the
ring.

## Commands

`/voicebot` group: `on`, `off`, `allow`, `deny`, `voices`, `voice-set`,
`voice-user`, `voice-clear`, `voice-add`, `voice-fish-add`, `voice-clone`,
`voice-tune`, `voice-fish-tune`, `fish-latency`,
`voice-describe`, `voice-say-set`, `voice-say-clear`, `emoji-alias`,
`emoji-aliases`, `emoji-alias-remove`, `status`, `stats`, `test`,
`limit`, `queue-clear` — plus legacy `!tts join` / `!tts stop` text commands.

## Usage statistics

`scripts/usage_stats.py` reports how many characters a user sent to synthesis
over a period: total, average per day and per 30 days. "Characters" means
the text length after the bot's normalization (emoji aliases, mentions,
stripped URLs, the `TTS_MAX_CHARS` cap) — exactly what the TTS provider got.

It combines the bot logs (`Queued TTS ... author=<id>` lines: exact, but
Docker drops them when the container is recreated) with the user's Discord
channel history (REST API, bot token) for the whole period. Each log line is
matched to its Discord message; the voiced share measured inside the log
window is applied to the history before it to fill the full period. A linear
extrapolation and an upper bound are printed for comparison.

Run it in the bot image from the repository root on the host:

```bash
docker logs discord_tts_bot 2>&1 | docker run --rm -i --env-file .env   -v "$PWD/scripts:/app/scripts:ro" -v "$PWD/data:/app/data:ro"   ghcr.io/sgtlomzik/discord-tts-bot:latest   python scripts/usage_stats.py --user <discord_user_id> --days 30 > exports/usage.md
```

Options: `--days` (default 30), `--guild` / `--channel` (repeatable) when
the user has no log lines, `--log-file` instead of stdin, `--json`.

## Development

```bash
python3.11 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python -m unittest discover -s tests -p 'test_*.py'  # ~360 tests, no network needed
```

The application lives in the `ttsbot/` package; `bot.py` is the entrypoint
and composition root. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for
the module map and runtime flow. Historical design notes are under
[docs/internal/](docs/internal/).

## License

[MIT](LICENSE)
