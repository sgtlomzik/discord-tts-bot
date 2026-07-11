"""Discord TTS bot application package.

``bot.py`` at the repository root is the composition root / entrypoint:
it reloads :mod:`ttsbot.config` from the environment, builds the bot
instance, registers events and slash commands, and re-exports the public
API for tests and backward compatibility.
"""
