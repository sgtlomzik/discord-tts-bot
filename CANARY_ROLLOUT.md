# Selective Hold Canary Rollout

## Safe defaults

- Default mode is `TTS_MERGE_ALGORITHM=legacy`.
- `selective_hold_v2` only activates when both are true:
  - `TTS_MERGE_ALGORITHM=selective_hold_v2`
  - `TTS_SELECTIVE_HOLD_TARGET_USERS` contains the author's Discord user ID.
- If `TTS_SELECTIVE_HOLD_TARGET_USERS` is empty, the bot stays on the legacy path.
- `TTS_SELECTIVE_HOLD_REACTION_PAUSE_MS` only affects messages when there is no active buffer.

## Enable for one user

Edit `.env`:

```env
TTS_MERGE_ALGORITHM=selective_hold_v2
TTS_SELECTIVE_HOLD_TARGET_USERS=441612025286885397
TTS_SELECTIVE_HOLD_LOG_DECISIONS=1
```

Apply:

```bash
cd /srv/dev-disk-by-label-DataDrive/tts-bot
docker compose up -d
docker ps --filter name=discord_tts_bot
docker logs --tail 120 discord_tts_bot
```

Expected startup log includes:

```text
Merge tuning: algorithm=selective_hold_v2 ... selective_target_users=441612025286885397
```

## Roll back

Fast rollback to the previous behavior:

```env
TTS_MERGE_ALGORITHM=legacy
```

Emergency disable of all merge buffering:

```env
TTS_MERGE_ALGORITHM=off
```

Apply either rollback with:

```bash
docker compose up -d
docker logs --tail 120 discord_tts_bot
```

## Manual smoke checklist

1. After a pause, send `бб` and confirm an immediate decision.
2. Send custom emoji-only and confirm no raw Discord ID is spoken.
3. Send animated custom emoji-only and confirm no raw Discord ID is spoken.
4. Send `ну там просто дается хп`, `при нажатии`, `манты`, `почему-то` and confirm short tails are appended instead of broken as reactions.
5. Send text starter, then custom emoji-only; confirm the text buffer flushes first.
6. Send text starter, then a question; confirm the text buffer flushes before the question.
7. Return to `legacy` and confirm merged text uses `. ` as the separator.

## First logs to watch

For the first 20-30 minutes, watch:

- `Merge tuning: algorithm=...`
- `Selective hold decision ...`
- `Merged buffer flushed ... reason=...`
- `stale_timer_ignored`
- `enqueue_fail_reason=...`
- `message_to_audio_enqueue_s`
- `queue_to_audio_enqueue_s`

