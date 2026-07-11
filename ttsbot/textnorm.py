"""Text normalization for TTS: Discord markup, emoji and mention handling.

Everything here is a pure function over strings (plus lightweight guild /
message objects used for mention resolution) — no bot state, no I/O.
"""

import logging
import re
import unicodedata

import discord
import emoji

from ttsbot import config

log = logging.getLogger("tts_bot")

EMOJI_MAP = {
    "Blya2x": "Бля",
    "pepe_sad": "Грустно",
    "kekw": "Кек",
}
CUSTOM_EMOJI_RE = re.compile(r"<a?:([A-Za-z0-9_]+):(\d+)>")
MENTION_RE = re.compile(r"<@!?\d+>|<@&\d+>|<#\d+>")
URL_RE = re.compile(r"https?://\S+|www\.\S+")
DISCORD_TOKEN_RE = re.compile(r"(<a?:[A-Za-z0-9_]+:\d+>|<@!?\d+>|<@&\d+>|<#\d+>|https?://\S+|www\.\S+)")
# A bare shortcode the way a user might type it into a slash-command arg.
SHORTCODE_RE = re.compile(r"^:([A-Za-z0-9_]+):$")
# Upper bound on an emoji pronunciation; long enough for a phrase, short
# enough that a single emoji can't smuggle a wall of text into synthesis.
EMOJI_PRONUNCIATION_MAX = 100

# Mention tokens with a capturing id group, for resolving to a spoken name.
MENTION_ID_RE = re.compile(r"<@!?(\d+)>|<@&(\d+)>|<#(\d+)>")


def parse_custom_emoji_arg(value: str, guild) -> tuple[str, str] | None:
    """Resolve a custom emoji given in a slash-command string arg.

    Accepts a ready token ``<:name:id>`` / ``<a:name:id>`` (animated and
    static share one id-space) or a bare ``:name:`` that is resolved against
    the guild's emojis. Returns ``(emoji_id, name)`` keyed by the globally
    unique, stable **id**. Returns ``None`` for a unicode emoji or plain
    text — the caller rejects those with a clear message.
    """
    if not value:
        return None
    value = value.strip()
    token = CUSTOM_EMOJI_RE.search(value)
    if token:
        return token.group(2), token.group(1)
    short = SHORTCODE_RE.match(value)
    if short and guild is not None:
        name = short.group(1)
        emojis = getattr(guild, "emojis", ()) or ()
        for guild_emoji in emojis:  # exact match first
            if guild_emoji.name == name:
                return str(guild_emoji.id), guild_emoji.name
        lowered = name.lower()
        for guild_emoji in emojis:  # then case-insensitive
            if guild_emoji.name.lower() == lowered:
                return str(guild_emoji.id), guild_emoji.name
    return None


def sanitize_pronunciation(text: str, *, max_chars: int = EMOJI_PRONUNCIATION_MAX) -> str | None:
    """Clean a user-supplied pronunciation before storing it.

    Strips nested Discord tokens (so an alias can't smuggle more emoji or
    mentions), removes control characters, collapses whitespace and caps the
    length. Returns ``None`` when nothing speakable is left.
    """
    if not text:
        return None
    text = DISCORD_TOKEN_RE.sub(" ", text)
    text = "".join(ch for ch in text if ch == " " or unicodedata.category(ch)[0] != "C")
    text = " ".join(text.split()).strip()
    if not text:
        return None
    if len(text) > max_chars:
        text = text[:max_chars].rstrip()
    return text or None


def substitute_emoji_aliases(text: str, aliases: dict[str, str]) -> str:
    """Replace custom-emoji tokens whose id has an alias with its spoken word.

    The word is padded with spaces so neighbours don't fuse (``да<:Kekis:1>``
    -> ``да кекис``). Tokens without an alias are left untouched for the
    normal stripping step to remove (default = silence).
    """
    if not text or not aliases:
        return text

    def _repl(match: re.Match[str]) -> str:
        say = aliases.get(match.group(2))
        return f" {say} " if say else match.group(0)

    return CUSTOM_EMOJI_RE.sub(_repl, text)


def build_mention_say_map(message: discord.Message) -> dict[str, str]:
    """Map mentioned ids -> spoken name from a live message's resolved
    mention lists: users by display name (nickname), roles by name, channels
    as "канал <name>". Keyed by id so it survives later renames.
    """
    say: dict[str, str] = {}
    for user in getattr(message, "mentions", ()) or ():
        say[str(user.id)] = getattr(user, "display_name", None) or user.name
    for role in getattr(message, "role_mentions", ()) or ():
        say[str(role.id)] = role.name
    for channel in getattr(message, "channel_mentions", ()) or ():
        say[str(channel.id)] = f"канал {channel.name}"
    return say


def build_mention_say_map_from_guild(text: str, guild) -> dict[str, str]:
    """Resolve only the mention tokens present in ``text`` against a guild —
    used on the /voicebot test path, where there is no message object."""
    if not text or guild is None:
        return {}
    say: dict[str, str] = {}
    for match in MENTION_ID_RE.finditer(text):
        if match.group(1):
            member = guild.get_member(int(match.group(1)))
            if member is not None:
                say[match.group(1)] = getattr(member, "display_name", None) or member.name
        elif match.group(2):
            role = guild.get_role(int(match.group(2)))
            if role is not None:
                say[match.group(2)] = role.name
        elif match.group(3):
            channel = guild.get_channel(int(match.group(3)))
            if channel is not None:
                say[match.group(3)] = f"канал {channel.name}"
    return say


def resolve_mentions(text: str, mention_names: dict[str, str]) -> str:
    """Replace mention tokens with their spoken name (space-padded). Tokens
    without a resolved name are left untouched for the normal stripping step
    to remove — so unresolved mentions stay silent, as before.
    """
    if not text or not mention_names:
        return text

    def _repl(match: re.Match[str]) -> str:
        mention_id = match.group(1) or match.group(2) or match.group(3)
        name = mention_names.get(mention_id)
        return f" {name} " if name else match.group(0)

    return MENTION_ID_RE.sub(_repl, text)


def speak_unicode_emoji(text: str) -> str:
    """Replace standard Unicode emoji with their Russian names so the bot
    reads them aloud (😂 -> "смеется до слез", 🇷🇺 -> "флаг Россия").

    Each emoji is named individually via its position, so underscores in the
    surrounding user text (``люблю_тебя``) are never touched. Skin-tone
    modifiers, flags and ZWJ sequences (families) resolve as one unit. Falls
    back to the English name for the rare emoji without a Russian translation;
    leaves the emoji as-is only if it has no name at all.
    """
    if not text:
        return text
    matches = emoji.emoji_list(text)
    if not matches:
        return text
    out: list[str] = []
    last = 0
    for match in matches:
        out.append(text[last : match["match_start"]])
        char = match["emoji"]
        name = emoji.demojize(char, language="ru", delimiters=("", ""))
        if name == char:  # no Russian name; try English
            name = emoji.demojize(char, language="en", delimiters=("", ""))
        name = name.replace("_", " ").strip()
        out.append(f" {name} " if name and name != char else " ")
        last = match["match_end"]
    out.append(text[last:])
    return "".join(out)


# Single source of truth for the cleanup applied just before handing
# text to either TTS provider. Returns None when there is nothing
# speakable left, so the caller can drop the message entirely
# (matches the spec: 186/3358 messages were empty after cleanup).
def normalize_for_tts(
    raw: str,
    *,
    max_chars: int | None = None,
    emoji_aliases: dict[str, str] | None = None,
    mentions: dict[str, str] | None = None,
) -> str | None:
    """Strip Discord markup from a message and prepare it for synthesis.

    Removes:
      - custom emoji markup ``<:name:id>`` and ``<a:name:id>``
      - user/role mentions ``<@id>``, ``<@!id>``, ``<@&id>``
      - channel mentions ``<#id>``
      - URLs ``https?://...``

    Also normalizes whitespace (``\\n`` -> ``". "``, multiple spaces
    collapsed) and enforces an upper bound on the resulting length.
    Returns ``None`` if the cleaned string is empty (caller should
    drop the message — do not waste an API call or queue slot).
    """
    if not raw:
        return None
    text = raw

    # Emoji aliases first: turn aliased custom emoji into their spoken word
    # *before* the strip regex removes the rest. Unaliased emoji stay silent.
    if emoji_aliases:
        text = substitute_emoji_aliases(text, emoji_aliases)
    # Custom emoji: <:name:id> and <a:name:id>
    text = re.sub(r"<a?:\w+:\d+>", " ", text)
    # Standard Unicode emoji -> spoken Russian names
    text = speak_unicode_emoji(text)
    # Mentions -> spoken name; unresolved tokens fall through to stripping
    if mentions:
        text = resolve_mentions(text, mentions)
    # User/role mentions
    text = re.sub(r"<@!?\d+>", " ", text)
    text = re.sub(r"<@&\d+>", " ", text)
    # Channel mentions
    text = re.sub(r"<#\d+>", " ", text)
    # URLs
    text = re.sub(r"https?://\S+", " ", text)

    # Newlines -> period for more natural speech rhythm
    text = text.replace("\n", ". ")
    # Collapse whitespace
    text = " ".join(text.split())
    text = text.strip()

    if not text:
        return None

    limit = max_chars if max_chars is not None else config.MAX_TEXT_LENGTH
    if limit and len(text) > limit:
        text = text[:limit].rstrip()
        if not text:
            return None
    return text


def _is_emoji_char(ch: str) -> bool:
    if not ch:
        return False
    if "\u2600" <= ch <= "\u27BF":
        return True
    if "\U0001F300" <= ch <= "\U0001FAFF":
        return True
    return unicodedata.category(ch) == "So"


def _is_keyboard_smash_word(word: str) -> bool:
    cleaned = re.sub(r"[^A-Za-zА-Яа-яЁё]", "", word)
    if len(cleaned) < 4:
        return False
    unique_chars = len(set(cleaned.lower()))
    return unique_chars <= 3 and len(cleaned) >= 5


def _strip_discord_tokens_for_speech(
    raw_text: str,
    aliases: dict[str, str] | None = None,
    mentions: dict[str, str] | None = None,
) -> str:
    aliases = aliases or {}

    def _emoji_repl(m: re.Match[str]) -> str:
        # id-keyed alias wins over the legacy name-keyed EMOJI_MAP fallback;
        # an emoji with neither becomes silence, as before.
        say = aliases.get(m.group(2))
        if say:
            return f" {say} "
        return f" {EMOJI_MAP.get(m.group(1), '')} "

    text = CUSTOM_EMOJI_RE.sub(_emoji_repl, raw_text)
    text = speak_unicode_emoji(text)
    if mentions:
        text = resolve_mentions(text, mentions)
    text = MENTION_RE.sub(" ", text)
    text = URL_RE.sub(" ", text)
    text = text.replace("\n", ". ")
    text = " ".join(text.split())
    return text.strip()
