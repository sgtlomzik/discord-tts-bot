"""When the bot's retention sweep will delete a session's audio and transcript.

Mirrors ``TranscriptionWorker.prune_audio`` / ``prune_transcripts``:

- an Ogg file goes ``audio_days`` after it ended, but only once it is
  transcribed (untranscribed audio is the only copy of that speech);
- the whole session folder goes ``transcript_days`` after the session
  ended, unless a file still waits for transcription;
- 0 days keeps it forever.

The sweep runs every ``prune_interval_hours``, so a date here is the
earliest a file may go; it can stay until the next sweep after it.
"""

from __future__ import annotations

from dataclasses import dataclass

from ttsbot.archive.store import SessionData

DAY = 86400


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    audio_days: int = 0
    transcript_days: int = 0
    prune_interval_hours: float = 24.0

    @classmethod
    def from_config(cls) -> "RetentionPolicy":
        from ttsbot import config

        return cls(config.RECORDING_AUDIO_KEEP_DAYS, config.RECORDING_TRANSCRIPT_KEEP_DAYS,
                   config.RECORDING_PRUNE_INTERVAL_HOURS)

    def to_json(self) -> dict:
        return {"audio_keep_days": self.audio_days, "transcript_keep_days": self.transcript_days,
                "prune_interval_hours": self.prune_interval_hours}

    def facts(self, data: SessionData) -> "RetentionFacts":
        """What retention depends on, small enough to cache per session."""
        return RetentionFacts(
            ended_at=data.ended_at,
            audio=tuple((chunk.ended_at, chunk.result is not None) for chunk in data.chunks
                        if chunk.audio is not None),
            deleted_audio=sum(1 for chunk in data.chunks if chunk.audio is None),
            waiting=any(chunk.status == "pending" for chunk in data.chunks) or data.open_files > 0,
        )

    def describe(self, facts: "RetentionFacts", now: float) -> dict:
        return {"audio": self._audio(facts, now), "transcript": self._transcript(facts, now)}

    def _audio(self, facts: "RetentionFacts", now: float) -> dict:
        present = len(facts.audio)
        state = "none" if not present and not facts.deleted_audio else (
            "deleted" if not present else "partial" if facts.deleted_audio else "kept")
        doc = {"state": state, "files": present, "deleted_files": facts.deleted_audio,
               "expires_at": None, "fully_expires_at": None, "seconds_left": None,
               "kept_until_transcribed": sum(1 for _, done in facts.audio if not done)}
        if self.audio_days > 0:
            dates = [ended + self.audio_days * DAY for ended, done in facts.audio if done]
            if dates:
                doc["expires_at"] = round(min(dates), 3)
                doc["fully_expires_at"] = round(max(dates), 3)
                doc["seconds_left"] = max(0, round(min(dates) - now))
        return doc

    def _transcript(self, facts: "RetentionFacts", now: float) -> dict:
        doc = {"expires_at": None, "seconds_left": None, "held": False}
        if self.transcript_days > 0 and facts.ended_at is not None:
            expires = facts.ended_at + self.transcript_days * DAY
            doc["expires_at"] = round(expires, 3)
            doc["seconds_left"] = max(0, round(expires - now))
            doc["held"] = facts.waiting  # not deleted while transcription is unfinished
        return doc


@dataclass(frozen=True, slots=True)
class RetentionFacts:
    ended_at: float | None
    audio: tuple[tuple[float, bool], ...]  # (chunk end, transcribed) of every Ogg file still there
    deleted_audio: int
    waiting: bool
