"""Synthesis pipeline for TTSBot: providers, streaming and workers.

Covers Piper file generation, the MiniMax streaming fast path with its
cache/circuit-breaker interplay, the prefetch generation worker and the
legacy single worker. Playback details live in ttsbot.playback.
"""

import asyncio
import logging
import os
import time
import uuid
import wave
from pathlib import Path

import discord

try:
    from piper import PiperVoice, SynthesisConfig
except Exception:  # pragma: no cover - optional dependency
    PiperVoice = None
    SynthesisConfig = None

from ttsbot import voice_registry
from ttsbot.providers import (
    MiniMaxError,
    MiniMaxProvider,
    MiniMaxVoiceNotFoundError,
    load_minimax_config_from_env,
)
from ttsbot import config
from ttsbot.audio import (
    PCM_FRAME_BYTES,
    PCM_FRAME_MS,
    ContinuousTTSAudioSource,
    build_tts_stream_pcm_command,
)
from ttsbot.models import PreparedAudio, TTSJob, VOICE_PROFILES, VoiceProfile

log = logging.getLogger("tts_bot")


def _voice_speed_factor(voice) -> float:
    """Playback speed of a voice relative to 1.0 (higher = faster speech).

    MiniMax exposes ``speed`` directly; Piper's ``length_scale`` stretches
    duration, so speed is its inverse.
    """
    if voice is None:
        return 1.0
    mm = getattr(voice, "minimax", None)
    if getattr(voice, "is_minimax", False) and mm is not None:
        return max(float(getattr(mm, "speed", 1.0) or 1.0), 0.1)
    piper = getattr(voice, "piper", None)
    if piper is not None:
        length_scale = float(getattr(piper, "length_scale", 1.0) or 1.0)
        return max(1.0 / max(length_scale, 0.1), 0.1)
    return 1.0


def playback_frame_limit(text: str, voice=None) -> int | None:
    """Max plausible 20ms frames for ``text``, or None when the guard is off.

    Estimate: chars / TTS_AUDIO_CHARS_PER_SECOND at speed 1.0, divided by the
    voice speed factor, times TTS_AUDIO_LIMIT_SAFETY, floored at
    TTS_AUDIO_LIMIT_MIN_SECONDS. Guards against MiniMax stutter loops that
    stream one sound indefinitely.
    """
    if not config.TTS_AUDIO_LIMIT_ENABLED:
        return None
    cps = max(config.TTS_AUDIO_CHARS_PER_SECOND, 0.1)
    seconds = len(text) / cps / _voice_speed_factor(voice)
    seconds = max(seconds * max(config.TTS_AUDIO_LIMIT_SAFETY, 1.0),
                  config.TTS_AUDIO_LIMIT_MIN_SECONDS)
    return max(1, int(seconds * 1000 / PCM_FRAME_MS))


def limit_pcm_frames(frames: list[bytes], text: str, voice=None) -> list[bytes]:
    """Truncate an already-decoded frame list to the playback limit."""
    limit = playback_frame_limit(text, voice)
    if limit is None or len(frames) <= limit:
        return frames
    log.warning(
        "Audio length limit: decoded %d frames for %d chars, capping at %d (%.1fs)",
        len(frames), len(text), limit, limit * PCM_FRAME_MS / 1000,
    )
    return frames[:limit]


class SynthesisPipelineMixin:
    """TTS generation, streaming and worker loops; mixed into TTSBot."""

    async def warmup_tts(self) -> None:
        filename = config.TMP_DIR / f"warmup_{uuid.uuid4().hex}.wav"
        try:
            # Warm Piper ONNX directly, bypassing the dispatcher. The
            # whole point of warmup is to preload the local model so the
            # first real request (including a fallback to local) is not
            # cold. If TTS_PRIMARY_PROVIDER=minimax, warming the cloud
            # provider is pointless (it is HTTP) and would burn an API
            # call on every restart.
            await self.tts_dispatcher.warm_local("Привет", filename)
            log.info("TTS warmup completed")
        except Exception:
            log.exception("TTS warmup failed")
        finally:
            if filename.exists():
                try:
                    filename.unlink()
                except OSError:
                    log.exception("Failed to remove warmup file: %s", filename)

    def _build_cloud_provider(self):
        """Construct the MiniMax provider if the bot has credentials.

        Returns ``None`` when MINIMAX_API_KEY or MINIMAX_VOICE_ID is
        missing — the dispatcher then silently uses the local provider
        regardless of TTS_PRIMARY_PROVIDER, and the bot starts cleanly.
        """
        cfg = load_minimax_config_from_env()
        # Only the API key is required now: the per-message voice_id comes
        # from the registry record, so the cloud provider is usable even
        # when MINIMAX_VOICE_ID is empty (as long as a minimax voice is in
        # the catalog). MINIMAX_VOICE_ID remains the first-start seed.
        if not cfg.api_key:
            log.info("MiniMax provider disabled (api_key MISSING); fall back to local")
            return None
        log.info(
            "MiniMax provider enabled model=%s default_voice_id=%s base_url=%s timeout=%.1fs",
            cfg.model, cfg.voice_id or "(per-record)", cfg.base_url, cfg.timeout_seconds,
        )
        return MiniMaxProvider(cfg)

    def _resolve_piper_profile(self, voice_profile: str | None) -> VoiceProfile:
        """Resolve a voice name to a Piper ``VoiceProfile`` via the registry.

        Falls back to the registry ``fallback_profile`` then ``VOICE_PROFILES``
        so a missing or non-Piper name still yields a usable Piper voice.
        """
        name = (
            voice_profile
            or self.voice_registry.fallback_profile
            or config.DEFAULT_VOICE_PROFILE
        )
        rec = self.voice_registry.get(name)
        if rec is not None and rec.is_piper and rec.piper is not None:
            return VoiceProfile(
                name=rec.name,
                label=rec.label,
                piper_model_path=rec.piper.model_path,
                piper_config_path=rec.piper.config_path,
                piper_speaker=rec.piper.speaker,
                piper_length_scale=rec.piper.length_scale,
            )
        return VOICE_PROFILES.get(name, VOICE_PROFILES[config.DEFAULT_VOICE_PROFILE])

    def persist_voice_registry(self) -> None:
        """Atomically write the current catalog to data/voices.json."""
        voice_registry.save_registry(config.VOICES_REGISTRY_PATH, self.voice_registry)

    async def validate_minimax_voice(self, voice_id: str) -> tuple[bool, str]:
        """Probe a MiniMax voice_id with a short phrase.

        Returns (ok, error_message). ok=True means status_code 0; a 2054
        (voice id not exist) yields a clear rejection. Used by voice-add
        before persisting a new voice.
        """
        cloud = getattr(self.tts_dispatcher, "cloud", None)
        if cloud is None:
            return False, "MiniMax не настроен (нет MINIMAX_API_KEY)."
        tmp = config.TMP_DIR / f"voiceadd_{uuid.uuid4().hex}.mp3"
        try:
            await cloud.synthesize("проверка голоса", tmp, voice_id=voice_id)
            return True, ""
        except MiniMaxVoiceNotFoundError:
            return False, f"voice_id `{voice_id}` не существует (MiniMax 2054)."
        except MiniMaxError as exc:
            return False, f"Ошибка MiniMax: {exc}"
        except Exception as exc:  # network/timeout/etc
            return False, f"Не удалось проверить голос: {type(exc).__name__}: {exc}"
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    async def clone_minimax_voice(
        self,
        *,
        name: str,
        voice_id: str,
        sample: bytes,
        filename: str,
        description: str,
    ) -> tuple[bool, str]:
        """Clone ``sample`` on MiniMax, then register + persist the voice.

        Wraps the provider's upload+clone, confirms the fresh clone actually
        synthesizes (a clone that 'succeeds' but won't speak is useless), then
        adds the record to the registry and saves data/voices.json. Returns
        (ok, error_message); on ok the voice ``name`` is ready for
        voice-set/voice-user. Used by /voicebot voice-clone.
        """
        cloud = getattr(self.tts_dispatcher, "cloud", None)
        if cloud is None:
            return False, "MiniMax не настроен (нет MINIMAX_API_KEY)."
        clone = getattr(cloud, "clone_voice", None)
        if clone is None:
            return False, "Провайдер не поддерживает клонирование."
        try:
            await clone(sample, voice_id=voice_id, filename=filename)
        except MiniMaxError as exc:
            return False, f"Ошибка клонирования: {exc}"
        except Exception as exc:  # network/timeout/decode/etc
            return False, f"Не удалось клонировать: {type(exc).__name__}: {exc}"
        # A clone can report success but not yet be usable; confirm it speaks
        # before we register it so we never persist a dead voice.
        ok, err = await self.validate_minimax_voice(voice_id)
        if not ok:
            return False, f"Клон создан, но не воспроизводится: {err}"
        record = voice_registry.VoiceRecord(
            name=name,
            label=f"{name} (клон)",
            description=description,
            provider=voice_registry.PROVIDER_MINIMAX,
            minimax=voice_registry.MiniMaxParams(voice_id=voice_id),
        )
        self.voice_registry.add(record)
        try:
            self.persist_voice_registry()
        except OSError as exc:
            log.exception("Failed to persist voices.json after voice-clone")
            return False, f"Голос создан, но не сохранён на диск: {exc}"
        return True, ""

    async def generate_piper_file(self, text: str, filename: Path, profile: VoiceProfile) -> None:
        if PiperVoice is None:
            raise RuntimeError("piper-tts is not installed")
        model_value = profile.piper_model_path or config.PIPER_MODEL_PATH
        config_value = profile.piper_config_path or config.PIPER_CONFIG_PATH
        if not model_value:
            raise RuntimeError("PIPER_MODEL_PATH is not set")
        model_path = Path(model_value)
        if not model_path.exists():
            raise RuntimeError(f"Piper model not found: {model_path}")
        config_path = Path(config_value) if config_value else None
        if config_path and not config_path.exists():
            raise RuntimeError(f"Piper config not found: {config_path}")

        voice_key = (str(model_path), str(config_path) if config_path else "")
        piper_voice = self.piper_voices.get(voice_key)
        if piper_voice is None:
            piper_voice = await asyncio.to_thread(
                PiperVoice.load,
                str(model_path),
                str(config_path) if config_path else None,
            )
            self.piper_voices[voice_key] = piper_voice

        started = time.perf_counter()
        log.info("Generating Piper TTS chars=%s model=%s profile=%s", len(text), model_path, profile.name)

        syn_config = None
        if SynthesisConfig is not None and (
            profile.piper_speaker >= 0 or abs(profile.piper_length_scale - 1.0) > 1e-6
        ):
            syn_config = SynthesisConfig(
                speaker_id=profile.piper_speaker if profile.piper_speaker >= 0 else None,
                length_scale=profile.piper_length_scale,
            )

        def _synthesize() -> None:
            with wave.open(str(filename), "wb") as wav_file:
                piper_voice.synthesize_wav(text, wav_file, syn_config=syn_config)

        await asyncio.to_thread(_synthesize)
        if not filename.exists() or filename.stat().st_size == 0:
            raise RuntimeError("Piper did not produce audio output")

        log.info(
            "Piper generated file=%s size=%s took=%.3fs",
            filename,
            filename.stat().st_size,
            time.perf_counter() - started,
        )

    async def generate_tts_file(self, text: str, filename: Path, voice_profile: str | None = None) -> str:
        # Resolve the stored voice name to a registry record; the dispatcher
        # routes by record.provider (piper -> local, minimax -> cloud) and
        # falls back to the registry fallback_profile on cloud failure.
        record = self.voice_registry.get(voice_profile)
        if record is None:
            record = self.voice_registry.fallback_record()
        provider_used = await self.tts_dispatcher.synthesize(text, filename, voice=record)
        log.info(
            "TTS engine used: %s voice=%s",
            provider_used,
            record.name if record is not None else (voice_profile or "default"),
        )
        return provider_used

    def _should_attempt_stream(self, voice) -> bool:
        """True when a job qualifies for the streaming fast path."""
        return (
            config.TTS_STREAMING_ENABLED
            and config.TTS_CONTINUOUS_STREAM
            and voice is not None
            and getattr(voice, "is_minimax", False)
            and self.tts_dispatcher.cloud is not None
        )

    async def _run_streaming_job(self, job: TTSJob, voice, worker_started: float) -> str:
        """Drive a streaming MiniMax job. Returns "done" or "fallback".

        "done": the job is fully handled (streamed ok, truncated mid-play, or
        a connection error that has nothing to fall back to). "fallback": the
        caller should run the Piper file path (pre-audio failure or open
        circuit breaker).
        """
        cb = self.tts_dispatcher.circuit_breaker
        # Connect first — enqueueing frames needs the voice client. Usually
        # instant because the continuous stream keeps the session open.
        try:
            vc = await self.ensure_voice(job.voice_channel)
        except Exception:
            log.exception("Voice prepare failed (streaming)")
            existing = discord.utils.get(self.voice_clients, guild=job.voice_channel.guild)
            if existing and existing.is_connected():
                await self.disconnect_guild_voice(job.voice_channel.guild)
            return "done"

        # Cache hit: play the stored audio from disk, no API call, no breaker
        # probe consumed. (Cache key already includes the voice.)
        cache = self.tts_dispatcher.cache
        if cache is not None:
            cached = cache.lookup(job.text, voice.name)
            if cached is not None:
                source = self.ensure_continuous_player(vc)
                try:
                    frames = await self.prepare_tts_pcm_frames(cached)
                except Exception:
                    log.exception("Cached audio decode failed; streaming instead")
                    frames = []
                frames = limit_pcm_frames(frames, job.text, voice)
                if frames:
                    source.enqueue_frames(frames)
                    log.info(
                        "Stream cache HIT guild=%s channel=%s frames=%d "
                        "message_to_audio_s=%.3f (no API)",
                        job.voice_channel.guild.id, job.voice_channel.id, len(frames),
                        time.perf_counter() - job.message_ts,
                    )
                    await source.wait_until_drained()
                    self.schedule_continuous_idle_stop(job.voice_channel.guild)
                    self.schedule_idle_disconnect(job.voice_channel.guild)
                    return "done"

        # Consume the breaker probe only now that we are about to call cloud.
        if not cb.allow_request():
            log.debug(
                "Circuit breaker open; skipping stream guild=%s", job.voice_channel.guild.id
            )
            return "fallback"

        log.info(
            "Ready to stream guild=%s channel=%s queue_wait=%.3fs prep_total=%.3fs",
            job.voice_channel.guild.id, job.voice_channel.id,
            worker_started - job.queued_at, time.perf_counter() - worker_started,
        )
        source = self.ensure_continuous_player(vc)
        try:
            status, frames = await self._stream_tts_to_source(source, voice, job)
        except Exception:
            log.exception("Streaming playback crashed; falling back to Piper")
            cb.record_failure()
            return "fallback"

        if status == "pre_audio":
            cb.record_failure()
            return "fallback"
        # Audio started: ok (full) or truncated (mid-stream failure). Either
        # way we do NOT overlay Piper on top of already-playing audio.
        cb.record_success() if status == "ok" else cb.record_failure()
        await source.wait_until_drained()
        log.info(
            "Playback finished (stream) guild=%s channel=%s frames=%d total_since_queue=%.3fs",
            job.voice_channel.guild.id, job.voice_channel.id, frames,
            time.perf_counter() - job.queued_at,
        )
        self.schedule_continuous_idle_stop(job.voice_channel.guild)
        self.schedule_idle_disconnect(job.voice_channel.guild)
        return "done"

    async def _stream_tts_to_source(
        self, source: "ContinuousTTSAudioSource", voice, job: TTSJob
    ) -> tuple[str, int]:
        """Stream a MiniMax voice into the continuous player frame-by-frame.

        Returns (status, frames_enqueued) where status is "ok", "truncated"
        (audio started then the stream failed mid-way) or "pre_audio" (failed
        before any audio — caller falls back to Piper). Circuit-breaker
        accounting is the caller's job.
        """
        cloud = self.tts_dispatcher.cloud
        mm = voice.minimax
        agen = cloud.stream_audio(
            job.text,
            voice_id=mm.voice_id, model=mm.model, speed=mm.speed,
            vol=mm.vol, pitch=mm.pitch, emotion=mm.emotion,
            language_boost=mm.language_boost,
        )
        # 1. First chunk under the TTFA budget; any failure here => clean
        #    fallback to Piper (no audio has played yet).
        try:
            first_chunk = await asyncio.wait_for(
                agen.__anext__(), timeout=config.TTS_STREAM_TTFA_TIMEOUT
            )
        except StopAsyncIteration:
            await agen.aclose()
            log.warning("Stream produced no audio; falling back to Piper")
            return ("pre_audio", 0)
        except asyncio.TimeoutError:
            await agen.aclose()
            log.warning(
                "Stream TTFA exceeded %.2fs; falling back to Piper", config.TTS_STREAM_TTFA_TIMEOUT
            )
            return ("pre_audio", 0)
        except Exception as exc:
            await agen.aclose()
            log.warning(
                "Stream failed before first audio (%s: %s); Piper fallback",
                type(exc).__name__, exc,
            )
            return ("pre_audio", 0)

        # 2. We have audio. Decode the MP3 byte stream via ffmpeg (stdin ->
        #    s16le stdout) while feeding chunks concurrently.
        proc = await asyncio.create_subprocess_exec(
            *build_tts_stream_pcm_command(),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        mid_error: list[BaseException] = []

        # Tee the MP3 byte stream to a cache ".part" file so a future repeat
        # of this (voice, text) plays from disk without hitting the API. Only
        # committed on a clean finish — partial streams are never cached.
        cache = self.tts_dispatcher.cache
        cache_final: Path | None = None
        cache_part = None
        if cache is not None:
            try:
                cache_final = cache.cache_path_for(job.text, voice.name)
                cache_final.parent.mkdir(parents=True, exist_ok=True)
                cache_part = open(str(cache_final) + ".part", "wb")
            except OSError:
                cache_final = None
                cache_part = None

        async def _feed() -> None:
            try:
                proc.stdin.write(first_chunk)
                await proc.stdin.drain()
                if cache_part is not None:
                    cache_part.write(first_chunk)
                async for chunk in agen:
                    proc.stdin.write(chunk)
                    await proc.stdin.drain()
                    if cache_part is not None:
                        cache_part.write(chunk)
            except Exception as exc:  # mid-stream API/network failure
                mid_error.append(exc)
            finally:
                try:
                    await agen.aclose()
                except Exception:
                    pass
                try:
                    proc.stdin.close()
                except Exception:
                    pass
                if cache_part is not None:
                    try:
                        cache_part.close()
                    except Exception:
                        pass

        feeder = asyncio.create_task(_feed())

        frame_limit = playback_frame_limit(job.text, voice)
        frames_enqueued = 0
        limit_hit = False
        first_frame_ts: float | None = None
        leftover = b""
        try:
            while True:
                data = await proc.stdout.read(PCM_FRAME_BYTES * 16)
                if not data:
                    break
                buf = leftover + data
                n = len(buf) - (len(buf) % PCM_FRAME_BYTES)
                if n:
                    out_frames = [buf[i:i + PCM_FRAME_BYTES] for i in range(0, n, PCM_FRAME_BYTES)]
                    if frame_limit is not None and frames_enqueued + len(out_frames) > frame_limit:
                        out_frames = out_frames[:max(frame_limit - frames_enqueued, 0)]
                        limit_hit = True
                    if out_frames:
                        source.enqueue_frames(out_frames)
                        if first_frame_ts is None:
                            first_frame_ts = time.perf_counter()
                            log.info(
                                "Stream first audio guild=%s channel=%s "
                                "message_to_first_audio_s=%.3f queue_to_first_audio_s=%.3f",
                                job.voice_channel.guild.id, job.voice_channel.id,
                                first_frame_ts - job.message_ts,
                                first_frame_ts - job.queued_at,
                            )
                        frames_enqueued += len(out_frames)
                    if limit_hit:
                        log.warning(
                            "Audio length limit hit guild=%s: %d frames (%.1fs) for %d chars; "
                            "aborting stream (likely a stutter loop)",
                            job.voice_channel.guild.id, frames_enqueued,
                            frames_enqueued * PCM_FRAME_MS / 1000, len(job.text),
                        )
                        try:
                            proc.kill()
                        except ProcessLookupError:
                            pass
                        break
                leftover = buf[n:]
        finally:
            await feeder
            if leftover and not limit_hit:
                source.enqueue_frames(
                    [leftover + b"\x00" * (PCM_FRAME_BYTES - len(leftover))]
                )
                frames_enqueued += 1
            try:
                await proc.wait()
            except Exception:
                pass

        part_path = (str(cache_final) + ".part") if cache_final is not None else None

        def _discard_cache() -> None:
            if part_path:
                try:
                    os.unlink(part_path)
                except OSError:
                    pass

        if limit_hit:
            _discard_cache()  # a stutter loop must never be cached
            return ("truncated", frames_enqueued)
        if mid_error:
            _discard_cache()  # never cache a partial stream
            log.warning(
                "Stream failed mid-playback after %d frames (%s); truncated",
                frames_enqueued, mid_error[0],
            )
            return ("truncated", frames_enqueued)
        if frames_enqueued == 0:
            _discard_cache()
            return ("pre_audio", 0)
        # Clean finish: finalize the cache file so repeats skip the API.
        if cache is not None and cache_final is not None and part_path:
            try:
                os.replace(part_path, cache_final)
                cache.commit_file(job.text, cache_final, voice.name)
            except OSError:
                _discard_cache()
        return ("ok", frames_enqueued)

    # ------------------------------------------------------------------
    # Prefetch pipeline (TTS_PREFETCH_ENABLED): generation_worker produces
    # PreparedAudio ahead of playback_worker, which plays strictly FIFO.
    # ------------------------------------------------------------------

    async def _generation_worker(self) -> None:
        await self.wait_until_ready()
        await self.warmup_tts()
        log.info("TTS generation worker started (lookahead=%d)", config.TTS_PREFETCH_LOOKAHEAD)
        while not self.is_closed():
            job = await self.message_queue.get()
            prepared = PreparedAudio(job=job, channel=asyncio.Queue())
            self.active_prepared.add(prepared)
            try:
                # Backpressure: blocks here when we are already `lookahead`
                # messages ahead of playback.
                await self.ready_queue.put(prepared)
                await self._prepare_into(prepared)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("TTS generation pipeline error")
                await prepared.channel.put(None)
            finally:
                self.message_queue.task_done()

    async def _prepare_into(self, prepared: PreparedAudio) -> None:
        """Generate a job's audio into ``prepared.channel`` (frame batches)."""
        job = prepared.job
        voice = self.voice_registry.get(job.voice_profile) or self.voice_registry.fallback_record()
        try:
            if prepared.cancelled:
                return
            if self._should_attempt_stream(voice):
                status = await self._generate_stream_into(prepared, voice)
                if status != "pre_audio":
                    return  # ok / truncated / cache / cancelled
                voice = self.voice_registry.fallback_record()  # pre-audio -> Piper
            await self._generate_file_into(prepared, voice)
        finally:
            # Always terminate the channel so the consumer never hangs.
            await prepared.channel.put(None)

    async def _safe_pcm_frames(self, path: Path) -> list[bytes]:
        try:
            return await self.prepare_tts_pcm_frames(path)
        except Exception:
            log.exception("PCM decode failed for %s", path)
            return []

    async def _generate_stream_into(self, prepared: PreparedAudio, voice) -> str:
        job = prepared.job
        cb = self.tts_dispatcher.circuit_breaker
        cache = self.tts_dispatcher.cache
        if cache is not None:
            cached = cache.lookup(job.text, voice.name)
            if cached is not None:
                frames = limit_pcm_frames(await self._safe_pcm_frames(cached), job.text, voice)
                if frames and not prepared.cancelled:
                    await prepared.channel.put(frames)
                    prepared.provider = "cache"
                    log.info(
                        "Cache HIT (prefetch) guild=%s voice=%s frames=%d (no API)",
                        job.guild_id, voice.name, len(frames),
                    )
                    return "cache"
        if prepared.cancelled:
            return "cancelled"
        if not cb.allow_request():
            return "pre_audio"  # breaker open -> caller does Piper fallback
        # Set the label before streaming so the playback worker logs the
        # provider on the first frame (a pre-audio fallback overwrites it
        # with the Piper provider in _generate_file_into).
        prepared.provider = "minimax"
        status, _ = await self._stream_to_channel(prepared, voice)
        if status == "pre_audio":
            cb.record_failure()
            return "pre_audio"
        if status == "cancelled":
            return "cancelled"
        cb.record_success() if status == "ok" else cb.record_failure()
        return status

    async def _generate_file_into(self, prepared: PreparedAudio, voice) -> None:
        if prepared.cancelled:
            return
        job = prepared.job
        filename = config.TMP_DIR / f"tts_{uuid.uuid4().hex}.wav"
        try:
            prepared.provider = await self.tts_dispatcher.synthesize(
                job.text, filename, voice=voice
            )
            if prepared.cancelled:
                return
            frames = limit_pcm_frames(await self._safe_pcm_frames(filename), job.text, voice)
            if frames:
                await prepared.channel.put(frames)
        finally:
            if filename.exists():
                try:
                    filename.unlink()
                except OSError:
                    log.exception("Failed to remove temp file: %s", filename)

    async def _stream_to_channel(self, prepared: PreparedAudio, voice) -> tuple[str, int]:
        """Stream MiniMax -> ffmpeg -> frame batches into ``prepared.channel``.

        Like _stream_tts_to_source but writes to the prefetch channel (not the
        live player) and honors cancellation. Returns (status, frames) with
        status in ok/truncated/pre_audio/cancelled.
        """
        job = prepared.job
        cloud = self.tts_dispatcher.cloud
        mm = voice.minimax
        agen = cloud.stream_audio(
            job.text, voice_id=mm.voice_id, model=mm.model, speed=mm.speed,
            vol=mm.vol, pitch=mm.pitch, emotion=mm.emotion, language_boost=mm.language_boost,
        )
        try:
            first_chunk = await asyncio.wait_for(
                agen.__anext__(), timeout=config.TTS_STREAM_TTFA_TIMEOUT
            )
        except StopAsyncIteration:
            await agen.aclose()
            log.warning("Stream produced no audio; Piper fallback")
            return ("pre_audio", 0)
        except asyncio.TimeoutError:
            await agen.aclose()
            log.warning("Stream TTFA exceeded %.2fs; Piper fallback", config.TTS_STREAM_TTFA_TIMEOUT)
            return ("pre_audio", 0)
        except Exception as exc:
            await agen.aclose()
            log.warning("Stream failed before first audio (%s: %s); Piper fallback",
                        type(exc).__name__, exc)
            return ("pre_audio", 0)
        if prepared.cancelled:
            await agen.aclose()
            return ("cancelled", 0)

        proc = await asyncio.create_subprocess_exec(
            *build_tts_stream_pcm_command(),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        mid_error: list[BaseException] = []
        cache = self.tts_dispatcher.cache
        cache_final: Path | None = None
        cache_part = None
        if cache is not None:
            try:
                cache_final = cache.cache_path_for(job.text, voice.name)
                cache_final.parent.mkdir(parents=True, exist_ok=True)
                cache_part = open(str(cache_final) + ".part", "wb")
            except OSError:
                cache_final = None
                cache_part = None

        async def _feed() -> None:
            try:
                proc.stdin.write(first_chunk)
                await proc.stdin.drain()
                if cache_part is not None:
                    cache_part.write(first_chunk)
                async for chunk in agen:
                    if prepared.cancelled:
                        break
                    proc.stdin.write(chunk)
                    await proc.stdin.drain()
                    if cache_part is not None:
                        cache_part.write(chunk)
            except Exception as exc:
                mid_error.append(exc)
            finally:
                try:
                    await agen.aclose()
                except Exception:
                    pass
                try:
                    proc.stdin.close()
                except Exception:
                    pass
                if cache_part is not None:
                    try:
                        cache_part.close()
                    except Exception:
                        pass

        feeder = asyncio.create_task(_feed())
        frame_limit = playback_frame_limit(job.text, voice)
        frames_count = 0
        limit_hit = False
        leftover = b""
        try:
            while True:
                data = await proc.stdout.read(PCM_FRAME_BYTES * 16)
                if not data:
                    break
                buf = leftover + data
                n = len(buf) - (len(buf) % PCM_FRAME_BYTES)
                if n and not prepared.cancelled:
                    out_frames = [buf[i:i + PCM_FRAME_BYTES] for i in range(0, n, PCM_FRAME_BYTES)]
                    if frame_limit is not None and frames_count + len(out_frames) > frame_limit:
                        out_frames = out_frames[:max(frame_limit - frames_count, 0)]
                        limit_hit = True
                    if out_frames:
                        await prepared.channel.put(out_frames)
                        frames_count += len(out_frames)
                    if limit_hit:
                        log.warning(
                            "Audio length limit hit guild=%s: %d frames (%.1fs) for %d chars; "
                            "aborting stream (likely a stutter loop)",
                            job.guild_id, frames_count,
                            frames_count * PCM_FRAME_MS / 1000, len(job.text),
                        )
                        try:
                            proc.kill()
                        except ProcessLookupError:
                            pass
                        break
                leftover = buf[n:]
        finally:
            await feeder
            if leftover and not prepared.cancelled and not limit_hit:
                await prepared.channel.put(
                    [leftover + b"\x00" * (PCM_FRAME_BYTES - len(leftover))]
                )
                frames_count += 1
            try:
                await proc.wait()
            except Exception:
                pass

        part_path = (str(cache_final) + ".part") if cache_final is not None else None

        def _discard() -> None:
            if part_path:
                try:
                    os.unlink(part_path)
                except OSError:
                    pass

        if prepared.cancelled:
            _discard()
            return ("cancelled", frames_count)
        if limit_hit:
            _discard()  # a stutter loop must never be cached
            return ("truncated", frames_count)
        if mid_error:
            _discard()
            log.warning("Stream failed mid-stream after %d frames (%s); truncated",
                        frames_count, mid_error[0])
            return ("truncated", frames_count)
        if frames_count == 0:
            _discard()
            return ("pre_audio", 0)
        if cache is not None and cache_final is not None and part_path:
            try:
                os.replace(part_path, cache_final)
                cache.commit_file(job.text, cache_final, voice.name)
            except OSError:
                _discard()
        return ("ok", frames_count)

    async def tts_worker(self) -> None:
        await self.wait_until_ready()
        await self.warmup_tts()
        log.info("TTS worker started")

        while not self.is_closed():
            job = await self.message_queue.get()
            filename = config.TMP_DIR / f"tts_{uuid.uuid4().hex}.wav"

            try:
                worker_started = time.perf_counter()
                voice = (
                    self.voice_registry.get(job.voice_profile)
                    or self.voice_registry.fallback_record()
                )

                # Streaming fast path: a MiniMax voice over the continuous
                # stream plays chunks as they arrive (lower Time-To-First-
                # Audio). On a pre-audio failure or an open breaker it returns
                # "fallback" and we drop to the Piper file path below.
                file_voice_name = job.voice_profile
                if self._should_attempt_stream(voice):
                    outcome = await self._run_streaming_job(job, voice, worker_started)
                    if outcome == "done":
                        continue
                    # Pre-audio fallback: use Piper directly, never re-hit cloud.
                    file_voice_name = self.voice_registry.fallback_profile

                connect_task = asyncio.create_task(self.ensure_voice(job.voice_channel))
                tts_task = asyncio.create_task(self.generate_tts_file(job.text, filename, file_voice_name))

                try:
                    vc, _ = await asyncio.gather(connect_task, tts_task)
                except Exception:
                    connect_error = connect_task.exception() if connect_task.done() else None
                    tts_error = tts_task.exception() if tts_task.done() else None
                    if connect_error:
                        log.exception("Voice prepare failed", exc_info=connect_error)
                        if not tts_task.done():
                            tts_task.cancel()
                        vc = discord.utils.get(self.voice_clients, guild=job.voice_channel.guild)
                        if vc and vc.is_connected():
                            await self.disconnect_guild_voice(job.voice_channel.guild)
                        else:
                            log.info(
                                "Skip disconnect cleanup guild=%s reason=voice_not_connected",
                                job.voice_channel.guild.id,
                            )
                    elif tts_error:
                        log.exception("TTS generation failed; keeping voice session", exc_info=tts_error)
                    else:
                        log.exception("TTS processing failed before playback")
                    continue

                log.info(
                    "Ready to play guild=%s channel=%s queue_wait=%.3fs prep_total=%.3fs",
                    job.voice_channel.guild.id,
                    job.voice_channel.id,
                    worker_started - job.queued_at,
                    time.perf_counter() - worker_started,
                )

                try:
                    if config.TTS_CONTINUOUS_STREAM:
                        source = self.ensure_continuous_player(vc)
                        file_voice = (
                            self.voice_registry.get(file_voice_name)
                            or self.voice_registry.fallback_record()
                        )
                        frames = limit_pcm_frames(
                            await self.prepare_tts_pcm_frames(filename), job.text, file_voice
                        )
                        audio_enqueue_ts = time.perf_counter()
                        source.enqueue_frames(frames)
                        log.info(
                            "Queued continuous playback guild=%s channel=%s frames=%s duration=%.3fs message_to_audio_enqueue_s=%.3f queue_to_audio_enqueue_s=%.3f",
                            job.voice_channel.guild.id,
                            job.voice_channel.id,
                            len(frames),
                            len(frames) * PCM_FRAME_MS / 1000,
                            audio_enqueue_ts - job.message_ts,
                            audio_enqueue_ts - job.queued_at,
                        )
                        await source.wait_until_drained()
                    else:
                        playback_request_ts = time.perf_counter()
                        log.info(
                            "Starting non-continuous playback metrics message_to_playback_request_s=%.3f queue_to_playback_request_s=%.3f",
                            playback_request_ts - job.message_ts,
                            playback_request_ts - job.queued_at,
                        )
                        await self.play_file(vc, filename)
                except Exception:
                    log.exception("Playback failed; disconnecting voice")
                    await self.disconnect_guild_voice(job.voice_channel.guild)
                    continue

                log.info(
                    "Playback finished guild=%s channel=%s total_since_queue=%.3fs",
                    job.voice_channel.guild.id,
                    job.voice_channel.id,
                    time.perf_counter() - job.queued_at,
                )

                self.schedule_continuous_idle_stop(job.voice_channel.guild)
                self.schedule_idle_disconnect(job.voice_channel.guild)

            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("TTS processing failed")
                await self.disconnect_guild_voice(job.voice_channel.guild)
            finally:
                if filename.exists():
                    try:
                        filename.unlink()
                    except OSError:
                        log.exception("Failed to remove temp file: %s", filename)
                self.message_queue.task_done()
