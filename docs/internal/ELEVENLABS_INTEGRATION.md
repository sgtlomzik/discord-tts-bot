# ElevenLabs (eleven_v4_turbo) integration

Branch `feat/elevenlabs`, based on `feat/gemini-openrouter` (cda6c1b).
Measurements taken 2026-09-29 from the home server, key with the
`text_to_speech` (+ later `voices_read`) permission, voice George
(`JBFqnCBsd6RMkjVDRZzb`, premade).

## 1. What the API gives us

| Question | Answer (measured) |
|---|---|
| Streaming? | Yes. `POST /v1/text-to-speech/{voice_id}/stream` sends chunks while generating. The WebSocket `stream-input` is for text that arrives in pieces; our messages are complete, so HTTP is simpler and not slower. |
| Opus 48 kHz? | Yes. `opus_48000_{32,64,96,128,192}` returns **Ogg/Opus, mono, 48 kHz, 20 ms packets** (`content-type: audio/opus`). The bot's `OggOpusDemuxer` accepts it unchanged, so the packets go to Discord as-is, exactly like Fish. |
| PCM? | Yes. `pcm_{8000..48000}`, raw s16le mono (`audio/pcm`). `pcm_48000` works on this key's plan (docs gate 44.1 kHz behind Pro). |
| First audio | 0.22-0.26 s warm for 2 to 133 characters (0.4-0.66 s on a cold TLS connection). Opus and PCM are equally fast. |
| Cost | `character-cost` header: 0.5 credit per character for v4 Turbo; audio tags are billed as text. |
| Voice settings | v4 honors `stability` and `similarity_boost`. `speed`/`style` are accepted without error but are not v4 settings (docs). |
| Audio tags | `[shouting]`, `[whispering]` etc. work in Russian and are not read aloud (checked by ear). A message can carry them as plain text. |

Error bodies are JSON `{"detail": {"status": ..., "message": ...}}`:
404 `voice_not_found`, 400 `invalid_api_key_length`, 401
`invalid_api_key` / `missing_permissions`, 401 `quota_exceeded`
(documented), 429 rate limits.

## 2. Playback paths

`ELEVENLABS_FORMAT` picks one; both stream and neither uses ffmpeg.

```text
opus_48000_64 (default):
  ElevenLabs Ogg chunks -> OggOpusDemuxer -> 20 ms Opus packets -> Discord
  (packets teed into a .dopus cache file; non-20 ms packets would fall back
   to ffmpeg without a second request - shared with Fish)

pcm_48000 / pcm_24000:
  ElevenLabs s16le chunks -> PcmFramer (soxr when rate != 48 kHz)
  -> 3840-byte PCM frames -> ContinuousTTSAudioSource encodes -> Discord
  (cached as .pcm with a header, shared with Gemini)
```

Opus is the default: nothing is decoded or re-encoded on our side. PCM
is kept as a switch in case a future voice/model returns non-20 ms
packets or local volume control is ever needed.

The two generic helpers were extracted from the existing code rather than
duplicated: `_stream_ogg_opus_to_channel` (was Fish-only) and
`_stream_pcm_to_channel` (was Gemini-only). The Fish and Gemini wrappers
keep their signatures and log wording.

Live end-to-end run of the bot code against the API (in the bot image):

| Format | First frame (live) | Cache hit | File path (ffmpeg) |
|---|---|---|---|
| opus_48000_64 | 0.29 s (0.51 s cold) | 1 ms | OK |
| pcm_48000 | 0.23 s (0.39 s cold) | 2-5 ms | OK |
| pcm_24000 | 0.23 s (0.41 s cold) | 3-11 ms | OK |

## 3. Failure handling

Own `CircuitBreaker` (`CB_*`), `ELEVENLABS_TTFA_TIMEOUT` (default 3 s)
until the first packet/frame, then Piper fallback. Quota errors
(`quota_exceeded`, HTTP 402) trip the breaker for
`TTS_QUOTA_COOLDOWN_SECONDS`; a 429 with `Retry-After` pauses it for that
long. Usage per request is logged as
`ElevenLabs usage credits=... session_credits=...` and shown in
`/voicebot stats`.

## 4. Operator surface

- `.env`: `ELEVENLABS_API_KEY` (enables the provider),
  `ELEVENLABS_VOICE_ID` (optional seed for `eleven-default`),
  `ELEVENLABS_MODEL`, `ELEVENLABS_FORMAT`, `ELEVENLABS_LANGUAGE_CODE`,
  `ELEVENLABS_TTFA_TIMEOUT`.
- `/voicebot voice-add provider:ElevenLabs voice_id:<id>` - probes the
  voice with one short request (~8 credits) before saving; with the
  `voices_read` permission voice_id autocompletes from the account.
- `/voicebot voice-tune` tunes ElevenLabs voices too (`stability`,
  `similarity`, `model`, `reset`). No separate command: `/voicebot` is at
  Discord's 25-subcommand limit.

## 5. Not done / follow-ups

- README and ARCHITECTURE.md are not updated in this branch: the main
  checkout has uncommitted edits to both, and touching them here would
  conflict. Fold this document into them after those edits land.
- Auto-emotion via audio tags (like Fish's `fish_tts_text`) was considered
  and deliberately left out.
