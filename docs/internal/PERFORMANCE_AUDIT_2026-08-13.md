# Performance Audit — 2026-08-13

## Scope and isolation

The audit covered the message intake, normalization/merge, provider/cache,
streaming decode, prefetch, playback and persistence paths. Production was not
restarted or redeployed. The running `discord_tts_bot` container uses code from
the published image; only `models`, `data` and the MiniMax token volume are
mounted. All modified-code tests ran in a separate detached Git worktree.

The production flags observed during the audit included:

- `TTS_CACHE_ENABLED=1`
- `TTS_MERGE_ALGORITHM=off`
- `TTS_CONTINUOUS_STREAM=1`

Streaming and prefetch are enabled by their code defaults when no explicit
override is present, so the optimized paths are relevant to the deployed
configuration.

## Applied changes

### 1. Normalize each incoming message once

`queue_or_merge_message()` already converts the Discord message into
`ParsedMessage.spoken_text`. Previously every immediate and buffered path then
called `enqueue_tts()`, which repeated custom emoji substitution, Unicode emoji
scanning, mention/URL stripping and whitespace normalization.

`enqueue_tts()` now accepts an explicitly pre-normalized internal path. That
path still strips outer whitespace and enforces `TTS_MAX_CHARS`, so the quota
and playback safeguards remain in force. Raw callers keep the original full
normalization behavior. `/voicebot test`, which already normalized its input,
uses the same fast path.

### 2. Cache repeated Unicode emoji conversion

`speak_unicode_emoji()` is pure for a given string, but `emoji.emoji_list()` is
comparatively expensive and was called for every message. A bounded 2,048-entry
LRU now reuses the conversion for repeated chat phrases. The bound prevents
unlimited memory growth; a first-seen phrase still follows the unchanged cold
path.

### 3. Remove a blocked executor thread from every playback wait

`ContinuousTTSAudioSource.wait_until_drained()` previously delegated
`threading.Event.wait()` to `asyncio.to_thread()`. That occupied one shared
executor thread for the complete playback duration. The source now notifies an
`asyncio.Event` from Discord's audio thread with `call_soon_threadsafe()`.

The implementation includes a generation/race check: a stale "drained"
notification cannot complete a waiter after a new frame batch has been queued.
A defensive `to_thread()` fallback remains only if a source is incorrectly
shared across different event loops.

### 4. Stop streaming work promptly and close ffmpeg pipes cleanly

When the audio-length guard killed ffmpeg on a simulated infinite MiniMax
stream, the feeder task could continue until its next network/write boundary.
It also closed subprocess stdin without awaiting `wait_closed()`. The test run
therefore ended with two unhandled `BrokenPipeError` futures even though all
tests were reported as passing.

The guard now cancels and retrieves the feeder task immediately, closes the
async generator/cache partial file, awaits stdin closure, consumes expected
pipe errors and then waits for ffmpeg. Partial or truncated streams remain
uncached exactly as before.

## Microbenchmarks

Measurements used the server's existing Python 3.11 virtual environment. Each
synchronous result is the median of five runs. They are focused microbenchmarks,
not end-to-end Discord latency claims.

| Hot operation | Before | After | Result |
|---|---:|---:|---:|
| Merge analysis plus enqueue normalization, repeated representative message | 239.001 us | 82.818 us | 65.3% less CPU time |
| Repeated plain-text emoji conversion | 88.971 us | 0.144 us | cache-hit path |
| Repeated emoji-text conversion | 77.388 us | 0.146 us | cache-hit path |
| Already-drained playback wait | 208.424 us | 0.255 us | about 817x lower scheduling overhead |

The playback-wait change also removes the more important cost that the table
does not express: a shared executor thread is no longer occupied while audio is
playing.

## Verification

- `python -m compileall -q bot.py ttsbot tests`: passed.
- `git diff --check`: passed.
- Full isolated server suite: **310 tests passed in 4.543 s**.
- The stutter-guard integration tests still cap direct and prefetch streams.
- No `Future exception was never retrieved` or unhandled `BrokenPipeError`
  remained after the optimized full suite.
- Production container ID/start time/restart count were not changed during the
  work; no deployment command was run.

The suite still emits pre-existing `ResourceWarning` messages from a few test
fixtures that leave temporary directories or event loops for implicit cleanup.
Those warnings are test-harness hygiene issues and were not mixed into this
runtime optimization change.

## Reviewed but deliberately unchanged

- MiniMax already uses one long-lived `httpx.AsyncClient` with keep-alive.
- Piper model objects are already loaded lazily, cached and synthesized outside
  the event loop.
- Phrase cache hits on the active streaming path already decode the cached file
  directly and skip the API/circuit-breaker probe.
- Generation/playback are already split into FIFO prefetch workers. Increasing
  provider concurrency would risk quota bursts and does not make serialized
  Discord playback faster.
- JSON guild/voice persistence is command-driven rather than message-hot, so a
  database or write-behind layer would add complexity without a useful runtime
  gain at the current scale.

One possible future optimization is incremental PCM decoding for cached/Piper
files. The current file path collects decoded PCM before enqueueing it. With the
active 300-character message cap this is bounded, and changing that contract is
higher risk than the optimizations above, so it was intentionally left for a
separate measured change.
