"""Unified voice registry: Piper + MiniMax voices as first-class profiles.

This module owns the **catalog** of available voices — what voices exist
and how to drive each engine. It is deliberately separate from the
**selection** state (per-guild default + per-user override), which lives
in the existing ``BotConfigStore`` (``data/config.json``). Never store
``active_voice``/default/per-user assignments here.

The catalog persists in ``data/voices.json`` (the ``./data`` directory is
a mounted volume that survives container rebuilds). Writes are atomic
(temp file + ``os.replace``).

On first start the file does not exist; ``load_or_seed`` migrates the
current hardcoded ``VOICE_PROFILES`` (all Piper) plus one MiniMax record
seeded from ``MINIMAX_VOICE_ID`` (if set) into a fresh catalog. After the
upgrade, behavior is identical: the same Piper profile names remain valid,
so existing selection state keeps resolving.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

log = logging.getLogger("tts_bot.registry")

REGISTRY_VERSION = 1

# Voice names are kebab-case by convention: lowercase, digits and hyphens,
# must start with an alphanumeric. Keeps names safe as JSON keys, file
# fragments, and slash-command argument values.
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")

PROVIDER_PIPER = "piper"
PROVIDER_MINIMAX = "minimax"
PROVIDER_FISH = "fish"


def valid_voice_name(name: str) -> bool:
    return bool(_NAME_RE.match(name or ""))


@dataclass(frozen=True)
class PiperParams:
    model_path: str = ""
    config_path: str = ""
    speaker: int = -1
    length_scale: float = 1.0


@dataclass(frozen=True)
class MiniMaxParams:
    voice_id: str = ""
    model: str = "speech-2.8-turbo"
    speed: float = 1.0
    vol: float = 1.0
    pitch: int = 0
    # Empty string means "do not send emotion" (MiniMax default / auto).
    emotion: str = ""
    language_boost: str = "Russian"


@dataclass(frozen=True)
class FishParams:
    reference_id: str = ""
    model: str = ""  # empty: use the configured Fish model
    speed: float = 1.0
    volume_db: float = 0.0
    pitch: int = 0
    emotion: str = ""
    temperature: float = 0.7
    top_p: float = 0.7


@dataclass(frozen=True)
class VoiceRecord:
    name: str
    label: str
    description: str
    provider: str  # PROVIDER_PIPER | PROVIDER_MINIMAX | PROVIDER_FISH
    piper: Optional[PiperParams] = None
    minimax: Optional[MiniMaxParams] = None
    fish: Optional[FishParams] = None

    @property
    def is_minimax(self) -> bool:
        return self.provider == PROVIDER_MINIMAX

    @property
    def is_piper(self) -> bool:
        return self.provider == PROVIDER_PIPER

    @property
    def is_fish(self) -> bool:
        return self.provider == PROVIDER_FISH


@dataclass
class VoiceRegistry:
    """In-memory catalog of voices, keyed by name."""

    fallback_profile: str
    voices: dict[str, VoiceRecord] = field(default_factory=dict)
    version: int = REGISTRY_VERSION

    def get(self, name: Optional[str]) -> Optional[VoiceRecord]:
        if not name:
            return None
        return self.voices.get(name)

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self.voices

    def names(self) -> list[str]:
        return sorted(self.voices.keys())

    def add(self, record: VoiceRecord) -> None:
        self.voices[record.name] = record

    def fallback_record(self) -> Optional[VoiceRecord]:
        return self.voices.get(self.fallback_profile)


# ---------------------------------------------------------------------------
# (de)serialization
# ---------------------------------------------------------------------------


def _piper_to_dict(p: PiperParams) -> dict:
    return {
        "model_path": p.model_path,
        "config_path": p.config_path,
        "speaker": p.speaker,
        "length_scale": p.length_scale,
    }


def _minimax_to_dict(m: MiniMaxParams) -> dict:
    return {
        "voice_id": m.voice_id,
        "model": m.model,
        "speed": m.speed,
        "vol": m.vol,
        "pitch": m.pitch,
        "emotion": m.emotion,
        "language_boost": m.language_boost,
    }


def _record_to_dict(r: VoiceRecord) -> dict:
    out: dict = {
        "label": r.label,
        "description": r.description,
        "provider": r.provider,
    }
    if r.piper is not None:
        out["piper"] = _piper_to_dict(r.piper)
    if r.minimax is not None:
        out["minimax"] = _minimax_to_dict(r.minimax)
    if r.fish is not None:
        out["fish"] = {
            "reference_id": r.fish.reference_id, "model": r.fish.model,
            "speed": r.fish.speed, "volume_db": r.fish.volume_db,
            "pitch": r.fish.pitch, "emotion": r.fish.emotion,
            "temperature": r.fish.temperature, "top_p": r.fish.top_p,
        }
    return out


def _piper_from_dict(d: dict) -> PiperParams:
    """Build PiperParams from a (possibly partial) dict; missing -> defaults."""
    d = d or {}
    return PiperParams(
        model_path=str(d.get("model_path", "") or ""),
        config_path=str(d.get("config_path", "") or ""),
        speaker=int(d.get("speaker", -1)) if d.get("speaker") is not None else -1,
        length_scale=float(d.get("length_scale", 1.0) or 1.0),
    )


def _minimax_from_dict(d: dict) -> MiniMaxParams:
    """Build MiniMaxParams from a (possibly partial) dict; missing -> defaults.

    A partial parameter block must never crash — operators may hand-edit
    voices.json and omit speed/pitch/emotion.
    """
    d = d or {}
    defaults = MiniMaxParams()

    def _f(key: str, default: float) -> float:
        try:
            v = d.get(key)
            return default if v is None else float(v)
        except (TypeError, ValueError):
            return default

    def _i(key: str, default: int) -> int:
        try:
            v = d.get(key)
            return default if v is None else int(v)
        except (TypeError, ValueError):
            return default

    return MiniMaxParams(
        voice_id=str(d.get("voice_id", "") or ""),
        model=str(d.get("model") or defaults.model),
        speed=_f("speed", defaults.speed),
        vol=_f("vol", defaults.vol),
        pitch=_i("pitch", defaults.pitch),
        emotion=str(d.get("emotion", "") or ""),
        language_boost=str(d.get("language_boost") or defaults.language_boost),
    )


def _fish_from_dict(d: dict) -> FishParams:
    if not isinstance(d, dict):
        d = {}
    defaults = FishParams()

    def number(key: str, default: float) -> float:
        try:
            return float(d[key]) if d.get(key) is not None else default
        except (TypeError, ValueError):
            return default

    try:
        pitch = int(d.get("pitch", defaults.pitch))
    except (TypeError, ValueError):
        pitch = defaults.pitch
    return FishParams(
        reference_id=str(d.get("reference_id", "") or ""),
        model=str(d.get("model", "") or ""),
        speed=number("speed", defaults.speed),
        volume_db=number("volume_db", defaults.volume_db),
        pitch=pitch,
        emotion=str(d.get("emotion", "") or ""),
        temperature=number("temperature", defaults.temperature),
        top_p=number("top_p", defaults.top_p),
    )


def _record_from_dict(name: str, d: dict) -> Optional[VoiceRecord]:
    if not isinstance(d, dict):
        return None
    provider = str(d.get("provider", "")).strip().lower()
    if provider not in (PROVIDER_PIPER, PROVIDER_MINIMAX, PROVIDER_FISH):
        log.warning("voices.json: skipping %r with unknown provider %r", name, provider)
        return None
    label = str(d.get("label", name) or name)
    description = str(d.get("description", "") or "")
    piper = _piper_from_dict(d.get("piper", {})) if provider == PROVIDER_PIPER else None
    minimax = (
        _minimax_from_dict(d.get("minimax", {})) if provider == PROVIDER_MINIMAX else None
    )
    fish = _fish_from_dict(d.get("fish") or {}) if provider == PROVIDER_FISH else None
    if provider == PROVIDER_MINIMAX and not (minimax and minimax.voice_id):
        log.warning("voices.json: skipping minimax voice %r with no voice_id", name)
        return None
    if provider == PROVIDER_FISH and not (fish and fish.reference_id):
        log.warning("voices.json: skipping fish voice %r with no reference_id", name)
        return None
    return VoiceRecord(
        name=name,
        label=label,
        description=description,
        provider=provider,
        piper=piper,
        minimax=minimax,
        fish=fish,
    )


def registry_to_dict(reg: VoiceRegistry) -> dict:
    return {
        "version": reg.version,
        "fallback_profile": reg.fallback_profile,
        "voices": {name: _record_to_dict(rec) for name, rec in sorted(reg.voices.items())},
    }


def registry_from_dict(data: dict) -> Optional[VoiceRegistry]:
    if not isinstance(data, dict):
        return None
    voices_raw = data.get("voices", {})
    if not isinstance(voices_raw, dict):
        return None
    voices: dict[str, VoiceRecord] = {}
    for name, rec_dict in voices_raw.items():
        rec = _record_from_dict(str(name), rec_dict)
        if rec is not None:
            voices[str(name)] = rec
    fallback = str(data.get("fallback_profile", "") or "")
    return VoiceRegistry(
        fallback_profile=fallback,
        voices=voices,
        version=int(data.get("version", REGISTRY_VERSION) or REGISTRY_VERSION),
    )


# ---------------------------------------------------------------------------
# Persistence (atomic)
# ---------------------------------------------------------------------------


def load_registry(path: Path) -> Optional[VoiceRegistry]:
    """Load the catalog from disk, or None if missing/corrupt."""
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        log.exception("Failed to read voices.json: %s", path)
        return None
    reg = registry_from_dict(data)
    if reg is None:
        log.error("voices.json has invalid structure: %s", path)
    return reg


def save_registry(path: Path, reg: VoiceRegistry) -> None:
    """Atomically write the catalog (temp file + os.replace)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=str(path.parent), delete=False
    ) as tmp:
        json.dump(registry_to_dict(reg), tmp, ensure_ascii=False, indent=2)
        tmp.write("\n")
        tmp_path = Path(tmp.name)
    os.replace(tmp_path, path)


# ---------------------------------------------------------------------------
# Seeding / migration
# ---------------------------------------------------------------------------


def seed_registry(
    piper_profiles: dict,
    *,
    fallback_profile: str,
    minimax_voice_id: str = "",
    minimax_model: str = "speech-2.8-turbo",
    minimax_language_boost: str = "Russian",
) -> VoiceRegistry:
    """Build a fresh catalog from the hardcoded Piper profiles + MiniMax env.

    ``piper_profiles`` is the bot's ``VOICE_PROFILES`` mapping (duck-typed:
    each value exposes ``label``, ``piper_model_path``, ``piper_config_path``,
    ``piper_speaker``, ``piper_length_scale``). The module does not import
    the bot, keeping it unit-testable in isolation.
    """
    voices: dict[str, VoiceRecord] = {}
    for name, prof in piper_profiles.items():
        voices[name] = VoiceRecord(
            name=name,
            label=getattr(prof, "label", name) or name,
            description=getattr(prof, "description", "") or "Локальный голос Piper",
            provider=PROVIDER_PIPER,
            piper=PiperParams(
                model_path=getattr(prof, "piper_model_path", "") or "",
                config_path=getattr(prof, "piper_config_path", "") or "",
                speaker=int(getattr(prof, "piper_speaker", -1)),
                length_scale=float(getattr(prof, "piper_length_scale", 1.0)),
            ),
        )

    if minimax_voice_id:
        # Name the seeded MiniMax record after its voice_id when that is a
        # valid kebab name (the common case, e.g. "bussshy01"); otherwise
        # fall back to a stable generic name.
        seed_name = minimax_voice_id if valid_voice_name(minimax_voice_id) else "minimax-default"
        voices[seed_name] = VoiceRecord(
            name=seed_name,
            label=f"{minimax_voice_id} (MiniMax)",
            # TODO: clone keepalive (7-day TTL) — re-validate cloned voices periodically.
            description="Сид из MINIMAX_VOICE_ID",
            provider=PROVIDER_MINIMAX,
            minimax=MiniMaxParams(
                voice_id=minimax_voice_id,
                model=minimax_model or "speech-2.8-turbo",
                language_boost=minimax_language_boost or "Russian",
            ),
        )

    fb = fallback_profile if fallback_profile in voices else (
        next(iter(voices), "")
    )
    return VoiceRegistry(fallback_profile=fb, voices=voices)


def load_or_seed(
    path: Path,
    piper_profiles: dict,
    *,
    fallback_profile: str,
    minimax_voice_id: str = "",
    minimax_model: str = "speech-2.8-turbo",
    minimax_language_boost: str = "Russian",
) -> VoiceRegistry:
    """Load the catalog if present; otherwise seed from current config + save.

    The seeded catalog is written back so the next start is a plain load and
    operators can hand-edit the file.
    """
    existing = load_registry(path)
    if existing is not None:
        log.info(
            "Voice registry loaded path=%s voices=%d fallback=%s",
            path, len(existing.voices), existing.fallback_profile,
        )
        return existing

    seeded = seed_registry(
        piper_profiles,
        fallback_profile=fallback_profile,
        minimax_voice_id=minimax_voice_id,
        minimax_model=minimax_model,
        minimax_language_boost=minimax_language_boost,
    )
    # Persist the seed only when the data directory already exists (the
    # container's mounted ./data volume). On a host import where the path
    # parent is absent (e.g. unit tests importing bot.py with the default
    # /app/data path), keep the registry in-memory rather than creating
    # stray directories on the host filesystem.
    if path.parent.exists():
        try:
            save_registry(path, seeded)
            log.info(
                "Voice registry seeded path=%s voices=%s fallback=%s minimax_seed=%s",
                path, seeded.names(), seeded.fallback_profile,
                minimax_voice_id or "none",
            )
        except OSError:
            log.exception(
                "Failed to persist seeded voices.json at %s (continuing in-memory)", path
            )
    else:
        log.info(
            "Voice registry seeded in-memory (data dir %s absent); voices=%s fallback=%s",
            path.parent, seeded.names(), seeded.fallback_profile,
        )
    return seeded
