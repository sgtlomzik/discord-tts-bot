"""Environment-derived runtime configuration.

All tunables live as UPPER_CASE module globals so the rest of the package
(and the tests) can read *and mutate* them at call time via
``config.NAME``. ``reload()`` re-reads everything from the environment;
``bot.py`` calls it on every exec so each test module load starts from a
clean, env-derived state (matching the old single-file behavior where
re-executing bot.py re-parsed the environment).
"""

import logging
import os
from pathlib import Path

LOG_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"

log = logging.getLogger("tts_bot")

# No implicit whitelist: WHITELIST_USERS must be set (main() fails fast
# otherwise), so a public build never ships with someone's personal id.
DEFAULT_WHITELIST = ""


def setup_logging() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(), format=LOG_FORMAT)


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


def _flag(name: str, default: str) -> bool:
    return os.getenv(name, default).strip().lower() not in {"0", "false", "no"}


def reload() -> None:
    """(Re)read every tunable from the environment into module globals."""
    global TMP_DIR, BOT_CONFIG_PATH, VOICES_REGISTRY_PATH, DEFAULT_VOICE_PROFILE
    global TOKEN, MAX_TEXT_LENGTH, TTS_MAX_CHARS, QUEUE_MAXSIZE
    global TTS_PREROLL_MS, TTS_PREROLL_MODE, TTS_PREROLL_VOLUME_DB, TTS_SILENCE_TAIL_MS
    global TTS_CONTINUOUS_STREAM, TTS_STREAMING_ENABLED, TTS_STREAM_TTFA_TIMEOUT
    global TTS_PREFETCH_ENABLED, TTS_PREFETCH_LOOKAHEAD
    global TTS_IDLE_FRAME_MODE, TTS_IDLE_VOLUME_DB, TTS_STREAM_TAIL_MS
    global TTS_MAX_CONTINUOUS_IDLE_SECONDS, IDLE_DISCONNECT_SECONDS
    global TTS_AUTO_CONNECT_ENABLED, AUTO_CONNECT_SUPPRESS_SECONDS
    global TTS_TRIM_SILENCE, FFMPEG_LOW_DELAY
    global TTS_MERGE_SHORT_MESSAGES, TTS_MERGE_MAX_CHARS, TTS_MERGE_WINDOW_MS
    global TTS_MERGE_MAX_PARTS, TTS_MERGE_ALGORITHM
    global TTS_SELECTIVE_HOLD_ENABLED, TTS_SELECTIVE_HOLD_HARD_CAP_MS
    global TTS_SELECTIVE_HOLD_START_EFFECTIVE_LEN, TTS_SELECTIVE_HOLD_START_MIN_WORDS_ALT
    global TTS_SELECTIVE_HOLD_START_MIN_EFFECTIVE_LEN_ALT, TTS_SELECTIVE_HOLD_REACTION_PAUSE_MS
    global TTS_SELECTIVE_HOLD_MAX_PARTS, TTS_SELECTIVE_HOLD_MAX_GROUP_EFFECTIVE_LEN
    global TTS_SELECTIVE_HOLD_JOIN_SEPARATOR, TTS_SELECTIVE_HOLD_DROP_URL_ONLY
    global TTS_SELECTIVE_HOLD_DROP_MENTION_ONLY, TTS_SELECTIVE_HOLD_LOG_DECISIONS
    global TTS_SELECTIVE_HOLD_ENABLE_ORDER_PRESERVING_FLUSH, TTS_QUEUE_PUT_TIMEOUT_MS
    global VOICE_CONNECT_COOLDOWN_SECONDS
    global PIPER_MODELS_DIR, PIPER_MODEL_PATH, PIPER_CONFIG_PATH
    global PIPER_SPEAKER, PIPER_LENGTH_SCALE
    global WHITELIST_USERS

    TMP_DIR = Path(os.getenv("TTS_TMP_DIR", "/dev/shm"))
    BOT_CONFIG_PATH = Path(os.getenv("BOT_CONFIG_PATH", "/app/data/config.json"))
    # Catalog of available voices (Piper + MiniMax). Lives in the mounted
    # ./data volume next to config.json; seeded on first start. This is the
    # CATALOG only — selection state stays in BotConfigStore/config.json.
    VOICES_REGISTRY_PATH = Path(
        os.getenv("VOICES_REGISTRY_PATH", str(BOT_CONFIG_PATH.parent / "voices.json"))
    )
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
    TTS_CONTINUOUS_STREAM = _flag("TTS_CONTINUOUS_STREAM", "1")
    # Stream MiniMax audio chunk-by-chunk so the bot starts talking before the
    # whole clip is generated (cuts Time-To-First-Audio). Requires the
    # continuous stream. Feature-flagged for instant revert without a redeploy.
    TTS_STREAMING_ENABLED = _flag("TTS_STREAMING_ENABLED", "1")
    # Budget for the FIRST audio chunk only. After the first chunk the stream
    # lives as long as it needs (long messages legitimately stream for seconds).
    TTS_STREAM_TTFA_TIMEOUT = float(
        os.getenv("TTS_STREAM_TTFA_TIMEOUT", os.getenv("TTS_REQUEST_TIMEOUT", "2.5"))
    )
    # Prefetch: decouple generation from playback so message N+1 is synthesized
    # while N is still playing (cuts queue_wait under bursts). Playback stays
    # strictly sequential FIFO. TTS_PREFETCH_ENABLED=0 reverts to the proven
    # single-worker path (runtime kill-switch). Lookahead = messages generated
    # ahead (1 is plenty; playback serializes anyway).
    TTS_PREFETCH_ENABLED = _flag("TTS_PREFETCH_ENABLED", "1")
    TTS_PREFETCH_LOOKAHEAD = max(1, int(os.getenv("TTS_PREFETCH_LOOKAHEAD", "1")))
    TTS_IDLE_FRAME_MODE = os.getenv("TTS_IDLE_FRAME_MODE", "silence").strip().lower()
    TTS_IDLE_VOLUME_DB = float(os.getenv("TTS_IDLE_VOLUME_DB", "-60"))
    TTS_STREAM_TAIL_MS = int(os.getenv("TTS_STREAM_TAIL_MS", "200"))
    TTS_MAX_CONTINUOUS_IDLE_SECONDS = int(os.getenv("TTS_MAX_CONTINUOUS_IDLE_SECONDS", "900"))
    IDLE_DISCONNECT_SECONDS = int(os.getenv("TTS_IDLE_DISCONNECT_SECONDS", "60"))
    TTS_AUTO_CONNECT_ENABLED = _flag("TTS_AUTO_CONNECT_ENABLED", "1")
    AUTO_CONNECT_SUPPRESS_SECONDS = int(os.getenv("TTS_AUTO_CONNECT_SUPPRESS_SECONDS", "30"))
    TTS_TRIM_SILENCE = _flag("TTS_TRIM_SILENCE", "1")
    FFMPEG_LOW_DELAY = _flag("FFMPEG_LOW_DELAY", "1")
    TTS_MERGE_SHORT_MESSAGES = _flag("TTS_MERGE_SHORT_MESSAGES", "1")
    TTS_MERGE_MAX_CHARS = int(os.getenv("TTS_MERGE_MAX_CHARS", "40"))
    TTS_MERGE_WINDOW_MS = int(os.getenv("TTS_MERGE_WINDOW_MS", "900"))
    TTS_MERGE_MAX_PARTS = int(os.getenv("TTS_MERGE_MAX_PARTS", "4"))
    TTS_MERGE_ALGORITHM = os.getenv("TTS_MERGE_ALGORITHM", "legacy").strip().lower()
    if TTS_MERGE_ALGORITHM not in {"legacy", "selective_hold_v2", "off"}:
        log.warning("Unknown TTS_MERGE_ALGORITHM=%s; using legacy", TTS_MERGE_ALGORITHM)
        TTS_MERGE_ALGORITHM = "legacy"
    TTS_SELECTIVE_HOLD_ENABLED = _flag("TTS_SELECTIVE_HOLD_ENABLED", "1")
    TTS_SELECTIVE_HOLD_HARD_CAP_MS = int(os.getenv("TTS_SELECTIVE_HOLD_HARD_CAP_MS", "1200"))
    TTS_SELECTIVE_HOLD_START_EFFECTIVE_LEN = int(
        os.getenv("TTS_SELECTIVE_HOLD_START_EFFECTIVE_LEN", "10")
    )
    TTS_SELECTIVE_HOLD_START_MIN_WORDS_ALT = int(
        os.getenv("TTS_SELECTIVE_HOLD_START_MIN_WORDS_ALT", "2")
    )
    TTS_SELECTIVE_HOLD_START_MIN_EFFECTIVE_LEN_ALT = int(
        os.getenv("TTS_SELECTIVE_HOLD_START_MIN_EFFECTIVE_LEN_ALT", "6")
    )
    TTS_SELECTIVE_HOLD_REACTION_PAUSE_MS = int(
        os.getenv("TTS_SELECTIVE_HOLD_REACTION_PAUSE_MS", "5000")
    )
    TTS_SELECTIVE_HOLD_MAX_PARTS = int(os.getenv("TTS_SELECTIVE_HOLD_MAX_PARTS", "3"))
    TTS_SELECTIVE_HOLD_MAX_GROUP_EFFECTIVE_LEN = int(
        os.getenv("TTS_SELECTIVE_HOLD_MAX_GROUP_EFFECTIVE_LEN", "56")
    )
    TTS_SELECTIVE_HOLD_JOIN_SEPARATOR = os.getenv("TTS_SELECTIVE_HOLD_JOIN_SEPARATOR", ", ")
    TTS_SELECTIVE_HOLD_DROP_URL_ONLY = _flag("TTS_SELECTIVE_HOLD_DROP_URL_ONLY", "1")
    TTS_SELECTIVE_HOLD_DROP_MENTION_ONLY = _flag("TTS_SELECTIVE_HOLD_DROP_MENTION_ONLY", "1")
    TTS_SELECTIVE_HOLD_LOG_DECISIONS = _flag("TTS_SELECTIVE_HOLD_LOG_DECISIONS", "0")
    TTS_SELECTIVE_HOLD_ENABLE_ORDER_PRESERVING_FLUSH = _flag(
        "TTS_SELECTIVE_HOLD_ENABLE_ORDER_PRESERVING_FLUSH", "1"
    )
    TTS_QUEUE_PUT_TIMEOUT_MS = int(os.getenv("TTS_QUEUE_PUT_TIMEOUT_MS", "500"))

    VOICE_CONNECT_COOLDOWN_SECONDS = int(os.getenv("VOICE_CONNECT_COOLDOWN_SECONDS", "60"))
    # One knob for bare-metal runs: point PIPER_MODELS_DIR at ./models and
    # the per-voice defaults follow. Explicit *_PATH values still win.
    PIPER_MODELS_DIR = os.getenv("PIPER_MODELS_DIR", "/app/models").strip().rstrip("/") or "/app/models"
    PIPER_MODEL_PATH = os.getenv(
        "PIPER_MODEL_PATH", f"{PIPER_MODELS_DIR}/ru_RU-ruslan-medium.onnx"
    ).strip()
    PIPER_CONFIG_PATH = os.getenv("PIPER_CONFIG_PATH", f"{PIPER_MODEL_PATH}.json").strip()
    PIPER_SPEAKER = int(os.getenv("PIPER_SPEAKER", "-1"))
    PIPER_LENGTH_SCALE = float(os.getenv("PIPER_LENGTH_SCALE", "1.0"))

    if TTS_PREROLL_MODE not in {"noise", "sine", "silence"}:
        log.warning("Unknown TTS_PREROLL_MODE=%s; using noise", TTS_PREROLL_MODE)
        TTS_PREROLL_MODE = "noise"

    if TTS_IDLE_FRAME_MODE not in {"comfort_noise", "silence"}:
        log.warning("Unknown TTS_IDLE_FRAME_MODE=%s; using comfort_noise", TTS_IDLE_FRAME_MODE)
        TTS_IDLE_FRAME_MODE = "comfort_noise"

    WHITELIST_USERS = parse_user_ids(os.getenv("WHITELIST_USERS", DEFAULT_WHITELIST))


reload()
