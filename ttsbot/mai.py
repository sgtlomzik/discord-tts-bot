"""Microsoft MAI-Voice through OpenRouter's /audio/speech endpoint (ttsbot.openrouter).

Enabled by OPENROUTER_API_KEY, the key Gemini uses. OpenRouter streams this
model: raw s16le PCM, 24 kHz mono, first audio in 0.6-1.0 s for
MAI-Voice-2.1-Flash (measured 2026-10-30), and it accepts only ``mp3`` and
``pcm``. A voice id carries its model as a suffix
(``ru-RU-Lev:MAI-Voice-2.1-Flash``), so a voice stores only the short name
and the suffix follows the model it is sent to.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

import httpx

from ttsbot import config as bot_config
from ttsbot import voice_registry
from ttsbot.httpclient import env_setting
from ttsbot.openrouter import DEFAULT_BASE_URL, OpenRouterConfig, OpenRouterSpeechProvider
from ttsbot.streams import StreamSpec

DEFAULT_MODEL = "microsoft/mai-voice-2.1-flash"
DEFAULT_VOICE = "ru-RU-Lev"



def voice_id(voice: str, model: str) -> str:
    """``ru-RU-Lev`` + ``microsoft/mai-voice-2.1-flash`` -> ``ru-RU-Lev:MAI-Voice-2.1-Flash``."""
    if ":" in voice:
        return voice
    parts = model.rsplit("/", 1)[-1].split("-")
    suffix = "-".join(part.upper() if part == "mai" else part.capitalize() for part in parts)
    return f"{voice}:{suffix}"


@dataclass(frozen=True)
class MaiConfig(OpenRouterConfig):
    model: str = DEFAULT_MODEL
    voice: str = DEFAULT_VOICE

    @classmethod
    def from_env(cls) -> "MaiConfig":
        return cls(
            api_key=env_setting("OPENROUTER_API_KEY", ""),
            model=env_setting("MAI_TTS_MODEL", DEFAULT_MODEL),
            voice=env_setting("MAI_TTS_VOICE", DEFAULT_VOICE),
            base_url=env_setting("OPENROUTER_BASE_URL", DEFAULT_BASE_URL),
            app_title=env_setting("OPENROUTER_APP_TITLE", ""),
            referer=env_setting("OPENROUTER_REFERER", ""),
        )

    def cache_key(self, params: object | None = None) -> str:
        """Model and voice change the audio; volume is applied locally, so
        retuning it reuses the cached audio."""
        model = getattr(params, "model", "") or self.model
        data = {
            "provider": "mai",
            "model": model,
            "voice": voice_id(getattr(params, "voice", "") or self.voice, model),
            "format": "pcm",
        }
        return "mai:" + hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


class MaiProvider(OpenRouterSpeechProvider):
    name = "mai"
    label = "MAI"

    def __init__(self, config: MaiConfig, client: httpx.AsyncClient | None = None) -> None:
        super().__init__(config, client)
        self.config: MaiConfig = config

    def request_voice(self, params: object | None) -> str:
        return voice_id(super().request_voice(params), self.request_model(params))

    def stream_spec(self, text: str, voice) -> StreamSpec:
        params = voice.mai
        return StreamSpec(
            kind="pcm", cache_key=self.config.cache_key(params),
            ttfa=bot_config.MAI_TTFA_TIMEOUT, label="MAI", volume_db=params.volume_db,
            open=lambda: self.open_stream(text, params),
        )


NAME = "mai"


def seed_voices(registry) -> bool:
    """Add mai-lev (ru-RU-Lev) once OPENROUTER_API_KEY is set; a voice
    already in the catalog is left as it is. True when it was added."""
    if not env_setting("OPENROUTER_API_KEY", "") or registry.get("mai-lev") is not None:
        return False
    registry.add(voice_registry.VoiceRecord(
        name="mai-lev", label="MAI Лев", description="MAI-Voice: Лев, мужской",
        provider=voice_registry.PROVIDER_MAI, mai=voice_registry.MaiParams(voice=DEFAULT_VOICE),
    ))
    return True


def create(store=None) -> "MaiProvider | None":
    """MAI-Voice through OpenRouter, or None without OPENROUTER_API_KEY."""
    cfg = MaiConfig.from_env()
    return MaiProvider(cfg) if cfg.api_key else None
