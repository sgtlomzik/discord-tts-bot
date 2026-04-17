# Beta: Implemented Changes (2026-04-17)

## Scope

This document summarizes all practical changes implemented in the `beta` branch to fix stability issues, move to local/offline TTS, and improve runtime behavior on low-resource hardware.

## 1) Voice/Reconnection Stability

- Preserved and validated cooldown-based voice reconnect protection.
- Ensured voice connect/move operations are serialized per guild with locks.
- Kept cleanup behavior safe:
  - disconnect on voice-prepare failures,
  - keep voice session on pure TTS-generation failures.
- Verified bot startup and gateway connection are stable in logs (no reconnect loop in current runs).

## 2) Access Control

- Bot reacts only to whitelisted users via `WHITELIST_USERS`.
- Current production whitelist remains:
  - `441612025286885397`

## 3) Offline TTS Engine Chain

- Implemented multi-engine fallback chain:
  - `piper -> rhvoice -> espeak`
- Added env-driven engine routing:
  - `TTS_ENGINE`
  - `TTS_ENGINE_FALLBACK_ORDER`
- Added robust engine-order parsing with validation and deduplication.
- Added per-engine generation functions and unified fallback execution.

## 4) Piper Integration (Primary Engine)

- Replaced dependency on external `piper` CLI with native Python integration through `piper-tts`.
- Added lazy loading/caching of Piper model in bot process.
- Added model/config existence checks and explicit runtime errors.
- Added Piper synthesis tuning:
  - `PIPER_SPEAKER`
  - `PIPER_LENGTH_SCALE`
- Current tested model:
  - `/app/models/ru_RU-ruslan-medium.onnx`
  - `/app/models/ru_RU-ruslan-medium.onnx.json`

## 5) RHVoice + eSpeak Fallback Readiness

- RHVoice HTTP session now created only when RHVoice is in configured chain.
- RHVoice warmup/readiness probe executes conditionally.
- eSpeak fallback path is available and installed in container (`espeak-ng`).

## 6) Playback Latency/Behavior Tuning

- Playback starts with configurable leading silence:
  - `TTS_START_PAD_MS` (currently set to `500` per request).
- Added ffmpeg low-delay mode toggle:
  - `FFMPEG_LOW_DELAY`
- Added configurable silence trimming in ffmpeg filter graph:
  - `TTS_TRIM_SILENCE`
- Added startup logs to print active playback tuning.

## 7) Merge of Short Messages

- Implemented short-message batching into a single phrase.
- Merge key: `(author_id, voice_channel_id)`.
- Merge behavior:
  - short messages buffered for merge window,
  - merged with sentence separator (`". "`),
  - flushed to queue as one TTS task.
- Added env controls:
  - `TTS_MERGE_SHORT_MESSAGES`
  - `TTS_MERGE_MAX_CHARS`
  - `TTS_MERGE_WINDOW_MS`
  - `TTS_MERGE_MAX_PARTS`
- Added startup logs to print active merge tuning.

## 8) Container/Runtime Changes

- `Dockerfile` updated to include `espeak-ng`.
- `requirements.txt` updated with `piper-tts==1.4.2`.
- `docker-compose.yml` mounts local models:
  - `./models:/app/models:ro`
- `.env.example` expanded with all new TTS/pipeline tuning options.

## 9) Testing and Validation

- Unit tests pass after changes:
  - `14/14 OK`
- Added tests for:
  - TTS engine order parsing,
  - fallback to next engine,
  - RHVoice readiness skip when disabled.
- Runtime logs confirm:
  - `TTS engine used: piper`
  - successful warmup and worker startup
  - active tuning values printed on startup.

## 10) Resource Usage Observation (During Active Generation)

Measured with synthetic in-container Piper workload (30 utterances):

- Peak CPU (container): ~`194% - 198%`
- Peak RAM (container): ~`375 MiB`
- Idle RAM after load: ~`154 MiB`
- Workload completion time: `26.436s`

This remains within the requested memory envelope (< 1 GiB during generation).

