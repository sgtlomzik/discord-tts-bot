"""Persistent per-guild settings (data/config.json).

BotConfigStore keeps the enable flag, allowed users, default and per-user
voices, per-user fixed phrases and custom-emoji pronunciation aliases.
Plain JSON on disk, written atomically — intentionally not a database.
"""

import json
import logging
import tempfile
from pathlib import Path

from ttsbot import config
from ttsbot.models import GuildConfig, VOICE_PROFILES

log = logging.getLogger("tts_bot")


class BotConfigStore:
    def __init__(self, path: Path, fallback_users: set[int], voice_registry=None) -> None:
        self.path = path
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
        self.load()

    def _is_valid_voice(self, name: str) -> bool:
        if self.voice_registry is not None:
            return name in self.voice_registry
        return name in VOICE_PROFILES

    def load(self) -> None:
        if not self.path.exists():
            return

        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            log.exception("Failed to read bot config: %s", self.path)
            return

        guilds = data.get("guilds", {}) if isinstance(data, dict) else {}
        for guild_id_raw, raw_config in guilds.items():
            try:
                guild_id = int(guild_id_raw)
            except (TypeError, ValueError):
                continue
            if not isinstance(raw_config, dict):
                continue
            allowed_users = {
                int(user_id)
                for user_id in raw_config.get("allowed_users", [])
                if str(user_id).isdigit()
            }
            user_voices = {
                int(user_id): voice
                for user_id, voice in raw_config.get("user_voices", {}).items()
                if str(user_id).isdigit() and self._is_valid_voice(voice)
            }
            default_voice = raw_config.get("default_voice", config.DEFAULT_VOICE_PROFILE)
            if not self._is_valid_voice(default_voice):
                default_voice = config.DEFAULT_VOICE_PROFILE
            user_fixed_phrases = {
                int(user_id): phrase
                for user_id, phrase in raw_config.get("user_fixed_phrases", {}).items()
                if str(user_id).isdigit() and isinstance(phrase, str) and phrase.strip()
            }
            self.guilds[guild_id] = GuildConfig(
                enabled=bool(raw_config.get("enabled", True)),
                allowed_users=allowed_users,
                default_voice=default_voice,
                user_voices=user_voices,
                user_fixed_phrases=user_fixed_phrases,
            )

        raw_aliases = data.get("emoji_aliases", {}) if isinstance(data, dict) else {}
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

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "version": 1,
            "guilds": {
                str(guild_id): {
                    "enabled": config.enabled,
                    "allowed_users": sorted(config.allowed_users),
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
        }
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=str(self.path.parent),
            delete=False,
        ) as tmp_file:
            json.dump(data, tmp_file, ensure_ascii=False, indent=2)
            tmp_file.write("\n")
            tmp_path = Path(tmp_file.name)
        tmp_path.replace(self.path)

    def get_guild(self, guild_id: int) -> GuildConfig:
        config = self.guilds.get(guild_id)
        if config is None:
            config = GuildConfig(allowed_users=set(self.fallback_users))
            self.guilds[guild_id] = config
        return config

    def is_enabled(self, guild_id: int) -> bool:
        return self.get_guild(guild_id).enabled

    def is_allowed(self, guild_id: int, user_id: int) -> bool:
        return user_id in self.get_guild(guild_id).allowed_users

    def add_user(self, guild_id: int, user_id: int) -> None:
        self.get_guild(guild_id).allowed_users.add(user_id)
        self.save()

    def remove_user(self, guild_id: int, user_id: int) -> None:
        config = self.get_guild(guild_id)
        config.allowed_users.discard(user_id)
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
