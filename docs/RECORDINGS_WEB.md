# Recordings website: architecture and API

A local website for the voice recordings the bot makes: every session with
its date, participants and retention countdown, the transcript with clickable
times, and one audio track per session to jump around in. This document
covers the backend (`ttsbot/archive`, `ttsbot/web`) and the API contract a
frontend builds on.

## 1. Shape

```
 Discord voice ──► bot (discord_tts_bot) ──writes──► data/recordings/<guild>/<session>/
                                                          │ read-only mount
                                                          ▼
 browser ◄──HTTP──► tts_web (discord_tts_web) ──► cache/web/<guild>/<session>/<artifact>/
                    python -m ttsbot.web
```

- **Separate process and container.** The website never shares the bot's
  event loop, memory or restarts: a slow page cannot delay speech, a bot
  crash does not take the site down. Same image, different command.
- **Read-only on the recordings.** `data/recordings` is mounted `:ro`; the
  website cannot change or delete anything the bot wrote. The bot stays
  the only writer and the only one applying retention.
- **No secrets.** The container gets only the settings it needs (listed in
  `docker-compose.yml`), not the bot's `.env` with Discord/API tokens.
- **Disk is the database.** No index to keep in sync: the session folders
  are the source of truth, re-read only when their files change.
- **Bounded.** `mem_limit: 512m` (the process itself uses ~80 MB; the
  rest is reclaimable page cache), read-only root filesystem, one mix
  build and one MP3 transcode at a time, at most 4 queued transcodes,
  cache capped at `WEB_CACHE_MAX_MB`.
- **Private by default.** It refuses to start without `WEB_PASSWORD`
  unless `WEB_ALLOW_ANONYMOUS=1` says the port is reachable only from
  trusted machines; `WEB_CORS_ORIGINS=*` is refused.
- **The bot's files are written atomically** (`ttsbot/recording/fsutil.py:
  write_atomic`): `session.json`, `names.json`, `transcript.*` and
  `.error.json` are replaced whole, so a reader never sees half a file.

### Layers

| Module | Responsibility | Knows about |
|---|---|---|
| `ttsbot/archive/store.py` | Find and parse session folders; validate ids; detect changes (`signature`) | files |
| `ttsbot/archive/mix.py` | Join per-speaker Ogg files into one track; map lines to positions (`MixPlan`) | Ogg/Opus packets |
| `ttsbot/archive/media.py` | Cache of derived files: atomic, de-duplicated builds, failure back-off, LRU cap | folders |
| `ttsbot/archive/retention.py` | When the bot's pruning deletes audio and transcripts | the bot's rules |
| `ttsbot/archive/service.py` | JSON documents of the API; caching of summaries | the layers above |
| `ttsbot/web/app.py` | HTTP: routes, errors, auth, CORS, static frontend, upkeep task | `ArchiveService` |

Each layer only uses the ones above it in the table, so each is tested on
its own (`tests/test_archive.py`, `tests/test_web.py`) and can be reused:
a Discord command or a CLI can call `ArchiveService` directly.

Shared with the bot: transcript lines are merged by the same functions that
write `transcript.md` (`ttsbot/recording/transcript.py: utterances,
merge_lines`), so the site and the file always agree.

## 2. Data the backend reads

One folder per stay of the bot in a voice channel (written by
`ttsbot/recording`): `session.json` (ids, names, start/end), `chunks.jsonl`
(one line per closed Ogg file: speaker, pieces = `[offset in file, wall
clock]`), `<user>/NNNN.ogg` (speech of one user, pauses cut to 0.5 s),
`<user>/NNNN.json` (Groq result: segments with file offsets and wall-clock
times), `names.json`, `tts.jsonl`.

The bot is writing while the site reads, so nothing is trusted:

- ids in URLs are validated against strict patterns before any path is built;
- a bad manifest line, result or name file is skipped and listed in the
  session's `problems`; only an unreadable `session.json` hides a session;
- an Ogg file cut off by a crash or corrupted (bad CRC) contributes the
  part before the damage; an unreadable one is left out of the mix;
- a session is re-read only when its `signature` (mtimes and sizes of its
  files and folders, and of the `.error.json` files, which change in
  place) changes.

## 3. The session audio (mix)

Each speaker has their own files, so the backend builds one track per
session: every utterance once, in the order they started, with
`WEB_MIX_GAP_MS` (500 ms) of silence between them, as the old manual MP3
mixes did. Long silences disappear; overlapping speech plays one after the
other. Opus packets are copied, never re-encoded: a 6.5 h session with
26 minutes of speech builds in under a second.

`MixPlan` (stored next to the mix as `plan.json`) remembers where each
utterance landed, and turns:

- a transcript segment (file + offset) into an exact position; a segment
  starting in a pause the mix cut out goes to the speech after the pause
  (other speakers can sit between the two parts in the mix);
- any wall-clock moment (used for TTS lines, which are not in the
  recording) into the position of what was being said then, or said next.

The mix is cached under a fingerprint (its *version*) of the audio files
it was built from. When files change (a new file closes, retention deletes
audio) the next request builds a new version; the old one stays while it
may be in use and is deleted by the upkeep once unused for 10 minutes
(so audio the bot has pruned leaves the cache within about 15 minutes).
Nothing is deleted while its session is building.

While a session is being recorded, a mix less than 2 minutes old is
reused rather than rebuilt for every page view: lines from files closed
since then have `position: null` until the next build.

Because utterances that overlapped in time are played one after the other,
lines in time order are not always in mix order: a line can start before
the previous one in the mix.

`WEB_PREBUILD_RECENT` newest finished sessions are built ahead of time by
the upkeep task, so their pages open at once. MP3 (for downloads) is
transcoded by ffmpeg in the background on first request.

## 4. API v1

Base path `/api/v1`. All JSON. Conventions:

- **Ids are strings** (Discord snowflakes do not fit a JavaScript number).
- **Times are epoch seconds** (UTC, float). `info.timezone` is the zone the
  transcripts use; format on the client with `Intl.DateTimeFormat`.
- **Positions are seconds into the session's audio** (`<audio>.currentTime`).
- **Errors:** `{"error": {"code": "session_not_found", "message": "..."}}`
  with the matching HTTP status.
- **Links:** documents carry ready-made `links` / `url`s; the client should
  follow them instead of building paths.
- Responses are `Cache-Control: no-store`; versioned audio (`?v=`) is
  `immutable`, the plain audio URL `no-cache`.
- Error codes: `session_not_found`, `audio_unavailable` (404),
  `bad_parameter` (400), `unauthorized` (401), `session_recording` (409),
  `audio_build_failed`, `busy` (503, with `Retry-After`), `internal_error`.

### `GET /api/v1/info`

```json
{"api_version": 1, "timezone": "Europe/Moscow",
 "retention": {"audio_keep_days": 90, "transcript_keep_days": 0, "prune_interval_hours": 24},
 "mix_gap_ms": 500,
 "features": ["sessions", "transcripts", "audio_mix", "audio_mp3", "transcript_markdown"]}
```

`features` lets a client show only what this backend supports.

### `GET /api/v1/guilds`

`{"items": [{"id": "1030…", "name": "Server", "sessions": 14, "last_started_at": 1791107596.4}]}`

### `GET /api/v1/sessions`

Query: `guild`, `user` (participant id), `since` / `until` (epoch, on the
start time), `limit` (1–200, default 50), `offset`. Newest first.

```json
{"total": 14, "limit": 50, "offset": 0, "items": [<session summary>]}
```

Session summary:

```json
{
  "id": "1030870431181312060/20261004-095316Z-1030870431181312064",
  "guild": {"id": "1030870431181312060", "name": "Server"},
  "channel": {"id": "1030870431181312064", "name": "General"},
  "started_at": 1791107596.441,
  "ended_at": 1791140100.292,
  "last_activity_at": 1791140090.1,
  "duration_seconds": 32503.9,
  "status": "done",
  "participants": [
    {"user_id": "4416…", "name": "SgtLomzik", "speech_seconds": 359.6, "voice_lines": 40, "tts_lines": 2}
  ],
  "speech_seconds": 1567.6,
  "line_count": 251,
  "transcription": {"files": 21, "done": 21, "pending": 0, "failed": 0, "recording": 0},
  "retention": {
    "audio": {"state": "kept", "files": 21, "deleted_files": 0,
              "expires_at": 1798883700.0, "fully_expires_at": 1798916100.0,
              "seconds_left": 7753370, "kept_until_transcribed": 0},
    "transcript": {"expires_at": null, "seconds_left": null, "held": false}
  },
  "problems": [],
  "links": {"self": "/api/v1/sessions/…", "audio": "/api/v1/sessions/…/audio",
            "audio_mp3": "/api/v1/sessions/…/audio.mp3",
            "transcript_markdown": "/api/v1/sessions/…/transcript.md"}
}
```

- `status`: `recording` (bot still in the channel) → `transcribing` (files
  wait for Groq) → `done`, or `partial` (Groq gave up on some files).
- `name` may be `null` (unknown person): show the id.
- `retention.audio.state`: `kept`, `partial` (some audio already deleted),
  `deleted`, `none` (nothing recorded). `expires_at` is when audio starts
  being deleted (the oldest file), `fully_expires_at` when the last goes;
  `null` = kept forever. `transcript.expires_at` `null` = kept forever;
  `held` = deletion postponed while transcription is unfinished. Deletion
  happens at the bot's next sweep after the date (`prune_interval_hours`).
- `channel.name` / `guild.name` are recorded from now on; older sessions
  have `null` there.
- `links.audio` is the always-latest mix (use `audio.url` from the session
  document for playback); `links.audio_mp3` is `null` while recording.

### `GET /api/v1/sessions/{guild}/{session}`

The summary plus:

```json
{
  "audio": {"status": "ready", "url": "/api/v1/sessions/…/audio?v=378e8f5f0d6cb8d92ef4",
            "version": "378e8f5f0d6cb8d92ef4", "mp3_url": "…/audio.mp3",
            "duration": 1570.1, "missing_files": 0, "damaged_files": [], "error": null},
  "lines": [
    {"index": 0, "user_id": "4491…", "name": "KEKWait", "kind": "voice",
     "at": 1791108842.986, "until": 1791108845.526, "text": "Вулканский салют.",
     "position": 2.42, "position_end": 4.96, "exact": true,
     "segments": [{"at": 1791108842.986, "until": 1791108845.526, "text": "Вулканский салют.",
                   "position": 2.42, "position_end": 4.96}]}
  ]
}
```

- `audio.status`: `ready`, `none` (no audio left: deleted by retention or
  nothing playable), `error` (build failed, `error: "build_failed"`;
  retried after 5 minutes). The transcript is always there regardless.
- `audio.url` is versioned: keep using it for the `<audio>` element while
  the page is open; positions in the document belong to that version.
  Reload the document (not just the audio) to get a newer version.
- A line is one speaker's consecutive speech (segments less than 2 s
  apart, as in `transcript.md`); `segments` are Whisper's sentences, each
  with its own position, for finer seeking.
- `kind`: `voice` (recorded speech) or `tts` (text the bot spoke for the
  user). `exact: false` for TTS: its `position` is the moment it was spoken
  at, not the TTS audio itself (the bot does not record its own voice).
- `position` / `position_end`: where the line or segment starts and ends
  in the audio. `null`: that audio is gone (or not in the mix yet); show
  the time, no seek.
- Clickable timestamp: show `at` as wall-clock time, on click set
  `audio.currentTime = position`.
- Highlighting what is playing: the segment with `position <= currentTime
  < position_end` (not "the last line that started": lines are in time
  order, which is not always mix order, see section 3).

### `GET …/{session}/audio`

The session mix, `audio/ogg` (Opus), with HTTP Range support (seeking).
`?download=1` adds `Content-Disposition: attachment`. 404
`audio_unavailable` when there is no audio left. Plays in Chrome, Firefox,
Edge and Safari 18.4+.

### `GET …/{session}/audio.mp3`

The mix as MP3 (for downloading/sharing). First request: `202
{"status": "building"}` with `Retry-After`; poll until `200`. 409
`session_recording` while the session is being recorded, 503 `busy` when
4 transcodes are queued already.

### `GET …/{session}/transcript.md`

The bot's own `transcript.md`, as is.

### `GET /api/v1/health`

`{"status": "ok", "recordings_readable": true, "sessions_indexed": 14, "cache": {...}}`;
503 when the recordings folder is unreadable. No auth (container healthcheck).

## 5. Frontend

The interface lives in `web/` and needs no build step: plain HTML, CSS and
ES modules, so what is in the repo is what the browser runs. It ships in the
image (`/app/web`) and is served at `/` (`WEB_STATIC_DIR` defaults to it in
`docker-compose.yml`); real files are served as they are, any other
non-API path gets `index.html` (the address `/sessions/<guild>/<session>?t=<seconds>`
opens a recording at that moment). Files are revalidated on every load and
the page carries a Content-Security-Policy (own scripts only, own origin only,
the two web fonts from Google Fonts).

```
web/index.html            page skeleton (no inline scripts: the policy forbids them)
web/css/app.css           the look
web/js/main.js            boot, address bar, keyboard
web/js/api.js             the only module that does HTTP
web/js/sessions.js        list of recordings (left column)
web/js/viewer.js          one recording: ties card, player, ribbon, transcript together
web/js/player.js          <audio> with a per-frame clock
web/js/ribbon.js          vertical map of the recording (lanes per speaker)
web/js/transcript.js      the scrolling transcript, follow mode, search hits
web/js/timeline.js        session document -> model; "what is playing at t" (pure)
web/js/format.js          dates in the server timezone, durations, countdowns (pure)
web/js/palette.js, dom.js, ui.js   colours per person, escaping, toasts
```

Rules the code keeps: everything that reaches `innerHTML` goes through
`esc()` / `markup()` (names and transcripts come from other people); the
playing line is found by audio *position* over a sorted index, never by line
order; a recording that is still being made is re-read every 30 s without
interrupting playback; missing audio, unknown names and `null` fields have
their own states instead of errors.

**Develop:** `WEB_STATIC_DIR=web WEB_ALLOW_ANONYMOUS=1 python -m ttsbot.web`
and open http://127.0.0.1:8095 (a mounted `./web` also works in a container:
edit, reload). **Test:** `node --test "tests/web/*.test.mjs"` (the pure
modules; Node 20+) and `python -m unittest tests.test_web` (headers, policy,
every file the page references exists). A frontend dev server on another
origin must be listed in `WEB_CORS_ORIGINS` and fetch with
`credentials: "include"`.

## 6. Running

```bash
docker compose build tts_bot          # the shared image
docker compose up -d tts_web          # http://<server>:8095/api/v1/info
```

Settings: `.env.example`, section "Recordings website". `WEB_PASSWORD` is
required (or `WEB_ALLOW_ANONYMOUS=1`). Logs:
`docker logs discord_tts_web`; cache: `./cache/web` (safe to delete).

## 7. Extending

- **New derived data** (waveform peaks, per-speaker tracks, search index,
  summaries): a new artifact kind in `MediaCache` — a name (`[a-z0-9_]`), a fingerprint
  of its inputs and a builder function; caching, atomicity, failure
  back-off and clean-up come for free. Expose it through `ArchiveService`
  and a route; add its name to `info.features`.
- **New fields**: add them to the documents in `service.py`; clients ignore
  fields they do not know, so v1 grows without breaking. Removing or
  changing a field's meaning means `/api/v2` (`API_VERSION`) next to v1.
- **New sources of truth** (e.g. a database instead of folders): replace
  `RecordingStore`; the service only needs `keys`, `signature`, `read`.
- **Changes in the bot's folder format**: `store.py` is the only reader;
  bump `mix.PLAN_VERSION` when the mix or plan changes, so every cached mix
  is rebuilt.
- **Background work**: `ArchiveService.maintain` runs every
  `WEB_MAINTENANCE_SECONDS`; long jobs go through `MediaCache` lanes so they
  never block the quick ones.
