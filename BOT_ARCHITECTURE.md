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
6. synthesizes speech with Piper;
7. converts the generated WAV into PCM frames;
8. plays the frames into a Discord voice channel;
9. disconnects when the voice channel goes idle.

The current production runtime is intentionally small:

- one Python module for the bot logic;
- one unittest file for coverage;
- one container image;
- one local Piper voice model;
- one JSON config file for guild state.

## 2. Runtime Layout

The active runtime pieces are:

- `bot.py` - application logic, Discord events, queueing, TTS, playback, and slash commands.
- `test_bot.py` - unit and async integration-style tests.
- `docker-compose.yml` - container wiring and bind mounts.
- `Dockerfile` - image build and system packages.
- `requirements.txt` - Python dependencies.
- `.env.example` - runtime flag reference.
- `models/ru_RU-ruslan-medium.onnx` and `.json` - Piper voice model files.
- `data/config.json` - persisted guild/user settings.

At runtime, the container mounts:

- `/app/bot.py`
- `/app/models`
- `/app/data`

This means code changes and config changes are reflected without rebuilding the whole image in the same way a fully baked image would require.

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

## 7. Piper Synthesis Path

Piper is the only active engine in production right now.

The synthesis path is:

```text
text
  -> generate_tts_file()
  -> generate_piper_file()
  -> WAV file in tmp storage
  -> ffmpeg PCM conversion
  -> Discord playback
```

The key runtime objects are:

- `VOICE_PROFILES` - currently contains only `piper-ruslan`;
- `PiperVoice` - loaded lazily and cached by model path;
- `SynthesisConfig` - used for speaker and length-scale tuning when needed.

This is the main extension point for a new engine. The outer worker and playback path do not need to change if the new engine can produce compatible audio.

## 8. Playback Path

After synthesis, the bot converts audio into Discord-friendly output.

There are two playback modes:

- continuous PCM stream;
- fallback FFmpeg playback file path.

The default path uses the continuous stream source:

```text
WAV
  -> ffmpeg PCM frames
  -> ContinuousTTSAudioSource
  -> Discord voice client
```

Why this matters:

- it keeps playback smooth;
- it allows idle silence between jobs;
- it avoids reopening the player for every job when continuous stream is enabled.

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
- default voice profile;
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
- `status` - show current runtime state;
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

- `TTS_DEFAULT_VOICE_PROFILE=piper-ruslan`
- `PIPER_MODEL_PATH=/app/models/ru_RU-ruslan-medium.onnx`
- `PIPER_CONFIG_PATH=/app/models/ru_RU-ruslan-medium.onnx.json`

## 13. Tests

`test_bot.py` is the main safety net.

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

The current test suite is important because the architecture is compact: most behavior changes happen in one file, so tests are the main guardrail.

## 14. File Responsibility Map

### `bot.py`

Holds the bot lifecycle, merge logic, TTS generation, playback, voice handling, and commands.

### `test_bot.py`

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

