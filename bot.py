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
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import discord
from discord import app_commands
from discord.ext import commands

try:
    import httpx
except Exception:  # pragma: no cover - optional dependency until Supertonic is enabled
    httpx = None

try:
    from piper import PiperVoice, SynthesisConfig
except Exception:  # pragma: no cover - optional dependency
    PiperVoice = None
    SynthesisConfig = None


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
DEFAULT_WHITELIST = "441612025286885397"
DEFAULT_VOICE_PROFILE = os.getenv("TTS_DEFAULT_VOICE_PROFILE", "piper-ruslan").strip()

TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
MAX_TEXT_LENGTH = int(os.getenv("TTS_MAX_TEXT_LENGTH", "500"))
QUEUE_MAXSIZE = int(os.getenv("TTS_QUEUE_MAXSIZE", "50"))
TTS_PREROLL_MS = int(os.getenv("TTS_PREROLL_MS", os.getenv("TTS_START_PAD_MS", "250")))
TTS_PREROLL_MODE = os.getenv("TTS_PREROLL_MODE", "silence").strip().lower()
TTS_PREROLL_VOLUME_DB = float(os.getenv("TTS_PREROLL_VOLUME_DB", "-90"))
TTS_SILENCE_TAIL_MS = int(os.getenv("TTS_SILENCE_TAIL_MS", "200"))
TTS_CONTINUOUS_STREAM = os.getenv("TTS_CONTINUOUS_STREAM", "1").strip().lower() not in {"0", "false", "no"}
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
PIPER_MODEL_PATH = os.getenv("PIPER_MODEL_PATH", "").strip()
PIPER_CONFIG_PATH = os.getenv("PIPER_CONFIG_PATH", "").strip()
PIPER_SPEAKER = int(os.getenv("PIPER_SPEAKER", "-1"))
PIPER_LENGTH_SCALE = float(os.getenv("PIPER_LENGTH_SCALE", "1.0"))
SUPERTONIC_ENABLED = os.getenv("SUPERTONIC_ENABLED", "0").strip().lower() not in {"0", "false", "no"}
SUPERTONIC_BASE_URL = os.getenv("SUPERTONIC_BASE_URL", "http://127.0.0.1:7788").strip().rstrip("/")
SUPERTONIC_TIMEOUT_SECONDS = float(os.getenv("SUPERTONIC_TIMEOUT_SECONDS", "20"))
SUPERTONIC_CONNECT_TIMEOUT_SECONDS = float(os.getenv("SUPERTONIC_CONNECT_TIMEOUT_SECONDS", "3"))
SUPERTONIC_MAX_TEXT_CHARS = int(os.getenv("SUPERTONIC_MAX_TEXT_CHARS", "500"))
SUPERTONIC_MAX_CONCURRENCY = int(os.getenv("SUPERTONIC_MAX_CONCURRENCY", "1"))
SUPERTONIC_DEFAULT_VOICE = os.getenv("SUPERTONIC_DEFAULT_VOICE", "M1").strip()
SUPERTONIC_DEFAULT_LANG = os.getenv("SUPERTONIC_DEFAULT_LANG", "ru").strip()
SUPERTONIC_DEFAULT_STEPS = int(os.getenv("SUPERTONIC_DEFAULT_STEPS", "8"))
SUPERTONIC_DEFAULT_SPEED = float(os.getenv("SUPERTONIC_DEFAULT_SPEED", "1.05"))
SUPERTONIC_RESPONSE_FORMAT = os.getenv("SUPERTONIC_RESPONSE_FORMAT", "wav").strip().lower()
TTS_SUPERTONIC_FALLBACK_TO_PIPER = os.getenv("TTS_SUPERTONIC_FALLBACK_TO_PIPER", "1").strip().lower() not in {
    "0",
    "false",
    "no",
}
TTS_SUPERTONIC_CIRCUIT_BREAKER_FAILURES = int(os.getenv("TTS_SUPERTONIC_CIRCUIT_BREAKER_FAILURES", "3"))
TTS_SUPERTONIC_CIRCUIT_BREAKER_COOLDOWN_SECONDS = int(
    os.getenv("TTS_SUPERTONIC_CIRCUIT_BREAKER_COOLDOWN_SECONDS", "60")
)

EMOJI_MAP = {
    "Blya2x": "Бля",
    "pepe_sad": "Грустно",
    "kekw": "Кек",
}
CUSTOM_EMOJI_RE = re.compile(r"<a?:([A-Za-z0-9_]+):(\d+)>")
MENTION_RE = re.compile(r"<@!?\d+>|<@&\d+>|<#\d+>")
URL_RE = re.compile(r"https?://\S+|www\.\S+")
DISCORD_TOKEN_RE = re.compile(r"(<a?:[A-Za-z0-9_]+:\d+>|<@!?\d+>|<@&\d+>|<#\d+>|https?://\S+|www\.\S+)")


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
    engine: str = "piper"
    lang: str = "ru"
    piper_model_path: str = ""
    piper_config_path: str = ""
    piper_speaker: int = -1
    piper_length_scale: float = 1.0
    supertonic_voice: str = ""
    supertonic_steps: int = SUPERTONIC_DEFAULT_STEPS
    supertonic_speed: float = SUPERTONIC_DEFAULT_SPEED
    supertonic_response_format: str = SUPERTONIC_RESPONSE_FORMAT
    experimental: bool = False


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
        engine="piper",
        lang="ru",
        piper_model_path=PIPER_MODEL_PATH,
        piper_config_path=PIPER_CONFIG_PATH,
        piper_speaker=PIPER_SPEAKER,
        piper_length_scale=PIPER_LENGTH_SCALE,
    ),
    "supertonic-m1-ru": VoiceProfile(
        name="supertonic-m1-ru",
        label="Supertonic M1 RU",
        engine="supertonic",
        lang="ru",
        supertonic_voice="M1",
        supertonic_steps=SUPERTONIC_DEFAULT_STEPS,
        supertonic_speed=SUPERTONIC_DEFAULT_SPEED,
        experimental=True,
    ),
    "supertonic-f1-ru": VoiceProfile(
        name="supertonic-f1-ru",
        label="Supertonic F1 RU",
        engine="supertonic",
        lang="ru",
        supertonic_voice="F1",
        supertonic_steps=SUPERTONIC_DEFAULT_STEPS,
        supertonic_speed=SUPERTONIC_DEFAULT_SPEED,
        experimental=True,
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
    def __init__(self, path: Path, fallback_users: set[int]) -> None:
        self.path = path
        self.fallback_users = set(fallback_users)
        self.guilds: dict[int, GuildConfig] = {}
        self.load()

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
                if str(user_id).isdigit() and voice in VOICE_PROFILES
            }
            default_voice = raw_config.get("default_voice", DEFAULT_VOICE_PROFILE)
            if default_voice not in VOICE_PROFILES:
                default_voice = DEFAULT_VOICE_PROFILE
            self.guilds[guild_id] = GuildConfig(
                enabled=bool(raw_config.get("enabled", True)),
                allowed_users=allowed_users,
                default_voice=default_voice,
                user_voices=user_voices,
            )

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


def _strip_discord_tokens_for_speech(raw_text: str) -> str:
    text = CUSTOM_EMOJI_RE.sub(lambda m: f" {EMOJI_MAP.get(m.group(1), '')} ", raw_text)
    text = MENTION_RE.sub(" ", text)
    text = URL_RE.sub(" ", text)
    text = text.replace("\n", ". ")
    text = " ".join(text.split())
    return text.strip()


def analyze_message_for_merge(raw_text: str) -> ParsedMessage:
    raw_text = raw_text or ""
    raw_length = len(raw_text)
    spoken_text = _strip_discord_tokens_for_speech(raw_text)
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


class TTSEngineError(RuntimeError):
    pass


def _is_trusted_supertonic_base_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme == "http" and parsed.hostname in {"supertonic", "127.0.0.1", "localhost"}


def _write_bytes_atomically(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("wb", dir=str(path.parent), delete=False) as tmp_file:
        tmp_file.write(data)
        tmp_path = Path(tmp_file.name)
    tmp_path.replace(path)


class SupertonicTTSEngine:
    name = "supertonic"

    def __init__(self, client: object | None = None) -> None:
        self.base_url = SUPERTONIC_BASE_URL
        self.semaphore = asyncio.Semaphore(max(SUPERTONIC_MAX_CONCURRENCY, 1))
        self.failure_count = 0
        self.circuit_open_until = 0.0
        if not _is_trusted_supertonic_base_url(self.base_url):
            log.warning("Ignoring untrusted SUPERTONIC_BASE_URL=%s", self.base_url)
            self.client = None
        elif client is not None:
            self.client = client
        elif httpx is not None:
            self.client = httpx.AsyncClient(
                timeout=httpx.Timeout(
                    SUPERTONIC_TIMEOUT_SECONDS,
                    connect=SUPERTONIC_CONNECT_TIMEOUT_SECONDS,
                ),
                follow_redirects=False,
            )
        else:
            self.client = None

    async def close(self) -> None:
        close = getattr(self.client, "aclose", None)
        if close is not None:
            await close()

    def is_circuit_open(self) -> bool:
        if self.circuit_open_until <= 0:
            return False
        if time.monotonic() >= self.circuit_open_until:
            self.circuit_open_until = 0.0
            self.failure_count = 0
            return False
        return True

    def _record_success(self) -> None:
        self.failure_count = 0
        self.circuit_open_until = 0.0

    def _record_failure(self) -> None:
        self.failure_count += 1
        if self.failure_count >= max(TTS_SUPERTONIC_CIRCUIT_BREAKER_FAILURES, 1):
            self.circuit_open_until = time.monotonic() + max(
                TTS_SUPERTONIC_CIRCUIT_BREAKER_COOLDOWN_SECONDS,
                1,
            )

    async def synthesize_to_wav(self, text: str, profile: VoiceProfile, output_path: Path) -> Path:
        if not SUPERTONIC_ENABLED:
            raise TTSEngineError("Supertonic is disabled")
        if self.is_circuit_open():
            raise TTSEngineError("Supertonic circuit breaker is open")
        if self.client is None:
            raise TTSEngineError("httpx is not installed")

        safe_text = text[: max(SUPERTONIC_MAX_TEXT_CHARS, 1)]
        payload = {
            "text": safe_text,
            "voice": profile.supertonic_voice or SUPERTONIC_DEFAULT_VOICE,
            "lang": profile.lang or SUPERTONIC_DEFAULT_LANG,
            "steps": int(profile.supertonic_steps or SUPERTONIC_DEFAULT_STEPS),
            "speed": float(profile.supertonic_speed or SUPERTONIC_DEFAULT_SPEED),
            "response_format": profile.supertonic_response_format or SUPERTONIC_RESPONSE_FORMAT,
        }

        started = time.perf_counter()
        try:
            async with self.semaphore:
                response = await self.client.post(f"{self.base_url}/v1/tts", json=payload)
            response.raise_for_status()
            content_type = response.headers.get("content-type", "")
            content = response.content
            if "audio" not in content_type.lower() and not content.startswith(b"RIFF"):
                raise TTSEngineError(f"Unexpected Supertonic response type: {content_type}")
            if not content.startswith(b"RIFF"):
                raise TTSEngineError("Supertonic response is not a WAV file")
            _write_bytes_atomically(output_path, content)
            self._record_success()
            log.info(
                "TTS engine used: supertonic profile=%s text_len=%s wav_bytes=%s took=%.3fs",
                profile.name,
                len(safe_text),
                output_path.stat().st_size if output_path.exists() else 0,
                time.perf_counter() - started,
            )
            return output_path
        except TTSEngineError:
            self._record_failure()
            raise
        except Exception as exc:
            self._record_failure()
            raise TTSEngineError(f"Supertonic request failed: {type(exc).__name__}") from exc


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
        self.idle_disconnect_tasks: dict[int, asyncio.Task[None]] = {}
        self.continuous_idle_stop_tasks: dict[int, asyncio.Task[None]] = {}
        self.voice_connect_locks: dict[int, asyncio.Lock] = {}
        self.voice_connect_cooldown_until: dict[int, float] = {}
        self.suppress_auto_connect_until: dict[int, float] = {}
        self.config_store = BotConfigStore(BOT_CONFIG_PATH, WHITELIST_USERS)
        self.piper_voices: dict[tuple[str, str], object] = {}
        self.supertonic_engine = SupertonicTTSEngine()
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
        self.worker_task = asyncio.create_task(self.tts_worker(), name="tts-worker")
        if SUPERTONIC_ENABLED:
            asyncio.create_task(self.warmup_supertonic(), name="supertonic-warmup")

    async def close(self) -> None:
        if self.worker_task:
            self.worker_task.cancel()

        for task in self.idle_disconnect_tasks.values():
            task.cancel()
        for task in self.continuous_idle_stop_tasks.values():
            task.cancel()
        for state in self.merge_buffers.values():
            if state.timer_task and not state.timer_task.done():
                state.timer_task.cancel()
        for source in self.continuous_sources.values():
            source.stop()
        await self.supertonic_engine.close()

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

    async def _idle_disconnect_after_timeout(self, guild: discord.Guild) -> None:
        try:
            await asyncio.sleep(IDLE_DISCONNECT_SECONDS)

            vc = discord.utils.get(self.voice_clients, guild=guild)
            if not vc or not vc.is_connected():
                return

            if vc.is_playing() or vc.is_paused():
                log.info("Skip idle disconnect guild=%s reason=playback_active", guild.id)
                return

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
            await self.generate_tts_file("Привет", filename)
            log.info("TTS warmup completed")
        except Exception:
            log.exception("TTS warmup failed")
        finally:
            if filename.exists():
                try:
                    filename.unlink()
                except OSError:
                    log.exception("Failed to remove warmup file: %s", filename)

    async def warmup_supertonic(self) -> None:
        filename = TMP_DIR / f"warmup_supertonic_{uuid.uuid4().hex}.wav"
        try:
            await asyncio.wait_for(
                self.generate_supertonic_file(
                    "Проверка синтеза речи.",
                    filename,
                    VOICE_PROFILES["supertonic-m1-ru"],
                ),
                timeout=max(SUPERTONIC_TIMEOUT_SECONDS, 1.0),
            )
            log.info("Supertonic warmup completed")
        except Exception:
            log.warning("Supertonic warmup failed", exc_info=True)
        finally:
            if filename.exists():
                try:
                    filename.unlink()
                except OSError:
                    log.exception("Failed to remove Supertonic warmup file: %s", filename)

    async def enqueue_tts(
        self,
        text: str,
        voice_channel: discord.VoiceChannel,
        author_id: int,
        text_channel_id: int,
        message_ts: float | None = None,
        voice_profile: str | None = None,
    ) -> bool:
        try:
            now = time.perf_counter()
            selected_profile = voice_profile or self.config_store.voice_for_user(voice_channel.guild.id, author_id)
            job = TTSJob(
                text=text[:MAX_TEXT_LENGTH],
                voice_channel=voice_channel,
                queued_at=now,
                author_id=author_id,
                guild_id=voice_channel.guild.id,
                text_channel_id=text_channel_id,
                voice_profile=selected_profile,
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
                selected_profile,
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
        parsed = analyze_message_for_merge(text)
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

    async def generate_tts_file(self, text: str, filename: Path, voice_profile: str | None = None) -> None:
        profile = VOICE_PROFILES.get(voice_profile or DEFAULT_VOICE_PROFILE, VOICE_PROFILES[DEFAULT_VOICE_PROFILE])
        if profile.engine == "piper":
            await self.generate_piper_file(text, filename, profile)
            log.info("TTS engine used: piper profile=%s", profile.name)
            return

        if profile.engine == "supertonic":
            try:
                await self.generate_supertonic_file(text, filename, profile)
                return
            except Exception as exc:
                log.warning(
                    "tts_fallback engine=supertonic profile=%s fallback=piper-ruslan error_type=%s",
                    profile.name,
                    type(exc).__name__,
                )
                if not TTS_SUPERTONIC_FALLBACK_TO_PIPER:
                    raise TTSEngineError(str(exc)) from exc
                fallback_profile = VOICE_PROFILES["piper-ruslan"]
                await self.generate_piper_file(text, filename, fallback_profile)
                log.info("TTS engine used: piper profile=%s fallback_from=%s", fallback_profile.name, profile.name)
                return

        raise TTSEngineError(f"Unknown TTS engine: {profile.engine}")

    async def generate_supertonic_file(self, text: str, filename: Path, profile: VoiceProfile) -> None:
        await self.supertonic_engine.synthesize_to_wav(text, profile, filename)

    async def tts_worker(self) -> None:
        await self.wait_until_ready()
        await self.warmup_tts()
        log.info("TTS worker started")

        while not self.is_closed():
            job = await self.message_queue.get()
            filename = TMP_DIR / f"tts_{uuid.uuid4().hex}.wav"

            try:
                worker_started = time.perf_counter()

                connect_task = asyncio.create_task(self.ensure_voice(job.voice_channel))
                tts_task = asyncio.create_task(self.generate_tts_file(job.text, filename, job.voice_profile))

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
        if not whitelisted_present and not vc.is_playing() and not vc.is_paused():
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
    profile = VOICE_PROFILES.get(voice_name)
    if profile is None:
        return None
    if profile.engine == "supertonic" and not SUPERTONIC_ENABLED:
        return None
    return voice_name


def voice_profile_status(profile: VoiceProfile) -> str:
    if profile.engine == "supertonic" and not SUPERTONIC_ENABLED:
        return "disabled"
    if profile.engine == "supertonic":
        return "experimental"
    return "available"


async def voice_profile_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    current = current.lower()
    return [
        app_commands.Choice(name=f"{name} - {profile.label}", value=name)
        for name, profile in sorted(VOICE_PROFILES.items())
        if current in name.lower() or current in profile.label.lower()
    ][:25]


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
                f"Supertonic: `{SUPERTONIC_ENABLED}`",
            ]
        ),
        ephemeral=True,
    )


@tts_group.command(name="voices", description="Показать доступные голоса")
async def slash_tts_voices(interaction: discord.Interaction) -> None:
    lines = []
    for name, profile in sorted(VOICE_PROFILES.items()):
        default_mark = " default" if name == DEFAULT_VOICE_PROFILE else ""
        lines.append(
            f"`{name}` - {profile.label} engine=`{profile.engine}` status=`{voice_profile_status(profile)}`{default_mark}"
        )
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@tts_group.command(name="set-voice", description="Установить голос по умолчанию")
@app_commands.describe(profile="Профиль голоса")
@app_commands.autocomplete(profile=voice_profile_autocomplete)
async def slash_tts_set_voice(interaction: discord.Interaction, profile: str) -> None:
    if not await require_guild_manager(interaction):
        return
    guild = interaction.guild
    assert guild is not None
    voice_name = validate_voice_profile(profile)
    if voice_name is None:
        await interaction.response.send_message("Неизвестный или отключенный профиль голоса.", ephemeral=True)
        return
    bot.config_store.set_default_voice(guild.id, voice_name)
    await interaction.response.send_message(f"Профиль по умолчанию: `{voice_name}`.", ephemeral=True)


@tts_group.command(name="set-user-voice", description="Установить голос пользователя")
@app_commands.describe(member="Пользователь", profile="Профиль голоса")
@app_commands.autocomplete(profile=voice_profile_autocomplete)
async def slash_tts_set_user_voice(
    interaction: discord.Interaction,
    member: discord.Member,
    profile: str,
) -> None:
    if not await require_guild_manager(interaction):
        return
    guild = interaction.guild
    assert guild is not None
    voice_name = validate_voice_profile(profile)
    if voice_name is None:
        await interaction.response.send_message("Неизвестный или отключенный профиль голоса.", ephemeral=True)
        return
    bot.config_store.set_user_voice(guild.id, member.id, voice_name)
    await interaction.response.send_message(f"Профиль для {member.mention}: `{voice_name}`.", ephemeral=True)


@tts_group.command(name="reset-voice", description="Вернуть Piper как голос по умолчанию")
async def slash_tts_reset_voice(interaction: discord.Interaction) -> None:
    if not await require_guild_manager(interaction):
        return
    guild = interaction.guild
    assert guild is not None
    bot.config_store.set_default_voice(guild.id, "piper-ruslan")
    await interaction.response.send_message("Профиль по умолчанию возвращен на `piper-ruslan`.", ephemeral=True)


@tts_group.command(name="test", description="Проиграть тестовую фразу")
@app_commands.describe(
    text="Текст для проверки",
    voice_channel="Голосовой канал для удаленного запуска",
    profile="Профиль голоса для разовой проверки",
)
@app_commands.autocomplete(profile=voice_profile_autocomplete)
async def slash_tts_test(
    interaction: discord.Interaction,
    text: str,
    voice_channel: discord.VoiceChannel | None = None,
    profile: str | None = None,
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
    final_text = process_text(text)
    if not final_text:
        await interaction.response.send_message("Нет текста для озвучки.", ephemeral=True)
        return
    voice_name = None
    if profile:
        voice_name = validate_voice_profile(profile)
        if voice_name is None:
            await interaction.response.send_message("Неизвестный или отключенный профиль голоса.", ephemeral=True)
            return
    await interaction.response.send_message("Тестовая фраза добавлена в очередь.", ephemeral=True)
    if voice_name:
        await bot.enqueue_tts(
            final_text,
            target_channel,
            interaction.user.id,
            interaction.channel_id or 0,
            voice_profile=voice_name,
        )
    else:
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
