"""Gemini TTS through OpenRouter's /audio/speech endpoint (ttsbot.openrouter).

The model returns raw s16le PCM (24 kHz mono). OpenRouter does not stream
this model: the response headers arrive only once the whole clip is
generated (measured 2026-09-28, docs/internal/GEMINI_OPENROUTER_PLAN.md
§2.1), so the first-audio budget grows with the text.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

import httpx

from ttsbot import config as bot_config
from ttsbot.httpclient import env_setting
from ttsbot.openrouter import (
    DEFAULT_BASE_URL,
    OpenRouterAudioStream,
    OpenRouterAuthError,
    OpenRouterConfig,
    OpenRouterError,
    OpenRouterQuotaExhaustedError,
    OpenRouterRateLimitError,
    OpenRouterRequestError,
    OpenRouterSpeechProvider,
    parse_pcm_content_type,
)
from ttsbot.streams import StreamSpec

__all__ = [
    "DEFAULT_MODEL", "DEFAULT_VOICE", "GEMINI_VOICES", "GEMINI_VOICE_STYLES", "GeminiAudioStream",
    "GeminiAuthError", "GeminiConfig", "GeminiError", "GeminiProvider", "GeminiQuotaExhaustedError",
    "GeminiRateLimitError", "GeminiRequestError", "create", "parse_pcm_content_type",
]

DEFAULT_MODEL = "google/gemini-3.8-flash-lite-tts"
DEFAULT_VOICE = "Kore"

# Prebuilt Gemini TTS voices with Google's style tag and voice gender, for
# autocomplete and profile descriptions. All 30 answered through OpenRouter
# on 2026-09-28; registration still probes, so the list is advisory.
GEMINI_VOICE_STYLES = {
    "Achernar": "мягкий, женский", "Achird": "дружелюбный, мужской",
    "Algenib": "с хрипотцой, мужской", "Algieba": "плавный, мужской",
    "Alnilam": "твёрдый, мужской", "Aoede": "лёгкий, женский",
    "Autonoe": "яркий, женский", "Callirrhoe": "непринуждённый, женский",
    "Charon": "информативный, мужской", "Despina": "плавный, женский",
    "Enceladus": "с придыханием, мужской", "Erinome": "чёткий, женский",
    "Fenrir": "возбуждённый, мужской", "Gacrux": "зрелый, женский",
    "Iapetus": "чёткий, мужской", "Kore": "твёрдый, женский",
    "Laomedeia": "бодрый, женский", "Leda": "юный, женский",
    "Orus": "твёрдый, мужской", "Puck": "бодрый, мужской",
    "Pulcherrima": "напористый, женский", "Rasalgethi": "информативный, мужской",
    "Sadachbia": "живой, мужской", "Sadaltager": "знающий, мужской",
    "Schedar": "ровный, мужской", "Sulafat": "тёплый, женский",
    "Umbriel": "непринуждённый, мужской", "Vindemiatrix": "нежный, женский",
    "Zephyr": "яркий, женский", "Zubenelgenubi": "небрежный, мужской",
}
GEMINI_VOICES = tuple(sorted(GEMINI_VOICE_STYLES))

# The OpenRouter transport's errors under the names the callers know.
GeminiError = OpenRouterError
GeminiAuthError = OpenRouterAuthError
GeminiRequestError = OpenRouterRequestError
GeminiQuotaExhaustedError = OpenRouterQuotaExhaustedError
GeminiRateLimitError = OpenRouterRateLimitError
GeminiAudioStream = OpenRouterAudioStream


@dataclass(frozen=True)
class GeminiConfig(OpenRouterConfig):
    model: str = DEFAULT_MODEL
    voice: str = DEFAULT_VOICE

    @classmethod
    def from_env(cls) -> "GeminiConfig":
        return cls(
            api_key=env_setting("OPENROUTER_API_KEY", ""),
            model=env_setting("GEMINI_TTS_MODEL", DEFAULT_MODEL),
            voice=env_setting("GEMINI_TTS_VOICE", DEFAULT_VOICE),
            base_url=env_setting("OPENROUTER_BASE_URL", DEFAULT_BASE_URL),
            app_title=env_setting("OPENROUTER_APP_TITLE", ""),
            referer=env_setting("OPENROUTER_REFERER", ""),
        )

    def cache_key(self, params: object | None = None) -> str:
        """Include every setting that changes the PCM the API returns.

        Volume is applied locally by PcmFramer, so it is left out: retuning
        the volume reuses the cached audio.
        """
        data = {
            "provider": "gemini",
            "model": getattr(params, "model", "") or self.model,
            "voice": getattr(params, "voice", "") or self.voice,
            "format": "pcm",
        }
        return "gemini:" + hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


class GeminiProvider(OpenRouterSpeechProvider):
    name = "gemini"
    label = "Gemini"

    def __init__(self, config: GeminiConfig, client: httpx.AsyncClient | None = None) -> None:
        super().__init__(config, client)
        self.config: GeminiConfig = config

    def stream_spec(self, text: str, voice) -> StreamSpec:
        """Raw PCM. OpenRouter sends nothing until the whole clip is
        generated, so the first-audio budget grows with the text."""
        params = voice.gemini
        return StreamSpec(
            kind="pcm", cache_key=self.config.cache_key(params),
            ttfa=bot_config.GEMINI_TTFA_TIMEOUT + bot_config.GEMINI_TTFA_PER_CHAR * len(text),
            label="Gemini", volume_db=params.volume_db,
            open=lambda: self.open_stream(text, params),
        )


NAME = "gemini"


def create(store=None) -> "GeminiProvider | None":
    """Gemini TTS through OpenRouter, enabled by OPENROUTER_API_KEY alone;
    voices are added with /voicebot voice-add provider:gemini."""
    cfg = GeminiConfig.from_env()
    return GeminiProvider(cfg) if cfg.api_key else None
