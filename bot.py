# Composition root and backward-compatible facade for the ttsbot package.
#
# This module wires the application together (config reload, bot instance,
# slash commands, event handlers) and re-exports the public API that used
# to live in this file when it was a monolith. The tests exec this file
# per test case and patch some of the module objects imported below
# (asyncio, time, discord), so keep those imports even though the facade
# itself barely uses them.

import asyncio  # noqa: F401  (patched via bot_mod.asyncio in tests)
import logging
import time  # noqa: F401  (patched via bot_mod.time in tests)

import discord

from ttsbot import voice_registry  # noqa: F401  (re-export: tests use bot_mod.voice_registry)
from ttsbot import config
from ttsbot.textnorm import (
    CUSTOM_EMOJI_RE,
    DISCORD_TOKEN_RE,
    EMOJI_MAP,
    EMOJI_PRONUNCIATION_MAX,
    MENTION_ID_RE,
    MENTION_RE,
    SHORTCODE_RE,
    URL_RE,
    _is_emoji_char,
    _is_keyboard_smash_word,
    _strip_discord_tokens_for_speech,
    build_mention_say_map,
    build_mention_say_map_from_guild,
    normalize_for_tts,
    parse_custom_emoji_arg,
    resolve_mentions,
    sanitize_pronunciation,
    speak_unicode_emoji,
    substitute_emoji_aliases,
)
from ttsbot.messages import (
    MergeBufferState,
    ParsedMessage,
    analyze_message_for_merge,
    is_reaction_like,
)
from ttsbot.models import (
    GuildConfig,
    PreparedAudio,
    TTSJob,
    VOICE_PROFILES,
    VoiceProfile,
    reload_voice_profiles,
)
from ttsbot.store import BotConfigStore
from ttsbot.core import TTSBot, intents
from ttsbot import commands as tts_commands
from ttsbot import events as tts_events
from ttsbot.commands import (
    _EMOTION_CHOICES,
    _MODEL_CHOICES,
    _fmt_int,
    _fmt_uptime,
    _render_emoji,
    is_guild_manager,
    require_guild_manager,
    resolve_tts_command_voice_channel,
)
from ttsbot.audio import (
    PCM_CHANNELS,
    PCM_FRAME_BYTES,
    PCM_FRAME_MS,
    PCM_SAMPLE_RATE,
    PCM_SAMPLE_WIDTH,
    ContinuousTTSAudioSource,
    build_idle_pcm_frame,
    build_playback_filter_complex,
    build_playback_prepare_command,
    build_preroll_lavfi_source,
    build_tts_pcm_command,
    build_tts_stream_pcm_command,
    load_opus,
    seconds_from_ms,
    split_pcm_frames,
)

# Re-read the environment on every exec of this file: production gets the
# same env-derived state as before the package split, and each test load
# of bot.py starts from a clean configuration.
config.reload()
config.setup_logging()

log = logging.getLogger("tts_bot")

parse_user_ids = config.parse_user_ids

# Rebuild the hardcoded Piper profiles from the (re)loaded config and
# validate the default profile name.
reload_voice_profiles()

# Snapshot of the env-derived configuration for read-only consumers (the
# tests read constants off this module). Writes that must affect runtime
# behavior go through ``config.NAME`` — code reads config at call time.
globals().update({_k: _v for _k, _v in vars(config).items() if _k.isupper()})


bot = TTSBot()

# Slash commands and prefix commands are built per bot instance (fresh
# command objects bound to *this* bot, matching the old module-global
# layout); events are attached the same way. Everything they return is
# re-exported here for tests and backward compatibility.
_command_ns = tts_commands.build_commands(bot)
bot.tts_group = _command_ns["tts_group"]
globals().update(_command_ns)
globals().update(tts_events.register_events(bot))


def main() -> None:
    config.TMP_DIR.mkdir(parents=True, exist_ok=True)

    if not config.TOKEN:
        raise RuntimeError("DISCORD_TOKEN is not set")
    if not config.WHITELIST_USERS:
        raise RuntimeError("WHITELIST_USERS is empty")
    if not load_opus():
        raise RuntimeError("Opus is required for Discord voice playback")

    bot.run(config.TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
