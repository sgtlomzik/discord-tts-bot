# TTS Known Issues

Problem tracker — observations from logs and manual checks. This file holds
no code; it just records what to look at next. Last reviewed: 2026-06-27.

## Open / to re-verify

1. Voice connect can race with itself.
   - `Already connected to a voice channel`, seen when a new TTS request
     arrives while the bot is still connecting to a channel.

2. Auto-connect can time out.
   - `Auto-connect failed ... TimeoutError`, after repeated voice handshake
     retries.

Both were observed historically; re-confirm against current logs before
spending time on them.

## Latency (context, largely addressed)

Earlier measurements showed short messages starting ~4.8–7.5s after queueing
and long messages 18–24s end-to-end, dominated by MiniMax generation plus the
voice connect/handshake. Those numbers predate the streaming, on-disk repeat
cache and prefetch pipeline now in `master`, which cut time-to-first-audio
and let generation run ahead of playback. Re-measure on a live burst before
treating the old figures as current.

## Notes

- All work is consolidated on `master` (the former `beta` and feature
  branches were merged in and deleted) — the old "compare against beta" item
  no longer applies.
