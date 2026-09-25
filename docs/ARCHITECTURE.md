# TTS Bot Architecture

This document describes the active Discord TTS bot in this repository as it exists now.
It focuses on the code that actually runs the bot, how the runtime is wired together, and where the main extension points are.

## 1. System Overview

The bot is a single-process Discord service that:

1. listens for Discord messages and voice state changes;
2. checks whether the author is allowed to use TTS;
3. normalizes and classifies message text;
4. optionally merges short messages into a buffer;
5. queues TTS jobs;
6. synthesizes speech with the selected Fish, MiniMax or Piper voice;
7. turns the result into 20 ms frames: Fish Opus packets pass through
   as-is, everything else is decoded to PCM;
8. plays the frames into a Discord voice channel through one continuous
   Opus player;
9. disconnects when the voice channel goes idle.

The current production runtime is intentionally small:

- one Python package (`ttsbot/`) plus a thin `bot.py` entrypoint;
- a unittest suite (`tests/`) for coverage;
- one container image;
- one local Piper voice model;
- one JSON config file for guild state.

## 2. Runtime Layout

The active runtime pieces are:

- `bot.py` - composition root and entrypoint: reloads config, creates the
  bot, attaches events and slash commands, re-exports the public API
  (the tests exec this file per test case).
- `ttsbot/` - the application package:
  - `config.py` - every env-derived tunable as a module global, with
    `reload()`; runtime code reads `config.NAME` at call time.
  - `textnorm.py` - Discord markup stripping, emoji aliases, mention
    resolution, `normalize_for_tts`.
  - `messages.py` - `ParsedMessage` / `MergeBufferState` and
    `analyze_message_for_merge` for the merge layer.
  - `models.py` - `VoiceProfile`, `GuildConfig`, `TTSJob`,
    `PreparedAudio` and the hardcoded Piper `VOICE_PROFILES`.
  - `store.py` - `BotConfigStore` (data/config.json persistence).
  - `audio.py` - PCM constants and framing, ffmpeg command builders
    (including the Fish pitch filter), Opus loading,
    `ContinuousTTSAudioSource`.
  - `errors.py` - `QuotaExhaustedError`, shared by the Fish and MiniMax
    "balance or plan used up" errors.
  - `ogg_opus.py` - incremental Ogg/Opus demuxer that turns Fish HTTP
    chunks into 20 ms Discord packets, plus the `.dopus` frame-cache format.
  - `core.py` - the `TTSBot` class: construction and task lifecycle,
    composed from the four concern mixins below.
  - `voice_lifecycle.py` - connect/move locks, cooldowns, stale-client
    recovery, idle disconnect, auto-connect policy.
  - `merge.py` - the enqueue boundary and legacy/selective-hold merge
    decisions.
  - `pipeline.py` - Piper/MiniMax/Fish synthesis, the streaming fast path,
    the prefetch generation worker and the legacy single worker.
  - `playback.py` - PCM preparation and the continuous Opus player feed.
  - `commands.py` - `build_commands(bot)` creates the /voicebot group
    bound to a bot instance; `events.py` - `register_events(bot)`.
- `ttsbot/providers.py` - provider abstraction: dispatcher, MiniMax
  client and its error classes, phrase cache, circuit breaker (including
  the long quota trip).
- `ttsbot/fish.py` - Fish HTTP client, request configuration and in-flight
  deduplication.
- `ttsbot/voice_registry.py` - the unified voice catalog
  (data/voices.json).
- `scripts/` - operator tools: model download, MiniMax voice cloning,
  `migrate_fish_default.py` (moves every guild to `fish-default`).
- `tests/` - unit and async integration-style tests.
- `docker-compose.yml` - container wiring and bind mounts.
- `Dockerfile` - image build and system packages.
- `requirements.txt` - Python dependencies.
- `.env.example` - runtime flag reference.
- `models/ru_RU-ruslan-medium.onnx` and `.json` - Piper voice model files.
- `data/config.json` - persisted guild/user settings.

At runtime, the container mounts only state:

- `/app/models` (read-only voice models)
- `/app/data` (guild config, voice catalog, phrase cache)

Code ships inside the image (built by CI and published to GHCR), so a
deployment is `docker compose pull && docker compose up -d`. For hot-reload
development, bind-mount `./bot.py` and `./ttsbot` via a local
`docker-compose.override.yml`.

## 3. Execution Flow

### 3.1 Startup

On boot, the bot:

1. loads environment variables;
2. checks token and whitelist state;
3. loads the Opus library;
4. starts the Discord bot;
5. registers slash commands;
6. launches the background TTS worker;
7. performs a warmup synthesis pass.

Representative startup flow:

```text
bot.run()
  -> on_ready()
  -> tree.sync()
  -> create tts_worker task
  -> warmup_tts()
```

### 3.2 Message Intake

Message handling lives in `on_message()`:

```python
@bot.event
async def on_message(message: discord.Message) -> None:
    if message.author.bot:
        return

    if (
        message.guild is not None
        and bot.config_store.is_enabled(message.guild.id)
        and bot.config_store.is_allowed(message.guild.id, message.author.id)
        and message.author.voice
        and isinstance(message.author.voice.channel, discord.VoiceChannel)
    ):
        await bot.queue_or_merge_message(
            message.content,
            message.author.voice.channel,
            message.author.id,
            message.channel.id,
        )

    await bot.process_commands(message)
```

This is the gate that decides whether a Discord message can enter the TTS pipeline at all.

### 3.3 Voice Updates

`on_voice_state_update()` handles:

- auto-connect for allowed users who join voice;
- idle disconnect when no allowed users remain in the voice channel.

This keeps the bot responsive without requiring a manual join command every time.

## 4. Message Normalization

The text layer is split into two functions:

- `process_text()` - removes URLs, replaces known custom emoji aliases, normalizes whitespace and newlines;
- `analyze_message_for_merge()` - classifies the message for merge decisions.

Example shape:

```python
text = process_text(raw_text)
parsed = analyze_message_for_merge(raw_text)
```

The parser is intentionally lightweight. It recognizes:

- custom emoji markup;
- mentions;
- URLs;
- Unicode emoji;
- single-digit and single-symbol replies;
- caps shout;
- keyboard-smash style text;
- question/terminal punctuation.

This parser exists so the merge layer can make a cheap decision before the bot spends time on synthesis.

## 5. Merge and Hold Logic

The current bot has two merge regimes:

- legacy short-message batching;
- `selective_hold_v2`.

The active decision code is in `queue_or_merge_message()`.

### 5.1 Legacy Path

When selective hold is disabled, short messages can be buffered for a fixed window and later flushed as one phrase.
That path still exists for rollback and compatibility.

### 5.2 Selective Hold Path

`selective_hold_v2` is the more nuanced pre-TTS decision layer. It distinguishes:

- no active buffer;
- already active buffer.

That matters because a short standalone message can be an isolated reaction, while the same short message inside an active cluster can be a meaningful continuation.

The buffer state tracks:

- author and voice channel key;
- first and last timestamps;
- deadline timestamp;
- generation id;
- current items;
- join separator.

Representative flow:

```text
incoming message
  -> analyze_message_for_merge()
  -> no buffer?
       -> immediate_long / immediate_special / hold_start / immediate_default
  -> buffer exists?
       -> hard_break / append_soft / flush_before_reclassify
```

Order preservation is a core requirement. If an older buffered message must be flushed first, the new message is not allowed to jump ahead of it.

## 6. Queue and Worker Model

### 6.1 TTS Queue

The bot uses an `asyncio.Queue` for TTS jobs.

Each job contains:

- final text to speak;
- target voice channel;
- queued timestamp;
- author id;
- guild id;
- text channel id;
- selected voice profile;
- original message timestamp.

This is the boundary between message handling and synthesis/playback.

### 6.2 Worker

`tts_worker()` is the background pipeline runner.

It does three big things:

1. waits for a job;
2. connects to voice and synthesizes in parallel;
3. prepares playback and streams the result.

Important behavior:

- voice connect and TTS generation are started concurrently;
- if TTS generation fails, the bot keeps the voice session;
- if voice connect fails, the bot cleans up the voice state;
- playback is followed by idle disconnect scheduling.

This is the part to preserve if a new TTS engine is introduced. The engine can change, but the worker contract should stay stable.

## 7. Synthesis Paths

The Fish cache-miss path starts playback before the HTTP response completes.
With pitch 0 (the default) no ffmpeg is involved:

```text
Discord text -> Fish POST /v1/tts (Ogg/Opus chunks)
             -> OggOpusDemuxer -> 20 ms Opus packets
             -> ContinuousTTSAudioSource -> Discord
             (packets are teed into a .dopus cache file, renamed on success)
```

A voice with a non-zero pitch goes through ffmpeg instead. So does a
stream whose packets are not 20 ms: the bytes already received are replayed
into ffmpeg and the same HTTP stream continues, so nothing is requested
twice.

```text
Fish Ogg/Opus -> ffmpeg stdin (pitch filter) -> PCM 48 kHz stereo frames
              -> ContinuousTTSAudioSource (encodes to Opus) -> Discord
              (the raw Ogg is cached as .opus)
```

The cache key covers the text, reference_id, model, latency, output format
and the tuning sent to Fish. Pitch is left out because ffmpeg applies it
after the request. `.dopus` hits play without decoding; `.opus` entries
(pitch shift) are decoded through ffmpeg. Identical concurrent requests
share one HTTP stream. MiniMax streaming and file paths share one cache key,
`voice_cache_key(voice)`, which includes the voice tuning, so re-tuning a
MiniMax voice never replays old audio.

Fallbacks and breakers:

- No first audio within `FISH_TTFA_TIMEOUT` (default 5 s; MiniMax uses
  `TTS_STREAM_TTFA_TIMEOUT`), an HTTP error or an open breaker sends the
  message to the registry's Piper fallback voice. Every case is logged. The
  budget runs until the first audio packet or decoded frame, because an Ogg
  stream's first bytes may be only the header pages.
- Fish and MiniMax each have a `CircuitBreaker` (`CB_FAILURE_THRESHOLD`
  failures open it for `CB_COOLDOWN_SECONDS`). The generation code does the
  accounting: a pre-audio exception is stored in `PreparedAudio.error` and
  passed to `TTSDispatcher.record_failure(breaker, exc)`.
- An exhausted balance or plan raises a `QuotaExhaustedError` subclass:
  `FishQuotaExhaustedError` (HTTP 402) or `MiniMaxQuotaExhaustedError`
  (status 1008/2056). `record_failure` then calls `CircuitBreaker.trip()` for
  `TTS_QUOTA_COOLDOWN_SECONDS`, which ordinary failures cannot shorten. Rate
  limits (HTTP 429, MiniMax 1039) count as ordinary failures.
- MiniMax reports errors as a plain JSON body, sometimes pretty-printed, even
  on streaming requests. The stream reader collects non-SSE lines and
  classifies them at the end instead of trusting the `Content-Type` header.

`/voicebot voice-clone` uploads a sample to Fish `POST /model` as multipart,
waits for `GET /model/{id}` to report `trained`, probes the new `reference_id`
through `/v1/tts`, and only then saves a Fish voice record in `voices.json`.

The local fallback path is:

```text
text
  -> generate_tts_file()
  -> generate_piper_file()
  -> WAV file in tmp storage
  -> ffmpeg PCM conversion
  -> Discord playback
```

The key runtime objects are:

- `VOICE_PROFILES` - contains the Ruslan and Irina Piper profiles;
- `PiperVoice` - loaded lazily and cached by model path;
- `SynthesisConfig` - used for speaker and length-scale tuning when needed.

Every engine ends up as 20 ms frames in the same player: Fish as Opus
packets, Piper and MiniMax as PCM. The Discord playback source and voice
lifecycle are shared.

## 8. Playback Path

After synthesis, the bot converts audio into Discord-friendly output.

There are two playback modes:

- continuous Opus stream (default, `TTS_CONTINUOUS_STREAM=1`);
- fallback FFmpeg playback file path.

The continuous source is always in Opus mode:

```text
Fish Opus packets ---------------------------\
WAV / MP3 / Ogg -> ffmpeg -> PCM frames -----> ContinuousTTSAudioSource -> Discord
```

`ContinuousTTSAudioSource.is_opus()` is true, so discord.py sends its
packets unchanged. It tells the two frame kinds apart by size (a PCM frame is
3840 bytes, an Opus packet at most 1275) and encodes PCM with its own
`discord.opus.Encoder`, created lazily on the player thread. Between jobs it
sends an Opus silence frame.

Why this matters:

- it keeps playback smooth;
- it allows idle silence between jobs;
- one player serves every engine, so alternating Fish and Piper/MiniMax
  voices never stops and restarts it.

The code also applies optional ffmpeg tuning:

- silence trimming;
- low-delay playback flags;
- preroll and tail padding.

## 9. Voice Connection and Idle Handling

The bot manages voice state separately from synthesis:

- per-guild voice connect locks prevent concurrent connect/move races;
- connect cooldown prevents rapid retry loops after failures;
- idle disconnect is scheduled after playback;
- auto-connect is suppressed briefly after explicit or idle disconnect.

This is a good separation to preserve when adding a new engine, because the TTS backend should not be responsible for voice lifecycle policy.

## 10. Persistence and Guild State

Persistent bot settings live in `data/config.json` and are managed by `BotConfigStore`.

The persisted state includes:

- whether TTS is enabled for the guild;
- allowed users;
- default voice profile (new guilds get `TTS_DEFAULT_VOICE_PROFILE`, or the
  registry fallback if that name is unknown);
- per-user voice override.

The config store is intentionally simple JSON, not a database.

That keeps the runtime easy to reason about and makes tests cheap.

## 11. Commands

The bot exposes the `/voicebot` slash group.

Current commands:

- `on` - enable TTS for the guild;
- `off` - disable TTS and clear active work;
- `allow` - add a member to the allowed list;
- `deny` - remove a member from the allowed list;
- `voices` - list available voice profiles;
- `voice-set` - change the guild default voice;
- `voice-user` - assign a voice profile to one member;
- `voice-clear` - clear a member's voice override;
- `voice-add` - register an existing MiniMax voice_id;
- `voice-fish-add` - register a Fish library voice by reference_id;
- `voice-clone` - clone a voice in Fish from an audio sample;
- `voice-tune` / `voice-fish-tune` - tune a MiniMax / Fish voice;
- `fish-latency` - show or set the global Fish latency mode;
- `voice-describe`, `voice-say-set`, `voice-say-clear` - voice metadata
  and fixed phrases;
- `emoji-alias`, `emoji-aliases`, `emoji-alias-remove` - custom emoji
  pronunciations;
- `status` - show current runtime state;
- `stats` - cache, queue, per-session usage and breaker states;
- `limit` - show or change the per-message text length cap (persisted in `data/config.json`);
- `queue-clear` - clear queued work;
- `test` - enqueue a test phrase, optionally to a specific voice channel.

There are also a couple of legacy text commands for manual control:

- `!tts stop`
- `!tts join`

## 12. Relevant Configuration

The active runtime flags are defined in `.env.example` and loaded from `.env` in production.

Main groups:

- Discord access and whitelist;
- queue sizing;
- preroll and tail tuning;
- continuous-stream control;
- idle disconnect timings;
- merge policy;
- selective hold settings;
- Piper model path and tuning.

The important current engine-specific values are:

- `FISH_MODEL=s2.1-pro-free`
- `FISH_FORMAT=opus`, `FISH_LATENCY=low`, `FISH_CHUNK_LENGTH=150`
- `FISH_OPUS_BITRATE=48000`
- `FISH_API_KEY` and `FISH_REFERENCE_ID` supplied from the untracked `.env`
- `FISH_TTFA_TIMEOUT=5`
- `CB_FAILURE_THRESHOLD=3`, `CB_COOLDOWN_SECONDS=60`
- `TTS_QUOTA_COOLDOWN_SECONDS=1800`
- `TTS_DEFAULT_VOICE_PROFILE=piper-ruslan`
- `PIPER_MODEL_PATH=/app/models/ru_RU-ruslan-medium.onnx`
- `PIPER_CONFIG_PATH=/app/models/ru_RU-ruslan-medium.onnx.json`

## 13. Tests

`tests/test_bot.py` is the main safety net.

It covers:

- text normalization;
- token parsing and message classification;
- config persistence;
- voice-channel resolution;
- audio helper commands;
- queue/merge behavior;
- selective hold edge cases;
- voice connect cooldowns;
- worker success and failure paths.

Engine-specific suites sit next to it: `test_fish.py` (Fish requests,
Ogg/Opus demuxing, the mixed Opus/PCM player, cache keys, pitch),
`test_minimax_quota.py` (quota errors, the long breaker trip, the Fish
first-audio timeout) and the `test_providers*.py` files.

The current test suite is important because it pins the public surface of the package: the tests exec `bot.py` per test case, mutate `ttsbot.config` for tuning, and exercise the pipeline through the facade — so a module can be reworked internally while the suite guards the observable behavior.

## 14. File Responsibility Map

### `bot.py`

Composition root: config reload, bot construction, command/event
registration, `main()`. Also the backward-compatibility facade — it
re-exports the package API so tests (and any external callers) can keep
using `bot.<name>`.

### `ttsbot/`

The application package; see section 2 for the per-module breakdown.
Config is read at call time (`config.NAME`), so tests tune behavior by
mutating `ttsbot.config` directly.

### `tests/`

Verifies parser behavior, queue semantics, engine selection assumptions, and worker behavior.

### `docker-compose.yml`

Describes how the bot is run in the existing host stack and which directories are mounted.

### `Dockerfile`

Builds the runtime image and installs OS-level dependencies like ffmpeg and libopus.

### `requirements.txt`

Defines the Python runtime dependency set.

### `.env.example`

Documents the supported runtime flags and their defaults.

### `data/config.json`

Stores live guild settings.

### `models/`

Stores the active Piper voice model files.

## 15. What Matters for a New Voice Engine

If the goal is to add a new voice engine, the architecture points that matter most are:

1. keep `on_message()` and `queue_or_merge_message()` mostly stable;
2. introduce a real engine boundary near `generate_tts_file()`;
3. preserve `TTSJob`, queueing, and worker flow;
4. make engine selection a profile-level concern, not a scattered global `if`;
5. keep playback and voice lifecycle unchanged unless the new engine genuinely needs a different artifact format;
6. add tests around engine selection, fallback, and missing-model behavior.

That is the smallest clean seam in the current codebase.
