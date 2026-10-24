"""A discord.py VoiceClient that also records what it hears.

discord.py 2.7 sends voice but has no receive API. It does expose the
two pieces receive needs: ``add_socket_listener`` hands every UDP packet
to a callback (on the socket reader thread), and a voice-websocket hook
sees each gateway message, including SPEAKING (op 5), which tells which
user owns an SSRC. The DAVE session discord.py keeps for sending also
decrypts what other members send.

Those are internal APIs, so requirements.txt pins discord.py.
"""

from __future__ import annotations

import collections
import logging
import threading
import time

import davey
import discord
from discord.voice_state import VoiceConnectionState

from ttsbot import config
from ttsbot.recording import rtp
from ttsbot.recording.recorder import ChannelRecorder, RecorderSettings
from ttsbot.recording.vad import webrtc_detector_factory

log = logging.getLogger("tts_bot")

# Voice gateway opcodes.
_OP_READY = 2
_OP_SPEAKING = 5
_OP_CLIENT_DISCONNECT = 13
# SPEAKING flags (voice gateway op 5).
_SPEAKING_MICROPHONE = 1
_SPEAKING_SOUNDSHARE = 2

# Packets kept per unknown SSRC until its SPEAKING event names the user.
_PENDING_PACKETS = 50
# Packets kept per user while DAVE cannot decrypt them yet: right after
# the bot joins (or someone joins) the sender's key arrives ~0.5 s after
# its audio. They are retried, in order, once a packet decrypts.
_DAVE_HOLD_PACKETS = 150


def settings_from_config() -> RecorderSettings:
    return RecorderSettings(
        root=config.RECORDING_DIR,
        chunk_seconds=config.RECORDING_CHUNK_SECONDS,
        idle_close_seconds=config.RECORDING_IDLE_CLOSE_SECONDS,
        pause_ms=config.RECORDING_PAUSE_MS,
        detector_factory=(
            webrtc_detector_factory(config.RECORDING_VAD_AGGRESSIVENESS)
            if config.RECORDING_VAD_AGGRESSIVENESS >= 0 else None
        ),
    )


class RecordingVoiceClient(discord.VoiceClient):
    def __init__(self, client: discord.Client, channel: discord.abc.Connectable) -> None:
        self._ssrc_users: dict[int, int] = {}
        self._ignored_ssrcs: set[int] = set()  # Go-Live sound streams
        self._pending: dict[int, collections.deque] = {}
        self._dave_warned: set[tuple[int, str]] = set()
        self._dave_held: dict[int, collections.deque] = {}
        self._named: set[int] = set()
        self._recorder: ChannelRecorder | None = None
        self._listening = False
        super().__init__(client, channel)

    def create_connection_state(self) -> VoiceConnectionState:
        return VoiceConnectionState(self, hook=self._ws_hook)

    # -- lifecycle -----------------------------------------------------

    async def connect(self, *, reconnect: bool, timeout: float, self_deaf: bool = False,
                      self_mute: bool = False) -> None:
        await super().connect(reconnect=reconnect, timeout=timeout, self_deaf=self_deaf, self_mute=self_mute)
        self._start_recording()

    async def on_voice_state_update(self, data) -> None:
        await super().on_voice_state_update(data)
        recorder = self._recorder
        channel_id = data.get("channel_id")
        if recorder is not None and channel_id is not None and int(channel_id) != recorder.channel_id:
            # Moved to another channel: one session folder per channel stay.
            self._recorder = None
            self._reset_streams()
            await self.client.loop.run_in_executor(None, recorder.stop)
            self._start_recording()

    def cleanup(self) -> None:
        recorder, self._recorder = self._recorder, None
        self._listening = False  # the socket reader drops its callbacks on stop
        if recorder is not None:
            # stop() joins the writer thread; keep that off the event loop.
            threading.Thread(target=recorder.stop, name="voice-recorder-stop", daemon=True).start()
        super().cleanup()

    def note_spoken_text(self, user_id: int, text: str) -> None:
        """The bot started speaking ``text`` for ``user_id``: put it in the transcript."""
        recorder = self._recorder
        if recorder is not None:
            try:
                self._note_name(recorder, user_id, self.guild.get_member(user_id))
                recorder.note_text(user_id, text)
            except OSError:
                log.exception("Recording TTS note failed guild=%s", self.guild.id)

    def _start_recording(self) -> None:
        # Recording is optional: a failure here (full disk, unwritable
        # RECORDING_DIR) is logged and the voice connection stays up for TTS.
        if self._recorder is None and self.channel is not None:
            try:
                recorder = ChannelRecorder(settings_from_config(), self.guild.id, self.channel.id,
                                           guild_name=getattr(self.guild, "name", None),
                                           channel_name=getattr(self.channel, "name", None))
                recorder.start()
            except Exception:
                log.exception("Recording could not start guild=%s channel=%s; TTS continues",
                              self.guild.id, self.channel.id)
                return
            self._recorder = recorder
        if self._recorder is None:
            return
        if not self._listening:
            self._connection.add_socket_listener(self._on_packet)
            self._listening = True

    def _reset_streams(self) -> None:
        self._ssrc_users.clear()
        self._ignored_ssrcs.clear()
        self._pending.clear()
        self._dave_held.clear()
        self._dave_warned.clear()
        self._named.clear()

    # -- voice websocket (event loop) ----------------------------------

    async def _ws_hook(self, ws, msg: dict) -> None:
        op = msg.get("op")
        data = msg.get("d") or {}
        if op == _OP_SPEAKING and "ssrc" in data and "user_id" in data:
            ssrc, user_id = int(data["ssrc"]), int(data["user_id"])
            flags = int(data.get("speaking") or 0)
            if flags & _SPEAKING_MICROPHONE:
                self._ignored_ssrcs.discard(ssrc)
            elif flags & _SPEAKING_SOUNDSHARE or ssrc in self._ignored_ssrcs:
                # Go-Live sound is the stream's audio, not the member's voice.
                self._ignored_ssrcs.add(ssrc)
                self._ssrc_users.pop(ssrc, None)
                return
            if self._ssrc_users.get(ssrc) != user_id:
                self._ssrc_users[ssrc] = user_id
                log.debug("Voice receive ssrc=%s user=%s guild=%s", ssrc, user_id, self.guild.id)
        elif op == _OP_CLIENT_DISCONNECT and "user_id" in data:
            user_id = int(data["user_id"])
            for ssrc in [s for s, u in self._ssrc_users.items() if u == user_id]:
                del self._ssrc_users[ssrc]
        elif op == _OP_READY:
            self._reset_streams()  # a new voice session assigns new SSRCs

    # -- socket reader thread ------------------------------------------

    def _on_packet(self, data: bytes) -> None:
        recorder = self._recorder
        if recorder is None or not rtp.is_rtp_audio(data):
            return
        connection = self._connection
        try:
            packet = rtp.decrypt_transport(data, connection.mode, bytes(connection.secret_key))
        except rtp.PacketError as exc:
            recorder.count("transport_errors")
            if recorder.counters["transport_errors"] == 1:
                log.warning("Voice receive transport error guild=%s: %s", self.guild.id, exc)
            return
        if not packet.payload:
            recorder.count("padding_packets")
            return
        if packet.payload == rtp.OPUS_SILENCE:
            return
        if packet.ssrc in self._ignored_ssrcs:
            recorder.count("soundshare_packets")
            self._pending.pop(packet.ssrc, None)
            return
        wall = time.time()
        user_id = self._ssrc_users.get(packet.ssrc)
        if user_id is None:
            recorder.count("unknown_ssrc_packets")
            pending = self._pending.setdefault(packet.ssrc, collections.deque(maxlen=_PENDING_PACKETS))
            pending.append((packet, wall))
            return
        pending = self._pending.pop(packet.ssrc, None)
        if pending:
            for early, early_wall in pending:
                self._deliver(recorder, user_id, early, early_wall)
        self._deliver(recorder, user_id, packet, wall)

    def _deliver(self, recorder: ChannelRecorder, user_id: int, packet: rtp.RtpPacket, wall: float) -> None:
        member = self.guild.get_member(user_id)
        if member is not None and member.bot:
            recorder.count("bot_packets")
            return
        self._note_name(recorder, user_id, member)
        connection = self._connection
        session = connection.dave_session
        if session is None or not connection.dave_protocol_version:
            self._submit(recorder, user_id, packet.timestamp, packet.payload, wall, packet.ssrc)
            return
        try:
            payload = session.decrypt(user_id, davey.MediaType.audio, packet.payload)
        except Exception as exc:
            reason = str(exc).split("{", 1)[0].strip()
            if "NoDecryptorForUser" in reason or "NoValidCryptorFound" in reason:
                # Expected right after the bot joins: members keep sending under
                # the previous epoch's key until Discord switches them over, and
                # a newcomer never gets that key. Counted, not warned about.
                recorder.count("dave_nokey")
                level = logging.DEBUG
            else:
                recorder.count("dave_errors")
                if "UnencryptedWhenPassthroughDisabled" in reason:
                    recorder.count("dave_unencrypted")
                level = logging.WARNING
            # One line per user and reason per channel stay; totals are in the stats.
            if (user_id, reason) not in self._dave_warned:
                self._dave_warned.add((user_id, reason))
                log.log(
                    level,
                    "Voice receive DAVE decrypt failed guild=%s user=%s ready=%s: %s: %s",
                    self.guild.id, user_id, session.ready, type(exc).__name__, exc,
                )
            held = self._dave_held.setdefault(user_id, collections.deque(maxlen=_DAVE_HOLD_PACKETS))
            if len(held) == held.maxlen:
                recorder.count("dave_lost")
            held.append((packet, wall))
            return
        held = self._dave_held.pop(user_id, None)
        if held:
            for old, old_wall in held:
                try:
                    old_payload = session.decrypt(user_id, davey.MediaType.audio, old.payload)
                except Exception:
                    recorder.count("dave_lost")
                    continue
                recorder.count("dave_recovered")
                self._submit(recorder, user_id, old.timestamp, old_payload, old_wall, old.ssrc)
        self._submit(recorder, user_id, packet.timestamp, payload, wall, packet.ssrc)

    def _note_name(self, recorder: ChannelRecorder, user_id: int, member) -> None:
        if member is None or user_id in self._named:
            return
        self._named.add(user_id)
        try:
            recorder.note_name(user_id, member.display_name)
        except OSError:
            log.exception("Recording name note failed guild=%s", self.guild.id)

    @staticmethod
    def _submit(recorder: ChannelRecorder, user_id: int, timestamp: int, payload: bytes, wall: float,
                ssrc: int = 0) -> None:
        if not payload:
            recorder.count("empty_payloads")
            return
        recorder.submit(user_id, timestamp, payload, wall, ssrc=ssrc)
