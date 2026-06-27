import asyncio
import ctypes.util
import json
import logging
import os
import queue as thread_queue
import re
import struct
import tempfile
import threading
import time
import unicodedata
import uuid
import wave
from dataclasses import dataclass, field, replace
from pathlib import Path

import discord
import emoji
from discord import app_commands
from discord.ext import commands

try:
    from piper import PiperVoice, SynthesisConfig
except Exception:  # pragma: no cover - optional dependency
    PiperVoice = None
    SynthesisConfig = None

from tts_providers import (
    LocalProvider,
    MiniMaxError,
    MiniMaxProvider,
    MiniMaxVoiceNotFoundError,
    TTSDispatcher,
    TTSPhraseCache,
    load_cache_config_from_env,
    load_circuit_breaker_from_env,
    load_dispatcher_config_from_env,
    load_minimax_config_from_env,
)
import voice_registry


LOG_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(), format=LOG_FORMAT)
log = logging.getLogger("tts_bot")

TMP_DIR = Path(os.getenv("TTS_TMP_DIR", "/dev/shm"))
PCM_SAMPLE_RATE = 48000
PCM_CHANNELS = 2
PCM_SAMPLE_WIDTH = 2
PCM_FRAME_MS = 20
PCM_FRAME_BYTES = int(PCM_SAMPLE_RATE * PCM_FRAME_MS / 1000) * PCM_CHANNELS * PCM_SAMPLE_WIDTH
BOT_CONFIG_PATH = Path(os.getenv("BOT_CONFIG_PATH", "/app/data/config.json"))
# Catalog of available voices (Piper + MiniMax). Lives in the mounted
# ./data volume next to config.json; seeded on first start. This is the
# CATALOG only — selection state stays in BotConfigStore/config.json.
VOICES_REGISTRY_PATH = Path(
    os.getenv("VOICES_REGISTRY_PATH", str(BOT_CONFIG_PATH.parent / "voices.json"))
)
DEFAULT_WHITELIST = "441612025286885397"
DEFAULT_VOICE_PROFILE = os.getenv("TTS_DEFAULT_VOICE_PROFILE", "piper-ruslan").strip()

TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
MAX_TEXT_LENGTH = int(os.getenv("TTS_MAX_TEXT_LENGTH", "500"))
# Per-message character cap applied at the enqueue boundary, before
# any provider is invoked. The spec recommends ~300 to protect the
# cloud API quota from accidental walls of text. Default 300; the
# legacy TTS_MAX_TEXT_LENGTH cap (500) is still the hard ceiling if
# this is unset/zero.
TTS_MAX_CHARS = int(os.getenv("TTS_MAX_CHARS", "300"))
QUEUE_MAXSIZE = int(os.getenv("TTS_QUEUE_MAXSIZE", "50"))
TTS_PREROLL_MS = int(os.getenv("TTS_PREROLL_MS", os.getenv("TTS_START_PAD_MS", "250")))
TTS_PREROLL_MODE = os.getenv("TTS_PREROLL_MODE", "silence").strip().lower()
TTS_PREROLL_VOLUME_DB = float(os.getenv("TTS_PREROLL_VOLUME_DB", "-90"))
TTS_SILENCE_TAIL_MS = int(os.getenv("TTS_SILENCE_TAIL_MS", "200"))
TTS_CONTINUOUS_STREAM = os.getenv("TTS_CONTINUOUS_STREAM", "1").strip().lower() not in {"0", "false", "no"}
# Stream MiniMax audio chunk-by-chunk so the bot starts talking before the
# whole clip is generated (cuts Time-To-First-Audio). Requires the
# continuous stream. Feature-flagged for instant revert without a redeploy.
TTS_STREAMING_ENABLED = os.getenv("TTS_STREAMING_ENABLED", "1").strip().lower() not in {"0", "false", "no"}
# Budget for the FIRST audio chunk only. After the first chunk the stream
# lives as long as it needs (long messages legitimately stream for seconds).
TTS_STREAM_TTFA_TIMEOUT = float(os.getenv("TTS_STREAM_TTFA_TIMEOUT", os.getenv("TTS_REQUEST_TIMEOUT", "2.5")))
# Prefetch: decouple generation from playback so message N+1 is synthesized
# while N is still playing (cuts queue_wait under bursts). Playback stays
# strictly sequential FIFO. TTS_PREFETCH_ENABLED=0 reverts to the proven
# single-worker path (runtime kill-switch). Lookahead = messages generated
# ahead (1 is plenty; playback serializes anyway).
TTS_PREFETCH_ENABLED = os.getenv("TTS_PREFETCH_ENABLED", "1").strip().lower() not in {"0", "false", "no"}
TTS_PREFETCH_LOOKAHEAD = max(1, int(os.getenv("TTS_PREFETCH_LOOKAHEAD", "1")))
TTS_IDLE_FRAME_MODE = os.getenv("TTS_IDLE_FRAME_MODE", "silence").strip().lower()
TTS_IDLE_VOLUME_DB = float(os.getenv("TTS_IDLE_VOLUME_DB", "-60"))
TTS_STREAM_TAIL_MS = int(os.getenv("TTS_STREAM_TAIL_MS", "200"))
TTS_MAX_CONTINUOUS_IDLE_SECONDS = int(os.getenv("TTS_MAX_CONTINUOUS_IDLE_SECONDS", "900"))
IDLE_DISCONNECT_SECONDS = int(os.getenv("TTS_IDLE_DISCONNECT_SECONDS", "60"))
TTS_AUTO_CONNECT_ENABLED = os.getenv("TTS_AUTO_CONNECT_ENABLED", "1").strip().lower() not in {
    "0",
    "false",
    "no",
}
AUTO_CONNECT_SUPPRESS_SECONDS = int(os.getenv("TTS_AUTO_CONNECT_SUPPRESS_SECONDS", "30"))
TTS_TRIM_SILENCE = os.getenv("TTS_TRIM_SILENCE", "1").strip().lower() not in {"0", "false", "no"}
FFMPEG_LOW_DELAY = os.getenv("FFMPEG_LOW_DELAY", "1").strip().lower() not in {"0", "false", "no"}
TTS_MERGE_SHORT_MESSAGES = os.getenv("TTS_MERGE_SHORT_MESSAGES", "1").strip().lower() not in {
    "0",
    "false",
    "no",
}
TTS_MERGE_MAX_CHARS = int(os.getenv("TTS_MERGE_MAX_CHARS", "40"))
TTS_MERGE_WINDOW_MS = int(os.getenv("TTS_MERGE_WINDOW_MS", "900"))
TTS_MERGE_MAX_PARTS = int(os.getenv("TTS_MERGE_MAX_PARTS", "4"))
TTS_MERGE_ALGORITHM = os.getenv("TTS_MERGE_ALGORITHM", "legacy").strip().lower()
if TTS_MERGE_ALGORITHM not in {"legacy", "selective_hold_v2", "off"}:
    log.warning("Unknown TTS_MERGE_ALGORITHM=%s; using legacy", TTS_MERGE_ALGORITHM)
    TTS_MERGE_ALGORITHM = "legacy"
TTS_SELECTIVE_HOLD_ENABLED = os.getenv("TTS_SELECTIVE_HOLD_ENABLED", "1").strip().lower() not in {
    "0",
    "false",
    "no",
}
TTS_SELECTIVE_HOLD_HARD_CAP_MS = int(os.getenv("TTS_SELECTIVE_HOLD_HARD_CAP_MS", "1200"))
TTS_SELECTIVE_HOLD_START_EFFECTIVE_LEN = int(os.getenv("TTS_SELECTIVE_HOLD_START_EFFECTIVE_LEN", "10"))
TTS_SELECTIVE_HOLD_START_MIN_WORDS_ALT = int(os.getenv("TTS_SELECTIVE_HOLD_START_MIN_WORDS_ALT", "2"))
TTS_SELECTIVE_HOLD_START_MIN_EFFECTIVE_LEN_ALT = int(os.getenv("TTS_SELECTIVE_HOLD_START_MIN_EFFECTIVE_LEN_ALT", "6"))
TTS_SELECTIVE_HOLD_REACTION_PAUSE_MS = int(os.getenv("TTS_SELECTIVE_HOLD_REACTION_PAUSE_MS", "5000"))
TTS_SELECTIVE_HOLD_MAX_PARTS = int(os.getenv("TTS_SELECTIVE_HOLD_MAX_PARTS", "3"))
TTS_SELECTIVE_HOLD_MAX_GROUP_EFFECTIVE_LEN = int(os.getenv("TTS_SELECTIVE_HOLD_MAX_GROUP_EFFECTIVE_LEN", "56"))
TTS_SELECTIVE_HOLD_JOIN_SEPARATOR = os.getenv("TTS_SELECTIVE_HOLD_JOIN_SEPARATOR", ", ")
TTS_SELECTIVE_HOLD_DROP_URL_ONLY = os.getenv("TTS_SELECTIVE_HOLD_DROP_URL_ONLY", "1").strip().lower() not in {
    "0",
    "false",
    "no",
}
TTS_SELECTIVE_HOLD_DROP_MENTION_ONLY = os.getenv("TTS_SELECTIVE_HOLD_DROP_MENTION_ONLY", "1").strip().lower() not in {
    "0",
    "false",
    "no",
}
TTS_SELECTIVE_HOLD_LOG_DECISIONS = os.getenv("TTS_SELECTIVE_HOLD_LOG_DECISIONS", "0").strip().lower() not in {
    "0",
    "false",
    "no",
}
TTS_SELECTIVE_HOLD_ENABLE_ORDER_PRESERVING_FLUSH = os.getenv(
    "TTS_SELECTIVE_HOLD_ENABLE_ORDER_PRESERVING_FLUSH", "1"
).strip().lower() not in {"0", "false", "no"}
TTS_QUEUE_PUT_TIMEOUT_MS = int(os.getenv("TTS_QUEUE_PUT_TIMEOUT_MS", "500"))

VOICE_CONNECT_COOLDOWN_SECONDS = int(os.getenv("VOICE_CONNECT_COOLDOWN_SECONDS", "60"))
PIPER_MODEL_PATH = os.getenv("PIPER_MODEL_PATH", "/app/models/ru_RU-ruslan-medium.onnx").strip()
PIPER_CONFIG_PATH = os.getenv(
    "PIPER_CONFIG_PATH",
    "/app/models/ru_RU-ruslan-medium.onnx.json",
).strip()
PIPER_SPEAKER = int(os.getenv("PIPER_SPEAKER", "-1"))
PIPER_LENGTH_SCALE = float(os.getenv("PIPER_LENGTH_SCALE", "1.0"))

EMOJI_MAP = {
    "Blya2x": "Бля",
    "pepe_sad": "Грустно",
    "kekw": "Кек",
}
CUSTOM_EMOJI_RE = re.compile(r"<a?:([A-Za-z0-9_]+):(\d+)>")
MENTION_RE = re.compile(r"<@!?\d+>|<@&\d+>|<#\d+>")
URL_RE = re.compile(r"https?://\S+|www\.\S+")
DISCORD_TOKEN_RE = re.compile(r"(<a?:[A-Za-z0-9_]+:\d+>|<@!?\d+>|<@&\d+>|<#\d+>|https?://\S+|www\.\S+)")
# A bare shortcode the way a user might type it into a slash-command arg.
SHORTCODE_RE = re.compile(r"^:([A-Za-z0-9_]+):$")
# Upper bound on an emoji pronunciation; long enough for a phrase, short
# enough that a single emoji can't smuggle a wall of text into synthesis.
EMOJI_PRONUNCIATION_MAX = 100


def parse_custom_emoji_arg(value: str, guild) -> tuple[str, str] | None:
    """Resolve a custom emoji given in a slash-command string arg.

    Accepts a ready token ``<:name:id>`` / ``<a:name:id>`` (animated and
    static share one id-space) or a bare ``:name:`` that is resolved against
    the guild's emojis. Returns ``(emoji_id, name)`` keyed by the globally
    unique, stable **id**. Returns ``None`` for a unicode emoji or plain
    text — the caller rejects those with a clear message.
    """
    if not value:
        return None
    value = value.strip()
    token = CUSTOM_EMOJI_RE.search(value)
    if token:
        return token.group(2), token.group(1)
    short = SHORTCODE_RE.match(value)
    if short and guild is not None:
        name = short.group(1)
        emojis = getattr(guild, "emojis", ()) or ()
        for emoji in emojis:  # exact match first
            if emoji.name == name:
                return str(emoji.id), emoji.name
        lowered = name.lower()
        for emoji in emojis:  # then case-insensitive
            if emoji.name.lower() == lowered:
                return str(emoji.id), emoji.name
    return None


def sanitize_pronunciation(text: str, *, max_chars: int = EMOJI_PRONUNCIATION_MAX) -> str | None:
    """Clean a user-supplied pronunciation before storing it.

    Strips nested Discord tokens (so an alias can't smuggle more emoji or
    mentions), removes control characters, collapses whitespace and caps the
    length. Returns ``None`` when nothing speakable is left.
    """
    if not text:
        return None
    text = DISCORD_TOKEN_RE.sub(" ", text)
    text = "".join(ch for ch in text if ch == " " or unicodedata.category(ch)[0] != "C")
    text = " ".join(text.split()).strip()
    if not text:
        return None
    if len(text) > max_chars:
        text = text[:max_chars].rstrip()
    return text or None


def substitute_emoji_aliases(text: str, aliases: dict[str, str]) -> str:
    """Replace custom-emoji tokens whose id has an alias with its spoken word.

    The word is padded with spaces so neighbours don't fuse (``да<:Kekis:1>``
    -> ``да кекис``). Tokens without an alias are left untouched for the
    normal stripping step to remove (default = silence).
    """
    if not text or not aliases:
        return text

    def _repl(match: re.Match[str]) -> str:
        say = aliases.get(match.group(2))
        return f" {say} " if say else match.group(0)

    return CUSTOM_EMOJI_RE.sub(_repl, text)


def speak_unicode_emoji(text: str) -> str:
    """Replace standard Unicode emoji with their Russian names so the bot
    reads them aloud (😂 -> "смеется до слез", 🇷🇺 -> "флаг Россия").

    Each emoji is named individually via its position, so underscores in the
    surrounding user text (``люблю_тебя``) are never touched. Skin-tone
    modifiers, flags and ZWJ sequences (families) resolve as one unit. Falls
    back to the English name for the rare emoji without a Russian translation;
    leaves the emoji as-is only if it has no name at all.
    """
    if not text:
        return text
    matches = emoji.emoji_list(text)
    if not matches:
        return text
    out: list[str] = []
    last = 0
    for match in matches:
        out.append(text[last : match["match_start"]])
        char = match["emoji"]
        name = emoji.demojize(char, language="ru", delimiters=("", ""))
        if name == char:  # no Russian name; try English
            name = emoji.demojize(char, language="en", delimiters=("", ""))
        name = name.replace("_", " ").strip()
        out.append(f" {name} " if name and name != char else " ")
        last = match["match_end"]
    out.append(text[last:])
    return "".join(out)


def parse_user_ids(value: str) -> set[int]:
    user_ids: set[int] = set()
    for part in value.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            user_ids.add(int(part))
        except ValueError:
            log.warning("Ignoring invalid WHITELIST_USERS entry: %r", part)
    return user_ids


WHITELIST_USERS = parse_user_ids(os.getenv("WHITELIST_USERS", DEFAULT_WHITELIST))


@dataclass(frozen=True)
class VoiceProfile:
    name: str
    label: str
    piper_model_path: str = ""
    piper_config_path: str = ""
    piper_speaker: int = -1
    piper_length_scale: float = 1.0


@dataclass
class GuildConfig:
    enabled: bool = True
    allowed_users: set[int] = field(default_factory=set)
    default_voice: str = DEFAULT_VOICE_PROFILE
    user_voices: dict[int, str] = field(default_factory=dict)


@dataclass
class TTSJob:
    text: str
    voice_channel: discord.VoiceChannel
    queued_at: float
    author_id: int
    guild_id: int
    text_channel_id: int
    voice_profile: str
    message_ts: float = field(default_factory=time.perf_counter)


@dataclass(eq=False)  # identity-based: each prepared item is unique (set member)
class PreparedAudio:
    """A job whose audio is being (or has been) generated ahead of playback.

    The generation worker fills ``channel`` with batches of 20ms PCM frames
    (``list[bytes]``) and ends it with a ``None`` sentinel. The playback
    worker drains ``channel`` into the continuous player in order. ``cancelled``
    is set by ``queue-clear`` to stop generation/playback of this item.
    """
    job: TTSJob
    channel: asyncio.Queue
    cancelled: bool = False
    provider: str = ""


@dataclass
class ParsedMessage:
    spoken_text: str
    raw_length: int
    effective_length: int
    word_count: int
    emoji_count: int
    custom_emoji_count: int
    animated_custom_emoji_count: int
    mention_count: int
    url_count: int
    is_unicode_emoji_only: bool
    is_custom_emoji_only: bool
    is_animated_custom_emoji_only: bool
    is_mention_only: bool
    is_url_only: bool
    is_single_digit: bool
    is_single_symbol: bool
    is_caps_shout: bool
    is_keyboard_smash: bool
    is_question_or_terminal: bool

    @property
    def is_special_only(self) -> bool:
        return (
            self.is_unicode_emoji_only
            or self.is_custom_emoji_only
            or self.is_animated_custom_emoji_only
            or self.is_mention_only
            or self.is_url_only
        )


@dataclass
class MergeBufferState:
    key: tuple[int, int]
    voice_channel: discord.VoiceChannel
    author_id: int
    text_channel_id: int
    first_ts: float
    last_ts: float
    deadline_ts: float
    generation_id: int
    join_separator: str
    items: list[ParsedMessage] = field(default_factory=list)
    timer_task: asyncio.Task[None] | None = None
    has_substantive_starter: bool = False

    @property
    def effective_len_total(self) -> int:
        return sum(item.effective_length for item in self.items)


VOICE_PROFILES: dict[str, VoiceProfile] = {
    "piper-ruslan": VoiceProfile(
        name="piper-ruslan",
        label="Piper Ruslan",
        piper_model_path=PIPER_MODEL_PATH,
        piper_config_path=PIPER_CONFIG_PATH,
        piper_speaker=PIPER_SPEAKER,
        piper_length_scale=PIPER_LENGTH_SCALE,
    ),
    "piper-irina": VoiceProfile(
        name="piper-irina",
        label="Piper Irina",
        piper_model_path="/app/models/ru_RU-irina-medium.onnx",
        piper_config_path="/app/models/ru_RU-irina-medium.onnx.json",
        piper_speaker=PIPER_SPEAKER,
        piper_length_scale=PIPER_LENGTH_SCALE,
    ),
}

if DEFAULT_VOICE_PROFILE not in VOICE_PROFILES:
    log.warning("Unknown TTS_DEFAULT_VOICE_PROFILE=%s; using piper-ruslan", DEFAULT_VOICE_PROFILE)
    DEFAULT_VOICE_PROFILE = "piper-ruslan"

if TTS_PREROLL_MODE not in {"noise", "sine", "silence"}:
    log.warning("Unknown TTS_PREROLL_MODE=%s; using noise", TTS_PREROLL_MODE)
    TTS_PREROLL_MODE = "noise"

if TTS_IDLE_FRAME_MODE not in {"comfort_noise", "silence"}:
    log.warning("Unknown TTS_IDLE_FRAME_MODE=%s; using comfort_noise", TTS_IDLE_FRAME_MODE)
    TTS_IDLE_FRAME_MODE = "comfort_noise"


class BotConfigStore:
    def __init__(self, path: Path, fallback_users: set[int], voice_registry=None) -> None:
        self.path = path
        self.fallback_users = set(fallback_users)
        # Validate stored voice names against the unified registry when
        # available (so MiniMax assignments survive reload); otherwise fall
        # back to the hardcoded Piper profiles (keeps tests that construct
        # the store without a registry working unchanged).
        self.voice_registry = voice_registry
        self.guilds: dict[int, GuildConfig] = {}
        # Custom-emoji pronunciation aliases, keyed by emoji id (globally
        # unique, stable). Value: {"name": <display name>, "say": <spoken>}.
        # TODO(per-guild): aliases are global for now (like voice clones);
        # key by guild_id when per-guild pronunciations are needed.
        self.emoji_aliases: dict[str, dict[str, str]] = {}
        self.load()

    def _is_valid_voice(self, name: str) -> bool:
        if self.voice_registry is not None:
            return name in self.voice_registry
        return name in VOICE_PROFILES

    def load(self) -> None:
        if not self.path.exists():
            return

        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            log.exception("Failed to read bot config: %s", self.path)
            return

        guilds = data.get("guilds", {}) if isinstance(data, dict) else {}
        for guild_id_raw, raw_config in guilds.items():
            try:
                guild_id = int(guild_id_raw)
            except (TypeError, ValueError):
                continue
            if not isinstance(raw_config, dict):
                continue
            allowed_users = {
                int(user_id)
                for user_id in raw_config.get("allowed_users", [])
                if str(user_id).isdigit()
            }
            user_voices = {
                int(user_id): voice
                for user_id, voice in raw_config.get("user_voices", {}).items()
                if str(user_id).isdigit() and self._is_valid_voice(voice)
            }
            default_voice = raw_config.get("default_voice", DEFAULT_VOICE_PROFILE)
            if not self._is_valid_voice(default_voice):
                default_voice = DEFAULT_VOICE_PROFILE
            self.guilds[guild_id] = GuildConfig(
                enabled=bool(raw_config.get("enabled", True)),
                allowed_users=allowed_users,
                default_voice=default_voice,
                user_voices=user_voices,
            )

        raw_aliases = data.get("emoji_aliases", {}) if isinstance(data, dict) else {}
        emoji_aliases: dict[str, dict[str, str]] = {}
        if isinstance(raw_aliases, dict):
            for emoji_id, entry in raw_aliases.items():
                if not str(emoji_id).isdigit() or not isinstance(entry, dict):
                    continue
                say = entry.get("say")
                if not isinstance(say, str) or not say.strip():
                    continue
                name = entry.get("name")
                emoji_aliases[str(emoji_id)] = {
                    "name": name if isinstance(name, str) else "",
                    "say": say,
                }
        self.emoji_aliases = emoji_aliases

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "version": 1,
            "guilds": {
                str(guild_id): {
                    "enabled": config.enabled,
                    "allowed_users": sorted(config.allowed_users),
                    "default_voice": config.default_voice,
                    "user_voices": {
                        str(user_id): voice
                        for user_id, voice in sorted(config.user_voices.items())
                    },
                }
                for guild_id, config in sorted(self.guilds.items())
            },
            "emoji_aliases": {
                emoji_id: {"name": entry.get("name", ""), "say": entry.get("say", "")}
                for emoji_id, entry in sorted(self.emoji_aliases.items())
            },
        }
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=str(self.path.parent),
            delete=False,
        ) as tmp_file:
            json.dump(data, tmp_file, ensure_ascii=False, indent=2)
            tmp_file.write("\n")
            tmp_path = Path(tmp_file.name)
        tmp_path.replace(self.path)

    def get_guild(self, guild_id: int) -> GuildConfig:
        config = self.guilds.get(guild_id)
        if config is None:
            config = GuildConfig(allowed_users=set(self.fallback_users))
            self.guilds[guild_id] = config
        return config

    def is_enabled(self, guild_id: int) -> bool:
        return self.get_guild(guild_id).enabled

    def is_allowed(self, guild_id: int, user_id: int) -> bool:
        return user_id in self.get_guild(guild_id).allowed_users

    def add_user(self, guild_id: int, user_id: int) -> None:
        self.get_guild(guild_id).allowed_users.add(user_id)
        self.save()

    def remove_user(self, guild_id: int, user_id: int) -> None:
        config = self.get_guild(guild_id)
        config.allowed_users.discard(user_id)
        config.user_voices.pop(user_id, None)
        self.save()

    def set_enabled(self, guild_id: int, enabled: bool) -> None:
        self.get_guild(guild_id).enabled = enabled
        self.save()

    def set_default_voice(self, guild_id: int, voice_name: str) -> None:
        self.get_guild(guild_id).default_voice = voice_name
        self.save()

    def set_user_voice(self, guild_id: int, user_id: int, voice_name: str) -> None:
        self.get_guild(guild_id).user_voices[user_id] = voice_name
        self.save()

    def clear_user_voice(self, guild_id: int, user_id: int) -> None:
        self.get_guild(guild_id).user_voices.pop(user_id, None)
        self.save()

    def voice_for_user(self, guild_id: int, user_id: int) -> str:
        config = self.get_guild(guild_id)
        return config.user_voices.get(user_id, config.default_voice)

    def emoji_say_map(self) -> dict[str, str]:
        """id -> spoken word, for substitution during normalization."""
        return {emoji_id: entry["say"] for emoji_id, entry in self.emoji_aliases.items()}

    def set_emoji_alias(self, emoji_id: str, name: str, say: str) -> None:
        self.emoji_aliases[str(emoji_id)] = {"name": name, "say": say}
        self.save()

    def remove_emoji_alias(self, emoji_id: str) -> bool:
        existed = str(emoji_id) in self.emoji_aliases
        self.emoji_aliases.pop(str(emoji_id), None)
        if existed:
            self.save()
        return existed

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
intents.guilds = True


def load_opus() -> bool:
    if discord.opus.is_loaded():
        return True

    opus_path = ctypes.util.find_library("opus")
    if not opus_path:
        log.error("Opus library not found")
        return False

    discord.opus.load_opus(opus_path)
    log.info("Opus loaded: %s", opus_path)
    return discord.opus.is_loaded()


def process_text(text: str) -> str:
    text = re.sub(r"http[s]?://\S+", "", text)

    def replace_emoji(match: re.Match[str]) -> str:
        return EMOJI_MAP.get(match.group(1), "")

    text = re.sub(r"<a?:([a-zA-Z0-9_]+):[0-9]+>", replace_emoji, text)
    text = text.replace("\n", ". ")
    text = " ".join(text.split())
    return text.strip()


# Single source of truth for the cleanup applied just before handing
# text to either TTS provider. Returns None when there is nothing
# speakable left, so the caller can drop the message entirely
# (matches the spec: 186/3358 messages were empty after cleanup).
def normalize_for_tts(
    raw: str,
    *,
    max_chars: int | None = None,
    emoji_aliases: dict[str, str] | None = None,
) -> str | None:
    """Strip Discord markup from a message and prepare it for synthesis.

    Removes:
      - custom emoji markup ``<:name:id>`` and ``<a:name:id>``
      - user/role mentions ``<@id>``, ``<@!id>``, ``<@&id>``
      - channel mentions ``<#id>``
      - URLs ``https?://...``

    Also normalizes whitespace (``\n`` -> ``". "``, multiple spaces
    collapsed) and enforces an upper bound on the resulting length.
    Returns ``None`` if the cleaned string is empty (caller should
    drop the message — do not waste an API call or queue slot).
    """
    if not raw:
        return None
    text = raw

    # Emoji aliases first: turn aliased custom emoji into their spoken word
    # *before* the strip regex removes the rest. Unaliased emoji stay silent.
    if emoji_aliases:
        text = substitute_emoji_aliases(text, emoji_aliases)
    # Custom emoji: <:name:id> and <a:name:id>
    text = re.sub(r"<a?:\w+:\d+>", " ", text)
    # Standard Unicode emoji -> spoken Russian names
    text = speak_unicode_emoji(text)
    # User/role mentions
    text = re.sub(r"<@!?\d+>", " ", text)
    text = re.sub(r"<@&\d+>", " ", text)
    # Channel mentions
    text = re.sub(r"<#\d+>", " ", text)
    # URLs
    text = re.sub(r"https?://\S+", " ", text)

    # Newlines -> period for more natural speech rhythm
    text = text.replace("\n", ". ")
    # Collapse whitespace
    text = " ".join(text.split())
    text = text.strip()

    if not text:
        return None

    limit = max_chars if max_chars is not None else MAX_TEXT_LENGTH
    if limit and len(text) > limit:
        text = text[:limit].rstrip()
        if not text:
            return None
    return text


def _is_emoji_char(ch: str) -> bool:
    if not ch:
        return False
    if "\u2600" <= ch <= "\u27BF":
        return True
    if "\U0001F300" <= ch <= "\U0001FAFF":
        return True
    return unicodedata.category(ch) == "So"


def _is_keyboard_smash_word(word: str) -> bool:
    cleaned = re.sub(r"[^A-Za-zА-Яа-яЁё]", "", word)
    if len(cleaned) < 4:
        return False
    unique_chars = len(set(cleaned.lower()))
    return unique_chars <= 3 and len(cleaned) >= 5


def _strip_discord_tokens_for_speech(
    raw_text: str, aliases: dict[str, str] | None = None
) -> str:
    aliases = aliases or {}

    def _emoji_repl(m: re.Match[str]) -> str:
        # id-keyed alias wins over the legacy name-keyed EMOJI_MAP fallback;
        # an emoji with neither becomes silence, as before.
        say = aliases.get(m.group(2))
        if say:
            return f" {say} "
        return f" {EMOJI_MAP.get(m.group(1), '')} "

    text = CUSTOM_EMOJI_RE.sub(_emoji_repl, raw_text)
    text = speak_unicode_emoji(text)
    text = MENTION_RE.sub(" ", text)
    text = URL_RE.sub(" ", text)
    text = text.replace("\n", ". ")
    text = " ".join(text.split())
    return text.strip()


def analyze_message_for_merge(
    raw_text: str, aliases: dict[str, str] | None = None
) -> ParsedMessage:
    raw_text = raw_text or ""
    raw_length = len(raw_text)
    spoken_text = _strip_discord_tokens_for_speech(raw_text, aliases)
    custom_tokens = list(CUSTOM_EMOJI_RE.finditer(raw_text))
    mention_tokens = list(MENTION_RE.finditer(raw_text))
    url_tokens = list(URL_RE.finditer(raw_text))
    words = re.findall(r"[A-Za-zА-Яа-яЁё0-9]+", spoken_text)
    punctuationless = re.sub(r"\s+", "", re.sub(r"[.,!?;:()\[\]{}\"'`~\-_/\\|]", "", spoken_text))
    emoji_count = sum(1 for ch in punctuationless if _is_emoji_char(ch))
    animated_custom_emoji_count = sum(1 for m in custom_tokens if m.group(0).startswith("<a:"))
    custom_emoji_count = len(custom_tokens)
    mention_count = len(mention_tokens)
    url_count = len(url_tokens)
    has_letters = bool(re.search(r"[A-Za-zА-Яа-яЁё]", spoken_text))
    raw_no_ws = re.sub(r"\s+", "", raw_text)
    is_mention_only = bool(raw_no_ws) and bool(MENTION_RE.fullmatch(raw_no_ws))
    is_url_only = bool(raw_no_ws) and bool(URL_RE.fullmatch(raw_no_ws))
    is_custom_emoji_only = bool(raw_no_ws) and bool(CUSTOM_EMOJI_RE.fullmatch(raw_no_ws))
    is_animated_custom_emoji_only = is_custom_emoji_only and raw_no_ws.startswith("<a:")
    is_unicode_emoji_only = bool(raw_no_ws) and all(
        _is_emoji_char(ch) or unicodedata.category(ch) in {"Cf", "Sk"} for ch in raw_no_ws
    )
    is_single_digit = bool(re.fullmatch(r"\d", spoken_text))
    is_single_symbol = len(spoken_text) == 1 and not spoken_text.isalnum()
    is_caps_shout = has_letters and spoken_text.upper() == spoken_text and len(spoken_text) >= 4
    is_keyboard_smash = any(_is_keyboard_smash_word(w) for w in spoken_text.split())
    is_question_or_terminal = bool(re.search(r"[!?]|[.]\s*$", spoken_text))
    effective_length = min(emoji_count, 4) + custom_emoji_count
    for word in words:
        if word.isdigit():
            effective_length += min(len(word), 3)
        else:
            effective_length += min(len(word), 12)
    if is_url_only and TTS_SELECTIVE_HOLD_DROP_URL_ONLY:
        spoken_text = ""
        effective_length = 0
    if is_mention_only and TTS_SELECTIVE_HOLD_DROP_MENTION_ONLY:
        spoken_text = ""
        effective_length = 0
    return ParsedMessage(
        spoken_text=spoken_text,
        raw_length=raw_length,
        effective_length=effective_length,
        word_count=len(words),
        emoji_count=emoji_count,
        custom_emoji_count=custom_emoji_count,
        animated_custom_emoji_count=animated_custom_emoji_count,
        mention_count=mention_count,
        url_count=url_count,
        is_unicode_emoji_only=is_unicode_emoji_only,
        is_custom_emoji_only=is_custom_emoji_only,
        is_animated_custom_emoji_only=is_animated_custom_emoji_only,
        is_mention_only=is_mention_only,
        is_url_only=is_url_only,
        is_single_digit=is_single_digit,
        is_single_symbol=is_single_symbol,
        is_caps_shout=is_caps_shout,
        is_keyboard_smash=is_keyboard_smash,
        is_question_or_terminal=is_question_or_terminal,
    )


def is_reaction_like(parsed: ParsedMessage) -> bool:
    return (
        bool(parsed.spoken_text)
        and not parsed.is_special_only
        and not parsed.is_caps_shout
        and not parsed.is_keyboard_smash
        and not parsed.is_question_or_terminal
        and parsed.effective_length <= 5
        and parsed.word_count <= 1
    )


def seconds_from_ms(value_ms: int) -> str:
    return f"{max(value_ms, 0) / 1000:.3f}".rstrip("0").rstrip(".") or "0"


def build_preroll_lavfi_source(mode: str, duration: str) -> str:
    if mode == "sine":
        return f"sine=frequency=180:duration={duration}:sample_rate=48000"
    if mode == "silence":
        return f"anullsrc=r=48000:cl=stereo:d={duration}"
    return f"anoisesrc=d={duration}:c=pink:r=48000"


def build_playback_filter_complex(
    trim_silence: bool,
    preroll_volume_db: float,
) -> str:
    speech_filters = ["aformat=sample_rates=48000:channel_layouts=stereo"]
    if trim_silence:
        speech_filters.append(
            "silenceremove="
            "start_periods=1:start_silence=0.03:start_threshold=-50dB:"
            "stop_periods=-1:stop_duration=0.12:stop_threshold=-50dB"
        )

    return ";".join(
        [
            f"[0:a]{','.join(speech_filters)}[speech]",
            (
                "[1:a]"
                f"volume={preroll_volume_db:g}dB,"
                "aformat=sample_rates=48000:channel_layouts=stereo"
                "[primer]"
            ),
            "[2:a]aformat=sample_rates=48000:channel_layouts=stereo[tail]",
            "[primer][speech][tail]concat=n=3:v=0:a=1[out]",
        ]
    )


def build_playback_prepare_command(source: Path, prepared: Path) -> list[str]:
    preroll_seconds = seconds_from_ms(TTS_PREROLL_MS)
    tail_seconds = seconds_from_ms(TTS_SILENCE_TAIL_MS)
    filter_complex = build_playback_filter_complex(TTS_TRIM_SILENCE, TTS_PREROLL_VOLUME_DB)

    return [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-i",
        str(source),
        "-f",
        "lavfi",
        "-i",
        build_preroll_lavfi_source(TTS_PREROLL_MODE, preroll_seconds),
        "-f",
        "lavfi",
        "-i",
        f"anullsrc=r=48000:cl=stereo:d={tail_seconds}",
        "-filter_complex",
        filter_complex,
        "-map",
        "[out]",
        "-ar",
        "48000",
        "-ac",
        "2",
        "-c:a",
        "pcm_s16le",
        str(prepared),
    ]


def build_tts_pcm_command(source: Path) -> list[str]:
    audio_filters: list[str] = ["aformat=sample_rates=48000:channel_layouts=stereo"]
    if TTS_TRIM_SILENCE:
        audio_filters.append(
            "silenceremove="
            "start_periods=1:start_silence=0.03:start_threshold=-50dB:"
            "stop_periods=-1:stop_duration=0.12:stop_threshold=-50dB"
        )

    return [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-i",
        str(source),
        "-vn",
        "-af",
        ",".join(audio_filters),
        "-f",
        "s16le",
        "-ar",
        str(PCM_SAMPLE_RATE),
        "-ac",
        str(PCM_CHANNELS),
        "pipe:1",
    ]


def build_tts_stream_pcm_command() -> list[str]:
    """ffmpeg: decode an MP3 byte stream on stdin to s16le 48k stereo on stdout.

    Used by the streaming path: MiniMax MP3 chunks are written to stdin and
    decoded PCM is read from stdout incrementally. No silence trimming — that
    needs the whole clip, and the continuous player already handles idle.
    """
    return [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-f",
        "mp3",
        "-i",
        "pipe:0",
        "-vn",
        "-af",
        "aformat=sample_rates=48000:channel_layouts=stereo",
        "-f",
        "s16le",
        "-ar",
        str(PCM_SAMPLE_RATE),
        "-ac",
        str(PCM_CHANNELS),
        "pipe:1",
    ]


def split_pcm_frames(pcm_data: bytes, tail_ms: int = 0) -> list[bytes]:
    frames: list[bytes] = []
    for offset in range(0, len(pcm_data), PCM_FRAME_BYTES):
        frame = pcm_data[offset : offset + PCM_FRAME_BYTES]
        if len(frame) < PCM_FRAME_BYTES:
            frame = frame + (b"\x00" * (PCM_FRAME_BYTES - len(frame)))
        frames.append(frame)

    tail_frames = max(tail_ms, 0) // PCM_FRAME_MS
    frames.extend([b"\x00" * PCM_FRAME_BYTES for _ in range(tail_frames)])
    return frames


def build_idle_pcm_frame(mode: str, volume_db: float) -> bytes:
    if mode == "silence":
        return b"\x00" * PCM_FRAME_BYTES

    amplitude = max(1, min(32767, int(32767 * (10 ** (volume_db / 20)))))
    samples: list[int] = []
    seed = 0x1234ABCD
    for _ in range(PCM_FRAME_BYTES // PCM_SAMPLE_WIDTH):
        seed = (1103515245 * seed + 12345) & 0x7FFFFFFF
        samples.append((seed % (amplitude * 2 + 1)) - amplitude)
    return struct.pack("<" + "h" * len(samples), *samples)


class ContinuousTTSAudioSource(discord.AudioSource):
    def __init__(self, idle_frame: bytes) -> None:
        self.idle_frame = idle_frame
        self.frames: thread_queue.Queue[bytes] = thread_queue.Queue()
        self._stopped = threading.Event()
        self._drained = threading.Event()
        self._drained.set()
        self._lock = threading.Lock()
        self._pending_frames = 0

    def read(self) -> bytes:
        if self._stopped.is_set():
            return b""

        try:
            frame = self.frames.get_nowait()
        except thread_queue.Empty:
            return self.idle_frame

        with self._lock:
            self._pending_frames = max(0, self._pending_frames - 1)
            if self._pending_frames == 0:
                self._drained.set()
        return frame

    def is_opus(self) -> bool:
        return False

    def enqueue_frames(self, frames: list[bytes]) -> None:
        if not frames:
            return
        with self._lock:
            self._pending_frames += len(frames)
            self._drained.clear()
        for frame in frames:
            if len(frame) != PCM_FRAME_BYTES:
                raise ValueError(f"PCM frame must be {PCM_FRAME_BYTES} bytes, got {len(frame)}")
            self.frames.put(frame)

    async def wait_until_drained(self, timeout: float | None = None) -> bool:
        return await asyncio.to_thread(self._drained.wait, timeout)

    def stop(self) -> None:
        self._stopped.set()
        self._drained.set()

    def cleanup(self) -> None:
        self.stop()

    @property
    def stopped(self) -> bool:
        return self._stopped.is_set()

    @property
    def is_drained(self) -> bool:
        return self._drained.is_set()


class TTSBot(commands.Bot):
    def __init__(self) -> None:
        super().__init__(command_prefix=("!tts ", "!tts"), intents=intents)
        self.message_queue: asyncio.Queue[TTSJob] = asyncio.Queue(maxsize=QUEUE_MAXSIZE)
        self.worker_task: asyncio.Task[None] | None = None
        # Prefetch pipeline: generation worker fills ready_queue (bounded by
        # lookahead) with PreparedAudio; playback worker drains it in order.
        self.ready_queue: asyncio.Queue[PreparedAudio] = asyncio.Queue(
            maxsize=TTS_PREFETCH_LOOKAHEAD
        )
        self.generation_task: asyncio.Task[None] | None = None
        self.playback_task: asyncio.Task[None] | None = None
        # In-flight + buffered prepared items, so queue-clear can cancel them.
        self.active_prepared: set[PreparedAudio] = set()
        self.idle_disconnect_tasks: dict[int, asyncio.Task[None]] = {}
        self.continuous_idle_stop_tasks: dict[int, asyncio.Task[None]] = {}
        self.voice_connect_locks: dict[int, asyncio.Lock] = {}
        self.voice_connect_cooldown_until: dict[int, float] = {}
        self.suppress_auto_connect_until: dict[int, float] = {}
        # Unified voice catalog (Piper + MiniMax). Seeded on first start
        # from the hardcoded VOICE_PROFILES + MINIMAX_VOICE_ID; thereafter
        # loaded from data/voices.json. Selection state stays separate in
        # BotConfigStore.
        _mm_seed = load_minimax_config_from_env()
        self.voice_registry = voice_registry.load_or_seed(
            VOICES_REGISTRY_PATH,
            VOICE_PROFILES,
            fallback_profile=DEFAULT_VOICE_PROFILE,
            minimax_voice_id=_mm_seed.voice_id,
            minimax_model=_mm_seed.model,
            minimax_language_boost=_mm_seed.language_boost,
        )
        self.config_store = BotConfigStore(
            BOT_CONFIG_PATH, WHITELIST_USERS, voice_registry=self.voice_registry
        )
        self.piper_voices: dict[tuple[str, str], object] = {}
        # TTS provider abstraction (see tts_providers.py). Skeleton
        # behavior in this commit: dispatcher always routes to local.
        # Cloud provider (MiniMax) and full CB logic land in commits 3+
        # and 6 respectively.
        cache_cfg = load_cache_config_from_env()
        self.tts_cache = TTSPhraseCache(cache_cfg) if cache_cfg.enabled else None
        # Wrap generate_piper_file so the dispatcher's piper callback
        # signature (str | None profile name) matches what
        # generate_piper_file expects (VoiceProfile object).
        async def _piper_synthesize(text: str, filename: Path, voice_profile: str | None) -> None:
            await self.generate_piper_file(
                text, filename, self._resolve_piper_profile(voice_profile)
            )
        self.tts_dispatcher = TTSDispatcher(
            local=LocalProvider(_piper_synthesize),
            cloud=self._build_cloud_provider(),
            config=load_dispatcher_config_from_env(),
            circuit_breaker=load_circuit_breaker_from_env(),
            cache=self.tts_cache,
            fallback_profile=self.voice_registry.fallback_profile,
        )
        self.continuous_sources: dict[int, ContinuousTTSAudioSource] = {}
        self.merge_buffers: dict[tuple[int, int], MergeBufferState] = {}
        self.merge_locks: dict[tuple[int, int], asyncio.Lock] = {}
        self.merge_generations: dict[tuple[int, int], int] = {}
        self.last_user_message_ts: dict[tuple[int, int], float] = {}

    async def setup_hook(self) -> None:
        self.tree.add_command(tts_group)
        try:
            synced = await self.tree.sync()
            log.info("Slash commands synced count=%s group=/%s", len(synced), tts_group.name)
        except Exception:
            log.exception("Failed to sync slash commands")
        if TTS_PREFETCH_ENABLED:
            self.generation_task = asyncio.create_task(
                self._generation_worker(), name="tts-generation")
            self.playback_task = asyncio.create_task(
                self._playback_worker(), name="tts-playback")
        else:
            self.worker_task = asyncio.create_task(self.tts_worker(), name="tts-worker")

    async def close(self) -> None:
        for task in (self.worker_task, self.generation_task, self.playback_task):
            if task:
                task.cancel()

        for task in self.idle_disconnect_tasks.values():
            task.cancel()
        for task in self.continuous_idle_stop_tasks.values():
            task.cancel()
        for state in self.merge_buffers.values():
            if state.timer_task and not state.timer_task.done():
                state.timer_task.cancel()
        for source in self.continuous_sources.values():
            source.stop()

        # Gracefully close the cloud provider's HTTP keep-alive pool.
        # Local Piper has no async resources to release.
        cloud = getattr(self.tts_dispatcher, "cloud", None)
        if cloud is not None and hasattr(cloud, "aclose"):
            try:
                await cloud.aclose()
            except Exception:
                log.exception("Failed to close cloud TTS provider cleanly")

        await super().close()

    def cancel_idle_disconnect(self, guild_id: int) -> None:
        task = self.idle_disconnect_tasks.pop(guild_id, None)
        if task and not task.done():
            task.cancel()
            log.info("Cancelled idle disconnect guild=%s", guild_id)

    def cancel_continuous_idle_stop(self, guild_id: int) -> None:
        task = self.continuous_idle_stop_tasks.pop(guild_id, None)
        if task and not task.done():
            task.cancel()
            log.info("Cancelled continuous stream idle stop guild=%s", guild_id)

    def get_voice_connect_lock(self, guild_id: int) -> asyncio.Lock:
        lock = self.voice_connect_locks.get(guild_id)
        if not lock:
            lock = asyncio.Lock()
            self.voice_connect_locks[guild_id] = lock
        return lock

    def set_voice_connect_cooldown(self, guild_id: int, reason: str) -> None:
        until = time.monotonic() + VOICE_CONNECT_COOLDOWN_SECONDS
        self.voice_connect_cooldown_until[guild_id] = until
        log.warning(
            "Voice connect cooldown set guild=%s seconds=%s reason=%s",
            guild_id,
            VOICE_CONNECT_COOLDOWN_SECONDS,
            reason,
        )

    def voice_connect_cooldown_remaining(self, guild_id: int) -> float:
        until = self.voice_connect_cooldown_until.get(guild_id)
        if until is None:
            return 0.0

        remaining = until - time.monotonic()
        if remaining <= 0:
            self.voice_connect_cooldown_until.pop(guild_id, None)
            return 0.0
        return remaining

    def suppress_auto_connect(self, guild_id: int, reason: str) -> None:
        if AUTO_CONNECT_SUPPRESS_SECONDS <= 0:
            return
        until = time.monotonic() + AUTO_CONNECT_SUPPRESS_SECONDS
        self.suppress_auto_connect_until[guild_id] = until
        log.info(
            "Auto-connect suppressed guild=%s seconds=%s reason=%s",
            guild_id,
            AUTO_CONNECT_SUPPRESS_SECONDS,
            reason,
        )

    def suppress_auto_connect_remaining(self, guild_id: int) -> float:
        until = self.suppress_auto_connect_until.get(guild_id)
        if until is None:
            return 0.0

        remaining = until - time.monotonic()
        if remaining <= 0:
            self.suppress_auto_connect_until.pop(guild_id, None)
            return 0.0
        return remaining

    def schedule_idle_disconnect(self, guild: discord.Guild) -> None:
        self.cancel_idle_disconnect(guild.id)
        task = asyncio.create_task(
            self._idle_disconnect_after_timeout(guild),
            name=f"idle-disconnect-{guild.id}",
        )
        self.idle_disconnect_tasks[guild.id] = task
        log.info("Scheduled idle disconnect guild=%s timeout=%ss", guild.id, IDLE_DISCONNECT_SECONDS)

    def has_active_voice_playback(self, guild_id: int, vc: discord.VoiceClient) -> bool:
        source = self.continuous_sources.get(guild_id)
        if source and not source.stopped:
            return not source.is_drained
        return vc.is_playing() or vc.is_paused()

    def _whitelisted_user_in_channel(
        self, guild: discord.Guild, channel: discord.VoiceChannel | None
    ) -> bool:
        if channel is None:
            return False
        return any(
            (not user.bot) and self.config_store.is_allowed(guild.id, user.id)
            for user in channel.members
        )

    async def _idle_disconnect_after_timeout(self, guild: discord.Guild) -> None:
        try:
            poll_interval = 5.0
            elapsed = 0.0
            while elapsed < IDLE_DISCONNECT_SECONDS:
                remaining = IDLE_DISCONNECT_SECONDS - elapsed
                await asyncio.sleep(min(poll_interval, remaining))
                elapsed += poll_interval

                vc = discord.utils.get(self.voice_clients, guild=guild)
                if not vc or not vc.is_connected():
                    return

                channel = vc.channel if isinstance(vc.channel, discord.VoiceChannel) else None
                if self._whitelisted_user_in_channel(guild, channel):
                    log.info(
                        "Cancel idle disconnect guild=%s reason=whitelisted_user_present",
                        guild.id,
                    )
                    return

                if self.has_active_voice_playback(guild.id, vc):
                    log.info("Skip idle disconnect guild=%s reason=playback_active", guild.id)
                    return

            vc = discord.utils.get(self.voice_clients, guild=guild)
            if not vc or not vc.is_connected():
                return

            if self.has_active_voice_playback(guild.id, vc):
                log.info("Skip idle disconnect guild=%s reason=playback_active", guild.id)
                return

            channel = vc.channel if isinstance(vc.channel, discord.VoiceChannel) else None
            if self._whitelisted_user_in_channel(guild, channel):
                log.info(
                    "Cancel idle disconnect guild=%s reason=whitelisted_user_present",
                    guild.id,
                )
                return

            source = self.continuous_sources.pop(guild.id, None)
            if source:
                source.stop()
                if vc.is_playing() or vc.is_paused():
                    vc.stop()

            await vc.disconnect(force=True)
            self.suppress_auto_connect(guild.id, "idle_disconnect")
            log.info("Idle disconnect executed guild=%s", guild.id)
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("Idle disconnect task failed guild=%s", guild.id)

    def schedule_continuous_idle_stop(self, guild: discord.Guild) -> None:
        if not TTS_CONTINUOUS_STREAM or TTS_MAX_CONTINUOUS_IDLE_SECONDS <= 0:
            return

        self.cancel_continuous_idle_stop(guild.id)
        task = asyncio.create_task(
            self._continuous_idle_stop_after_timeout(guild),
            name=f"continuous-idle-stop-{guild.id}",
        )
        self.continuous_idle_stop_tasks[guild.id] = task
        log.info(
            "Scheduled continuous stream idle stop guild=%s timeout=%ss",
            guild.id,
            TTS_MAX_CONTINUOUS_IDLE_SECONDS,
        )

    async def _continuous_idle_stop_after_timeout(self, guild: discord.Guild) -> None:
        try:
            await asyncio.sleep(TTS_MAX_CONTINUOUS_IDLE_SECONDS)

            source = self.continuous_sources.get(guild.id)
            if not source or source.stopped:
                return
            if not source.is_drained:
                log.info("Skip continuous stream idle stop guild=%s reason=speech_pending", guild.id)
                return

            self.continuous_sources.pop(guild.id, None)
            source.stop()

            vc = discord.utils.get(self.voice_clients, guild=guild)
            if vc and vc.is_connected() and (vc.is_playing() or vc.is_paused()):
                vc.stop()

            log.info("Continuous stream idle stop executed guild=%s", guild.id)
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("Continuous stream idle stop task failed guild=%s", guild.id)

    async def ensure_voice(self, voice_channel: discord.VoiceChannel) -> discord.VoiceClient:
        started = time.perf_counter()
        guild_id = voice_channel.guild.id
        self.cancel_idle_disconnect(guild_id)
        lock = self.get_voice_connect_lock(guild_id)

        async with lock:
            remaining = self.voice_connect_cooldown_remaining(guild_id)
            if remaining > 0:
                raise RuntimeError(f"Voice connect cooldown active for {remaining:.1f}s")

            vc = discord.utils.get(self.voice_clients, guild=voice_channel.guild)

            if not vc or not vc.is_connected():
                log.info(
                    "Connecting to voice channel guild=%s channel=%s",
                    guild_id,
                    voice_channel.id,
                )
                try:
                    vc = await voice_channel.connect(timeout=60.0, self_deaf=True)
                except discord.errors.ClientException as exc:
                    if "Already connected" in str(exc):
                        # State desync: discord.py internal state has an active voice client
                        # that isn't reflected in self.voice_clients yet. Recover it instead
                        # of setting a cooldown and looping forever.
                        existing_vc = voice_channel.guild.voice_client
                        if existing_vc and existing_vc.is_connected():
                            log.warning(
                                "Voice state desync recovered guild=%s channel=%s",
                                guild_id,
                                voice_channel.id,
                            )
                            if existing_vc.channel != voice_channel:
                                await existing_vc.move_to(voice_channel)
                            return existing_vc
                    self.set_voice_connect_cooldown(guild_id, type(exc).__name__)
                    raise
                except Exception as exc:
                    self.set_voice_connect_cooldown(guild_id, type(exc).__name__)
                    raise

                log.info(
                    "Voice connect done guild=%s channel=%s took=%.3fs",
                    guild_id,
                    voice_channel.id,
                    time.perf_counter() - started,
                )
            elif vc.channel != voice_channel:
                log.info(
                    "Moving voice client guild=%s from=%s to=%s",
                    guild_id,
                    getattr(vc.channel, "id", "unknown"),
                    voice_channel.id,
                )
                try:
                    await vc.move_to(voice_channel)
                except Exception as exc:
                    self.set_voice_connect_cooldown(guild_id, f"move:{type(exc).__name__}")
                    raise

                log.info(
                    "Voice move done guild=%s channel=%s took=%.3fs",
                    guild_id,
                    voice_channel.id,
                    time.perf_counter() - started,
                )
            else:
                log.info(
                    "Voice ready guild=%s channel=%s took=%.3fs",
                    guild_id,
                    voice_channel.id,
                    time.perf_counter() - started,
                )

        return vc

    async def warmup_tts(self) -> None:
        filename = TMP_DIR / f"warmup_{uuid.uuid4().hex}.wav"
        try:
            # Warm Piper ONNX directly, bypassing the dispatcher. The
            # whole point of warmup is to preload the local model so the
            # first real request (including a fallback to local) is not
            # cold. If TTS_PRIMARY_PROVIDER=minimax, warming the cloud
            # provider is pointless (it is HTTP) and would burn an API
            # call on every restart.
            await self.tts_dispatcher.warm_local("Привет", filename)
            log.info("TTS warmup completed")
        except Exception:
            log.exception("TTS warmup failed")
        finally:
            if filename.exists():
                try:
                    filename.unlink()
                except OSError:
                    log.exception("Failed to remove warmup file: %s", filename)

    def _build_cloud_provider(self):
        """Construct the MiniMax provider if the bot has credentials.

        Returns ``None`` when MINIMAX_API_KEY or MINIMAX_VOICE_ID is
        missing — the dispatcher then silently uses the local provider
        regardless of TTS_PRIMARY_PROVIDER, and the bot starts cleanly.
        """
        cfg = load_minimax_config_from_env()
        # Only the API key is required now: the per-message voice_id comes
        # from the registry record, so the cloud provider is usable even
        # when MINIMAX_VOICE_ID is empty (as long as a minimax voice is in
        # the catalog). MINIMAX_VOICE_ID remains the first-start seed.
        if not cfg.api_key:
            log.info("MiniMax provider disabled (api_key MISSING); fall back to local")
            return None
        log.info(
            "MiniMax provider enabled model=%s default_voice_id=%s base_url=%s timeout=%.1fs",
            cfg.model, cfg.voice_id or "(per-record)", cfg.base_url, cfg.timeout_seconds,
        )
        return MiniMaxProvider(cfg)

    def _resolve_piper_profile(self, voice_profile: str | None) -> VoiceProfile:
        """Resolve a voice name to a Piper ``VoiceProfile`` via the registry.

        Falls back to the registry ``fallback_profile`` then ``VOICE_PROFILES``
        so a missing or non-Piper name still yields a usable Piper voice.
        """
        name = (
            voice_profile
            or self.voice_registry.fallback_profile
            or DEFAULT_VOICE_PROFILE
        )
        rec = self.voice_registry.get(name)
        if rec is not None and rec.is_piper and rec.piper is not None:
            return VoiceProfile(
                name=rec.name,
                label=rec.label,
                piper_model_path=rec.piper.model_path,
                piper_config_path=rec.piper.config_path,
                piper_speaker=rec.piper.speaker,
                piper_length_scale=rec.piper.length_scale,
            )
        return VOICE_PROFILES.get(name, VOICE_PROFILES[DEFAULT_VOICE_PROFILE])

    def persist_voice_registry(self) -> None:
        """Atomically write the current catalog to data/voices.json."""
        voice_registry.save_registry(VOICES_REGISTRY_PATH, self.voice_registry)

    async def validate_minimax_voice(self, voice_id: str) -> tuple[bool, str]:
        """Probe a MiniMax voice_id with a short phrase.

        Returns (ok, error_message). ok=True means status_code 0; a 2054
        (voice id not exist) yields a clear rejection. Used by voice-add
        before persisting a new voice.
        """
        cloud = getattr(self.tts_dispatcher, "cloud", None)
        if cloud is None:
            return False, "MiniMax не настроен (нет MINIMAX_API_KEY)."
        tmp = TMP_DIR / f"voiceadd_{uuid.uuid4().hex}.mp3"
        try:
            await cloud.synthesize("проверка голоса", tmp, voice_id=voice_id)
            return True, ""
        except MiniMaxVoiceNotFoundError:
            return False, f"voice_id `{voice_id}` не существует (MiniMax 2054)."
        except MiniMaxError as exc:
            return False, f"Ошибка MiniMax: {exc}"
        except Exception as exc:  # network/timeout/etc
            return False, f"Не удалось проверить голос: {type(exc).__name__}: {exc}"
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    async def enqueue_tts(
        self,
        text: str,
        voice_channel: discord.VoiceChannel,
        author_id: int,
        text_channel_id: int,
        message_ts: float | None = None,
    ) -> bool:
        # Normalize once at the boundary so every caller (merge buffer,
        # /voicebot test, future commands) gets identical cleanup. The
        # TTS_MAX_CHARS cap is enforced here (300 by default, per spec
        # §"Препроцессинг текста") — it protects the cloud API quota
        # from accidental walls of text and keeps Piper CPU bounded.
        cleaned = normalize_for_tts(
            text,
            max_chars=TTS_MAX_CHARS,
            emoji_aliases=self.config_store.emoji_say_map(),
        )
        if not cleaned:
            log.info(
                "Skipped TTS enqueue author=%s reason=empty_after_normalize",
                author_id,
            )
            return False
        try:
            now = time.perf_counter()
            voice_profile = self.config_store.voice_for_user(voice_channel.guild.id, author_id)
            job = TTSJob(
                text=cleaned,
                voice_channel=voice_channel,
                queued_at=now,
                author_id=author_id,
                guild_id=voice_channel.guild.id,
                text_channel_id=text_channel_id,
                voice_profile=voice_profile,
                message_ts=message_ts or now,
            )
            await asyncio.wait_for(
                self.message_queue.put(job),
                timeout=max(TTS_QUEUE_PUT_TIMEOUT_MS, 1) / 1000.0,
            )
            log.info(
                "Queued TTS guild=%s text_channel=%s voice_channel=%s author=%s queue=%s chars=%s voice=%s",
                voice_channel.guild.id,
                text_channel_id,
                voice_channel.id,
                author_id,
                self.message_queue.qsize(),
                len(text),
                voice_profile,
            )
            return True
        except (asyncio.QueueFull, TimeoutError):
            log.warning("TTS enqueue failed author=%s enqueue_fail_reason=queue_timeout", author_id)
            return False

    def _merge_lock(self, key: tuple[int, int]) -> asyncio.Lock:
        lock = self.merge_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self.merge_locks[key] = lock
        return lock

    def _next_merge_generation(self, key: tuple[int, int]) -> int:
        generation = self.merge_generations.get(key, 0) + 1
        self.merge_generations[key] = generation
        return generation

    def _decision_log(self, decision: str, parsed: ParsedMessage, **extra: object) -> None:
        if not TTS_SELECTIVE_HOLD_LOG_DECISIONS:
            return
        payload = {
            "decision_policy": TTS_MERGE_ALGORITHM,
            "decision": decision,
            "raw_length": parsed.raw_length,
            "effective_length": parsed.effective_length,
            "word_count": parsed.word_count,
            "emoji_count": parsed.emoji_count,
            "custom_emoji_count": parsed.custom_emoji_count,
            "animated_custom_emoji_count": parsed.animated_custom_emoji_count,
            "mention_count": parsed.mention_count,
            "url_count": parsed.url_count,
        }
        payload.update(extra)
        log.info("Selective hold decision %s", payload)

    async def _enqueue_buffer_state(self, state: MergeBufferState, reason: str) -> bool:
        if not state.items:
            return True
        text = state.join_separator.join(item.spoken_text for item in state.items if item.spoken_text).strip()
        if not text:
            return True
        try:
            ok = await self.enqueue_tts(
                text,
                state.voice_channel,
                state.author_id,
                state.text_channel_id,
                message_ts=state.first_ts,
            )
            if ok:
                log.info(
                    "Merged buffer flushed key=%s parts=%s reason=%s buffer_age_ms=%s effective_length=%s",
                    state.key,
                    len(state.items),
                    reason,
                    round((time.perf_counter() - state.first_ts) * 1000),
                    state.effective_len_total,
                )
            return ok
        except Exception:
            log.exception("Failed to enqueue merged state key=%s reason=%s", state.key, reason)
            return False

    async def _flush_buffer_locked(self, key: tuple[int, int], reason: str, expected_generation: int | None = None) -> bool:
        state = self.merge_buffers.get(key)
        if not state:
            return True
        if expected_generation is not None and state.generation_id != expected_generation:
            log.info(
                "Selective hold stale_timer_ignored key=%s expected_generation=%s current_generation=%s",
                key,
                expected_generation,
                state.generation_id,
            )
            return False
        if state.timer_task and not state.timer_task.done():
            state.timer_task.cancel()
        ok = await self._enqueue_buffer_state(state, reason)
        if ok:
            self.merge_buffers.pop(key, None)
        return ok

    async def _flush_merge_after_delay(self, key: tuple[int, int], generation_id: int, deadline_ts: float) -> None:
        sleep_for = max(0.0, deadline_ts - time.perf_counter())
        try:
            await asyncio.sleep(sleep_for)
            async with self._merge_lock(key):
                await self._flush_buffer_locked(key, "timer_flush", expected_generation=generation_id)
        except asyncio.CancelledError:
            return
        except asyncio.QueueFull:
            return
        except Exception:
            log.exception("Failed to flush merged messages key=%s", key)

    async def queue_or_merge_message(
        self,
        text: str,
        voice_channel: discord.VoiceChannel,
        author_id: int,
        text_channel_id: int,
    ) -> None:
        key = (author_id, voice_channel.id)
        now = time.perf_counter()
        parsed = analyze_message_for_merge(text, self.config_store.emoji_say_map())
        previous_ts = self.last_user_message_ts.get(key)
        gap_prev_ms = None if previous_ts is None else round((now - previous_ts) * 1000)
        self.last_user_message_ts[key] = now

        if TTS_MERGE_ALGORITHM == "off":
            if parsed.spoken_text:
                await self.enqueue_tts(parsed.spoken_text, voice_channel, author_id, text_channel_id, message_ts=now)
            return

        selective_enabled_for_author = (
            TTS_MERGE_ALGORITHM == "selective_hold_v2"
            and TTS_SELECTIVE_HOLD_ENABLED
        )
        if not selective_enabled_for_author:
            if not parsed.spoken_text:
                return
            if not TTS_MERGE_SHORT_MESSAGES or len(parsed.spoken_text) > TTS_MERGE_MAX_CHARS:
                await self.enqueue_tts(parsed.spoken_text, voice_channel, author_id, text_channel_id, message_ts=now)
                return
            async with self._merge_lock(key):
                state = self.merge_buffers.get(key)
                if state is None:
                    state = MergeBufferState(
                        key=key,
                        voice_channel=voice_channel,
                        author_id=author_id,
                        text_channel_id=text_channel_id,
                        first_ts=now,
                        last_ts=now,
                        deadline_ts=now + max(TTS_MERGE_WINDOW_MS, 0) / 1000.0,
                        generation_id=self._next_merge_generation(key),
                        join_separator=". ",
                        items=[],
                    )
                    self.merge_buffers[key] = state
                state.items.append(parsed)
                if state.timer_task and not state.timer_task.done():
                    state.timer_task.cancel()
                state.generation_id += 1
                state.timer_task = asyncio.create_task(
                    self._flush_merge_after_delay(key, state.generation_id, time.perf_counter() + max(TTS_MERGE_WINDOW_MS, 0) / 1000.0)
                )
            return

        async with self._merge_lock(key):
            state = self.merge_buffers.get(key)
            if state is None:
                if not parsed.spoken_text:
                    self._decision_log("drop_empty", parsed)
                    return
                if parsed.effective_length >= max(TTS_MERGE_MAX_CHARS, 40):
                    self._decision_log("immediate_long", parsed)
                    await self.enqueue_tts(parsed.spoken_text, voice_channel, author_id, text_channel_id, message_ts=now)
                    return
                if parsed.is_special_only or parsed.is_caps_shout or parsed.is_keyboard_smash or parsed.is_question_or_terminal:
                    self._decision_log("immediate_special", parsed)
                    if parsed.spoken_text:
                        await self.enqueue_tts(parsed.spoken_text, voice_channel, author_id, text_channel_id, message_ts=now)
                    return
                if (
                    is_reaction_like(parsed)
                    and (
                        previous_ts is None
                        or gap_prev_ms is not None
                        and gap_prev_ms > TTS_SELECTIVE_HOLD_REACTION_PAUSE_MS
                    )
                ):
                    self._decision_log(
                        "immediate_isolated_reaction",
                        parsed,
                        gap_prev_ms=gap_prev_ms,
                    )
                    await self.enqueue_tts(parsed.spoken_text, voice_channel, author_id, text_channel_id, message_ts=now)
                    return
                strong = (
                    parsed.effective_length >= TTS_SELECTIVE_HOLD_START_EFFECTIVE_LEN
                    or (
                        parsed.effective_length >= TTS_SELECTIVE_HOLD_START_MIN_EFFECTIVE_LEN_ALT
                        and parsed.word_count >= TTS_SELECTIVE_HOLD_START_MIN_WORDS_ALT
                    )
                    or (parsed.is_single_digit and parsed.effective_length >= 4)
                )
                if not strong:
                    self._decision_log("immediate_default", parsed)
                    await self.enqueue_tts(parsed.spoken_text, voice_channel, author_id, text_channel_id, message_ts=now)
                    return
                generation = self._next_merge_generation(key)
                deadline = now + max(TTS_SELECTIVE_HOLD_HARD_CAP_MS, 1) / 1000.0
                state = MergeBufferState(
                    key=key,
                    voice_channel=voice_channel,
                    author_id=author_id,
                    text_channel_id=text_channel_id,
                    first_ts=now,
                    last_ts=now,
                    deadline_ts=deadline,
                    generation_id=generation,
                    join_separator=TTS_SELECTIVE_HOLD_JOIN_SEPARATOR,
                    items=[parsed],
                    has_substantive_starter=True,
                )
                state.timer_task = asyncio.create_task(self._flush_merge_after_delay(key, generation, deadline))
                self.merge_buffers[key] = state
                self._decision_log(
                    "hold_start",
                    parsed,
                    chosen_timeout_ms=TTS_SELECTIVE_HOLD_HARD_CAP_MS,
                    messages_in_buffer=1,
                    buffer_age_ms=0,
                )
                return

            if state.voice_channel.id != voice_channel.id or state.text_channel_id != text_channel_id:
                await self._flush_buffer_locked(key, "voice_context_changed")
                if parsed.spoken_text:
                    await self.enqueue_tts(parsed.spoken_text, voice_channel, author_id, text_channel_id, message_ts=now)
                return

            hard_break = (
                parsed.is_special_only
                or parsed.is_question_or_terminal
                or parsed.is_caps_shout
                or parsed.is_keyboard_smash
                or now > state.deadline_ts
            )
            if hard_break:
                self._decision_log(
                    "hard_break",
                    parsed,
                    messages_in_buffer=len(state.items),
                    buffer_effective_length=state.effective_len_total,
                    buffer_age_ms=round((now - state.first_ts) * 1000),
                )
                ok = await self._flush_buffer_locked(key, "flush_before_hard_break")
                if ok and parsed.spoken_text:
                    await self.enqueue_tts(parsed.spoken_text, voice_channel, author_id, text_channel_id, message_ts=now)
                return

            prospective_parts = len(state.items) + 1
            prospective_effective = state.effective_len_total + parsed.effective_length
            if (
                prospective_parts > max(TTS_SELECTIVE_HOLD_MAX_PARTS, 1)
                or prospective_effective > max(TTS_SELECTIVE_HOLD_MAX_GROUP_EFFECTIVE_LEN, 1)
            ):
                ok = await self._flush_buffer_locked(key, "flush_before_reclassify")
                if ok:
                    await self.enqueue_tts(parsed.spoken_text, voice_channel, author_id, text_channel_id, message_ts=now)
                return

            state.items.append(parsed)
            state.last_ts = now
            self._decision_log(
                "append_soft",
                parsed,
                messages_in_buffer=len(state.items),
                buffer_effective_length=state.effective_len_total,
                buffer_age_ms=round((now - state.first_ts) * 1000),
            )

    def clear_merge_buffers(self, guild_id: int) -> None:
        stale_keys = [key for key, state in self.merge_buffers.items() if state.voice_channel.guild.id == guild_id]
        for key in stale_keys:
            state = self.merge_buffers.pop(key, None)
            if state and state.timer_task and not state.timer_task.done():
                state.timer_task.cancel()

    def clear_queue_for_guild(self, guild_id: int) -> int:
        kept: list[TTSJob] = []
        removed = 0
        while True:
            try:
                job = self.message_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if job.guild_id == guild_id:
                removed += 1
                self.message_queue.task_done()
            else:
                kept.append(job)
                self.message_queue.task_done()

        for job in kept:
            self.message_queue.put_nowait(job)

        # Cancel prefetched/in-flight prepared audio for this guild so it is
        # neither played nor finishes burning quota on generation.
        for prepared in self.active_prepared:
            if prepared.job.guild_id == guild_id and not prepared.cancelled:
                prepared.cancelled = True
                removed += 1
        return removed

    async def generate_piper_file(self, text: str, filename: Path, profile: VoiceProfile) -> None:
        if PiperVoice is None:
            raise RuntimeError("piper-tts is not installed")
        model_value = profile.piper_model_path or PIPER_MODEL_PATH
        config_value = profile.piper_config_path or PIPER_CONFIG_PATH
        if not model_value:
            raise RuntimeError("PIPER_MODEL_PATH is not set")
        model_path = Path(model_value)
        if not model_path.exists():
            raise RuntimeError(f"Piper model not found: {model_path}")
        config_path = Path(config_value) if config_value else None
        if config_path and not config_path.exists():
            raise RuntimeError(f"Piper config not found: {config_path}")

        voice_key = (str(model_path), str(config_path) if config_path else "")
        piper_voice = self.piper_voices.get(voice_key)
        if piper_voice is None:
            piper_voice = await asyncio.to_thread(
                PiperVoice.load,
                str(model_path),
                str(config_path) if config_path else None,
            )
            self.piper_voices[voice_key] = piper_voice

        started = time.perf_counter()
        log.info("Generating Piper TTS chars=%s model=%s profile=%s", len(text), model_path, profile.name)

        syn_config = None
        if SynthesisConfig is not None and (
            profile.piper_speaker >= 0 or abs(profile.piper_length_scale - 1.0) > 1e-6
        ):
            syn_config = SynthesisConfig(
                speaker_id=profile.piper_speaker if profile.piper_speaker >= 0 else None,
                length_scale=profile.piper_length_scale,
            )

        def _synthesize() -> None:
            with wave.open(str(filename), "wb") as wav_file:
                piper_voice.synthesize_wav(text, wav_file, syn_config=syn_config)

        await asyncio.to_thread(_synthesize)
        if not filename.exists() or filename.stat().st_size == 0:
            raise RuntimeError("Piper did not produce audio output")

        log.info(
            "Piper generated file=%s size=%s took=%.3fs",
            filename,
            filename.stat().st_size,
            time.perf_counter() - started,
        )

    async def generate_tts_file(self, text: str, filename: Path, voice_profile: str | None = None) -> str:
        # Resolve the stored voice name to a registry record; the dispatcher
        # routes by record.provider (piper -> local, minimax -> cloud) and
        # falls back to the registry fallback_profile on cloud failure.
        record = self.voice_registry.get(voice_profile)
        if record is None:
            record = self.voice_registry.fallback_record()
        provider_used = await self.tts_dispatcher.synthesize(text, filename, voice=record)
        log.info(
            "TTS engine used: %s voice=%s",
            provider_used,
            record.name if record is not None else (voice_profile or "default"),
        )
        return provider_used

    def _should_attempt_stream(self, voice) -> bool:
        """True when a job qualifies for the streaming fast path."""
        return (
            TTS_STREAMING_ENABLED
            and TTS_CONTINUOUS_STREAM
            and voice is not None
            and getattr(voice, "is_minimax", False)
            and self.tts_dispatcher.cloud is not None
        )

    async def _run_streaming_job(self, job: TTSJob, voice, worker_started: float) -> str:
        """Drive a streaming MiniMax job. Returns "done" or "fallback".

        "done": the job is fully handled (streamed ok, truncated mid-play, or
        a connection error that has nothing to fall back to). "fallback": the
        caller should run the Piper file path (pre-audio failure or open
        circuit breaker).
        """
        cb = self.tts_dispatcher.circuit_breaker
        # Connect first — enqueueing frames needs the voice client. Usually
        # instant because the continuous stream keeps the session open.
        try:
            vc = await self.ensure_voice(job.voice_channel)
        except Exception:
            log.exception("Voice prepare failed (streaming)")
            existing = discord.utils.get(self.voice_clients, guild=job.voice_channel.guild)
            if existing and existing.is_connected():
                await self.disconnect_guild_voice(job.voice_channel.guild)
            return "done"

        # Cache hit: play the stored audio from disk, no API call, no breaker
        # probe consumed. (Cache key already includes the voice.)
        cache = self.tts_dispatcher.cache
        if cache is not None:
            cached = cache.lookup(job.text, voice.name)
            if cached is not None:
                source = self.ensure_continuous_player(vc)
                try:
                    frames = await self.prepare_tts_pcm_frames(cached)
                except Exception:
                    log.exception("Cached audio decode failed; streaming instead")
                    frames = []
                if frames:
                    source.enqueue_frames(frames)
                    log.info(
                        "Stream cache HIT guild=%s channel=%s frames=%d "
                        "message_to_audio_s=%.3f (no API)",
                        job.voice_channel.guild.id, job.voice_channel.id, len(frames),
                        time.perf_counter() - job.message_ts,
                    )
                    await source.wait_until_drained()
                    self.schedule_continuous_idle_stop(job.voice_channel.guild)
                    self.schedule_idle_disconnect(job.voice_channel.guild)
                    return "done"

        # Consume the breaker probe only now that we are about to call cloud.
        if not cb.allow_request():
            log.debug(
                "Circuit breaker open; skipping stream guild=%s", job.voice_channel.guild.id
            )
            return "fallback"

        log.info(
            "Ready to stream guild=%s channel=%s queue_wait=%.3fs prep_total=%.3fs",
            job.voice_channel.guild.id, job.voice_channel.id,
            worker_started - job.queued_at, time.perf_counter() - worker_started,
        )
        source = self.ensure_continuous_player(vc)
        try:
            status, frames = await self._stream_tts_to_source(source, voice, job)
        except Exception:
            log.exception("Streaming playback crashed; falling back to Piper")
            cb.record_failure()
            return "fallback"

        if status == "pre_audio":
            cb.record_failure()
            return "fallback"
        # Audio started: ok (full) or truncated (mid-stream failure). Either
        # way we do NOT overlay Piper on top of already-playing audio.
        cb.record_success() if status == "ok" else cb.record_failure()
        await source.wait_until_drained()
        log.info(
            "Playback finished (stream) guild=%s channel=%s frames=%d total_since_queue=%.3fs",
            job.voice_channel.guild.id, job.voice_channel.id, frames,
            time.perf_counter() - job.queued_at,
        )
        self.schedule_continuous_idle_stop(job.voice_channel.guild)
        self.schedule_idle_disconnect(job.voice_channel.guild)
        return "done"

    async def _stream_tts_to_source(
        self, source: "ContinuousTTSAudioSource", voice, job: TTSJob
    ) -> tuple[str, int]:
        """Stream a MiniMax voice into the continuous player frame-by-frame.

        Returns (status, frames_enqueued) where status is "ok", "truncated"
        (audio started then the stream failed mid-way) or "pre_audio" (failed
        before any audio — caller falls back to Piper). Circuit-breaker
        accounting is the caller's job.
        """
        cloud = self.tts_dispatcher.cloud
        mm = voice.minimax
        agen = cloud.stream_audio(
            job.text,
            voice_id=mm.voice_id, model=mm.model, speed=mm.speed,
            vol=mm.vol, pitch=mm.pitch, emotion=mm.emotion,
            language_boost=mm.language_boost,
        )
        # 1. First chunk under the TTFA budget; any failure here => clean
        #    fallback to Piper (no audio has played yet).
        try:
            first_chunk = await asyncio.wait_for(
                agen.__anext__(), timeout=TTS_STREAM_TTFA_TIMEOUT
            )
        except StopAsyncIteration:
            await agen.aclose()
            log.warning("Stream produced no audio; falling back to Piper")
            return ("pre_audio", 0)
        except asyncio.TimeoutError:
            await agen.aclose()
            log.warning(
                "Stream TTFA exceeded %.2fs; falling back to Piper", TTS_STREAM_TTFA_TIMEOUT
            )
            return ("pre_audio", 0)
        except Exception as exc:
            await agen.aclose()
            log.warning(
                "Stream failed before first audio (%s: %s); Piper fallback",
                type(exc).__name__, exc,
            )
            return ("pre_audio", 0)

        # 2. We have audio. Decode the MP3 byte stream via ffmpeg (stdin ->
        #    s16le stdout) while feeding chunks concurrently.
        proc = await asyncio.create_subprocess_exec(
            *build_tts_stream_pcm_command(),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        mid_error: list[BaseException] = []

        # Tee the MP3 byte stream to a cache ".part" file so a future repeat
        # of this (voice, text) plays from disk without hitting the API. Only
        # committed on a clean finish — partial streams are never cached.
        cache = self.tts_dispatcher.cache
        cache_final: Path | None = None
        cache_part = None
        if cache is not None:
            try:
                cache_final = cache.cache_path_for(job.text, voice.name)
                cache_final.parent.mkdir(parents=True, exist_ok=True)
                cache_part = open(str(cache_final) + ".part", "wb")
            except OSError:
                cache_final = None
                cache_part = None

        async def _feed() -> None:
            try:
                proc.stdin.write(first_chunk)
                await proc.stdin.drain()
                if cache_part is not None:
                    cache_part.write(first_chunk)
                async for chunk in agen:
                    proc.stdin.write(chunk)
                    await proc.stdin.drain()
                    if cache_part is not None:
                        cache_part.write(chunk)
            except Exception as exc:  # mid-stream API/network failure
                mid_error.append(exc)
            finally:
                try:
                    proc.stdin.close()
                except Exception:
                    pass
                if cache_part is not None:
                    try:
                        cache_part.close()
                    except Exception:
                        pass

        feeder = asyncio.create_task(_feed())

        frames_enqueued = 0
        first_frame_ts: float | None = None
        leftover = b""
        try:
            while True:
                data = await proc.stdout.read(PCM_FRAME_BYTES * 16)
                if not data:
                    break
                buf = leftover + data
                n = len(buf) - (len(buf) % PCM_FRAME_BYTES)
                if n:
                    out_frames = [buf[i:i + PCM_FRAME_BYTES] for i in range(0, n, PCM_FRAME_BYTES)]
                    source.enqueue_frames(out_frames)
                    if first_frame_ts is None:
                        first_frame_ts = time.perf_counter()
                        log.info(
                            "Stream first audio guild=%s channel=%s "
                            "message_to_first_audio_s=%.3f queue_to_first_audio_s=%.3f",
                            job.voice_channel.guild.id, job.voice_channel.id,
                            first_frame_ts - job.message_ts,
                            first_frame_ts - job.queued_at,
                        )
                    frames_enqueued += len(out_frames)
                leftover = buf[n:]
        finally:
            await feeder
            if leftover:
                source.enqueue_frames(
                    [leftover + b"\x00" * (PCM_FRAME_BYTES - len(leftover))]
                )
                frames_enqueued += 1
            try:
                await proc.wait()
            except Exception:
                pass

        part_path = (str(cache_final) + ".part") if cache_final is not None else None

        def _discard_cache() -> None:
            if part_path:
                try:
                    os.unlink(part_path)
                except OSError:
                    pass

        if mid_error:
            _discard_cache()  # never cache a partial stream
            log.warning(
                "Stream failed mid-playback after %d frames (%s); truncated",
                frames_enqueued, mid_error[0],
            )
            return ("truncated", frames_enqueued)
        if frames_enqueued == 0:
            _discard_cache()
            return ("pre_audio", 0)
        # Clean finish: finalize the cache file so repeats skip the API.
        if cache is not None and cache_final is not None and part_path:
            try:
                os.replace(part_path, cache_final)
                cache.commit_file(job.text, cache_final, voice.name)
            except OSError:
                _discard_cache()
        return ("ok", frames_enqueued)

    # ------------------------------------------------------------------
    # Prefetch pipeline (TTS_PREFETCH_ENABLED): generation_worker produces
    # PreparedAudio ahead of playback_worker, which plays strictly FIFO.
    # ------------------------------------------------------------------

    async def _generation_worker(self) -> None:
        await self.wait_until_ready()
        await self.warmup_tts()
        log.info("TTS generation worker started (lookahead=%d)", TTS_PREFETCH_LOOKAHEAD)
        while not self.is_closed():
            job = await self.message_queue.get()
            prepared = PreparedAudio(job=job, channel=asyncio.Queue())
            self.active_prepared.add(prepared)
            try:
                # Backpressure: blocks here when we are already `lookahead`
                # messages ahead of playback.
                await self.ready_queue.put(prepared)
                await self._prepare_into(prepared)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("TTS generation pipeline error")
                await prepared.channel.put(None)
            finally:
                self.message_queue.task_done()

    async def _prepare_into(self, prepared: PreparedAudio) -> None:
        """Generate a job's audio into ``prepared.channel`` (frame batches)."""
        job = prepared.job
        voice = self.voice_registry.get(job.voice_profile) or self.voice_registry.fallback_record()
        try:
            if prepared.cancelled:
                return
            if self._should_attempt_stream(voice):
                status = await self._generate_stream_into(prepared, voice)
                if status != "pre_audio":
                    return  # ok / truncated / cache / cancelled
                voice = self.voice_registry.fallback_record()  # pre-audio -> Piper
            await self._generate_file_into(prepared, voice)
        finally:
            # Always terminate the channel so the consumer never hangs.
            await prepared.channel.put(None)

    async def _safe_pcm_frames(self, path: Path) -> list[bytes]:
        try:
            return await self.prepare_tts_pcm_frames(path)
        except Exception:
            log.exception("PCM decode failed for %s", path)
            return []

    async def _generate_stream_into(self, prepared: PreparedAudio, voice) -> str:
        job = prepared.job
        cb = self.tts_dispatcher.circuit_breaker
        cache = self.tts_dispatcher.cache
        if cache is not None:
            cached = cache.lookup(job.text, voice.name)
            if cached is not None:
                frames = await self._safe_pcm_frames(cached)
                if frames and not prepared.cancelled:
                    await prepared.channel.put(frames)
                    prepared.provider = "cache"
                    log.info(
                        "Cache HIT (prefetch) guild=%s voice=%s frames=%d (no API)",
                        job.guild_id, voice.name, len(frames),
                    )
                    return "cache"
        if prepared.cancelled:
            return "cancelled"
        if not cb.allow_request():
            return "pre_audio"  # breaker open -> caller does Piper fallback
        # Set the label before streaming so the playback worker logs the
        # provider on the first frame (a pre-audio fallback overwrites it
        # with the Piper provider in _generate_file_into).
        prepared.provider = "minimax"
        status, _ = await self._stream_to_channel(prepared, voice)
        if status == "pre_audio":
            cb.record_failure()
            return "pre_audio"
        if status == "cancelled":
            return "cancelled"
        cb.record_success() if status == "ok" else cb.record_failure()
        return status

    async def _generate_file_into(self, prepared: PreparedAudio, voice) -> None:
        if prepared.cancelled:
            return
        job = prepared.job
        filename = TMP_DIR / f"tts_{uuid.uuid4().hex}.wav"
        try:
            prepared.provider = await self.tts_dispatcher.synthesize(
                job.text, filename, voice=voice
            )
            if prepared.cancelled:
                return
            frames = await self._safe_pcm_frames(filename)
            if frames:
                await prepared.channel.put(frames)
        finally:
            if filename.exists():
                try:
                    filename.unlink()
                except OSError:
                    log.exception("Failed to remove temp file: %s", filename)

    async def _stream_to_channel(self, prepared: PreparedAudio, voice) -> tuple[str, int]:
        """Stream MiniMax -> ffmpeg -> frame batches into ``prepared.channel``.

        Like _stream_tts_to_source but writes to the prefetch channel (not the
        live player) and honors cancellation. Returns (status, frames) with
        status in ok/truncated/pre_audio/cancelled.
        """
        job = prepared.job
        cloud = self.tts_dispatcher.cloud
        mm = voice.minimax
        agen = cloud.stream_audio(
            job.text, voice_id=mm.voice_id, model=mm.model, speed=mm.speed,
            vol=mm.vol, pitch=mm.pitch, emotion=mm.emotion, language_boost=mm.language_boost,
        )
        try:
            first_chunk = await asyncio.wait_for(
                agen.__anext__(), timeout=TTS_STREAM_TTFA_TIMEOUT
            )
        except StopAsyncIteration:
            await agen.aclose()
            log.warning("Stream produced no audio; Piper fallback")
            return ("pre_audio", 0)
        except asyncio.TimeoutError:
            await agen.aclose()
            log.warning("Stream TTFA exceeded %.2fs; Piper fallback", TTS_STREAM_TTFA_TIMEOUT)
            return ("pre_audio", 0)
        except Exception as exc:
            await agen.aclose()
            log.warning("Stream failed before first audio (%s: %s); Piper fallback",
                        type(exc).__name__, exc)
            return ("pre_audio", 0)
        if prepared.cancelled:
            await agen.aclose()
            return ("cancelled", 0)

        proc = await asyncio.create_subprocess_exec(
            *build_tts_stream_pcm_command(),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        mid_error: list[BaseException] = []
        cache = self.tts_dispatcher.cache
        cache_final: Path | None = None
        cache_part = None
        if cache is not None:
            try:
                cache_final = cache.cache_path_for(job.text, voice.name)
                cache_final.parent.mkdir(parents=True, exist_ok=True)
                cache_part = open(str(cache_final) + ".part", "wb")
            except OSError:
                cache_final = None
                cache_part = None

        async def _feed() -> None:
            try:
                proc.stdin.write(first_chunk)
                await proc.stdin.drain()
                if cache_part is not None:
                    cache_part.write(first_chunk)
                async for chunk in agen:
                    if prepared.cancelled:
                        break
                    proc.stdin.write(chunk)
                    await proc.stdin.drain()
                    if cache_part is not None:
                        cache_part.write(chunk)
            except Exception as exc:
                mid_error.append(exc)
            finally:
                try:
                    proc.stdin.close()
                except Exception:
                    pass
                if cache_part is not None:
                    try:
                        cache_part.close()
                    except Exception:
                        pass

        feeder = asyncio.create_task(_feed())
        frames_count = 0
        leftover = b""
        try:
            while True:
                data = await proc.stdout.read(PCM_FRAME_BYTES * 16)
                if not data:
                    break
                buf = leftover + data
                n = len(buf) - (len(buf) % PCM_FRAME_BYTES)
                if n and not prepared.cancelled:
                    out_frames = [buf[i:i + PCM_FRAME_BYTES] for i in range(0, n, PCM_FRAME_BYTES)]
                    await prepared.channel.put(out_frames)
                    frames_count += len(out_frames)
                leftover = buf[n:]
        finally:
            await feeder
            if leftover and not prepared.cancelled:
                await prepared.channel.put(
                    [leftover + b"\x00" * (PCM_FRAME_BYTES - len(leftover))]
                )
                frames_count += 1
            try:
                await proc.wait()
            except Exception:
                pass

        part_path = (str(cache_final) + ".part") if cache_final is not None else None

        def _discard() -> None:
            if part_path:
                try:
                    os.unlink(part_path)
                except OSError:
                    pass

        if prepared.cancelled:
            _discard()
            return ("cancelled", frames_count)
        if mid_error:
            _discard()
            log.warning("Stream failed mid-stream after %d frames (%s); truncated",
                        frames_count, mid_error[0])
            return ("truncated", frames_count)
        if frames_count == 0:
            _discard()
            return ("pre_audio", 0)
        if cache is not None and cache_final is not None and part_path:
            try:
                os.replace(part_path, cache_final)
                cache.commit_file(job.text, cache_final, voice.name)
            except OSError:
                _discard()
        return ("ok", frames_count)

    async def _playback_worker(self) -> None:
        await self.wait_until_ready()
        log.info("TTS playback worker started")
        while not self.is_closed():
            prepared = await self.ready_queue.get()
            try:
                await self._play_prepared(prepared)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("TTS playback failed; disconnecting voice")
                await self.disconnect_guild_voice(prepared.job.voice_channel.guild)
            finally:
                self.active_prepared.discard(prepared)
                self.ready_queue.task_done()

    async def _drain_channel(self, prepared: PreparedAudio) -> None:
        """Consume a prepared channel to its sentinel without playing."""
        while True:
            batch = await prepared.channel.get()
            if batch is None:
                return

    async def _play_prepared(self, prepared: PreparedAudio) -> None:
        job = prepared.job
        pickup_ts = time.perf_counter()
        if prepared.cancelled:
            await self._drain_channel(prepared)
            log.info("Skipped cancelled job guild=%s channel=%s",
                     job.voice_channel.guild.id, job.voice_channel.id)
            return
        try:
            vc = await self.ensure_voice(job.voice_channel)
        except Exception:
            log.exception("Voice prepare failed (playback)")
            existing = discord.utils.get(self.voice_clients, guild=job.voice_channel.guild)
            if existing and existing.is_connected():
                await self.disconnect_guild_voice(job.voice_channel.guild)
            await self._drain_channel(prepared)
            return

        source = self.ensure_continuous_player(vc)
        first_ts: float | None = None
        total = 0
        while True:
            batch = await prepared.channel.get()
            if batch is None:
                break
            if prepared.cancelled:
                continue  # stop feeding but drain to the sentinel
            source.enqueue_frames(batch)
            total += len(batch)
            if first_ts is None:
                first_ts = time.perf_counter()
                log.info(
                    "Audio start guild=%s channel=%s provider=%s queue_wait=%.3fs "
                    "message_to_audio_s=%.3f queue_to_audio_s=%.3f",
                    job.voice_channel.guild.id, job.voice_channel.id,
                    prepared.provider or "?", pickup_ts - job.queued_at,
                    first_ts - job.message_ts, first_ts - job.queued_at,
                )
        if total == 0:
            return
        await source.wait_until_drained()
        log.info(
            "Playback finished guild=%s channel=%s frames=%d total_since_queue=%.3fs",
            job.voice_channel.guild.id, job.voice_channel.id, total,
            time.perf_counter() - job.queued_at,
        )
        self.schedule_continuous_idle_stop(job.voice_channel.guild)
        self.schedule_idle_disconnect(job.voice_channel.guild)

    async def tts_worker(self) -> None:
        await self.wait_until_ready()
        await self.warmup_tts()
        log.info("TTS worker started")

        while not self.is_closed():
            job = await self.message_queue.get()
            filename = TMP_DIR / f"tts_{uuid.uuid4().hex}.wav"

            try:
                worker_started = time.perf_counter()
                voice = (
                    self.voice_registry.get(job.voice_profile)
                    or self.voice_registry.fallback_record()
                )

                # Streaming fast path: a MiniMax voice over the continuous
                # stream plays chunks as they arrive (lower Time-To-First-
                # Audio). On a pre-audio failure or an open breaker it returns
                # "fallback" and we drop to the Piper file path below.
                file_voice_name = job.voice_profile
                if self._should_attempt_stream(voice):
                    outcome = await self._run_streaming_job(job, voice, worker_started)
                    if outcome == "done":
                        continue
                    # Pre-audio fallback: use Piper directly, never re-hit cloud.
                    file_voice_name = self.voice_registry.fallback_profile

                connect_task = asyncio.create_task(self.ensure_voice(job.voice_channel))
                tts_task = asyncio.create_task(self.generate_tts_file(job.text, filename, file_voice_name))

                try:
                    vc, _ = await asyncio.gather(connect_task, tts_task)
                except Exception:
                    connect_error = connect_task.exception() if connect_task.done() else None
                    tts_error = tts_task.exception() if tts_task.done() else None
                    if connect_error:
                        log.exception("Voice prepare failed", exc_info=connect_error)
                        if not tts_task.done():
                            tts_task.cancel()
                        vc = discord.utils.get(self.voice_clients, guild=job.voice_channel.guild)
                        if vc and vc.is_connected():
                            await self.disconnect_guild_voice(job.voice_channel.guild)
                        else:
                            log.info(
                                "Skip disconnect cleanup guild=%s reason=voice_not_connected",
                                job.voice_channel.guild.id,
                            )
                    elif tts_error:
                        log.exception("TTS generation failed; keeping voice session", exc_info=tts_error)
                    else:
                        log.exception("TTS processing failed before playback")
                    continue

                log.info(
                    "Ready to play guild=%s channel=%s queue_wait=%.3fs prep_total=%.3fs",
                    job.voice_channel.guild.id,
                    job.voice_channel.id,
                    worker_started - job.queued_at,
                    time.perf_counter() - worker_started,
                )

                try:
                    if TTS_CONTINUOUS_STREAM:
                        source = self.ensure_continuous_player(vc)
                        frames = await self.prepare_tts_pcm_frames(filename)
                        audio_enqueue_ts = time.perf_counter()
                        source.enqueue_frames(frames)
                        log.info(
                            "Queued continuous playback guild=%s channel=%s frames=%s duration=%.3fs message_to_audio_enqueue_s=%.3f queue_to_audio_enqueue_s=%.3f",
                            job.voice_channel.guild.id,
                            job.voice_channel.id,
                            len(frames),
                            len(frames) * PCM_FRAME_MS / 1000,
                            audio_enqueue_ts - job.message_ts,
                            audio_enqueue_ts - job.queued_at,
                        )
                        await source.wait_until_drained()
                    else:
                        playback_request_ts = time.perf_counter()
                        log.info(
                            "Starting non-continuous playback metrics message_to_playback_request_s=%.3f queue_to_playback_request_s=%.3f",
                            playback_request_ts - job.message_ts,
                            playback_request_ts - job.queued_at,
                        )
                        await self.play_file(vc, filename)
                except Exception:
                    log.exception("Playback failed; disconnecting voice")
                    await self.disconnect_guild_voice(job.voice_channel.guild)
                    continue

                log.info(
                    "Playback finished guild=%s channel=%s total_since_queue=%.3fs",
                    job.voice_channel.guild.id,
                    job.voice_channel.id,
                    time.perf_counter() - job.queued_at,
                )

                self.schedule_continuous_idle_stop(job.voice_channel.guild)
                self.schedule_idle_disconnect(job.voice_channel.guild)

            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("TTS processing failed")
                await self.disconnect_guild_voice(job.voice_channel.guild)
            finally:
                if filename.exists():
                    try:
                        filename.unlink()
                    except OSError:
                        log.exception("Failed to remove temp file: %s", filename)
                self.message_queue.task_done()

    def ensure_continuous_player(self, vc: discord.VoiceClient) -> ContinuousTTSAudioSource:
        guild_id = vc.guild.id
        self.cancel_continuous_idle_stop(guild_id)
        source = self.continuous_sources.get(guild_id)
        source_created = False
        if source is None or source.stopped:
            source = ContinuousTTSAudioSource(build_idle_pcm_frame(TTS_IDLE_FRAME_MODE, TTS_IDLE_VOLUME_DB))
            self.continuous_sources[guild_id] = source
            source_created = True

        if source_created and (vc.is_playing() or vc.is_paused()):
            log.warning("Stopping previous voice source before continuous stream guild=%s", guild_id)
            vc.stop()

        if source_created or (not vc.is_playing() and not vc.is_paused()):
            vc.play(source)
            log.info(
                "Continuous TTS stream started guild=%s mode=%s idle_volume_db=%s max_idle_seconds=%s",
                guild_id,
                TTS_IDLE_FRAME_MODE,
                TTS_IDLE_VOLUME_DB,
                TTS_MAX_CONTINUOUS_IDLE_SECONDS,
            )
        return source

    async def prepare_tts_pcm_frames(self, source: Path) -> list[bytes]:
        started = time.perf_counter()
        cmd = build_tts_pcm_command(source)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            stderr_text = stderr.decode("utf-8", errors="ignore").strip()
            raise RuntimeError(f"ffmpeg PCM preparation failed rc={proc.returncode} stderr={stderr_text}")

        frames = split_pcm_frames(stdout, TTS_STREAM_TAIL_MS)
        if not frames:
            raise RuntimeError("ffmpeg PCM preparation produced no audio frames")

        log.info(
            "PCM preparation took=%.3fs source_size=%s pcm_bytes=%s frames=%s tail_ms=%s",
            time.perf_counter() - started,
            source.stat().st_size if source.exists() else "unknown",
            len(stdout),
            len(frames),
            TTS_STREAM_TAIL_MS,
        )
        return frames

    async def prepare_playback_file(self, source: Path, prepared: Path) -> Path:
        started = time.perf_counter()
        cmd = build_playback_prepare_command(source, prepared)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            stdout_text = stdout.decode("utf-8", errors="ignore").strip()
            stderr_text = stderr.decode("utf-8", errors="ignore").strip()
            raise RuntimeError(
                f"ffmpeg playback preparation failed rc={proc.returncode} "
                f"stdout={stdout_text} stderr={stderr_text}"
            )
        if not prepared.exists() or prepared.stat().st_size == 0:
            raise RuntimeError("ffmpeg playback preparation produced empty output")

        log.info(
            "Playback preparation took=%.3fs source_size=%s prepared_size=%s "
            "preroll_ms=%s preroll_mode=%s preroll_volume_db=%s tail_ms=%s",
            time.perf_counter() - started,
            source.stat().st_size if source.exists() else "unknown",
            prepared.stat().st_size,
            TTS_PREROLL_MS,
            TTS_PREROLL_MODE,
            TTS_PREROLL_VOLUME_DB,
            TTS_SILENCE_TAIL_MS,
        )
        return prepared

    async def play_file(self, vc: discord.VoiceClient, filename: Path) -> None:
        if vc.is_playing() or vc.is_paused():
            log.warning("Voice client was already playing; stopping previous source")
            vc.stop()

        finished = asyncio.Event()
        loop = asyncio.get_running_loop()
        started = time.perf_counter()

        def after(error: Exception | None) -> None:
            if error:
                log.exception("Playback callback error", exc_info=error)
            loop.call_soon_threadsafe(finished.set)

        before_options = "-hide_banner -loglevel warning"
        if FFMPEG_LOW_DELAY:
            before_options = (
                f"{before_options} "
                "-fflags nobuffer -flags low_delay -probesize 32 -analyzeduration 0"
            )

        prepared = TMP_DIR / f"playback_{uuid.uuid4().hex}.wav"
        playback_file = filename
        try:
            try:
                playback_file = await self.prepare_playback_file(filename, prepared)
            except Exception:
                log.exception("Playback preparation failed; using source file")

            audio = discord.FFmpegPCMAudio(
                str(playback_file),
                before_options=before_options,
                options="-vn",
            )

            log.info("Starting playback file=%s size=%s", playback_file, playback_file.stat().st_size)
            vc.play(audio, after=after)
            await finished.wait()

            log.info("Playback duration took=%.3fs", time.perf_counter() - started)
        finally:
            if prepared.exists():
                try:
                    prepared.unlink()
                except OSError:
                    log.exception("Failed to remove prepared playback file: %s", prepared)

    async def disconnect_guild_voice(self, guild: discord.Guild) -> None:
        self.cancel_idle_disconnect(guild.id)
        self.cancel_continuous_idle_stop(guild.id)
        source = self.continuous_sources.pop(guild.id, None)
        if source:
            source.stop()
        vc = discord.utils.get(self.voice_clients, guild=guild)
        if not vc:
            return
        try:
            await vc.disconnect(force=True)
            self.suppress_auto_connect(guild.id, "explicit_disconnect")
            log.info("Voice disconnected guild=%s", guild.id)
        except Exception:
            log.exception("Voice disconnect cleanup failed")

    async def auto_connect_for_member(self, member: discord.Member, channel: discord.VoiceChannel) -> None:
        if not TTS_AUTO_CONNECT_ENABLED:
            log.info(
                "Skip auto-connect guild=%s member=%s reason=disabled",
                channel.guild.id,
                member.id,
            )
            return
        if not self.config_store.is_enabled(channel.guild.id):
            return
        if not self.config_store.is_allowed(channel.guild.id, member.id):
            return

        remaining = self.voice_connect_cooldown_remaining(channel.guild.id)
        if remaining > 0:
            log.info(
                "Skip auto-connect guild=%s member=%s reason=cooldown remaining=%.1fs",
                channel.guild.id,
                member.id,
                remaining,
            )
            return

        suppress_remaining = self.suppress_auto_connect_remaining(channel.guild.id)
        if suppress_remaining > 0:
            log.info(
                "Skip auto-connect guild=%s member=%s reason=recent_disconnect remaining=%.1fs",
                channel.guild.id,
                member.id,
                suppress_remaining,
            )
            return

        try:
            await self.ensure_voice(channel)
            log.info(
                "Auto-connected for whitelisted user guild=%s member=%s channel=%s",
                channel.guild.id,
                member.id,
                channel.id,
            )
        except Exception:
            log.exception(
                "Auto-connect failed guild=%s member=%s channel=%s",
                channel.guild.id,
                member.id,
                channel.id,
            )


bot = TTSBot()


@bot.event
async def on_ready() -> None:
    log.info("TTS bot logged in as %s (%s)", bot.user, bot.user.id if bot.user else "unknown")
    log.info("Opus loaded: %s", discord.opus.is_loaded())
    log.info("Env fallback whitelist users: %s", ",".join(str(user_id) for user_id in sorted(WHITELIST_USERS)))
    log.info("Bot config path: %s", BOT_CONFIG_PATH)
    log.info("Voice profiles: %s", ",".join(sorted(VOICE_PROFILES)))
    log.info("Piper tuning: speaker=%s length_scale=%.2f", PIPER_SPEAKER, PIPER_LENGTH_SCALE)
    log.info(
        "Per-message cap: max_text_length=%s tts_max_chars=%s",
        MAX_TEXT_LENGTH, TTS_MAX_CHARS,
    )
    log.info(
        "Playback tuning: preroll_ms=%s preroll_mode=%s preroll_volume_db=%s tail_ms=%s trim_silence=%s ffmpeg_low_delay=%s",
        TTS_PREROLL_MS,
        TTS_PREROLL_MODE,
        TTS_PREROLL_VOLUME_DB,
        TTS_SILENCE_TAIL_MS,
        TTS_TRIM_SILENCE,
        FFMPEG_LOW_DELAY,
    )
    log.info(
        "Continuous stream: enabled=%s idle_mode=%s idle_volume_db=%s stream_tail_ms=%s max_idle_seconds=%s",
        TTS_CONTINUOUS_STREAM,
        TTS_IDLE_FRAME_MODE,
        TTS_IDLE_VOLUME_DB,
        TTS_STREAM_TAIL_MS,
        TTS_MAX_CONTINUOUS_IDLE_SECONDS,
    )
    log.info(
        "Merge tuning: algorithm=%s enabled=%s max_chars=%s window_ms=%s max_parts=%s selective_enabled=%s selective_scope=all_allowed_users reaction_pause_ms=%s",
        TTS_MERGE_ALGORITHM,
        TTS_MERGE_SHORT_MESSAGES,
        TTS_MERGE_MAX_CHARS,
        TTS_MERGE_WINDOW_MS,
        TTS_MERGE_MAX_PARTS,
        TTS_SELECTIVE_HOLD_ENABLED,
        TTS_SELECTIVE_HOLD_REACTION_PAUSE_MS,
    )


@bot.event
async def on_message(message: discord.Message) -> None:
    if message.author.bot:
        return

    if (
        message.guild is not None
        and bot.config_store.is_enabled(message.guild.id)
        and bot.config_store.is_allowed(message.guild.id, message.author.id)
        and message.author.voice
        and isinstance(message.author.voice.channel, discord.VoiceChannel)
    ):
        await bot.queue_or_merge_message(
            message.content,
            message.author.voice.channel,
            message.author.id,
            message.channel.id,
        )

    await bot.process_commands(message)


@bot.event
async def on_voice_state_update(
    member: discord.Member,
    before: discord.VoiceState,
    after: discord.VoiceState,
) -> None:
    if member.bot:
        return
    if not bot.config_store.is_enabled(member.guild.id):
        return
    if not bot.config_store.is_allowed(member.guild.id, member.id):
        return

    if isinstance(after.channel, discord.VoiceChannel):
        await bot.auto_connect_for_member(member, after.channel)

    guild = member.guild
    vc = discord.utils.get(bot.voice_clients, guild=guild)
    if not vc or not vc.is_connected():
        return

    if isinstance(vc.channel, discord.VoiceChannel):
        whitelisted_present = any(
            (not user.bot) and bot.config_store.is_allowed(guild.id, user.id)
            for user in vc.channel.members
        )
        if not whitelisted_present and not bot.has_active_voice_playback(guild.id, vc):
            bot.schedule_idle_disconnect(guild)


def is_guild_manager(member: discord.Member | discord.User) -> bool:
    permissions = getattr(member, "guild_permissions", None)
    return bool(
        permissions
        and (getattr(permissions, "manage_guild", False) or getattr(permissions, "administrator", False))
    )


async def require_guild_manager(interaction: discord.Interaction) -> bool:
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
        return False
    if not is_guild_manager(interaction.user):
        await interaction.response.send_message("Нужны права Manage Server или Administrator.", ephemeral=True)
        return False
    return True


def validate_voice_profile(voice: str) -> str | None:
    voice_name = voice.strip().lower()
    return voice_name if voice_name in bot.voice_registry else None


async def voice_profile_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    current = current.lower()
    choices: list[app_commands.Choice[str]] = []
    for name in bot.voice_registry.names():
        rec = bot.voice_registry.get(name)
        label = rec.label if rec is not None else name
        if current in name.lower() or current in label.lower():
            choices.append(app_commands.Choice(name=f"{name} - {label}", value=name))
    return choices[:25]


def resolve_tts_command_voice_channel(
    interaction: discord.Interaction,
    requested_channel: discord.VoiceChannel | None = None,
) -> discord.VoiceChannel | None:
    if requested_channel is not None:
        return requested_channel

    if interaction.guild is not None:
        vc = discord.utils.get(bot.voice_clients, guild=interaction.guild)
        if vc and vc.is_connected() and isinstance(vc.channel, discord.VoiceChannel):
            return vc.channel

    user_voice = getattr(getattr(interaction.user, "voice", None), "channel", None)
    if isinstance(user_voice, discord.VoiceChannel):
        return user_voice

    return None


tts_group = app_commands.Group(name="voicebot", description="Управление озвучкой сообщений")


@tts_group.command(name="off", description="Отключить озвучку на сервере")
async def slash_tts_off(interaction: discord.Interaction) -> None:
    if not await require_guild_manager(interaction):
        return
    guild = interaction.guild
    assert guild is not None
    bot.config_store.set_enabled(guild.id, False)
    bot.clear_merge_buffers(guild.id)
    cleared = bot.clear_queue_for_guild(guild.id)
    await bot.disconnect_guild_voice(guild)
    await interaction.response.send_message(f"TTS отключен. Очередь очищена: {cleared}.", ephemeral=True)


@tts_group.command(name="on", description="Включить озвучку на сервере")
async def slash_tts_on(interaction: discord.Interaction) -> None:
    if not await require_guild_manager(interaction):
        return
    guild = interaction.guild
    assert guild is not None
    bot.config_store.set_enabled(guild.id, True)
    await interaction.response.send_message("TTS включен.", ephemeral=True)


@tts_group.command(name="allow", description="Добавить пользователя в озвучку")
@app_commands.describe(user="Пользователь, чьи сообщения нужно озвучивать")
async def slash_tts_allow(interaction: discord.Interaction, user: discord.Member) -> None:
    if not await require_guild_manager(interaction):
        return
    guild = interaction.guild
    assert guild is not None
    bot.config_store.add_user(guild.id, user.id)
    await interaction.response.send_message(f"Добавлен в озвучку: {user.mention}", ephemeral=True)


@tts_group.command(name="deny", description="Исключить пользователя из озвучки")
@app_commands.describe(user="Пользователь, чьи сообщения больше не нужно озвучивать")
async def slash_tts_deny(interaction: discord.Interaction, user: discord.Member) -> None:
    if not await require_guild_manager(interaction):
        return
    guild = interaction.guild
    assert guild is not None
    bot.config_store.remove_user(guild.id, user.id)
    await interaction.response.send_message(f"Исключен из озвучки: {user.mention}", ephemeral=True)


@tts_group.command(name="voice-set", description="Сменить озвучку по умолчанию")
@app_commands.describe(voice="Профиль голоса")
@app_commands.autocomplete(voice=voice_profile_autocomplete)
async def slash_tts_voice_set(interaction: discord.Interaction, voice: str) -> None:
    if not await require_guild_manager(interaction):
        return
    voice_name = validate_voice_profile(voice)
    if not voice_name:
        await interaction.response.send_message(
            "Неизвестный профиль голоса. Используйте `/voicebot voices`.",
            ephemeral=True,
        )
        return
    guild = interaction.guild
    assert guild is not None
    bot.config_store.set_default_voice(guild.id, voice_name)
    await interaction.response.send_message(f"Озвучка по умолчанию: `{voice_name}`.", ephemeral=True)


@tts_group.command(name="voice-user", description="Назначить отдельную озвучку пользователю")
@app_commands.describe(user="Пользователь", voice="Профиль голоса")
@app_commands.autocomplete(voice=voice_profile_autocomplete)
async def slash_tts_voice_user(interaction: discord.Interaction, user: discord.Member, voice: str) -> None:
    if not await require_guild_manager(interaction):
        return
    voice_name = validate_voice_profile(voice)
    if not voice_name:
        await interaction.response.send_message(
            "Неизвестный профиль голоса. Используйте `/voicebot voices`.",
            ephemeral=True,
        )
        return
    guild = interaction.guild
    assert guild is not None
    bot.config_store.set_user_voice(guild.id, user.id, voice_name)
    await interaction.response.send_message(f"Для {user.mention} назначено: `{voice_name}`.", ephemeral=True)


@tts_group.command(name="voice-clear", description="Сбросить персональную озвучку пользователя")
@app_commands.describe(user="Пользователь")
async def slash_tts_voice_clear(interaction: discord.Interaction, user: discord.Member) -> None:
    if not await require_guild_manager(interaction):
        return
    guild = interaction.guild
    assert guild is not None
    bot.config_store.clear_user_voice(guild.id, user.id)
    await interaction.response.send_message(f"Персональная озвучка сброшена: {user.mention}", ephemeral=True)


@tts_group.command(name="voices", description="Показать доступные озвучки")
async def slash_tts_voices(interaction: discord.Interaction) -> None:
    reg = bot.voice_registry
    lines: list[str] = []
    for name in reg.names():
        rec = reg.get(name)
        if rec is None:
            continue
        tag = "MiniMax" if rec.is_minimax else "Piper"
        fb = " (fallback)" if name == reg.fallback_profile else ""
        desc = f" — {rec.description}" if rec.description else ""
        lines.append(f"`{name}` [{tag}]{fb} - {rec.label}{desc}")
    await interaction.response.send_message(
        "\n".join(lines) or "Каталог пуст.", ephemeral=True
    )


@tts_group.command(name="voice-add", description="Зарегистрировать MiniMax-голос (системный или клон)")
@app_commands.describe(
    name="Имя профиля (kebab-case: a-z, 0-9, дефис)",
    voice_id="MiniMax voice_id (системный или клон)",
    description="Описание (необязательно)",
)
async def slash_tts_voice_add(
    interaction: discord.Interaction,
    name: str,
    voice_id: str,
    description: str = "",
) -> None:
    if not await require_guild_manager(interaction):
        return
    name = name.strip().lower()
    voice_id = voice_id.strip()
    if not voice_registry.valid_voice_name(name):
        await interaction.response.send_message(
            "Имя должно быть в kebab-case: строчные буквы, цифры и дефис, "
            "начинаться с буквы или цифры (например `mm-qingse`).",
            ephemeral=True,
        )
        return
    if name in bot.voice_registry:
        await interaction.response.send_message(
            f"Голос `{name}` уже существует. Выберите другое имя.", ephemeral=True
        )
        return
    if not voice_id:
        await interaction.response.send_message("Укажите voice_id.", ephemeral=True)
        return
    # Validation hits the network; defer so the interaction does not expire.
    await interaction.response.defer(ephemeral=True, thinking=True)
    ok, err = await bot.validate_minimax_voice(voice_id)
    if not ok:
        await interaction.followup.send(f"Не добавлено: {err}", ephemeral=True)
        return
    record = voice_registry.VoiceRecord(
        name=name,
        label=f"{name} (MiniMax)",
        description=description.strip(),
        provider=voice_registry.PROVIDER_MINIMAX,
        minimax=voice_registry.MiniMaxParams(voice_id=voice_id),
    )
    bot.voice_registry.add(record)
    try:
        bot.persist_voice_registry()
    except OSError as exc:
        log.exception("Failed to persist voices.json after voice-add")
        await interaction.followup.send(
            f"Голос проверен, но не сохранён на диск: {exc}", ephemeral=True
        )
        return
    await interaction.followup.send(
        f"Добавлен голос `{name}` (voice_id=`{voice_id}`). "
        f"Назначьте его через `/voicebot voice-user` или `/voicebot voice-set`.",
        ephemeral=True,
    )


@tts_group.command(name="voice-describe", description="Изменить описание голоса")
@app_commands.describe(name="Имя профиля", text="Новое описание")
@app_commands.autocomplete(name=voice_profile_autocomplete)
async def slash_tts_voice_describe(
    interaction: discord.Interaction, name: str, text: str
) -> None:
    if not await require_guild_manager(interaction):
        return
    name = name.strip().lower()
    rec = bot.voice_registry.get(name)
    if rec is None:
        await interaction.response.send_message(
            f"Голос `{name}` не найден. Список: `/voicebot voices`.", ephemeral=True
        )
        return
    bot.voice_registry.add(replace(rec, description=text.strip()))
    try:
        bot.persist_voice_registry()
    except OSError as exc:
        log.exception("Failed to persist voices.json after voice-describe")
        await interaction.response.send_message(
            f"Не удалось сохранить: {exc}", ephemeral=True
        )
        return
    await interaction.response.send_message(
        f"Описание `{name}` обновлено.", ephemeral=True
    )


def _render_emoji(guild: discord.Guild | None, emoji_id: str, name: str) -> str:
    """Render a stored alias's emoji: prefer the live guild emoji object (so
    animated emoji and exact image render correctly); fall back to a shortcode
    if the emoji was deleted."""
    if guild is not None:
        try:
            live = discord.utils.get(guild.emojis, id=int(emoji_id))
        except (TypeError, ValueError):
            live = None
        if live is not None:
            return str(live)
    return f"`:{name or emoji_id}:`"


async def emoji_alias_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    current = current.lower()
    choices: list[app_commands.Choice[str]] = []
    for emoji_id, entry in bot.config_store.emoji_aliases.items():
        name = entry.get("name") or emoji_id
        say = entry.get("say", "")
        if current in name.lower() or current in say.lower() or current in emoji_id:
            label = f":{name}: → {say}"[:100]
            choices.append(app_commands.Choice(name=label, value=emoji_id))
    return choices[:25]


@tts_group.command(
    name="emoji-alias", description="Задать, как бот произносит кастомный эмодзи"
)
@app_commands.describe(
    emoji="Кастомный эмодзи сервера (или :имя:)",
    pronunciation="Чем его озвучивать",
)
async def slash_emoji_alias(
    interaction: discord.Interaction, emoji: str, pronunciation: str
) -> None:
    if not await require_guild_manager(interaction):
        return
    parsed = parse_custom_emoji_arg(emoji, interaction.guild)
    if parsed is None:
        await interaction.response.send_message(
            "Нужен кастомный эмодзи этого сервера — пришлите сам эмодзи, "
            "токен `<:Имя:123…>` или `:Имя:`. Обычные Unicode-эмодзи и текст "
            "не поддерживаются.",
            ephemeral=True,
        )
        return
    emoji_id, name = parsed
    say = sanitize_pronunciation(pronunciation)
    if not say:
        await interaction.response.send_message(
            "Произношение пустое или состоит только из недопустимых символов.",
            ephemeral=True,
        )
        return
    bot.config_store.set_emoji_alias(emoji_id, name, say)
    rendered = _render_emoji(interaction.guild, emoji_id, name)
    await interaction.response.send_message(
        f"Готово: {rendered} будет озвучиваться как «{say}».", ephemeral=True
    )


@tts_group.command(name="emoji-alias-remove", description="Удалить алиас эмодзи")
@app_commands.describe(emoji="Эмодзи или его текущий алиас")
@app_commands.autocomplete(emoji=emoji_alias_autocomplete)
async def slash_emoji_alias_remove(
    interaction: discord.Interaction, emoji: str
) -> None:
    if not await require_guild_manager(interaction):
        return
    parsed = parse_custom_emoji_arg(emoji, interaction.guild)
    if parsed is not None:
        emoji_id = parsed[0]
    elif emoji.strip().isdigit():  # raw id, e.g. picked from autocomplete
        emoji_id = emoji.strip()
    else:
        await interaction.response.send_message(
            "Не распознан эмодзи. Выберите из списка автодополнения "
            "или пришлите сам эмодзи.",
            ephemeral=True,
        )
        return
    if bot.config_store.remove_emoji_alias(emoji_id):
        await interaction.response.send_message(
            f"Алиас удалён (id `{emoji_id}`).", ephemeral=True
        )
    else:
        await interaction.response.send_message(
            f"Алиаса для id `{emoji_id}` нет.", ephemeral=True
        )


@tts_group.command(name="emoji-aliases", description="Показать заданные алиасы эмодзи")
async def slash_emoji_aliases(interaction: discord.Interaction) -> None:
    if not await require_guild_manager(interaction):
        return
    aliases = bot.config_store.emoji_aliases
    if not aliases:
        await interaction.response.send_message(
            "Алиасы эмодзи не заданы. Добавьте: `/voicebot emoji-alias`.",
            ephemeral=True,
        )
        return
    lines: list[str] = []
    for emoji_id, entry in aliases.items():
        rendered = _render_emoji(interaction.guild, emoji_id, entry.get("name", ""))
        lines.append(f"{rendered} → «{entry.get('say', '')}»")
    await interaction.response.send_message("\n".join(lines)[:1900], ephemeral=True)


@tts_group.command(name="status", description="Показать состояние TTS на сервере")
async def slash_tts_status(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
        return
    config = bot.config_store.get_guild(interaction.guild.id)
    vc = discord.utils.get(bot.voice_clients, guild=interaction.guild)
    channel_name = getattr(getattr(vc, "channel", None), "name", "не подключен") if vc else "не подключен"
    await interaction.response.send_message(
        "\n".join(
            [
                f"Enabled: `{config.enabled}`",
                f"Voice channel: `{channel_name}`",
                f"Queue: `{bot.message_queue.qsize()}`",
                f"Default voice: `{config.default_voice}`",
                f"Allowed users: `{len(config.allowed_users)}`",
                f"Merge: `{TTS_MERGE_SHORT_MESSAGES}`",
            ]
        ),
        ephemeral=True,
    )


@tts_group.command(name="test", description="Проиграть тестовую фразу")
@app_commands.describe(
    text="Текст для проверки",
    voice_channel="Голосовой канал для удаленного запуска",
)
async def slash_tts_test(
    interaction: discord.Interaction,
    text: str,
    voice_channel: discord.VoiceChannel | None = None,
) -> None:
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
        return
    allowed = bot.config_store.is_allowed(interaction.guild.id, interaction.user.id)
    if not allowed and not is_guild_manager(interaction.user):
        await interaction.response.send_message("Вы не добавлены в озвучку.", ephemeral=True)
        return
    target_channel = resolve_tts_command_voice_channel(interaction, voice_channel)
    if target_channel is None:
        await interaction.response.send_message(
            "Выберите голосовой канал или зайдите в него сами.",
            ephemeral=True,
        )
        return
    final_text = normalize_for_tts(text, emoji_aliases=bot.config_store.emoji_say_map()) or ""
    if not final_text:
        await interaction.response.send_message("Нет текста для озвучки.", ephemeral=True)
        return
    await interaction.response.send_message("Тестовая фраза добавлена в очередь.", ephemeral=True)
    await bot.enqueue_tts(final_text, target_channel, interaction.user.id, interaction.channel_id or 0)


@tts_group.command(name="queue-clear", description="Очистить очередь TTS")
async def slash_tts_queue_clear(interaction: discord.Interaction) -> None:
    if not await require_guild_manager(interaction):
        return
    guild = interaction.guild
    assert guild is not None
    bot.clear_merge_buffers(guild.id)
    cleared = bot.clear_queue_for_guild(guild.id)
    await interaction.response.send_message(f"Очередь очищена: {cleared}.", ephemeral=True)


@bot.command()
async def stop(ctx: commands.Context) -> None:
    if ctx.guild is None:
        await ctx.reply("Command can only be used in a guild.", mention_author=False)
        return
    if not isinstance(ctx.author, discord.Member) or not is_guild_manager(ctx.author):
        await ctx.reply("Only server managers can stop TTS.", mention_author=False)
        return

    await bot.disconnect_guild_voice(ctx.guild)
    await ctx.reply("TTS disconnected.", mention_author=False)


@bot.command()
async def join(ctx: commands.Context) -> None:
    if ctx.guild is None:
        await ctx.reply("Command can only be used in a guild.", mention_author=False)
        return
    if not isinstance(ctx.author, discord.Member):
        await ctx.reply("Command can only be used by guild members.", mention_author=False)
        return
    if not bot.config_store.is_allowed(ctx.guild.id, ctx.author.id) and not is_guild_manager(ctx.author):
        await ctx.reply("You are not allowed to use TTS.", mention_author=False)
        return

    if not ctx.author.voice or not isinstance(ctx.author.voice.channel, discord.VoiceChannel):
        await ctx.reply("You must be in a voice channel.", mention_author=False)
        return

    await bot.ensure_voice(ctx.author.voice.channel)
    await ctx.reply("TTS connected.", mention_author=False)


def main() -> None:
    TMP_DIR.mkdir(parents=True, exist_ok=True)

    if not TOKEN:
        raise RuntimeError("DISCORD_TOKEN is not set")
    if not WHITELIST_USERS:
        raise RuntimeError("WHITELIST_USERS is empty")
    if not load_opus():
        raise RuntimeError("Opus is required for Discord voice playback")

    bot.run(TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
