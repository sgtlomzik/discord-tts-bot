"""Read side of the voice recordings: what the website shows.

Layers, each usable without the ones above it:

- ``store`` reads the session folders the bot writes (never writes to
  them) into ``SessionData``; damaged files cost only their own data.
- ``mix`` joins a session's per-speaker Ogg files into one track without
  re-encoding and maps transcript lines to positions in it.
- ``media`` caches derived files (the mix, its MP3) by a fingerprint of
  their sources: atomic writes, one build at a time, LRU size cap.
- ``retention`` says when the bot's pruning will delete audio/transcripts.
- ``service`` puts these together into the API's JSON documents.

``ttsbot.web`` serves ``service`` over HTTP; other front ends (a CLI, a
Discord command) can use ``service`` the same way.
"""
