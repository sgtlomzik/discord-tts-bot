"""Environment-derived runtime configuration.

All tunables live as UPPER_CASE module globals, read at call time via
``config.NAME``. ``reload()`` re-reads everything from the environment;
``app.create_bot()`` calls it, so each test's bot starts from a clean,
env-derived state. Runtime code never assigns to these (a test enforces
it): settings changed while the bot runs (``/voicebot limit``, Fish
latency) live in ``BotConfigStore``.
"""

import logging
import logging.handlers
import os
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

_N = TypeVar("_N", int, float)

LOG_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"

log = logging.getLogger("tts_bot")

# No implicit whitelist: WHITELIST_USERS must be set (main() fails fast
# otherwise), so a public build never ships with someone's personal id.
DEFAULT_WHITELIST = ""


def setup_logging() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(), format=LOG_FORMAT)
    # The container log is lost when the container is recreated (every
    # deploy), and the latency lines live only there: LOG_FILE keeps a
    # rotating copy, e.g. /app/data/logs/bot.log (LOG_FILE_MAX_MB x
    # LOG_FILE_BACKUPS files).
    log_file = os.getenv("LOG_FILE", "").strip()
    if log_file:
        path = Path(log_file)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            handler = logging.handlers.RotatingFileHandler(
                path,
                maxBytes=_number("LOG_FILE_MAX_MB", 20, int, 1) * 1024 * 1024,
                backupCount=_number("LOG_FILE_BACKUPS", 5, int, 1),
                encoding="utf-8",
            )
        except OSError as exc:
            log.warning("LOG_FILE %s cannot be written (%s); logging to stdout only", path, exc)
        else:
            handler.setFormatter(logging.Formatter(LOG_FORMAT))
            logging.getLogger().addHandler(handler)
    # Every provider request gets a richer "<Provider> HTTP ..." line from
    # ttsbot.httpclient (connection reuse, idle time, header latency).
    logging.getLogger("httpx").setLevel(logging.WARNING)


def parse_user_ids(value: str, name: str = "WHITELIST_USERS") -> set[int]:
    user_ids: set[int] = set()
    for part in value.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            user_ids.add(int(part))
        except ValueError:
            log.warning("Ignoring invalid %s entry: %r", name, part)
    return user_ids


def _flag(name: str, default: str) -> bool:
    return os.getenv(name, default).strip().lower() not in {"0", "false", "no", "off"}


def _number(
    name: str, default: _N, kind: Callable[[str], _N], low: _N | None = None, high: _N | None = None,
) -> _N:
    """A numeric setting; a malformed one logs a warning and uses ``default``."""
    raw = os.getenv(name, "").strip()
    try:
        value = kind(raw) if raw else default
    except ValueError:
        log.warning("Ignoring invalid %s=%r; using %s", name, raw, default)
        value = default
    if low is not None:
        value = max(low, value)
    if high is not None:
        value = min(high, value)
    return value


def provider_max_concurrency(name: str) -> int:
    """Requests allowed at once to provider ``name`` (``FISH_MAX_CONCURRENCY``,
    ``PIPER_MAX_CONCURRENCY``, ...; default 1, at least 1)."""
    return _number(f"{name.upper()}_MAX_CONCURRENCY", 1, int, 1)


def reload() -> None:
    """(Re)read every tunable from the environment into module globals."""
    global TMP_DIR, BOT_CONFIG_PATH, VOICES_REGISTRY_PATH, DEFAULT_VOICE_PROFILE
    global TOKEN, MAX_TEXT_LENGTH, TTS_MAX_CHARS, QUEUE_MAXSIZE
    global TTS_STREAM_TTFA_TIMEOUT
    global FISH_TTFA_TIMEOUT, GEMINI_TTFA_TIMEOUT, GEMINI_TTFA_PER_CHAR
    global ELEVENLABS_TTFA_TIMEOUT, MAI_TTFA_TIMEOUT, TTS_CONNECTION_KEEPALIVE_SECONDS
    global PROVIDER_HEALTH_PATH, TTS_HEALTH_CHECK_INTERVAL, STATE_DB_PATH
    global TTS_PREFETCH_LOOKAHEAD
    global TTS_IDLE_FRAME_MODE, TTS_IDLE_VOLUME_DB, TTS_STREAM_TAIL_MS
    global TTS_MAX_CONTINUOUS_IDLE_SECONDS, IDLE_DISCONNECT_SECONDS
    global TTS_AUTO_CONNECT_ENABLED, AUTO_CONNECT_SUPPRESS_SECONDS
    global TTS_TRIM_SILENCE, TTS_NUMBERS
    global TTS_QUEUE_PUT_TIMEOUT_MS
    global TTS_AUDIO_LIMIT_ENABLED, TTS_AUDIO_CHARS_PER_SECOND
    global TTS_AUDIO_LIMIT_SAFETY, TTS_AUDIO_LIMIT_MIN_SECONDS
    global VOICE_CONNECT_COOLDOWN_SECONDS
    global PIPER_MODELS_DIR, PIPER_MODEL_PATH, PIPER_CONFIG_PATH
    global PIPER_SPEAKER, PIPER_LENGTH_SCALE
    global WHITELIST_USERS, OWNER_IDS
    global RECORDING_ENABLED, RECORDING_GUILD_IDS, RECORDING_DIR, RECORDING_CHUNK_SECONDS
    global RECORDING_IDLE_CLOSE_SECONDS, RECORDING_PAUSE_MS, RECORDING_VAD_AGGRESSIVENESS
    global RECORDING_AUDIO_KEEP_DAYS, RECORDING_TRANSCRIPT_KEEP_DAYS, RECORDING_PRUNE_INTERVAL_HOURS
    global STT_ENABLED, GROQ_API_KEY, STT_MODEL, STT_LANGUAGE, STT_PROMPT
    global STT_AUDIO_SECONDS_PER_HOUR, STT_AUDIO_SECONDS_PER_DAY, STT_MIN_BILLED_SECONDS
    global STT_REQUESTS_PER_MINUTE, TRANSCRIPT_TIMEZONE
    global WEB_HOST, WEB_PORT, WEB_CACHE_DIR, WEB_CACHE_MAX_MB, WEB_STATIC_DIR
    global WEB_USERNAME, WEB_PASSWORD, WEB_CORS_ORIGINS, WEB_MIX_GAP_MS
    global WEB_MAINTENANCE_SECONDS, WEB_PREBUILD_RECENT, WEB_ALLOW_ANONYMOUS

    TMP_DIR = Path(os.getenv("TTS_TMP_DIR", "/dev/shm"))
    # The JSON state files of earlier versions, imported once into
    # STATE_DB_PATH (ttsbot.statedb) and no longer written. BOT_CONFIG_PATH
    # also names the data directory the other paths default into.
    BOT_CONFIG_PATH = Path(os.getenv("BOT_CONFIG_PATH", "/app/data/config.json"))
    VOICES_REGISTRY_PATH = Path(
        os.getenv("VOICES_REGISTRY_PATH", str(BOT_CONFIG_PATH.parent / "voices.json"))
    )
    DEFAULT_VOICE_PROFILE = os.getenv("TTS_DEFAULT_VOICE_PROFILE", "piper-ruslan").strip()

    TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
    MAX_TEXT_LENGTH = _number("TTS_MAX_TEXT_LENGTH", 500, int, 1)
    # Per-message character cap applied at the enqueue boundary, before
    # any provider is invoked. The spec recommends ~300 to protect the
    # cloud API quota from accidental walls of text. Default 300; 0 uses
    # TTS_MAX_TEXT_LENGTH, so a message is always capped.
    TTS_MAX_CHARS = _number("TTS_MAX_CHARS", 300, int, 0) or MAX_TEXT_LENGTH
    QUEUE_MAXSIZE = _number("TTS_QUEUE_MAXSIZE", 50, int)
    # Budget for the FIRST audio chunk only. After the first chunk the stream
    # lives as long as it needs (long messages legitimately stream for seconds).
    TTS_STREAM_TTFA_TIMEOUT = _number(
        "TTS_STREAM_TTFA_TIMEOUT", _number("TTS_REQUEST_TIMEOUT", 2.5, float), float
    )
    # Fish needs its own budget: its first byte takes 0.5-2.3 s in production,
    # so the MiniMax-tuned 2.5 s silently dropped ~3% of Fish messages to Piper.
    FISH_TTFA_TIMEOUT = _number("FISH_TTFA_TIMEOUT", 5.0, float, 0.5)
    # OpenRouter sends Gemini audio only after the whole clip is generated
    # (~1.2 s + ~18 ms per character measured), so the first-audio budget
    # grows with the text: base + per_char * len(text).
    GEMINI_TTFA_TIMEOUT = _number("GEMINI_TTFA_TIMEOUT", 3.0, float, 0.5)
    GEMINI_TTFA_PER_CHAR = _number("GEMINI_TTFA_PER_CHAR", 0.03, float, 0.0)
    # ElevenLabs streams while it generates: first audio measured 0.22-0.26 s
    # (0.66 s on a cold connection) for 2-133 characters, so a flat budget.
    ELEVENLABS_TTFA_TIMEOUT = _number("ELEVENLABS_TTFA_TIMEOUT", 3.0, float, 0.5)
    # OpenRouter streams MAI-Voice-2.1-Flash: first audio measured 0.56-0.99 s
    # for 12-253 characters (2026-10-30), so a flat budget too.
    MAI_TTFA_TIMEOUT = _number("MAI_TTFA_TIMEOUT", 3.0, float, 0.5)
    # While an allowed user sits in the bot's voice channel, ping their cloud
    # provider after this many idle seconds so the TLS connection stays open
    # (a new one costs 0.3-0.5 s through the VPN). ~1 KB per ping, no
    # credits. 0 disables; typing warm-ups still run.
    TTS_CONNECTION_KEEPALIVE_SECONDS = _number("TTS_CONNECTION_KEEPALIVE_SECONDS", 60.0, float, 0.0)
    PROVIDER_HEALTH_PATH = Path(
        os.getenv("TTS_HEALTH_PATH", str(BOT_CONFIG_PATH.parent / "provider_health.json"))
    )
    # Guild settings, the voice catalog and provider pauses: one SQLite file
    # in the mounted ./data volume (":memory:" keeps them in memory only).
    STATE_DB_PATH = Path(os.getenv("STATE_DB_PATH", str(BOT_CONFIG_PATH.parent / "state.db")))
    # Paused providers are checked (quota report, host ping) every
    # TTS_HEALTH_CHECK_INTERVAL seconds once their pause is over.
    TTS_HEALTH_CHECK_INTERVAL = _number("TTS_HEALTH_CHECK_INTERVAL", 30.0, float, 5.0)
    # Prefetch: generation runs ahead of playback so message N+1 is
    # synthesized while N is still playing; playback stays strictly FIFO.
    # Lookahead = messages generated ahead (1 is plenty).
    TTS_PREFETCH_LOOKAHEAD = _number("TTS_PREFETCH_LOOKAHEAD", 1, int, 1)
    TTS_IDLE_FRAME_MODE = os.getenv("TTS_IDLE_FRAME_MODE", "silence").strip().lower()
    TTS_IDLE_VOLUME_DB = _number("TTS_IDLE_VOLUME_DB", -60.0, float)
    TTS_STREAM_TAIL_MS = _number("TTS_STREAM_TAIL_MS", 200, int)
    TTS_MAX_CONTINUOUS_IDLE_SECONDS = _number("TTS_MAX_CONTINUOUS_IDLE_SECONDS", 900, int)
    IDLE_DISCONNECT_SECONDS = _number("TTS_IDLE_DISCONNECT_SECONDS", 60, int)
    TTS_AUTO_CONNECT_ENABLED = _flag("TTS_AUTO_CONNECT_ENABLED", "1")
    AUTO_CONNECT_SUPPRESS_SECONDS = _number("TTS_AUTO_CONNECT_SUPPRESS_SECONDS", 30, int)
    TTS_TRIM_SILENCE = _flag("TTS_TRIM_SILENCE", "1")
    # Numbers in Russian messages spoken as Russian words (ttsbot/numerals.py).
    TTS_NUMBERS = _flag("TTS_NUMBERS", "1")
    TTS_QUEUE_PUT_TIMEOUT_MS = _number("TTS_QUEUE_PUT_TIMEOUT_MS", 500, int)
    # Playback length guard: MiniMax occasionally "stutters" and streams one
    # sound forever. We cap playback at a rough estimate of how long the text
    # SHOULD take to speak: chars / chars-per-second (at voice speed 1.0),
    # scaled by the voice speed, times a safety margin, with a floor for
    # very short texts. Anything past the cap is cut off and the stream is
    # treated as truncated (never cached).
    TTS_AUDIO_LIMIT_ENABLED = _flag("TTS_AUDIO_LIMIT_ENABLED", "1")
    TTS_AUDIO_CHARS_PER_SECOND = _number("TTS_AUDIO_CHARS_PER_SECOND", 12.0, float)
    TTS_AUDIO_LIMIT_SAFETY = _number("TTS_AUDIO_LIMIT_SAFETY", 2.0, float)
    TTS_AUDIO_LIMIT_MIN_SECONDS = _number("TTS_AUDIO_LIMIT_MIN_SECONDS", 5.0, float)

    # Voice recording (ttsbot/recording): off by default. When on, the bot
    # joins voice undeafened and writes every non-bot speaker to Ogg files
    # under RECORDING_DIR, one folder per channel stay. A file closes after
    # RECORDING_CHUNK_SECONDS of speech (at an utterance boundary) or
    # RECORDING_IDLE_CLOSE_SECONDS of that user's silence. WebRTC VAD at
    # RECORDING_VAD_AGGRESSIVENESS (0-3; -1 keeps everything) drops
    # breathing and silence; 3 was the one that dropped loud breathing in
    # the first live test. Longer gaps become RECORDING_PAUSE_MS of silence.
    RECORDING_ENABLED = _flag("RECORDING_ENABLED", "0")
    # Guilds whose voice is recorded (comma-separated ids); empty = every
    # guild the bot joins. Elsewhere the bot joins deafened and only speaks.
    RECORDING_GUILD_IDS = parse_user_ids(os.getenv("RECORDING_GUILD_IDS", ""), "RECORDING_GUILD_IDS")
    RECORDING_DIR = Path(os.getenv("RECORDING_DIR", str(BOT_CONFIG_PATH.parent / "recordings")))
    RECORDING_CHUNK_SECONDS = _number("RECORDING_CHUNK_SECONDS", 600.0, float, 10.0)
    # 15 min: Groq bills every file for at least 10 s, and a 60 s close
    # produced 0.5 s files from a single word.
    RECORDING_IDLE_CLOSE_SECONDS = _number("RECORDING_IDLE_CLOSE_SECONDS", 900.0, float, 5.0)
    RECORDING_PAUSE_MS = _number("RECORDING_PAUSE_MS", 500, int, 0)
    RECORDING_VAD_AGGRESSIVENESS = _number("RECORDING_VAD_AGGRESSIVENESS", 3, int, -1, 3)
    # Retention, run by the STT worker (so it needs the worker) as it starts
    # and then every RECORDING_PRUNE_INTERVAL_HOURS (0 = only at start):
    # transcribed Ogg files older than RECORDING_AUDIO_KEEP_DAYS are deleted,
    # transcripts stay; finished sessions (transcripts and all) older than
    # RECORDING_TRANSCRIPT_KEEP_DAYS are deleted. 0 = keep forever.
    RECORDING_AUDIO_KEEP_DAYS = _number("RECORDING_AUDIO_KEEP_DAYS", 90, int, 0)
    RECORDING_TRANSCRIPT_KEEP_DAYS = _number("RECORDING_TRANSCRIPT_KEEP_DAYS", 0, int, 0)
    RECORDING_PRUNE_INTERVAL_HOURS = _number("RECORDING_PRUNE_INTERVAL_HOURS", 24.0, float, 0.0)
    # Transcription of recorded chunks with Groq Whisper (ttsbot/recording/
    # worker.py); runs while recording is on and GROQ_API_KEY is set. The
    # limits are Groq's free plan; results stay on disk next to the audio.
    GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
    STT_ENABLED = _flag("STT_ENABLED", "1") and bool(GROQ_API_KEY)
    STT_MODEL = os.getenv("STT_MODEL", "whisper-large-v3").strip()
    STT_LANGUAGE = os.getenv("STT_LANGUAGE", "ru").strip()
    STT_PROMPT = os.getenv("STT_PROMPT", "").strip()
    STT_AUDIO_SECONDS_PER_HOUR = _number("STT_AUDIO_SECONDS_PER_HOUR", 7200.0, float, 60.0)
    STT_AUDIO_SECONDS_PER_DAY = _number("STT_AUDIO_SECONDS_PER_DAY", 28800.0, float, 60.0)
    STT_MIN_BILLED_SECONDS = _number("STT_MIN_BILLED_SECONDS", 10.0, float, 0.0)
    STT_REQUESTS_PER_MINUTE = _number("STT_REQUESTS_PER_MINUTE", 20, int, 1)
    TRANSCRIPT_TIMEZONE = os.getenv("TRANSCRIPT_TIMEZONE", "UTC").strip() or "UTC"

    # Recordings website (python -m ttsbot.web, docs/RECORDINGS_WEB.md): a
    # separate process that only reads RECORDING_DIR. Derived audio (the
    # joined session mix, MP3) is cached in WEB_CACHE_DIR, least recently
    # used first out over WEB_CACHE_MAX_MB. WEB_PASSWORD turns on HTTP
    # Basic auth (required unless WEB_ALLOW_ANONYMOUS=1); WEB_CORS_ORIGINS
    # (comma-separated) lets a frontend dev server on another origin call
    # the API. The bot shares this module but never uses these, so a bad
    # value only logs a warning and falls back to the default.
    WEB_HOST = os.getenv("WEB_HOST", "0.0.0.0").strip() or "0.0.0.0"
    WEB_PORT = _number("WEB_PORT", 8095, int, 1, 65535)
    WEB_CACHE_DIR = Path(os.getenv("WEB_CACHE_DIR", "/app/cache"))
    WEB_CACHE_MAX_MB = _number("WEB_CACHE_MAX_MB", 2048, int, 64)
    WEB_STATIC_DIR = os.getenv("WEB_STATIC_DIR", "").strip()
    WEB_USERNAME = os.getenv("WEB_USERNAME", "").strip()
    WEB_PASSWORD = os.getenv("WEB_PASSWORD", "")
    WEB_ALLOW_ANONYMOUS = _flag("WEB_ALLOW_ANONYMOUS", "0")
    WEB_CORS_ORIGINS = tuple(o.strip().rstrip("/") for o in os.getenv("WEB_CORS_ORIGINS", "").split(",") if o.strip())
    WEB_MIX_GAP_MS = _number("WEB_MIX_GAP_MS", 500, int, 0, 5000)
    WEB_MAINTENANCE_SECONDS = _number("WEB_MAINTENANCE_SECONDS", 300.0, float, 30.0)
    WEB_PREBUILD_RECENT = _number("WEB_PREBUILD_RECENT", 3, int, 0)

    VOICE_CONNECT_COOLDOWN_SECONDS = _number("VOICE_CONNECT_COOLDOWN_SECONDS", 60, int)
    # One knob for bare-metal runs: point PIPER_MODELS_DIR at ./models and
    # the per-voice defaults follow. Explicit *_PATH values still win.
    PIPER_MODELS_DIR = os.getenv("PIPER_MODELS_DIR", "/app/models").strip().rstrip("/") or "/app/models"
    PIPER_MODEL_PATH = os.getenv(
        "PIPER_MODEL_PATH", f"{PIPER_MODELS_DIR}/ru_RU-ruslan-medium.onnx"
    ).strip()
    PIPER_CONFIG_PATH = os.getenv("PIPER_CONFIG_PATH", f"{PIPER_MODEL_PATH}.json").strip()
    PIPER_SPEAKER = _number("PIPER_SPEAKER", -1, int)
    PIPER_LENGTH_SCALE = _number("PIPER_LENGTH_SCALE", 1.0, float)

    if TTS_IDLE_FRAME_MODE not in {"comfort_noise", "silence"}:
        log.warning("Unknown TTS_IDLE_FRAME_MODE=%s; using silence", TTS_IDLE_FRAME_MODE)
        TTS_IDLE_FRAME_MODE = "silence"

    WHITELIST_USERS = parse_user_ids(os.getenv("WHITELIST_USERS", DEFAULT_WHITELIST))
    # Who may change settings shared by every guild; the Discord
    # application's owner always may.
    OWNER_IDS = parse_user_ids(os.getenv("OWNER_IDS", ""), "OWNER_IDS")


reload()
