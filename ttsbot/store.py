"""Persistent per-guild settings (the guilds, emoji_aliases and settings
tables of data/state.db, see ttsbot.statedb).

BotConfigStore keeps the enable flag, allowed users, default and per-user
voices, per-user fixed phrases, custom-emoji pronunciation aliases and the
settings changed at runtime. It works on memory and saves everything in
one transaction after each change.
"""

import copy
import logging

from ttsbot import config
from ttsbot.models import GuildConfig, VOICE_PROFILES
from ttsbot.statedb import StateDB

log = logging.getLogger("tts_bot")


def _as_dict(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _user_ids(value: object) -> set[int]:
    return {int(user_id) for user_id in (value if isinstance(value, list) else []) if str(user_id).isdigit()}


def _as_bool(value: object, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return default


class BotConfigStore:
    def __init__(self, db: StateDB, fallback_users: set[int], voice_registry=None) -> None:
        self.db = db
        self.fallback_users = set(fallback_users)
        # Validate stored voice names against the unified registry when
        # available (so MiniMax assignments survive reload); otherwise fall
        # back to the hardcoded Piper profiles (keeps tests that construct
        # the store without a registry working unchanged).
        self.voice_registry = voice_registry
        self.guilds: dict[int, GuildConfig] = {}
        # Custom-emoji pronunciation aliases, keyed by emoji id (globally
        # unique, stable). Value: {"name": <display name>, "say": <spoken>}.
        # TODO(per-guild): aliases are global for now (like voice clones);
        # key by guild_id when per-guild pronunciations are needed.
        self.emoji_aliases: dict[str, dict[str, str]] = {}
        # Runtime-adjustable global settings that override env defaults.
        self.settings: dict[str, int | str] = {}
        self.load()
        # What is on disk now: a failed save() rolls memory back to it.
        self._saved = self._snapshot()

    def _snapshot(self) -> tuple:
        return copy.deepcopy((self.guilds, self.emoji_aliases, self.settings))

    def _is_valid_voice(self, name: str) -> bool:
        if self.voice_registry is not None:
            return name in self.voice_registry
        return name in VOICE_PROFILES

    def _default_voice(self) -> str:
        """TTS_DEFAULT_VOICE_PROFILE, or the registry fallback if it is unknown.

        Fish becomes the default only when the operator sets
        TTS_DEFAULT_VOICE_PROFILE=fish-default; its mere presence in the
        registry must not override the configured default.
        """
        if self._is_valid_voice(config.DEFAULT_VOICE_PROFILE):
            return config.DEFAULT_VOICE_PROFILE
        fallback = getattr(self.voice_registry, "fallback_profile", "")
        return fallback if fallback and self._is_valid_voice(fallback) else config.DEFAULT_VOICE_PROFILE

    def load(self) -> None:
        # Values are checked as they are read: the rows may come from a
        # hand-edited config.json of an earlier version (statedb import).
        data = self.db.load_config()

        guilds = _as_dict(data.get("guilds"))
        for guild_id_raw, raw_config in guilds.items():
            try:
                guild_id = int(guild_id_raw)
            except (TypeError, ValueError):
                continue
            if not isinstance(raw_config, dict):
                continue
            allowed_users = _user_ids(raw_config.get("allowed_users"))
            denied_users = _user_ids(raw_config.get("denied_users"))
            user_voices = {
                int(user_id): voice
                for user_id, voice in _as_dict(raw_config.get("user_voices")).items()
                if str(user_id).isdigit() and isinstance(voice, str) and self._is_valid_voice(voice)
            }
            default_voice = raw_config.get("default_voice", self._default_voice())
            if not isinstance(default_voice, str) or not self._is_valid_voice(default_voice):
                default_voice = self._default_voice()
            user_fixed_phrases = {
                int(user_id): phrase
                for user_id, phrase in _as_dict(raw_config.get("user_fixed_phrases")).items()
                if str(user_id).isdigit() and isinstance(phrase, str) and phrase.strip()
            }
            self.guilds[guild_id] = GuildConfig(
                enabled=_as_bool(raw_config.get("enabled", True)),
                allowed_users=allowed_users,
                denied_users=denied_users,
                default_voice=default_voice,
                user_voices=user_voices,
                user_fixed_phrases=user_fixed_phrases,
            )

        raw_aliases = data.get("emoji_aliases", {})
        emoji_aliases: dict[str, dict[str, str]] = {}
        if isinstance(raw_aliases, dict):
            for emoji_id, entry in raw_aliases.items():
                if not str(emoji_id).isdigit() or not isinstance(entry, dict):
                    continue
                say = entry.get("say")
                if not isinstance(say, str) or not say.strip():
                    continue
                name = entry.get("name")
                emoji_aliases[str(emoji_id)] = {
                    "name": name if isinstance(name, str) else "",
                    "say": say,
                }
        self.emoji_aliases = emoji_aliases

        raw_settings = data.get("settings", {})
        if isinstance(raw_settings, dict):
            max_chars = raw_settings.get("tts_max_chars")
            if isinstance(max_chars, int) and max_chars > 0:
                self.settings["tts_max_chars"] = max_chars
            fish_latency = raw_settings.get("fish_latency")
            if isinstance(fish_latency, str) and fish_latency in {"low", "balanced", "normal"}:
                self.settings["fish_latency"] = fish_latency
            elevenlabs_key = raw_settings.get("elevenlabs_key")
            if isinstance(elevenlabs_key, str) and elevenlabs_key:
                self.settings["elevenlabs_key"] = elevenlabs_key

    def max_chars(self) -> int:
        """The per-message character cap: /voicebot limit, else TTS_MAX_CHARS."""
        value = self.settings.get("tts_max_chars")
        return value if isinstance(value, int) and value > 0 else config.TTS_MAX_CHARS

    def set_tts_max_chars(self, value: int) -> None:
        self.settings["tts_max_chars"] = int(value)
        self.save()

    def set_fish_latency(self, mode: str) -> None:
        if mode not in {"low", "balanced", "normal"}:
            raise ValueError("Fish latency must be low, balanced, or normal")
        self.settings["fish_latency"] = mode
        self.save()

    def set_elevenlabs_key(self, fingerprint: str) -> None:
        """Remember the active ElevenLabs key (its fingerprint, never the key)."""
        self.settings["elevenlabs_key"] = fingerprint
        self.save()

    def save(self) -> None:
        """Write the state; on failure roll memory back to the last save.

        Mutators change memory and then save, so without the rollback a
        failed write would leave settings in effect that are not on disk.
        """
        try:
            self._write()
        except Exception:
            self.guilds, self.emoji_aliases, self.settings = copy.deepcopy(self._saved)
            raise
        self._saved = self._snapshot()

    def _write(self) -> None:
        data = {
            "guilds": {
                str(guild_id): {
                    "enabled": config.enabled,
                    "allowed_users": sorted(config.allowed_users),
                    "denied_users": sorted(config.denied_users),
                    "default_voice": config.default_voice,
                    "user_voices": {
                        str(user_id): voice
                        for user_id, voice in sorted(config.user_voices.items())
                    },
                    "user_fixed_phrases": {
                        str(user_id): phrase
                        for user_id, phrase in sorted(config.user_fixed_phrases.items())
                    },
                }
                for guild_id, config in sorted(self.guilds.items())
            },
            "emoji_aliases": {
                emoji_id: {"name": entry.get("name", ""), "say": entry.get("say", "")}
                for emoji_id, entry in sorted(self.emoji_aliases.items())
            },
            "settings": dict(sorted(self.settings.items())),
        }
        self.db.save_config(data)

    def get_guild(self, guild_id: int) -> GuildConfig:
        config = self.guilds.get(guild_id)
        if config is None:
            # WHITELIST_USERS is checked live in is_allowed(), not copied in:
            # a copy would outlive the user's removal from the env.
            config = GuildConfig(default_voice=self._default_voice())
            self.guilds[guild_id] = config
        return config

    def is_enabled(self, guild_id: int) -> bool:
        return self.get_guild(guild_id).enabled

    def is_allowed(self, guild_id: int, user_id: int) -> bool:
        """The guild's deny wins; then WHITELIST_USERS, then the guild's list."""
        config = self.get_guild(guild_id)
        if user_id in config.denied_users:
            return False
        return user_id in self.fallback_users or user_id in config.allowed_users

    def is_env_user(self, user_id: int) -> bool:
        """Allowed by WHITELIST_USERS in every guild that has not denied them."""
        return user_id in self.fallback_users

    def add_user(self, guild_id: int, user_id: int) -> None:
        config = self.get_guild(guild_id)
        config.allowed_users.add(user_id)
        config.denied_users.discard(user_id)
        self.save()

    def remove_user(self, guild_id: int, user_id: int) -> None:
        """Deny the user in this guild, even one WHITELIST_USERS allows."""
        config = self.get_guild(guild_id)
        config.allowed_users.discard(user_id)
        config.denied_users.add(user_id)
        config.user_voices.pop(user_id, None)
        config.user_fixed_phrases.pop(user_id, None)
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

    def set_user_fixed_phrase(self, guild_id: int, user_id: int, phrase: str) -> None:
        self.get_guild(guild_id).user_fixed_phrases[user_id] = phrase
        self.save()

    def clear_user_fixed_phrase(self, guild_id: int, user_id: int) -> None:
        self.get_guild(guild_id).user_fixed_phrases.pop(user_id, None)
        self.save()

    def fixed_phrase_for_user(self, guild_id: int, user_id: int) -> str | None:
        return self.get_guild(guild_id).user_fixed_phrases.get(user_id)

    def emoji_say_map(self) -> dict[str, str]:
        """id -> spoken word, for substitution during normalization."""
        return {emoji_id: entry["say"] for emoji_id, entry in self.emoji_aliases.items()}

    def set_emoji_alias(self, emoji_id: str, name: str, say: str) -> None:
        self.emoji_aliases[str(emoji_id)] = {"name": name, "say": say}
        self.save()

    def remove_emoji_alias(self, emoji_id: str) -> bool:
        existed = str(emoji_id) in self.emoji_aliases
        self.emoji_aliases.pop(str(emoji_id), None)
        if existed:
            self.save()
        return existed
