"""Message classification for the merge / selective-hold layer.

``analyze_message_for_merge`` turns a raw Discord message into a
:class:`ParsedMessage` — a cheap structural profile (emoji/mention/url
counts, shout/keyboard-smash flags, effective length) that the merge
decision code uses before any synthesis work is spent.
"""

import asyncio
import re
import unicodedata
from dataclasses import dataclass, field

import discord

from ttsbot import config
from ttsbot.textnorm import (
    CUSTOM_EMOJI_RE,
    MENTION_RE,
    URL_RE,
    _is_emoji_char,
    _is_keyboard_smash_word,
    _strip_discord_tokens_for_speech,
)


@dataclass
class ParsedMessage:
    spoken_text: str
    raw_length: int
    effective_length: int
    word_count: int
    emoji_count: int
    custom_emoji_count: int
    animated_custom_emoji_count: int
    mention_count: int
    url_count: int
    is_unicode_emoji_only: bool
    is_custom_emoji_only: bool
    is_animated_custom_emoji_only: bool
    is_mention_only: bool
    is_url_only: bool
    is_single_digit: bool
    is_single_symbol: bool
    is_caps_shout: bool
    is_keyboard_smash: bool
    is_question_or_terminal: bool

    @property
    def is_special_only(self) -> bool:
        return (
            self.is_unicode_emoji_only
            or self.is_custom_emoji_only
            or self.is_animated_custom_emoji_only
            or self.is_mention_only
            or self.is_url_only
        )


@dataclass
class MergeBufferState:
    key: tuple[int, int]
    voice_channel: discord.VoiceChannel
    author_id: int
    text_channel_id: int
    first_ts: float
    last_ts: float
    deadline_ts: float
    generation_id: int
    join_separator: str
    items: list[ParsedMessage] = field(default_factory=list)
    timer_task: asyncio.Task[None] | None = None
    has_substantive_starter: bool = False

    @property
    def effective_len_total(self) -> int:
        return sum(item.effective_length for item in self.items)


def analyze_message_for_merge(
    raw_text: str,
    aliases: dict[str, str] | None = None,
    mentions: dict[str, str] | None = None,
) -> ParsedMessage:
    raw_text = raw_text or ""
    raw_length = len(raw_text)
    spoken_text = _strip_discord_tokens_for_speech(raw_text, aliases, mentions)
    custom_tokens = list(CUSTOM_EMOJI_RE.finditer(raw_text))
    mention_tokens = list(MENTION_RE.finditer(raw_text))
    url_tokens = list(URL_RE.finditer(raw_text))
    words = re.findall(r"[A-Za-zА-Яа-яЁё0-9]+", spoken_text)
    punctuationless = re.sub(r"\s+", "", re.sub(r"[.,!?;:()\[\]{}\"'`~\-_/\\|]", "", spoken_text))
    emoji_count = sum(1 for ch in punctuationless if _is_emoji_char(ch))
    animated_custom_emoji_count = sum(1 for m in custom_tokens if m.group(0).startswith("<a:"))
    custom_emoji_count = len(custom_tokens)
    mention_count = len(mention_tokens)
    url_count = len(url_tokens)
    has_letters = bool(re.search(r"[A-Za-zА-Яа-яЁё]", spoken_text))
    raw_no_ws = re.sub(r"\s+", "", raw_text)
    is_mention_only = bool(raw_no_ws) and bool(MENTION_RE.fullmatch(raw_no_ws))
    is_url_only = bool(raw_no_ws) and bool(URL_RE.fullmatch(raw_no_ws))
    is_custom_emoji_only = bool(raw_no_ws) and bool(CUSTOM_EMOJI_RE.fullmatch(raw_no_ws))
    is_animated_custom_emoji_only = is_custom_emoji_only and raw_no_ws.startswith("<a:")
    is_unicode_emoji_only = bool(raw_no_ws) and all(
        _is_emoji_char(ch) or unicodedata.category(ch) in {"Cf", "Sk"} for ch in raw_no_ws
    )
    is_single_digit = bool(re.fullmatch(r"\d", spoken_text))
    is_single_symbol = len(spoken_text) == 1 and not spoken_text.isalnum()
    is_caps_shout = has_letters and spoken_text.upper() == spoken_text and len(spoken_text) >= 4
    is_keyboard_smash = any(_is_keyboard_smash_word(w) for w in spoken_text.split())
    is_question_or_terminal = bool(re.search(r"[!?]|[.]\s*$", spoken_text))
    effective_length = min(emoji_count, 4) + custom_emoji_count
    for word in words:
        if word.isdigit():
            effective_length += min(len(word), 3)
        else:
            effective_length += min(len(word), 12)
    if is_url_only and config.TTS_SELECTIVE_HOLD_DROP_URL_ONLY:
        spoken_text = ""
        effective_length = 0
    # A bare mention now resolves to a name (feature: read mentions aloud), so
    # only drop a mention-only message when nothing resolved (e.g. unknown id).
    if is_mention_only and config.TTS_SELECTIVE_HOLD_DROP_MENTION_ONLY and not spoken_text:
        spoken_text = ""
        effective_length = 0
    return ParsedMessage(
        spoken_text=spoken_text,
        raw_length=raw_length,
        effective_length=effective_length,
        word_count=len(words),
        emoji_count=emoji_count,
        custom_emoji_count=custom_emoji_count,
        animated_custom_emoji_count=animated_custom_emoji_count,
        mention_count=mention_count,
        url_count=url_count,
        is_unicode_emoji_only=is_unicode_emoji_only,
        is_custom_emoji_only=is_custom_emoji_only,
        is_animated_custom_emoji_only=is_animated_custom_emoji_only,
        is_mention_only=is_mention_only,
        is_url_only=is_url_only,
        is_single_digit=is_single_digit,
        is_single_symbol=is_single_symbol,
        is_caps_shout=is_caps_shout,
        is_keyboard_smash=is_keyboard_smash,
        is_question_or_terminal=is_question_or_terminal,
    )


def is_reaction_like(parsed: ParsedMessage) -> bool:
    return (
        bool(parsed.spoken_text)
        and not parsed.is_special_only
        and not parsed.is_caps_shout
        and not parsed.is_keyboard_smash
        and not parsed.is_question_or_terminal
        and parsed.effective_length <= 5
        and parsed.word_count <= 1
    )
