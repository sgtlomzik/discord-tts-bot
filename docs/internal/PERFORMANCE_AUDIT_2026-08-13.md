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

## Additional optimization pass

The second pass used seven days of production logs plus read-only measurements
against the live persistent cache. It found 6,365 MP3 entries (about 185 MiB).
The historical 301 file decodes had a median of 0.202 s, p95 of 0.490 s and a
maximum of 6.782 s, despite most of those requests already being provider-cache
hits.

### 5. Bypass merge classification when merge is disabled

Production runs with `TTS_MERGE_ALGORITHM=off`, but every message still built a
full `ParsedMessage`: multiple regex scans, token lists, structural flags and
effective-length accounting. The off branch now performs only the shared speech
text conversion it consumes. It also no longer grows `last_user_message_ts`, a
dictionary that is never read in this mode. Legacy and selective merge policies
retain the complete analyzer unchanged.

### 6. Avoid task/timer creation for an available message queue

The queue used `asyncio.wait_for(queue.put(...))` for every message, even when
the queue had room. During the observed week all 1,446 enqueues succeeded, the
maximum logged depth was 2 of 50, and no queue timeout occurred. The common path
now uses `put_nowait()`; on `QueueFull` it falls back to the same bounded await,
preserving backpressure and timeout behavior.

### 7. Reject non-emoji text before invoking the emoji parser

The outer full-text LRU helps exact repeats, but every new plain message still
called `emoji.emoji_list()`. An import-time first-character set derived from the
installed `emoji.EMOJI_DATA` now rejects plain text cheaply. Keycap sequences
are handled through their required combining mark, and a test checks every
installed emoji sequence for false negatives. A separate bounded 512-entry LRU
also reuses the spoken name of an emoji across otherwise different messages.

### 8. Cache decoded PCM for genuinely hot phrases

An on-disk cache hit still launched ffmpeg and materialized the same PCM again.
A second-level 32 MiB payload-bounded LRU now stores decoded frames for
persistent phrase-cache files. The entry key includes decoder settings and the
source path; mtime and size are checked on every lookup, so replacement or
external eviction cannot serve stale frames. Temporary synthesis files are not
eligible. `TTS_DECODED_PCM_CACHE_MAX_BYTES=0` is the rollback switch.

The first disk hit populates this LRU; subsequent hot hits skip the ffmpeg
process entirely. This intentionally avoids retaining every newly streamed
phrase before it proves hot.

### 9. Apply TTFA to audible PCM, not an MP3 header

MiniMax sent 1,139 measured streams with median time-to-audio 0.902 s and p95
2.194 s. A small subset still stalled badly: even after restricting to jobs with
less than 0.1 s queue wait, seven exceeded the configured 2.5 s budget and three
exceeded 5 s. The former deadline covered only the first upstream byte chunk; a
prompt MP3 header could therefore satisfy it while ffmpeg waited many seconds
for enough data to emit sound.

Both direct and prefetch streaming paths now share a total deadline to the first
complete decoded PCM frame. Once a frame exists, the deadline is removed and a
legitimate long stream may continue normally. A deterministic real-ffmpeg test
covers the header-then-stall case and verifies clean Piper fallback with no
partial cache commit.

### 10. Keep MiniMax connections warm across normal chat pauses

The long-lived `httpx.AsyncClient` used the library's five-second keep-alive
expiry. Across 2,693 observed MiniMax usage events, the median gap was 8.49 s;
1,271 intervals (47.2%) were between 5 and 60 seconds and therefore could not
reuse a connection under the old default. The owned client now defaults to a
60-second expiry, configurable through `MINIMAX_KEEPALIVE_EXPIRY_SECONDS`, while
retaining the same small connection limits.

### 11. Rehydrate the persistent cache with one stat per file

Startup previously performed up to three filesystem metadata calls per MP3:
file filtering, mtime sorting and size accounting. A single `os.scandir()` now
captures mtime and size together while preserving approximate LRU order,
orphan `.part` cleanup and byte/count eviction.

### 12. Make streaming cache keys tuning-aware

The dispatcher already keyed MiniMax files by parameters that change audio, but
the streaming paths used only `voice.name`. They now use the same
`voice_cache_key()` and include voice id, speed, volume, pitch, emotion, model
and language boost. Retuning or replacing a voice therefore cannot replay stale
audio. Existing name-only streaming entries remain harmless disk files and
age out through the normal LRU.

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
| `merge=off` message path | 52.97 us | 4.83 us | 90.9% less CPU time |
| Enqueue into an available asyncio queue | 49.05 us | 1.54 us | 96.9% less CPU time |
| Unique plain-text emoji check | 26.59 us | 3.14 us | about 8.5x faster |
| Median-size persistent MP3, decoded cache hit | 152.038 ms | 0.032 ms | ffmpeg process skipped |
| Rehydrate 6,365 persistent cache files | 158.746 ms | 38.719 ms | about 4.1x faster |

The playback-wait change also removes the more important cost that the table
does not express: a shared executor thread is no longer occupied while audio is
playing.

## Verification

- `python -m compileall -q bot.py ttsbot tests`: passed.
- `git diff --check`: passed.
- First-pass isolated server suite: **310 tests passed in 4.543 s**.
- Final isolated server suite after the additional pass: **331 tests passed in
  5.558 s**.
- The stutter-guard integration tests still cap direct and prefetch streams.
- Direct and prefetch header-stall tests fall back before any PCM is enqueued.
- The decoded PCM cache tests cover LRU promotion/eviction, strict payload cap,
  file replacement invalidation and one ffmpeg launch across two hot decodes.
- The complete installed emoji corpus is covered by the fast-reject gate test.
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
- Phrase cache hits still skip the API/circuit-breaker probe; only persistent
  files are eligible for the new bounded decoded-frame LRU.
- Generation/playback are already split into FIFO prefetch workers. Increasing
  provider concurrency would risk quota bursts and does not make serialized
  Discord playback faster.
- JSON guild/voice persistence is command-driven rather than message-hot, so a
  database or write-behind layer would add complexity without a useful runtime
  gain at the current scale.
- Incremental PCM decoding remains useful for cold cache/Piper files, especially
  very long clips, but it changes producer/consumer backpressure and cancellation
  semantics. The bounded hot-phrase LRU captures the lower-risk repeated-phrase
  win first.
- Bounded backpressure between ffmpeg, the prefetch channel and Discord's audio
  thread could reduce worst-case PCM memory. It requires coordinated high/low
  watermarks rather than a queue-size tweak, so it remains a separate change.
- Per-guild playback lanes could remove cross-guild head-of-line blocking, but
  the observed traffic was predominantly one guild and does not yet justify the
  concurrency complexity.
- Stopping the continuous silence source sooner may reduce idle Opus work, but
  its time-to-first-audio effect should be measured in Discord before changing
  the current warm-session behavior.
