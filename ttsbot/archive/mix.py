"""Join a session's per-speaker recordings into one track, and find lines in it.

Every speaker has their own Ogg files, and each file is a run of
"pieces" (utterances) with the wall-clock time each started. The mix
plays every piece once, in the order they started, with ``gap_ms`` of
silence between them: long silences go, overlapping speech is played
one after the other. Packets are copied as they are (no decoding or
re-encoding), so building it costs little more than reading the files.

``MixPlan`` records where each piece landed; it maps a transcript
segment (a file and an offset in it) or any wall-clock moment to a
position in the mix. It is stored as JSON next to the mix.
"""

from __future__ import annotations

import bisect
import hashlib
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from ttsbot.ogg_opus import UnsupportedOpusStream
from ttsbot.recording.ogg import OggOpusWriter, OggPacketIndex
from ttsbot.recording.rtp import OPUS_SILENCE

from ttsbot.archive.store import Chunk

log = logging.getLogger("tts_web")

# Bump when the mix or plan format changes: every cached mix is rebuilt.
PLAN_VERSION = 1
_SERIAL = 0x4D495831  # "MIX1"


@dataclass(frozen=True, slots=True)
class MixPiece:
    user_id: int
    file: str
    file_start: float  # where the piece starts in its own file, seconds
    wall: float  # when it was said, epoch seconds
    start: float  # where it starts in the mix, seconds
    duration: float

    def to_list(self) -> list:
        return [self.user_id, self.file, self.file_start, self.wall, self.start, self.duration]


@dataclass
class MixPlan:
    pieces: list[MixPiece]
    duration: float
    gap: float
    missing_files: list[str] = field(default_factory=list)  # audio deleted by retention
    damaged_files: list[str] = field(default_factory=list)  # unreadable or cut short

    def __post_init__(self) -> None:
        self._by_file: dict[str, list[MixPiece]] = {}
        for piece in self.pieces:
            self._by_file.setdefault(piece.file, []).append(piece)
        for pieces in self._by_file.values():
            pieces.sort(key=lambda p: p.file_start)
        self._file_starts = {f: [p.file_start for p in ps] for f, ps in self._by_file.items()}
        self._walls = [p.wall for p in self.pieces]  # pieces are in wall-clock order

    @property
    def empty(self) -> bool:
        return not self.pieces

    def position_in_file(self, file: str, offset: float) -> float | None:
        """Mix position of ``offset`` seconds into chunk ``file`` (None: not in the mix)."""
        pieces = self._by_file.get(file)
        if not pieces:
            return None
        i = max(0, bisect.bisect_right(self._file_starts[file], offset + 1e-6) - 1)
        piece = pieces[i]
        inside = min(max(0.0, offset - piece.file_start), piece.duration)
        return round(piece.start + inside, 3)

    def position_at(self, wall: float) -> float | None:
        """Mix position of a wall-clock moment: inside the piece being said
        then, or else the start of the next one (what was said after it)."""
        if not self.pieces:
            return None
        i = bisect.bisect_right(self._walls, wall) - 1
        if i >= 0:
            piece = self.pieces[i]
            if wall <= piece.wall + piece.duration:
                return round(piece.start + (wall - piece.wall), 3)
        if i + 1 < len(self.pieces):
            return self.pieces[i + 1].start
        last = self.pieces[-1]
        return round(last.start + last.duration, 3)

    def to_json(self) -> dict:
        return {"version": PLAN_VERSION, "duration": self.duration, "gap": self.gap,
                "missing_files": self.missing_files, "damaged_files": self.damaged_files,
                "pieces": [p.to_list() for p in self.pieces]}

    @classmethod
    def from_json(cls, data: dict) -> "MixPlan":
        if data.get("version") != PLAN_VERSION:
            raise ValueError(f"mix plan version {data.get('version')}")
        return cls([MixPiece(*row) for row in data["pieces"]], data["duration"], data["gap"],
                   list(data.get("missing_files", ())), list(data.get("damaged_files", ())))


def fingerprint(chunks: Iterable[Chunk], gap_ms: int) -> str:
    """Changes whenever the mix would: audio added or deleted, or settings."""
    digest = hashlib.sha256(f"v{PLAN_VERSION}:{gap_ms}".encode())
    for chunk in sorted(chunks, key=lambda c: c.file):
        if chunk.audio is not None:
            digest.update(f"|{chunk.file}:{chunk.audio[0]}:{chunk.audio[1]}".encode())
    return digest.hexdigest()[:20]


def fingerprint_of(artifact: Path) -> str:
    """The fingerprint in a cached mix folder's name (``mix-<fingerprint>``)."""
    return artifact.name.split("-", 1)[1]


def build_mix(session: Path, chunks: list[Chunk], out: Path, gap_ms: int) -> MixPlan:
    """Write the mix of ``chunks`` to ``out`` (an Ogg Opus file); return its plan.

    No audio at all gives an empty plan and no file. A file that cannot be
    read is left out (``damaged_files``); one cut short contributes what
    it has.
    """
    order = []  # (wall, user, file, piece offset, next piece offset or None)
    missing = []
    for chunk in chunks:
        if chunk.audio is None:
            missing.append(chunk.file)
            continue
        pieces = sorted(chunk.entry["pieces"], key=lambda p: p[0])
        for i, (offset, wall) in enumerate(pieces):
            following = pieces[i + 1][0] if i + 1 < len(pieces) else None
            order.append((wall, chunk.user_id, chunk.file, offset, following))
    order.sort(key=lambda item: item[:4])
    last_use = {item[2]: n for n, item in enumerate(order)}

    plan_pieces: list[MixPiece] = []
    damaged: list[str] = []
    indexes: dict[str, OggPacketIndex | None] = {}
    writer: OggOpusWriter | None = None
    gap_samples = gap_ms * 48
    try:
        for n, (wall, user_id, file, offset, following) in enumerate(order):
            index = indexes.get(file, False)
            if index is False:
                index = indexes[file] = _open(session / file, damaged)
            if index is not None:
                first = index.index_at(offset)
                last = index.index_at(following) if following is not None else len(index)
                while last > first and index.packet(last - 1) == OPUS_SILENCE:
                    last -= 1  # the pause the recorder put before the next piece
                if last > first:
                    if writer is None:
                        writer = OggOpusWriter(out, serial=_SERIAL)
                    elif gap_samples:
                        writer.write_silence(gap_samples)
                    start = writer.seconds
                    for k in range(first, last):
                        try:
                            writer.write(index.packet(k))
                        except UnsupportedOpusStream:
                            continue  # one undecodable packet: drop it, keep the rest
                    plan_pieces.append(MixPiece(user_id, file, round(index.start_sample(first) / 48000, 3),
                                                round(wall, 3), round(start, 3),
                                                round(writer.seconds - start, 3)))
            if last_use[file] == n and indexes.get(file) is not None:
                indexes[file].close()  # done with this file: keep few open at a time
                indexes[file] = None
    finally:
        for index in indexes.values():
            if index is not None:
                index.close()
        if writer is not None:
            writer.close()
    return MixPlan(plan_pieces, round(writer.seconds, 3) if writer else 0.0, gap_ms / 1000,
                   missing, damaged)


def _open(path: Path, damaged: list[str]) -> OggPacketIndex | None:
    name = f"{path.parent.name}/{path.name}"
    try:
        index = OggPacketIndex(path)
    except FileNotFoundError:
        damaged.append(name)  # deleted between the scan and now
        return None
    except (OSError, ValueError) as exc:
        log.warning("Mix skips an unreadable file %s: %s", path, exc)
        damaged.append(name)
        return None
    if index.damaged:
        log.warning("Mix uses the readable part of a damaged file %s", path)
        damaged.append(name)
    return index


def save_plan(plan: MixPlan, path: Path) -> None:
    path.write_text(json.dumps(plan.to_json(), separators=(",", ":")), encoding="utf-8")


def load_plan(path: Path) -> MixPlan:
    return MixPlan.from_json(json.loads(path.read_text(encoding="utf-8")))
