"""Playback path for TTSBot: PCM or direct Opus into the continuous player.

Feeds prepared 20 ms audio frames to the per-guild
ContinuousTTSAudioSource (or the non-continuous FFmpeg file path when the
continuous stream is disabled).
"""

import asyncio
import logging
import time
import uuid
from pathlib import Path

import discord

from ttsbot import config
from ttsbot.audio import (
    ContinuousTTSAudioSource,
    OPUS_SILENCE_FRAME,
    build_idle_pcm_frame,
    build_playback_prepare_command,
    build_tts_pcm_command,
    split_pcm_frames,
)
from ttsbot.models import PreparedAudio

log = logging.getLogger("tts_bot")


class PlaybackMixin:
    """Continuous/file playback for PCM and Opus; mixed into TTSBot."""

    async def _playback_worker(self) -> None:
        await self.wait_until_ready()
        log.info("TTS playback worker started")
        while not self.is_closed():
            prepared = await self.ready_queue.get()
            try:
                await self._play_prepared(prepared)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("TTS playback failed; disconnecting voice")
                await self.disconnect_guild_voice(prepared.job.voice_channel.guild)
            finally:
                self.active_prepared.discard(prepared)
                self.ready_queue.task_done()

    async def _drain_channel(self, prepared: PreparedAudio) -> None:
        """Consume a prepared channel to its sentinel without playing."""
        while True:
            batch = await prepared.channel.get()
            if batch is None:
                return

    async def _play_prepared(self, prepared: PreparedAudio) -> None:
        job = prepared.job
        pickup_ts = time.perf_counter()
        if prepared.cancelled:
            await self._drain_channel(prepared)
            log.info("Skipped cancelled job guild=%s channel=%s",
                     job.voice_channel.guild.id, job.voice_channel.id)
            return
        try:
            vc = await self.ensure_voice(job.voice_channel)
        except Exception:
            log.exception("Voice prepare failed (playback)")
            existing = discord.utils.get(self.voice_clients, guild=job.voice_channel.guild)
            if existing and existing.is_connected():
                await self.disconnect_guild_voice(job.voice_channel.guild)
            await self._drain_channel(prepared)
            return

        source: ContinuousTTSAudioSource | None = None
        first_ts: float | None = None
        total = 0
        while True:
            batch = await prepared.channel.get()
            if batch is None:
                break
            if prepared.cancelled:
                continue  # stop feeding but drain to the sentinel
            if source is None:
                source = self.ensure_continuous_player(
                    vc, opus=prepared.codec == "opus", initial_frames=batch,
                )
            else:
                source.enqueue_frames(batch)
            total += len(batch)
            if first_ts is None:
                first_ts = time.perf_counter()
                log.info(
                    "Audio start guild=%s channel=%s provider=%s queue_wait=%.3fs "
                    "message_to_audio_s=%.3f queue_to_audio_s=%.3f",
                    job.voice_channel.guild.id, job.voice_channel.id,
                    prepared.provider or "?", pickup_ts - job.queued_at,
                    first_ts - job.message_ts, first_ts - job.queued_at,
                )
        if total == 0 or source is None:
            return
        await source.wait_until_drained()
        log.info(
            "Playback finished guild=%s channel=%s frames=%d total_since_queue=%.3fs",
            job.voice_channel.guild.id, job.voice_channel.id, total,
            time.perf_counter() - job.queued_at,
        )
        self.schedule_continuous_idle_stop(job.voice_channel.guild)
        self.schedule_idle_disconnect(job.voice_channel.guild)

    def ensure_continuous_player(
        self, vc: discord.VoiceClient, *, opus: bool = False,
        initial_frames: list[bytes] | None = None,
    ) -> ContinuousTTSAudioSource:
        guild_id = vc.guild.id
        self.cancel_continuous_idle_stop(guild_id)
        source = self.continuous_sources.get(guild_id)
        source_created = False
        if source is None or source.stopped or source.is_opus() != opus:
            if source is not None:
                source.stop()
            idle = OPUS_SILENCE_FRAME if opus else build_idle_pcm_frame(
                config.TTS_IDLE_FRAME_MODE, config.TTS_IDLE_VOLUME_DB,
            )
            source = ContinuousTTSAudioSource(idle, opus=opus)
            self.continuous_sources[guild_id] = source
            source_created = True

        if initial_frames:
            source.enqueue_frames(initial_frames)

        if source_created and (vc.is_playing() or vc.is_paused()):
            log.warning("Stopping previous voice source before continuous stream guild=%s", guild_id)
            vc.stop()

        if source_created or (not vc.is_playing() and not vc.is_paused()):
            vc.play(source)
            log.info(
                "Continuous TTS stream started guild=%s codec=%s mode=%s idle_volume_db=%s max_idle_seconds=%s",
                guild_id,
                "opus" if opus else "pcm",
                config.TTS_IDLE_FRAME_MODE,
                config.TTS_IDLE_VOLUME_DB,
                config.TTS_MAX_CONTINUOUS_IDLE_SECONDS,
            )
        return source

    async def prepare_tts_pcm_frames(self, source: Path, pitch: int = 0) -> list[bytes]:
        started = time.perf_counter()
        cmd = build_tts_pcm_command(source, pitch)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            stderr_text = stderr.decode("utf-8", errors="ignore").strip()
            raise RuntimeError(f"ffmpeg PCM preparation failed rc={proc.returncode} stderr={stderr_text}")

        frames = split_pcm_frames(stdout, config.TTS_STREAM_TAIL_MS)
        if not frames:
            raise RuntimeError("ffmpeg PCM preparation produced no audio frames")

        log.info(
            "PCM preparation took=%.3fs source_size=%s pcm_bytes=%s frames=%s tail_ms=%s",
            time.perf_counter() - started,
            source.stat().st_size if source.exists() else "unknown",
            len(stdout),
            len(frames),
            config.TTS_STREAM_TAIL_MS,
        )
        return frames

    async def prepare_playback_file(self, source: Path, prepared: Path, pitch: int = 0) -> Path:
        started = time.perf_counter()
        cmd = build_playback_prepare_command(source, prepared, pitch)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            stdout_text = stdout.decode("utf-8", errors="ignore").strip()
            stderr_text = stderr.decode("utf-8", errors="ignore").strip()
            raise RuntimeError(
                f"ffmpeg playback preparation failed rc={proc.returncode} "
                f"stdout={stdout_text} stderr={stderr_text}"
            )
        if not prepared.exists() or prepared.stat().st_size == 0:
            raise RuntimeError("ffmpeg playback preparation produced empty output")

        log.info(
            "Playback preparation took=%.3fs source_size=%s prepared_size=%s "
            "preroll_ms=%s preroll_mode=%s preroll_volume_db=%s tail_ms=%s",
            time.perf_counter() - started,
            source.stat().st_size if source.exists() else "unknown",
            prepared.stat().st_size,
            config.TTS_PREROLL_MS,
            config.TTS_PREROLL_MODE,
            config.TTS_PREROLL_VOLUME_DB,
            config.TTS_SILENCE_TAIL_MS,
        )
        return prepared

    async def play_file(self, vc: discord.VoiceClient, filename: Path, pitch: int = 0) -> None:
        if vc.is_playing() or vc.is_paused():
            log.warning("Voice client was already playing; stopping previous source")
            vc.stop()

        finished = asyncio.Event()
        loop = asyncio.get_running_loop()
        started = time.perf_counter()

        def after(error: Exception | None) -> None:
            if error:
                log.exception("Playback callback error", exc_info=error)
            loop.call_soon_threadsafe(finished.set)

        before_options = "-hide_banner -loglevel warning"
        if config.FFMPEG_LOW_DELAY:
            before_options = (
                f"{before_options} "
                "-fflags nobuffer -flags low_delay -probesize 32 -analyzeduration 0"
            )

        prepared = config.TMP_DIR / f"playback_{uuid.uuid4().hex}.wav"
        playback_file = filename
        try:
            try:
                playback_file = await self.prepare_playback_file(filename, prepared, pitch)
            except Exception:
                log.exception("Playback preparation failed; using source file")

            audio = discord.FFmpegPCMAudio(
                str(playback_file),
                before_options=before_options,
                options="-vn",
            )

            log.info("Starting playback file=%s size=%s", playback_file, playback_file.stat().st_size)
            vc.play(audio, after=after)
            await finished.wait()

            log.info("Playback duration took=%.3fs", time.perf_counter() - started)
        finally:
            if prepared.exists():
                try:
                    prepared.unlink()
                except OSError:
                    log.exception("Failed to remove prepared playback file: %s", prepared)
