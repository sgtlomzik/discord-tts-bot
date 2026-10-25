# TTS Bot Architecture

This document describes the active Discord TTS bot in this repository as it exists now.
It focuses on the code that actually runs the bot, how the runtime is wired together, and where the main extension points are.

## 1. System Overview

The bot is a single-process Discord service that:

1. listens for Discord messages and voice state changes;
2. checks whether the author is allowed to use TTS;
3. turns the message into speakable text (markup, links, emoji, mentions);
4. queues a TTS job per message, in arrival order;
5. synthesizes speech with the selected Fish, ElevenLabs, MiniMax, Gemini
   or Piper voice, falling back along the voice's chain;
6. turns the result into 20 ms frames: Fish and ElevenLabs Opus packets
   pass through as-is, everything else is decoded to PCM;
7. plays the frames into a Discord voice channel through one continuous
   Opus player;
8. records and transcribes the voice channel when recording is on;
9. disconnects when the voice channel goes idle.

## 2. Runtime Layout

The active runtime pieces are:

- `bot.py` - the entrypoint (`python -u bot.py`), three lines.
- `ttsbot/` - the application package:
  - `app.py` - composition root: `create_bot()` re-reads the environment,
    builds a `TTSBot` and attaches the slash commands and event handlers
    bound to it; `main()` checks the essentials and runs it. Nothing
    happens on import.
  - `config.py` - every env-derived tunable as a module global, with
    `reload()`; runtime code reads `config.NAME` at call time.
  - `textnorm.py` - Discord markup stripping, emoji aliases, mention
    resolution, one cleanup behind `speakable_text` and `normalize_for_tts`.
  - `models.py` - `VoiceProfile`, `GuildConfig`, `TTSJob`,
    `PreparedAudio` and the hardcoded Piper `VOICE_PROFILES`.
  - `store.py` - `BotConfigStore` (guild settings in data/state.db; a failed
    save rolls memory back to what is on disk).
  - `statefile.py` - reading JSON state files: a damaged file raises
    `StateFileError` and stops startup instead of being replaced.
  - `supervisor.py` - `TaskSupervisor`: the long-running workers are
    restarted when they crash; `/voicebot status` lists them.
  - `audio.py` - PCM constants and framing, ffmpeg command builders
    (including the Fish pitch filter), Opus loading,
    `ContinuousTTSAudioSource`.
  - `ogg_opus.py` - incremental Ogg/Opus demuxer that turns Fish HTTP
    chunks into 20 ms Discord packets, plus the `.dopus` frame-cache format.
  - `core.py` - the `TTSBot` class: construction and task lifecycle,
    composed from the four concern mixins below.
  - `voice_lifecycle.py` - connect/move locks, cooldowns, stale-client
    recovery, idle disconnect, auto-connect policy.
  - `intake.py` - the enqueue boundary: `queue_message`, `enqueue_tts`,
    `clear_queue_for_guild`.
  - `pipeline.py` - Piper/MiniMax/Fish/Gemini/ElevenLabs synthesis, the
    streaming paths and the prefetch generation worker.
  - `playback.py` - PCM preparation and the continuous Opus player feed.
  - `commands.py` - `build_commands(bot)` creates the /voicebot and
    /fallback groups bound to a bot instance; `events.py` -
    `register_events(bot)`.
  - `recording/` - voice receive (off unless `RECORDING_ENABLED=1`):
    `client.py` (a `VoiceClient` subclass), `rtp.py` (transport
    decryption), `recorder.py` (per-stream chunk files with a reorder
    window), `ogg.py` (Ogg Opus writer), `vad.py` (speech filter);
    transcription: `groq.py` (client and audio budget), `worker.py`
    (queue), `transcript.py`.
- `ttsbot/providers.py` - provider abstraction: dispatcher (fallback chain,
  health checks), MiniMax client and its error classes, phrase cache.
- `ttsbot/health.py` - failure kinds, the circuit breaker with its backoff
  and quota pauses, and `HealthStore` (pauses saved in `data/state.db`).
- `ttsbot/errors.py` - marker classes every provider error inherits:
  `QuotaExhaustedError`, `AuthError`, `RateLimitError`, `RequestError`.
- `ttsbot/fish.py` - Fish HTTP client, request configuration and in-flight
  deduplication.
- `ttsbot/elevenlabs.py` - ElevenLabs streaming client, error mapping and
  the API-key ring.
- `ttsbot/gemini.py` - Gemini TTS through OpenRouter (raw PCM).
- `ttsbot/pcm.py` - `PcmFramer`: raw PCM to 48 kHz stereo 20 ms frames
  in-process (soxr), plus the `.pcm` cache format.
- `ttsbot/voice_registry.py` - the unified voice catalog
  (saved in data/state.db).
- `scripts/` - operator tools: model download, MiniMax voice cloning,
  `add_gemini_voices.py` (registers every prebuilt Gemini voice),
  `usage_stats.py` (per-user generated characters from logs + Discord history).
- `tests/` - unit and async integration-style tests; `tests/support.py`
  builds a fresh bot and a namespace of the package's names for them.
- `docker-compose.yml` - container wiring and bind mounts; both services
  use the image tag `TTS_IMAGE_TAG`.
- `Dockerfile` - image build and system packages.
- `requirements.txt` - Python dependencies.
- `.env.example` - runtime flag reference.

At runtime, the container mounts only state:

- `/app/models` (read-only voice models)
- `/app/data` (guild config, voice catalog, phrase cache, recordings)

Code ships inside the image (built by CI and published to GHCR), so a
deployment is setting `TTS_IMAGE_TAG` and `docker compose pull && docker
compose up -d`. For hot-reload development, bind-mount `./bot.py` and
`./ttsbot` via a local `docker-compose.override.yml`.

## 3. Execution Flow

### 3.1 Startup

`bot.py` calls `ttsbot.app.main()`, which builds the bot, checks the
token, whitelist and Opus library and runs it. `TTSBot.setup_hook` syncs
the slash commands and starts the workers under the supervisor:
`tts-generation` (warms up Piper first), `tts-playback`, and when
configured `tts-connection-keepalive`, `tts-provider-health` and
`stt-worker`.

### 3.2 Message Intake

`on_message()` lets a message in when the guild has TTS on, the author is
allowed (the guild's list or `WHITELIST_USERS`, unless the guild denied
them) and is in a voice channel. `queue_message()` hands the raw text and
its resolved mentions to `enqueue_tts()`, which cleans it once, applies
the length cap (`/voicebot limit`, else `TTS_MAX_CHARS`) and queues a
`TTSJob`. Every message is queued as it arrives; nothing is held back
or merged.

### 3.3 Voice Updates

`on_voice_state_update()` auto-connects when an allowed member joins or
switches a channel (not on mute, deafen or stream toggles) and schedules
the idle disconnect when no allowed member remains with the bot.

## 4. Queue and Worker Model

Each guild has its own `GuildSession` (`ttsbot/session.py`), created on
first use by `session_for()`. A guild's first message calls
`start_session()`, which starts the guild's two workers under the bot's
`TaskSupervisor` (`tts-generation:<guild>` and `tts-playback:<guild>`):

```text
session.queue (TTSJob) -> _generation_worker -> session.ready (PreparedAudio, lookahead)
                              | starts _generate(prepared) as a task
                              v
                         PreparedAudio.channel -> _playback_worker -> session.player
```

The generation worker puts each `PreparedAudio` into `session.ready` first,
which fixes the play order and bounds the prefetch window
(`TTS_PREFETCH_LOOKAHEAD`). It then starts that message's generation as its
own task and moves on, so the next message's request goes out while the
current one is still downloading or playing. The playback worker drains the
channels strictly in order into the guild's continuous player.

Each provider takes a bounded number of requests at once, across all
guilds (`TTSDispatcher.slot_for`, `<NAME>_MAX_CONCURRENCY`, default 1;
`PIPER_MAX_CONCURRENCY` for Piper's CPU): a plan's concurrency limit answers
429, which would pause the provider for everyone. A message that had to
wait for its slot checks the phrase cache again, so a phrase repeated back
to back is generated (and billed) once.

Guilds never wait for each other: a long message or a stuck player in one
guild holds up only that guild. `queue-clear`, `/voicebot off` and a
disconnect cancel the guild's queued and in-flight items
(`GuildSession.cancel_all()`). The drain wait after a message is bounded,
and a player stopped under the worker (`!tts stop`, a kick) never holds it.
`/voicebot status` shows each worker's state and restart count.

## 5. Synthesis Paths

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

Fallback chain:

- The voice catalog holds `fallback_chain`, a list of voice names tried in order
  when a message's voice cannot speak it, and each voice may have its own
  `fallback` list instead. `VoiceRegistry.chain_for(voice)` returns the
  voice followed by those voices (unknown names and repeats skipped). An
  unset chain means `[fallback_profile]` (the Piper voice), as before
  chains existed; an empty list means no fallback. Nothing in the code
  names a provider: a new provider only needs voices. `/fallback set`
  shows and edits both lists; it warns when a chain does not end in Piper.
- `_prepare_into` streams the chain's voices in turn. No first audio within
  the provider's budget (`FISH_TTFA_TIMEOUT`, default 5 s; MiniMax uses
  `TTS_STREAM_TTFA_TIMEOUT`), an HTTP error or an open breaker moves to the
  next voice, with one `Fallback ... voice=A -> B reason=...` line (INFO
  while the provider is paused, WARNING otherwise). The budget runs until
  the first audio packet or decoded frame, because an Ogg stream's first
  bytes may be only the header pages. A provider that failed for a reason
  other than this one message is skipped for the rest of the chain, so one
  outage costs one timeout. A voice whose provider is not configured is
  skipped; the first Piper voice is synthesized to a file
  (`TTSDispatcher.synthesize_piper`) and ends the chain. Each voice uses
  its own cache key. Every cloud provider takes the same path: it answers
  `stream_spec(text, voice)` (`ttsbot/streams.py`) with the kind of audio
  (Opus played as-is, Ogg or MP3 decoded by ffmpeg, raw PCM), its cache
  key and first-audio budget, and `_stream_voice_into` does the rest.

Provider health (`ttsbot/health.py`):

- Every cloud provider has a `CircuitBreaker`. The generation code stores a
  pre-audio exception in `PreparedAudio.error` and passes it to
  `TTSDispatcher.record_failure(breaker, exc)`, which sorts it with
  `classify_failure` by the marker classes in `ttsbot/errors.py`:

  | kind | examples | effect |
  |---|---|---|
  | request | missing voice, message too long for the credits left | not counted; next voice speaks this message |
  | transient | timeout, network error, 5xx, no audio | `CB_FAILURE_THRESHOLD` in a row pause for `CB_COOLDOWN_SECONDS`, x`CB_BACKOFF_FACTOR` per failed probe up to `CB_MAX_COOLDOWN_SECONDS` |
  | rate_limit | HTTP 429 | the Retry-After; without one, counted as transient |
  | quota | HTTP 402, MiniMax 1008/2056, all ElevenLabs keys empty | paused until checked (see below) |
  | auth | HTTP 401/403, MiniMax 1004 | like quota, starting at `TTS_AUTH_RECHECK_SECONDS` |

- The health worker (`_provider_health_worker`, every
  `TTS_HEALTH_CHECK_INTERVAL` s) runs `TTSDispatcher.check_health()` for
  paused providers whose pause is over, so a user message is not spent on
  finding out:
  - quota/auth: a provider with `check_quota()` is asked 60 s after the
    failure and then at its reported reset time, at least daily
    (`TTS_MAX_RECHECK_SECONDS`). ElevenLabs implements it with the free
    `GET /v1/user/subscription` per key (credits left, monthly reset) and
    moves the key ring to a key with credits. Without `check_quota`, a real
    request probes after `TTS_QUOTA_COOLDOWN_SECONDS`, doubling to a day.
  - transient: the provider's `ConnectionWarmer.reachable()` pings the
    host; while it does not answer the pause grows, once it answers the
    next request probes.
  A breaker waiting for its check (`await_check`) lets a real request probe
  anyway 5 minutes after the pause, in case the worker is stuck; a probe
  that never reports back frees the slot after 2 minutes.
- `HealthStore` keeps every breaker in `data/state.db` (`provider_health`)
  with a fingerprint of the provider's keys, so a
  provider out of quota stays paused across restarts, and a changed key
  starts clean. `/fallback status` shows each provider's state, its live
  quota where available, and resets a pause by hand.
- MiniMax reports errors as a plain JSON body, sometimes pretty-printed, even
  on streaming requests. The stream reader collects non-SSE lines and
  classifies them at the end instead of trusting the `Content-Type` header.

`/voicebot voice-clone` uploads a sample to Fish `POST /model` as multipart,
waits for `GET /model/{id}` to report `trained`, probes the new `reference_id`
through `/v1/tts`, and only then saves a Fish voice record in the catalog.

ElevenLabs uses `POST /v1/text-to-speech/{voice_id}/stream`, which sends
audio while it is generated (first audio ~0.2 s with `eleven_v4_turbo`).
`ELEVENLABS_FORMAT` picks one of two shared paths, neither with ffmpeg:

```text
opus_48000_* -> Ogg/Opus, mono, 20 ms packets
             -> _stream_ogg_opus_to_channel (the Fish direct path, .dopus cache)
pcm_*        -> raw s16le -> _stream_pcm_to_channel (the Gemini PcmFramer path, .pcm cache)
```

Several keys (`ELEVENLABS_API_KEYS`) form a ring inside
`ElevenLabsProvider.open_stream`: a key that is out of credits (under 50
left, read from the `quota_exceeded` message), HTTP 402 or an invalid key
moves the ring and the same request is retried with the next key before
any audio. A message only too long for a key's balance, or a voice the
key's plan does not allow (`paid_plan_required`), is retried with the next
key without moving the ring. When every key is empty the provider raises
`ElevenLabsQuotaExhaustedError` (a quota pause, checked through
`check_quota`); request-level errors (`ElevenLabsRequestError`: missing
voice, message too long for every key, plan-restricted voice) go to the
next voice of the chain without a breaker failure.
The active key is persisted as a sha256 fingerprint in `data/state.db`.

The local fallback path is:

```text
text
  -> _generate_file_into() -> TTSDispatcher.synthesize_piper()
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

## 6. Playback Path

After synthesis, the bot converts audio into Discord-friendly output.

Every message plays through the guild's continuous Opus source:

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

Decoded files (Piper, MiniMax, cache) get their leading and trailing
silence trimmed by ffmpeg (`TTS_TRIM_SILENCE`).

## 7. Voice Connection and Idle Handling

The bot manages voice state separately from synthesis. It is kept in the
guild's `GuildSession`, next to its queue:

- `voice_lock` prevents concurrent connect/move races;
- `connect_cooldown_until` prevents rapid retry loops after a failed
  connect or move; it is checked only when the bot has to connect or move,
  never against a connection that already works (`VoiceCooldown` skips the
  message without disconnecting);
- `idle_disconnect_task` disconnects after `IDLE_DISCONNECT_SECONDS` without
  allowed members, and `idle_stop_task` stops an idle continuous player;
- `auto_connect_suppressed_until` holds auto-connect off briefly after an
  explicit or idle disconnect;
- `player` is the guild's `ContinuousTTSAudioSource`.

This is a good separation to preserve when adding a new engine, because the TTS backend should not be responsible for voice lifecycle policy.

### 7.1 Voice recording

With `RECORDING_ENABLED=1` the bot joins voice undeafened (a deafened
member receives nothing) as `recording.client.RecordingVoiceClient`.
discord.py 2.7 has no receive API, so the client uses two internal hooks,
which is why requirements.txt pins discord.py:

- `VoiceConnectionState.add_socket_listener` gets every UDP packet on the
  socket reader thread. `rtp.decrypt_transport` removes the
  `aead_xchacha20_poly1305_rtpsize` layer, then the DAVE session
  discord.py keeps for sending decrypts the Opus packet for its sender.
- A voice-websocket hook reads SPEAKING (op 5), which maps each SSRC to a
  user id; packets from an SSRC not yet mapped wait (up to 1 s of them).

Other bots' audio and Discord's end-of-speech silence frames are dropped.
A packet DAVE cannot decrypt yet (the sender's key arrives ~0.5 s after
their audio when someone joins) is held for up to 3 s and retried once a
later packet from that user decrypts.

`recorder.ChannelRecorder` hands the packets to a writer thread that
muxes each user into `RECORDING_DIR/<guild>/<UTC start>-<channel>/<user>/NNNN.ogg`
without re-encoding. Discord keeps transmitting through breathing, room
noise and long runs of digital silence (25 of 35 s in the first live
test), so the writer decodes each packet only to classify it with WebRTC
VAD (`vad.py`, `RECORDING_VAD_AGGRESSIVENESS`, default 3: mode 2 still
kept loud breathing) and keeps speech runs with 200 ms before and 300 ms
after. A gap between kept speech longer than `RECORDING_PAUSE_MS` becomes
that much silence and starts a new "piece". Each closed file adds a line to
`chunks.jsonl` with its pieces as `[offset in file, wall-clock start]`,
which maps transcript timestamps back to real time; `session.json` holds
the channel and start/end times. Log lines: `Recording started`,
`Recording chunk closed ... audio_s= pieces= reason=full|idle|stop`,
`Recording stats` every minute and `Recording finished` with counters:
`packets` and `heard_ms` received, `written_ms` kept (the rest was
filtered out),
`filled_ms` short gaps kept as silence, `unknown_ssrc_packets`,
`dave_errors` (failed first attempts), `dave_recovered` / `dave_lost`,
`transport_errors`, `late_packets`, `bot_packets`, `vad_errors`.

A file stays open until `RECORDING_CHUNK_SECONDS` of speech or
`RECORDING_IDLE_CLOSE_SECONDS` (15 min) of that user's silence: Groq
bills each file for at least 10 s. Pieces are also appended to
`NNNN.pieces.jsonl` as they start, so a file left open by a crash or a
restart is recovered (`recorder.recover_session`) with exact timestamps.
Text the bot speaks for someone is logged to `tts.jsonl`, and each
speaker's display name to `names.json` (kept if they leave the server).

### 7.2 Transcription

With `GROQ_API_KEY` set, `recording.worker.TranscriptionWorker` (task
`stt-worker`) sends closed files to Groq Whisper (`STT_MODEL`,
`STT_LANGUAGE`, `verbose_json` with segments), oldest first, one at a
time. The disk is the queue: a file in `chunks.jsonl` without a
`NNNN.json` next to it is pending, so restarts lose nothing.

`groq.AudioBudget` keeps the plan's limits. The hourly audio limit is a
bucket refilling at 2 s per second (headers
`x-ratelimit-remaining-audio-seconds` / `-reset-audio-seconds`); the
budget follows the last response's level and falls back to its ledger
(`RECORDING_DIR/groq_ledger.jsonl`) after a restart or when a response
lacks those headers (seen from httpx, HTTP/1.1). The ledger also
enforces the day limit. A 429, 5xx or network error pauses sending for
the time Groq asks (or 30-60 s); another 4xx writes `NNNN.error.json`,
and the file is tried 3 times in all. A file over 25 MB is re-encoded to
32 kb/s mono first.

`NNNN.json` holds Groq's kept segments with `at`/`until` in wall-clock
time (through the chunk's pieces) and the dropped ones: known Whisper
hallucinations ("Субтитры сделал DimaTorzok", "Продолжение следует"),
no-speech and repetition loops. After each result the session's
`transcript.md` (`[HH:MM:SS] Name: text` in `TRANSCRIPT_TIMEZONE`, a
speaker's lines less than 2 s apart merged, TTS lines as `Name (TTS)`)
and `transcript.jsonl` are rebuilt. Log lines: `Transcription worker
started`, `Transcription done ... audio_s= took= groq_remaining_s=
day_used_s= pending=`, `Transcription waiting for Groq budget`,
`Transcription failed, retry in` and `Transcription rejected`.

Retention: the worker applies two independent limits as it starts and
then every `RECORDING_PRUNE_INTERVAL_HOURS` (default 24; 0 = only at
start), transcripts first, then audio. A sweep runs off the event loop; a
failing one is logged and retried at the next interval. 0 turns a limit
off.

- `RECORDING_AUDIO_KEEP_DAYS` (default 90) deletes the Ogg file of every
  chunk older than that; age is the chunk's `ended_at` in `chunks.jsonl`.
  A file goes only when its `NNNN.json` exists, so audio that was never
  transcribed (Groq rejected it, STT was off) is kept, and sessions still
  being recorded are skipped. `NNNN.json`, `chunks.jsonl` and
  `transcript.*` stay. Log line: `Recording audio pruned files=
  freed_mb= keep_days=`.
- `RECORDING_TRANSCRIPT_KEEP_DAYS` (default 0, keep forever) deletes the
  whole folder of a finished session older than that (`session.json`
  `ended_at`): transcripts, results, manifest and any audio still in it,
  so it also overrides the audio limit. It skips sessions being recorded,
  not yet ended, or with a chunk still waiting for transcription (one
  given up on after 3 tries does not count), and removes a guild folder
  it empties. Log line: `Recording sessions pruned sessions= freed_mb=
  keep_days=`.

Both run inside the worker, so nothing is pruned while `GROQ_API_KEY` is
unset or `STT_ENABLED=0`.

### 7.3 Recordings website

`python -m ttsbot.web` (the `tts_web` service) serves the sessions, their
transcripts with clickable times and one joined audio track per session
as a JSON API, from a separate container that mounts the recordings
read-only. `ttsbot/archive` holds the read side (store, mix, cache,
retention, service), `ttsbot/web` the HTTP layer. The bot's only part in
it: `session.json` also keeps `guild_name` / `channel_name`, written
atomically. See [RECORDINGS_WEB.md](RECORDINGS_WEB.md).

## 8. Persistence and Guild State

All persistent state lives in one SQLite file, `data/state.db`
(`ttsbot/statedb.py`, `STATE_DB_PATH`), in a table per kind of data:

- `guilds`, `emoji_aliases`, `settings` - `BotConfigStore`: whether TTS is
  enabled for the guild, allowed users, the default voice (new guilds get
  `TTS_DEFAULT_VOICE_PROFILE`, or the registry fallback if that name is
  unknown), per-user voices and fixed phrases, emoji pronunciations and the
  settings changed at runtime (`limit`, Fish latency, the active ElevenLabs
  key's fingerprint);
- `voices`, `voice_catalog` - the voice catalog and fallback chain;
- `provider_health` - provider pauses (`HealthStore`).

The stores work on memory and save in one transaction after each change,
so a crash or a full disk leaves the previous state; a failed save rolls
memory back. A save SQLite refuses (full disk, a lock held past the 5 s
busy timeout) is rolled back, COMMIT included, and raised as
`StateWriteError`, an `OSError`, so the save handlers that caught failed
JSON writes catch it too. Nested values are JSON text columns. A missing
data directory is created. A database that cannot be opened stops startup
(`StateFileError`) and is left as it is. The `config.json`, `voices.json`
and `provider_health.json` of earlier versions are imported once on
startup and left in place (an older image still finds them); a damaged
`provider_health.json` is skipped with a warning, since it only caches
provider pauses. `sqlite3 data/state.db .dump` shows everything.

## 9. Commands

The bot exposes the `/voicebot` slash group.

Current commands:

- `on` - enable TTS for the guild;
- `off` - disable TTS and clear active work;
- `allow` - add a member to the allowed list;
- `deny` - remove a member from the allowed list and deny them in this
  guild, even one `WHITELIST_USERS` allows (`allow` lifts it);
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
  pronunciations. Aliases are keyed by emoji id and apply in every guild,
  so only the guild that owns an emoji can set, remove or see its alias;
- `status` - show current runtime state;
- `stats` - cache, queue, per-session usage and breaker states;

`/voicebot` is at Discord's limit of 25 subcommands, so a second group
holds the fallback chain and provider health:

- `/fallback status` - each provider's health and live quota; `reset:`
  ends a pause by hand;
- `/fallback set` - show or set the fallback chain (global, or `voice:`).
- `limit` - show or change the per-message text length cap (persisted in `data/state.db`);
- `queue-clear` - clear queued work;
- `test` - enqueue a test phrase, optionally to a specific voice channel.

There are also two text commands for manual control:

- `!tts stop`
- `!tts join`

## 10. Relevant Configuration

The active runtime flags are defined in `.env.example` and loaded from `.env` in production.

Main groups:

- Discord access and whitelist;
- queue sizing;
- continuous-stream idle tuning;
- idle disconnect timings;
- Piper model path and tuning;
- voice recording and transcription (`RECORDING_*`, `STT_*`,
  `TRANSCRIPT_TIMEZONE`), including `RECORDING_AUDIO_KEEP_DAYS` and
  `RECORDING_TRANSCRIPT_KEEP_DAYS` for how long audio and transcripts are kept and
  `RECORDING_PRUNE_INTERVAL_HOURS` for how often that is applied.

The important current engine-specific values are:

- `FISH_MODEL=s2.1-pro-free`
- `FISH_FORMAT=opus`, `FISH_LATENCY=low`, `FISH_CHUNK_LENGTH=150`
- `FISH_OPUS_BITRATE=48000`
- `FISH_API_KEY` and `FISH_REFERENCE_ID` supplied from the untracked `.env`
- `FISH_TTFA_TIMEOUT=5`
- `ELEVENLABS_MODEL=eleven_v4_turbo`, `ELEVENLABS_FORMAT=opus_48000_64`,
  `ELEVENLABS_TTFA_TIMEOUT=3`; `ELEVENLABS_API_KEY` / `ELEVENLABS_API_KEYS`
  supplied from `.env`
- `CB_FAILURE_THRESHOLD=3`, `CB_COOLDOWN_SECONDS=60`
- `TTS_QUOTA_COOLDOWN_SECONDS=1800`
- `TTS_DEFAULT_VOICE_PROFILE=piper-ruslan`
- `PIPER_MODEL_PATH=/app/models/ru_RU-ruslan-medium.onnx`
- `PIPER_CONFIG_PATH=/app/models/ru_RU-ruslan-medium.onnx.json`

## 11. Tests

`tests/test_bot.py` is the main safety net.

It covers:

- text normalization;
- speakable text of a message;
- config persistence;
- voice-channel resolution;
- audio helper commands;
- queue behavior;
- voice connect cooldowns;
- worker success and failure paths.

Engine-specific suites sit next to it: `test_fish.py` (Fish requests,
Ogg/Opus demuxing, the mixed Opus/PCM player, cache keys, pitch),
`test_minimax_quota.py` (quota errors, the long breaker trip, the Fish
first-audio timeout), `test_gemini.py` and `test_pcm.py` (the PCM path),
`test_elevenlabs.py` (both ElevenLabs paths, error mapping, the key ring)
and the `test_providers*.py` files.

Each test builds a fresh bot through `tests/support.py` (`ttsbot.app.create_bot()` re-reads the environment), mutates `ttsbot.config` for tuning and exercises the pipeline through the bot's methods.

## 12. File Responsibility Map

### `bot.py`

The entrypoint; it calls `ttsbot.app.main()`.

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

### `data/state.db`

Stores live guild settings, the voice catalog and provider pauses.

### `models/`

Stores the active Piper voice model files.

## 13. What Matters for a New Voice Engine

If the goal is to add a new voice engine, the architecture points that matter most are:

1. keep `on_message()` and `queue_message()` mostly stable;
2. add the engine at the provider boundary (`TTSDispatcher`, `_stream_voice_into`);
3. preserve `TTSJob`, queueing, and worker flow;
4. make engine selection a profile-level concern, not a scattered global `if`;
5. keep playback and voice lifecycle unchanged unless the new engine genuinely needs a different artifact format;
6. add tests around engine selection, fallback, and missing-model behavior;
7. for a cloud engine, build the httpx client with `provider_limits()` and
   call `attach_warmer()` on it (`ttsbot/httpclient.py`), and return the
   provider from `TTSDispatcher.provider_for()`. That gives it the 120 s
   keep-alive and the typing warm-up with no other code.

That is the smallest clean seam in the current codebase.

### Connection keep-alive and typing warm-up

httpx closes idle connections after 5 s by default, so a message after a
pause used to pay a new TLS handshake (ElevenLabs first audio 0.33 s
instead of 0.18-0.20 s). Every cloud client now keeps idle connections for
120 s (`KEEPALIVE_EXPIRY`). For longer pauses, `on_typing` warms the
connection of the typing user's voice: an unauthenticated `GET` on the
provider host (no credits), at most once per 30 s of idleness, and skipped
while the connection is in use. Measured through the VPN after a 125 s
pause: first audio 0.20 s with the warm-up, 0.34 s without.

While an allowed user whose voice is a cloud voice sits in the bot's voice
channel, a background task pings that provider after
`TTS_CONNECTION_KEEPALIVE_SECONDS` (60) idle seconds, so even a message
after a long silence finds the connection open (~1 KB per ping, no
credits; `0` disables). Measured 2026-09-29: ElevenLabs kept an idle
connection open for 130 s, and pings every 40 s kept it for 150 s.

`on_message` logs `Discord delivery ... delay_ms=`: the time from the
message's Discord timestamp to the bot's handler, the one stage before
`message_ts` that the other timings do not cover.

### Latency log lines

Every job gets a number, and each line on its path carries `job=`:

| Line | What it measures |
|---|---|
| `Queued TTS ... job=` | message normalized and queued |
| `Generation start ... wait_s=` | queue to start of generation (the previous message's download, or lookahead backpressure) |
| `<Provider> HTTP <method> <path> status= conn=new/reused idle_before_s= headers_s=` | one per provider request (from `attach_warmer`); `conn=new` means a TLS handshake was paid |
| `<Provider> direct first packet` / `first frame ... request_to_packet_s=` | first playable audio, from the message and from the request |
| `<Provider> stream done ... audio_s= download_s=` | whole download against playing time |
| `Audio start ... generation_wait_s=` | how long the player waited for this job after picking it up (0 when prefetched) |
| `Playback finished ... provider=` | end of playback |
| `TTS cache hit / stored / evicted` | repeat cache use; misses are DEBUG |

httpx's own `HTTP Request:` lines are silenced (WARNING) because the
provider line above carries the same request plus the connection data.
