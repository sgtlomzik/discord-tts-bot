"""The bot's discord.py voice client: channel moves without a 30 s stall.

discord.py 2.7.1 reads voice UDP on a ``SocketReader`` thread that blocks in
``select([state.socket], [], [], 30)``. A channel move brings a
VOICE_SERVER_UPDATE, and ``VoiceConnectionState._create_socket`` swaps in a
new socket while the reader is still waiting on the old one. The new voice
websocket's IP discovery reply lands on the new socket, and nothing reads it
until that select times out, so ``move_to`` took about 30 s, during which the
bot spoke nothing. Shutting the old socket down wakes the select at once
(Linux reports a shut-down datagram socket as readable, connected or not),
and the reader's next loop waits on the new socket.

Uses discord.py internals, so requirements.txt pins discord.py.
"""

from __future__ import annotations

import asyncio
import logging
import socket

import discord
from discord.voice_state import VoiceConnectionState

log = logging.getLogger("tts_bot")


class PromptVoiceConnectionState(VoiceConnectionState):
    def _create_socket(self) -> None:
        old = self.socket
        super()._create_socket()
        if isinstance(old, socket.socket) and old is not self.socket:
            release_reader_socket(old)


# How long the replaced socket stays open after the wake-up. Linux select()
# looks the descriptor number up again when it wakes; closed at once, the
# number could already belong to another idle socket and select() would
# sleep out its 30 s after all. The reader leaves select() in microseconds.
RELEASE_CLOSE_DELAY_SECONDS = 5.0


def release_reader_socket(sock: socket.socket) -> None:
    """Wake a SocketReader blocked in select() on ``sock``; close it later.

    The caller has already pointed the reader's state at the new socket, so
    the woken reader does not read this one again.
    """
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        # ENOTCONN still wakes pollers; EBADF means it is already closed.
        pass
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        _close_quietly(sock)  # no loop to defer to (tests, shutdown)
    else:
        loop.call_later(RELEASE_CLOSE_DELAY_SECONDS, _close_quietly, sock)


def _close_quietly(sock: socket.socket) -> None:
    try:
        sock.close()
    except OSError:
        log.debug("Closing the replaced voice socket failed", exc_info=True)


class TTSVoiceClient(discord.VoiceClient):
    """discord.VoiceClient with PromptVoiceConnectionState."""

    def create_connection_state(self) -> VoiceConnectionState:
        return PromptVoiceConnectionState(self)
