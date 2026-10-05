"""Turn Groq results into a readable, time-ordered session transcript.

Each chunk's result (``NNNN.json`` next to ``NNNN.ogg``) keeps Groq's
segments with their offsets mapped back to wall-clock time through the
chunk's pieces. ``build_session_transcript`` merges every speaker's
segments and the bot's TTS lines into ``transcript.md`` (for people) and
``transcript.jsonl`` (one line per utterance, for tools).
"""

from __future__ import annotations

import bisect
import datetime as dt
import json
import re
from pathlib import Path
from typing import Callable

# Whisper's well-known inventions on noise or silence, mostly learned from
# subtitles; a segment matching one is dropped (kept under "dropped").
_HALLUCINATIONS = re.compile(
    r"субтитр|dimatorzok|продолжение следует|спасибо за просмотр|"
    r"подпис\w* на (наш |мой )?канал|ставьте лайк|amara\.org",
    re.IGNORECASE,
)


def wall_time(pieces: list[list[float]], offset: float) -> float:
    """Wall-clock time of ``offset`` seconds into a chunk file."""
    starts = [p[0] for p in pieces]
    i = max(0, bisect.bisect_right(starts, offset) - 1)
    piece_offset, piece_wall = pieces[i]
    return piece_wall + max(0.0, offset - piece_offset)


def drop_reason(segment: dict) -> str | None:
    text = segment.get("text", "").strip()
    if not text:
        return "empty"
    if _HALLUCINATIONS.search(text):
        return "hallucination"
    # Whisper's own no-speech rule, and its repetition-loop signal.
    if segment.get("no_speech_prob", 0) > 0.6 and segment.get("avg_logprob", 0) < -1.0:
        return "no_speech"
    if segment.get("compression_ratio", 0) > 2.4:
        return "repetition"
    return None


def chunk_result(entry: dict, groq: dict, *, model: str, took: float) -> dict:
    """The ``NNNN.json`` stored next to a chunk file."""
    kept, dropped = [], []
    for segment in groq.get("segments") or []:
        item = {
            "start": round(segment["start"], 2),
            "end": round(segment["end"], 2),
            "at": round(wall_time(entry["pieces"], segment["start"]), 3),
            "until": round(wall_time(entry["pieces"], segment["end"]), 3),
            "text": segment.get("text", "").strip(),
        }
        reason = drop_reason(segment)
        if reason:
            dropped.append({**item, "why": reason})
        else:
            kept.append(item)
    return {
        "file": entry["file"],
        "user_id": entry["user_id"],
        "model": model,
        "language": groq.get("language"),
        "audio_seconds": entry["audio_seconds"],
        "took_s": round(took, 3),
        "text": " ".join(s["text"] for s in kept),
        "segments": kept,
        "dropped": dropped,
    }


def result_path(session: Path, chunk_file: str) -> Path:
    return (session / chunk_file).with_suffix(".json")


MERGE_GAP_SECONDS = 2.0


def utterances(entries: list[dict], results: dict[str, dict], tts: list[dict]) -> list[dict]:
    """Every kept segment and TTS line of a session, in time order.

    ``results`` maps a chunk file to its ``NNNN.json``; a voice line also
    says where it is in that file (``file``, ``offset`` in seconds), so a
    player can find it.
    """
    lines: list[dict] = []
    for entry in entries:
        result = results.get(entry["file"])
        if result is None:
            continue
        for segment in result["segments"]:
            lines.append({"user_id": entry["user_id"], "kind": "voice", "at": segment["at"],
                          "until": segment["until"], "text": segment["text"],
                          "file": entry["file"], "offset": segment["start"]})
    for item in tts:
        lines.append({"user_id": item["user_id"], "kind": "tts", "at": item["at"],
                      "until": item["at"], "text": item["text"]})
    lines.sort(key=lambda line: line["at"])
    return lines


def merge_lines(lines: list[dict], merge_gap: float = MERGE_GAP_SECONDS) -> list[dict]:
    """Join consecutive lines of one speaker (and kind) less than
    ``merge_gap`` seconds apart; each merged line keeps its ``parts``."""
    merged: list[dict] = []
    for line in lines:
        last = merged[-1] if merged else None
        if (last and last["user_id"] == line["user_id"] and last["kind"] == line["kind"]
                and line["at"] - last["until"] <= merge_gap):
            last["text"] += " " + line["text"]
            last["until"] = max(last["until"], line["until"])
            last["parts"].append(line)
        else:
            merged.append({"user_id": line["user_id"], "kind": line["kind"], "at": line["at"],
                           "until": line["until"], "text": line["text"], "parts": [line]})
    return merged


def build_session_transcript(session: Path, name_for: Callable[[int], str], tz: dt.tzinfo,
                             merge_gap: float = MERGE_GAP_SECONDS) -> dict:
    """Write ``transcript.md`` / ``transcript.jsonl``; return a summary."""
    info = json.loads((session / "session.json").read_text(encoding="utf-8"))
    entries = []
    manifest = session / "chunks.jsonl"
    if manifest.exists():
        entries = [json.loads(line) for line in manifest.read_text(encoding="utf-8").splitlines() if line]
    results: dict[str, dict] = {}
    pending = 0
    for entry in entries:
        path = result_path(session, entry["file"])
        if not path.exists():
            pending += 1
            continue
        results[entry["file"]] = json.loads(path.read_text(encoding="utf-8"))
    tts_lines = []
    tts = session / "tts.jsonl"
    if tts.exists():
        tts_lines = [json.loads(raw) for raw in tts.read_text(encoding="utf-8").splitlines() if raw]
    merged = merge_lines(utterances(entries, results, tts_lines), merge_gap)

    def clock(epoch: float, fmt: str = "%H:%M:%S") -> str:
        return dt.datetime.fromtimestamp(epoch, tz).strftime(fmt)

    names_file = session / "names.json"
    names: dict[int, str] = {}
    if names_file.exists():
        names = {int(k): v for k, v in json.loads(names_file.read_text(encoding="utf-8")).items()}
    out = [f"# Запись голосового канала {info['channel_id']}", ""]
    ended = info.get("ended_at")
    out.append(f"{clock(info['started_at'], '%Y-%m-%d %H:%M')} — "
               f"{clock(ended, '%H:%M') if ended else 'идёт запись'}")
    if pending:
        out.append(f"Ещё не расшифровано файлов: {pending}")
    out.append("")
    for line in merged:
        name = names.setdefault(line["user_id"], name_for(line["user_id"]))
        label = f"{name} (TTS)" if line["kind"] == "tts" else name
        out.append(f"[{clock(line['at'])}] {label}: {line['text']}")
    (session / "transcript.md").write_text("\n".join(out) + "\n", encoding="utf-8")
    with (session / "transcript.jsonl").open("w", encoding="utf-8") as jsonl:
        for line in merged:
            row = {key: line[key] for key in ("user_id", "kind", "at", "until", "text")}
            jsonl.write(json.dumps({**row, "name": names[line["user_id"]]}, ensure_ascii=False) + "\n")
    return {"lines": len(merged), "pending": pending, "files": len(entries)}
