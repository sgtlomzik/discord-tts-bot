"""Core data types shared across the bot: voice profiles and job records."""

import asyncio
import logging
import time
from dataclasses import dataclass, field

import discord

from ttsbot import config

log = logging.getLogger("tts_bot")


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
    default_voice: str = field(default_factory=lambda: config.DEFAULT_VOICE_PROFILE)
    user_voices: dict[int, str] = field(default_factory=dict)
    user_fixed_phrases: dict[int, str] = field(default_factory=dict)


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

    The generation worker fills ``channel`` with batches of 20ms PCM or Opus frames
    (``list[bytes]`` of PCM or raw Opus) and ends it with a ``None`` sentinel. The playback
    worker drains ``channel`` into the continuous player in order. ``cancelled``
    is set by ``queue-clear`` to stop generation/playback of this item.
    ``error`` holds the exception behind a pre-audio stream failure, so the
    caller can count it against the right circuit breaker.
    """
    job: TTSJob
    channel: asyncio.Queue
    cancelled: bool = False
    provider: str = ""
    error: BaseException | None = None


# Hardcoded Piper profiles. This dict is mutated IN PLACE by
# reload_voice_profiles() so that from-imports stay valid across config
# reloads (each exec of bot.py rebuilds it from the environment).
VOICE_PROFILES: dict[str, VoiceProfile] = {}


def reload_voice_profiles() -> None:
    """Rebuild VOICE_PROFILES from config and validate the default profile."""
    VOICE_PROFILES.clear()
    VOICE_PROFILES.update(
        {
            "piper-ruslan": VoiceProfile(
                name="piper-ruslan",
                label="Piper Ruslan",
                piper_model_path=config.PIPER_MODEL_PATH,
                piper_config_path=config.PIPER_CONFIG_PATH,
                piper_speaker=config.PIPER_SPEAKER,
                piper_length_scale=config.PIPER_LENGTH_SCALE,
            ),
            "piper-irina": VoiceProfile(
                name="piper-irina",
                label="Piper Irina",
                piper_model_path=f"{config.PIPER_MODELS_DIR}/ru_RU-irina-medium.onnx",
                piper_config_path=f"{config.PIPER_MODELS_DIR}/ru_RU-irina-medium.onnx.json",
                piper_speaker=config.PIPER_SPEAKER,
                piper_length_scale=config.PIPER_LENGTH_SCALE,
            ),
        }
    )

    if config.DEFAULT_VOICE_PROFILE not in VOICE_PROFILES:
        log.warning(
            "Unknown TTS_DEFAULT_VOICE_PROFILE=%s; using piper-ruslan",
            config.DEFAULT_VOICE_PROFILE,
        )
        config.DEFAULT_VOICE_PROFILE = "piper-ruslan"


reload_voice_profiles()
