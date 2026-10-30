"""The cloud TTS providers the bot can use, built from the environment.

Each plugin is a module with ``NAME`` (the provider name its voices use),
``create(store)`` returning the configured provider or None, and
optionally ``seed_voices(registry)`` that adds the default voice its
settings name (FISH_REFERENCE_ID, ELEVENLABS_VOICE_ID). A provider also
answers ``stream_spec(text, voice)`` (ttsbot.streams). Adding a provider
is a new module and a line in PLUGINS; the order is the order /fallback
status lists them in.
"""

from __future__ import annotations

from ttsbot import elevenlabs, fish, gemini, mai, minimax

PLUGINS = (minimax, fish, gemini, mai, elevenlabs)


def seed_voices(registry) -> bool:
    """Let every plugin seed its default voice; True when anything changed."""
    changed = False
    for plugin in PLUGINS:
        seed = getattr(plugin, "seed_voices", None)
        if seed is not None and seed(registry):
            changed = True
    return changed


def create_providers(store) -> dict:
    """Every plugin's provider by name (None when it is not configured)."""
    return {plugin.NAME: plugin.create(store) for plugin in PLUGINS}
