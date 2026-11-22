"""Channel moves must not wait for discord.py's 30 s socket reader select.

These run the real discord.py SocketReader thread on loopback UDP sockets,
the way a move swaps the voice socket under it.
"""

from __future__ import annotations

import asyncio
import socket
import threading
import time
import unittest
from unittest.mock import patch

from discord.voice_state import SocketReader, VoiceConnectionState

from ttsbot import voice_connection
from ttsbot.recording.client import RecordingVoiceClient
from ttsbot.voice_connection import PromptVoiceConnectionState, TTSVoiceClient


def _udp() -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    sock.setblocking(False)
    return sock


class _Harness:
    """A connection state with only what _create_socket and the reader use."""

    def __init__(self, state_cls: type[VoiceConnectionState], *, connected_old: bool) -> None:
        self.state = state_cls.__new__(state_cls)
        self.peer = _udp()
        old = _udp()
        if connected_old:
            old.connect(self.peer.getsockname())  # as discord.py connects it to the voice server
        self.state.socket = old
        self.old = old
        self.received: list[bytes] = []
        self.got = threading.Event()
        self.reader = SocketReader(self.state, start_paused=False)  # type: ignore[arg-type]
        self.state._socket_reader = self.reader
        self.reader.start()
        self.reader.register(self._on_packet)
        time.sleep(0.5)  # the reader is now in select() on the old socket

    def _on_packet(self, data: bytes) -> None:
        self.received.append(data)
        self.got.set()

    def move(self) -> float:
        """Swap the socket like VOICE_SERVER_UPDATE; seconds until the reply is read."""
        self.state._create_socket()
        new = self.state.socket
        new.bind(("127.0.0.1", 0))  # sock_connect binds it in discord.py
        started = time.monotonic()
        self.peer.sendto(b"\x00\x02" + b"\x00" * 72, new.getsockname())  # IP discovery reply
        self.got.wait(2.0)
        return time.monotonic() - started

    def close(self) -> None:
        self.reader.stop()
        for sock in (self.state.socket, self.old):
            try:
                sock.shutdown(socket.SHUT_RDWR)  # wake the reader so it can exit
            except OSError:
                pass
        self.reader.join(2.0)
        for sock in (self.state.socket, self.old, self.peer):
            sock.close()
        assert not self.reader.is_alive(), "the socket reader did not exit"


class SocketSwapTests(unittest.TestCase):
    def _harness(self, state_cls, *, connected_old: bool) -> _Harness:
        harness = _Harness(state_cls, connected_old=connected_old)
        self.addCleanup(harness.close)
        return harness

    def test_the_stock_reader_misses_the_new_socket(self):
        # Guards the test itself: without the fix the reply waits up to 30 s.
        harness = self._harness(VoiceConnectionState, connected_old=True)
        harness.move()
        self.assertFalse(harness.got.is_set())

    def test_the_reply_on_the_new_socket_is_read_at_once(self):
        for connected_old in (True, False):
            with self.subTest(connected_old=connected_old):
                harness = self._harness(PromptVoiceConnectionState, connected_old=connected_old)
                took = harness.move()
                self.assertTrue(harness.got.is_set())
                self.assertLess(took, 1.0)
                self.assertEqual([len(packet) for packet in harness.received], [74])

    def test_the_replaced_socket_is_closed(self):
        harness = self._harness(PromptVoiceConnectionState, connected_old=True)
        harness.move()
        self.assertEqual(harness.old.fileno(), -1)
        self.assertIsNot(harness.state.socket, harness.old)

    def test_under_the_event_loop_the_replaced_socket_closes_later(self):
        # Closing at once could hand its descriptor number to another socket
        # before the woken select() looks it up again.
        harness = self._harness(PromptVoiceConnectionState, connected_old=True)

        async def move_in_loop() -> tuple[int, int]:
            took = harness.move()
            self.assertLess(took, 1.0)
            right_after = harness.old.fileno()
            await asyncio.sleep(0.1)
            return right_after, harness.old.fileno()

        with patch.object(voice_connection, "RELEASE_CLOSE_DELAY_SECONDS", 0.05):
            right_after, later = asyncio.run(move_in_loop())
        self.assertNotEqual(right_after, -1)
        self.assertEqual(later, -1)

    def test_the_first_socket_has_nothing_to_release(self):
        state = PromptVoiceConnectionState.__new__(PromptVoiceConnectionState)
        state.socket = None  # discord.py starts with MISSING
        state._socket_reader = SocketReader(state, start_paused=True)  # type: ignore[arg-type]
        state._create_socket()
        self.addCleanup(state.socket.close)
        self.assertIsInstance(state.socket, socket.socket)


class ClientClassTests(unittest.TestCase):
    def test_both_voice_clients_use_the_prompt_state(self):
        for cls in (TTSVoiceClient, RecordingVoiceClient):
            with self.subTest(cls=cls.__name__):
                client = cls.__new__(cls)
                state = client.create_connection_state()
                state._socket_reader.stop()
                self.assertIsInstance(state, PromptVoiceConnectionState)


if __name__ == "__main__":
    unittest.main()
