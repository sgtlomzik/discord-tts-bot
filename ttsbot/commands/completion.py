"""Autocomplete callbacks and voice-name validation shared by the voice commands."""

import logging

import discord
from discord import app_commands

from ttsbot.gemini import GEMINI_VOICE_STYLES, GEMINI_VOICES

log = logging.getLogger("tts_bot")


def register(bot, tts_group, fallback_group, shared: dict) -> dict:
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


    async def voice_id_autocomplete(
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        """Suggest Gemini voice names or the ElevenLabs account's voices."""
        provider = getattr(interaction.namespace, "provider", None)
        current = current.lower()
        if provider == "elevenlabs":
            elevenlabs = bot.tts_dispatcher.provider_for("elevenlabs")
            voices = await elevenlabs.list_voices() if elevenlabs is not None else []
            return [
                app_commands.Choice(name=f"{title} ({voice_id})"[:100], value=voice_id)
                for voice_id, title in voices
                if current in title.lower() or current in voice_id.lower()
            ][:25]
        if provider != "gemini":
            return []
        return [
            app_commands.Choice(name=f"{voice} — {GEMINI_VOICE_STYLES[voice]}", value=voice)
            for voice in GEMINI_VOICES if current in voice.lower()
        ][:25]


    def _configured_model(provider: str, default: str) -> str:
        """The model a configured provider uses, else its default."""
        configured = bot.tts_dispatcher.provider_for(provider)
        return configured.config.model if configured is not None else default

    return dict(locals())
