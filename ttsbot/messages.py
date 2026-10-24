"""What part of a Discord message is spoken.

``speakable_text`` strips Discord markup for speech (custom emoji without
an alias, mention ids, URLs, Unicode emoji spoken by name) and drops a
message that is nothing but a link.
"""

from ttsbot.textnorm import URL_RE, _strip_discord_tokens_for_speech


def speakable_text(
    raw_text: str,
    aliases: dict[str, str] | None = None,
    mentions: dict[str, str] | None = None,
) -> str:
    raw_text = raw_text or ""
    compact = "".join(raw_text.split())
    if compact and URL_RE.fullmatch(compact):
        return ""  # a bare link has nothing worth reading out
    # A mention that resolved to nobody is stripped, so a message of only
    # that comes out empty and is not queued.
    return _strip_discord_tokens_for_speech(raw_text, aliases, mentions)
