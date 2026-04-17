# Beta Reconnect Fix

## Goal
Stop repeated voice reconnect loops in beta when local RHVoice fails or is misconfigured.

## Actions Applied
1. RHVoice URL handling in `bot.py`:
- Added URL normalization for `RHVOICE_URL`.
- If port is missing, default port `5002` is appended.
- Added `build_rhvoice_url()` helper for `/say` requests.

2. Worker reconnect logic in `bot.py`:
- Split failure handling between voice errors and TTS generation errors.
- On TTS generation failure, bot logs error and keeps existing voice session.
- On voice connect/move/playback failure, bot disconnects as before.

3. Config docs:
- Updated `.env.example` from edge settings to RHVoice settings.
- Added RHVoice vars and latency/idle settings used by beta.

4. Tests for beta code:
- Added unit tests for text processing with newline normalization.
- Added unit test for RHVoice URL normalization.
- Added unit test for RHVoice `/say` URL parameters.
- Added async worker tests:
  - TTS generation failure does not trigger disconnect.
  - Voice connect failure still triggers disconnect.

## Stable Log Baseline (2026-04-17)
- 57 queued messages, 56 successful playbacks.
- One TTS provider failure (`NoAudioReceived`) caused disconnect in stable flow.
- Several `Disconnected from voice by force... potentially reconnecting` lines happened even with successful playback.
- Conclusion: this line alone is not a fatal condition; disconnect must not be forced on isolated TTS generation errors.
