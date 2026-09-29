"""Synthesis pipeline for TTSBot: providers, streaming and workers.

Covers Piper file generation, the MiniMax, Fish, Gemini and ElevenLabs streaming paths with their
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
from ttsbot.elevenlabs import ElevenLabsError, ElevenLabsRequestError
from ttsbot.errors import QuotaExhaustedError
from ttsbot.fish import FishError
from ttsbot.gemini import GeminiError
from ttsbot.pcm import PcmFramer, pcm_cache_header, pcm_to_frames, read_pcm_cache
from ttsbot.providers import (
    MiniMaxError,
    MiniMaxProvider,
    MiniMaxVoiceNotFoundError,
    load_minimax_config_from_env,
    voice_cache_key,
)
from ttsbot import config
from ttsbot.audio import (
    PCM_FRAME_BYTES,
    PCM_FRAME_MS,
    ContinuousTTSAudioSource,
    build_tts_stream_pcm_command,
)
from ttsbot.models import PreparedAudio, TTSJob, VOICE_PROFILES, VoiceProfile
from ttsbot.ogg_opus import (
    FRAME_CACHE_MAGIC,
    OggOpusDemuxer,
    UnsupportedOpusStream,
    read_frame_cache,
    write_frame,
)

log = logging.getLogger("tts_bot")

# Appended to a Fish/ElevenLabs cache key for Discord-ready .dopus frame
# files, so they never collide with the raw Ogg (.opus) entry of the request.
DISCORD_OPUS_CACHE_SUFFIX = ":discord-opus-v1"


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
    fish = getattr(voice, "fish", None)
    if getattr(voice, "is_fish", False) and fish is not None:
        return max(float(getattr(fish, "speed", 1.0) or 1.0), 0.1)
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


def _log_stream_done(label: str, job, status: str, frames: int, started: float) -> None:
    """One line per provider stream: how long the whole download took next
    to how long it plays. download_s well under audio_s means the next
    message is generated long before this one finishes playing."""
    log.info(
        "%s stream done guild=%s job=%s status=%s frames=%d audio_s=%.2f download_s=%.3f",
        label, job.guild_id, job.job_id, status, frames, frames * PCM_FRAME_MS / 1000,
        time.perf_counter() - started,
    )


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

    def _keepalive_warmers(self) -> dict:
        """Warmers of the cloud voices of allowed users in the bot's voice
        channels, by provider name."""
        warmers = {}
        for vc in self.voice_clients:
            channel = getattr(vc, "channel", None)
            if not vc.is_connected() or not isinstance(channel, discord.VoiceChannel):
                continue
            guild_id = channel.guild.id
            if not self.config_store.is_enabled(guild_id):
                continue
            for member in channel.members:
                if member.bot or not self.config_store.is_allowed(guild_id, member.id):
                    continue
                record = self.voice_registry.get(self.config_store.voice_for_user(guild_id, member.id))
                provider = self.tts_dispatcher.provider_for(getattr(record, "provider", None))
                warmer = getattr(provider, "warmer", None)
                if warmer is not None:
                    warmers[warmer.name] = warmer
        return warmers

    async def _connection_keepalive_worker(self) -> None:
        """Keep the cloud connections of present users open (see config)."""
        await self.wait_until_ready()
        active: set[str] = set()
        while not self.is_closed():
            try:
                warmers = self._keepalive_warmers()
                if set(warmers) != active:
                    active = set(warmers)
                    log.info(
                        "Connection keep-alive providers=%s idle_s=%.0f",
                        ",".join(sorted(active)) or "none", config.TTS_CONNECTION_KEEPALIVE_SECONDS,
                    )
                for warmer in warmers.values():
                    warmer.maybe_warm("keepalive", idle=config.TTS_CONNECTION_KEEPALIVE_SECONDS)
            except Exception:
                log.exception("Connection keep-alive check failed")
            await asyncio.sleep(10)

    def warm_tts_connection(self, guild_id: int, user_id: int):
        """Warm the HTTP connection of the user's cloud voice (on typing).

        Returns the warm-up task, or None when the voice is local or its
        connection was used in the last 30 s.
        """
        record = self.voice_registry.get(self.config_store.voice_for_user(guild_id, user_id))
        provider = self.tts_dispatcher.provider_for(getattr(record, "provider", None))
        warmer = getattr(provider, "warmer", None)
        return warmer.maybe_warm() if warmer is not None else None

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

    async def register_fish_voice(
        self, *, name: str, reference_id: str, label: str, description: str,
    ) -> str:
        """Probe a Fish ``reference_id`` and save it as voice ``name``.

        Returns "" on success or a short reason (lowercase, for a message
        prefix). The name is re-checked after the probe because clone/add
        take long enough for another command to claim it meanwhile.
        """
        fish = self.tts_dispatcher.fish
        probe = config.TMP_DIR / f"fish_probe_{uuid.uuid4().hex}.opus"
        try:
            await fish.synthesize("Проверка голоса.", probe, reference_id=reference_id)
            with probe.open("rb") as audio:
                if audio.read(4) != b"OggS":
                    raise FishError("Fish returned no Ogg/Opus audio")
        except Exception as exc:
            return f"проверка озвучки не прошла: {type(exc).__name__}: {exc}"
        finally:
            probe.unlink(missing_ok=True)
        if name in self.voice_registry:
            return f"голос `{name}` уже существует"
        self.voice_registry.add(voice_registry.VoiceRecord(
            name=name,
            label=label,
            description=description,
            provider=voice_registry.PROVIDER_FISH,
            fish=voice_registry.FishParams(reference_id=reference_id),
        ))
        try:
            self.persist_voice_registry()
        except OSError as exc:
            self.voice_registry.voices.pop(name, None)
            log.exception("Failed to persist voices.json after adding Fish voice %s", name)
            return f"не удалось сохранить: {exc}"
        return ""

    async def register_gemini_voice(
        self, *, name: str, voice: str, label: str, description: str,
    ) -> str:
        """Probe a Gemini prebuilt ``voice`` and save it as voice ``name``.

        Returns "" on success or a short reason (lowercase, for a message
        prefix). The probe is one short billed request.
        """
        gemini = self.tts_dispatcher.gemini
        params = voice_registry.GeminiParams(voice=voice)
        try:
            _, _, pcm = await gemini.fetch_pcm("Проверка голоса.", params)
            if not pcm:
                raise GeminiError("Gemini returned no audio")
        except Exception as exc:
            return f"проверка озвучки не прошла: {type(exc).__name__}: {exc}"
        if name in self.voice_registry:
            return f"голос `{name}` уже существует"
        self.voice_registry.add(voice_registry.VoiceRecord(
            name=name,
            label=label,
            description=description,
            provider=voice_registry.PROVIDER_GEMINI,
            gemini=params,
        ))
        try:
            self.persist_voice_registry()
        except OSError as exc:
            self.voice_registry.voices.pop(name, None)
            log.exception("Failed to persist voices.json after adding Gemini voice %s", name)
            return f"не удалось сохранить: {exc}"
        return ""

    async def register_elevenlabs_voice(
        self, *, name: str, voice_id: str, label: str, description: str,
    ) -> str:
        """Probe an ElevenLabs ``voice_id`` and save it as voice ``name``.

        Returns "" on success or a short reason (lowercase, for a message
        prefix). The probe is one short billed request (~8 credits).
        """
        elevenlabs = self.tts_dispatcher.elevenlabs
        params = voice_registry.ElevenLabsParams(voice_id=voice_id)
        try:
            _, _, audio = await elevenlabs.fetch("Проверка голоса.", params)
            if not audio:
                raise ElevenLabsError("ElevenLabs returned no audio")
        except Exception as exc:
            return f"проверка озвучки не прошла: {type(exc).__name__}: {exc}"
        if name in self.voice_registry:
            return f"голос `{name}` уже существует"
        self.voice_registry.add(voice_registry.VoiceRecord(
            name=name,
            label=label,
            description=description,
            provider=voice_registry.PROVIDER_ELEVENLABS,
            elevenlabs=params,
        ))
        try:
            self.persist_voice_registry()
        except OSError as exc:
            self.voice_registry.voices.pop(name, None)
            log.exception("Failed to persist voices.json after adding ElevenLabs voice %s", name)
            return f"не удалось сохранить: {exc}"
        return ""

    async def clone_fish_voice(
        self, *, name: str, sample: bytes, filename: str, description: str,
    ) -> tuple[bool, str]:
        """Create, probe, and register a Fish voice for /voicebot voice-clone.

        On success the second tuple member is the new reference_id. On a
        post-creation failure the error includes the id so it can be recovered.
        """
        fish = getattr(self.tts_dispatcher, "fish", None)
        if fish is None:
            return False, "Fish Audio не настроен (нет FISH_API_KEY)."
        try:
            reference_id = await fish.clone_voice(
                sample, title=name, filename=filename, description=description,
            )
        except Exception as exc:
            return False, f"Не удалось клонировать в Fish: {type(exc).__name__}: {exc}"
        error = await self.register_fish_voice(
            name=name, reference_id=reference_id,
            label=f"{name} (Fish клон)", description=description,
        )
        if error:
            return False, f"Клон создан (reference_id=`{reference_id}`), но {error}"
        return True, reference_id

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
            and (
                (getattr(voice, "is_minimax", False) and self.tts_dispatcher.cloud is not None)
                or (getattr(voice, "is_fish", False) and self.tts_dispatcher.fish is not None)
                or (getattr(voice, "is_gemini", False) and self.tts_dispatcher.gemini is not None)
                or (
                    getattr(voice, "is_elevenlabs", False)
                    and getattr(self.tts_dispatcher, "elevenlabs", None) is not None
                )
            )
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
            cached = cache.lookup(job.text, voice_cache_key(voice))
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
            if isinstance(exc, QuotaExhaustedError):
                # The caller only sees "pre_audio"; trip the long pause here.
                self.tts_dispatcher.record_failure(self.tts_dispatcher.circuit_breaker, exc)
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
                cache_final = cache.cache_path_for(job.text, voice_cache_key(voice))
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
                cache.commit_file(job.text, cache_final, voice_cache_key(voice))
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
                # wait_s: time in the queue before generation began (the
                # previous message's download, or lookahead backpressure).
                log.info(
                    "Generation start guild=%s job=%s voice=%s wait_s=%.3f",
                    job.guild_id, job.job_id, job.voice_profile, time.perf_counter() - job.queued_at,
                )
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
            if getattr(voice, "is_fish", False) and self._should_attempt_stream(voice):
                status = await self._generate_fish_stream_into(prepared, voice)
                if status != "pre_audio":
                    return
                voice = self.voice_registry.fallback_record()
            if getattr(voice, "is_gemini", False) and self._should_attempt_stream(voice):
                status = await self._generate_gemini_stream_into(prepared, voice)
                if status != "pre_audio":
                    return
                voice = self.voice_registry.fallback_record()
            if getattr(voice, "is_elevenlabs", False) and self._should_attempt_stream(voice):
                status = await self._generate_elevenlabs_stream_into(prepared, voice)
                if status != "pre_audio":
                    return
                voice = self.voice_registry.fallback_record()
            if self._should_attempt_stream(voice):
                status = await self._generate_stream_into(prepared, voice)
                if status != "pre_audio":
                    return  # ok / truncated / cache / cancelled
                voice = self.voice_registry.fallback_record()  # pre-audio -> Piper
            await self._generate_file_into(prepared, voice)
        finally:
            # Always terminate the channel so the consumer never hangs.
            await prepared.channel.put(None)

    async def _safe_pcm_frames(self, path: Path, pitch: int = 0) -> list[bytes]:
        try:
            return await self.prepare_tts_pcm_frames(path, pitch)
        except Exception:
            log.exception("PCM decode failed for %s", path)
            return []

    async def _generate_stream_into(self, prepared: PreparedAudio, voice) -> str:
        job = prepared.job
        cb = self.tts_dispatcher.circuit_breaker
        cache = self.tts_dispatcher.cache
        if cache is not None:
            cached = cache.lookup(job.text, voice_cache_key(voice))
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
        if status == "ok":
            cb.record_success()
        elif status != "cancelled":
            self.tts_dispatcher.record_failure(cb, prepared.error)
        return status

    async def _generate_fish_stream_into(self, prepared: PreparedAudio, voice) -> str:
        job = prepared.job
        fish_cfg = self.tts_dispatcher.fish.config
        cb = self.tts_dispatcher.fish_circuit_breaker
        cache = self.tts_dispatcher.cache
        ogg_cache_key = fish_cfg.cache_key(voice.fish.reference_id, voice.fish)
        # Pitch is applied by ffmpeg, so only pitch 0 can skip decoding.
        direct = voice.fish.pitch == 0
        if cache is not None:
            if direct:
                cached = cache.lookup(job.text, ogg_cache_key + DISCORD_OPUS_CACHE_SUFFIX)
            else:
                cached = cache.lookup(job.text, ogg_cache_key)
            if cached is not None:
                if direct:
                    try:
                        frames = limit_pcm_frames(list(read_frame_cache(cached)), job.text, voice)
                    except (OSError, UnsupportedOpusStream):
                        log.warning("Invalid Discord Opus cache file %s; regenerating", cached)
                        frames = []
                else:
                    frames = limit_pcm_frames(
                        await self._safe_pcm_frames(cached, voice.fish.pitch), job.text, voice,
                    )
                if frames and not prepared.cancelled:
                    await prepared.channel.put(frames)
                    prepared.provider = "cache"
                    return "cache"
        if prepared.cancelled:
            return "cancelled"
        if not cb.allow_request():
            log.info("Fish circuit breaker open; Piper fallback guild=%s", job.guild_id)
            return "pre_audio"
        prepared.provider = "fish"
        stream = self._stream_fish_opus_to_channel if direct else self._stream_fish_to_channel
        status, _ = await stream(prepared, voice, ogg_cache_key, request_config=fish_cfg)
        if status == "ok":
            cb.record_success()
        elif status != "cancelled":
            self.tts_dispatcher.record_failure(cb, prepared.error)
        return status

    async def _generate_gemini_stream_into(self, prepared: PreparedAudio, voice) -> str:
        """Gemini PCM through PcmFramer: cache, breaker, then the API.

        No ffmpeg on this path: raw PCM is resampled and framed in-process,
        both for a cache hit and for a live response.
        """
        job = prepared.job
        gemini = self.tts_dispatcher.gemini
        cb = self.tts_dispatcher.gemini_circuit_breaker
        cache = self.tts_dispatcher.cache
        cache_key = gemini.config.cache_key(voice.gemini)
        if cache is not None:
            cached = cache.lookup(job.text, cache_key)
            if cached is not None:
                try:
                    rate, channels, pcm = read_pcm_cache(cached)
                    frames = limit_pcm_frames(
                        pcm_to_frames(pcm, rate, channels, voice.gemini.volume_db), job.text, voice,
                    )
                except (OSError, ValueError) as exc:
                    log.warning("Invalid Gemini cache file %s (%s); regenerating", cached, exc)
                    frames = []
                if frames and not prepared.cancelled:
                    await prepared.channel.put(frames)
                    prepared.provider = "cache"
                    return "cache"
        if prepared.cancelled:
            return "cancelled"
        if not cb.allow_request():
            log.info("Gemini circuit breaker open; Piper fallback guild=%s", job.guild_id)
            return "pre_audio"
        prepared.provider = "gemini"
        status, _ = await self._stream_gemini_to_channel(prepared, voice, cache_key)
        if status == "ok":
            cb.record_success()
        elif status != "cancelled":
            self.tts_dispatcher.record_failure(cb, prepared.error)
        return status

    async def _generate_elevenlabs_stream_into(self, prepared: PreparedAudio, voice) -> str:
        """ElevenLabs: cache, breaker, then the streaming API.

        ``opus_48000_*`` output is Ogg/Opus with 20 ms packets, so it takes
        the Fish direct path (packets go to Discord as-is, cached as .dopus);
        ``pcm_*`` output takes the Gemini PcmFramer path. Neither uses ffmpeg
        unless the Opus packets turn out not to be 20 ms.
        """
        job = prepared.job
        elevenlabs = self.tts_dispatcher.elevenlabs
        cfg = elevenlabs.config
        cb = self.tts_dispatcher.elevenlabs_circuit_breaker
        cache = self.tts_dispatcher.cache
        params = voice.elevenlabs
        cache_key = cfg.cache_key(params)
        opus = cfg.kind == "opus"
        if cache is not None:
            cached = cache.lookup(job.text, cache_key + DISCORD_OPUS_CACHE_SUFFIX if opus else cache_key)
            if cached is not None:
                try:
                    if opus:
                        frames = list(read_frame_cache(cached))
                    else:
                        rate, channels, pcm = read_pcm_cache(cached)
                        frames = pcm_to_frames(pcm, rate, channels)
                    frames = limit_pcm_frames(frames, job.text, voice)
                except (OSError, ValueError) as exc:  # UnsupportedOpusStream is a ValueError
                    log.warning("Invalid ElevenLabs cache file %s (%s); regenerating", cached, exc)
                    frames = []
                if frames and not prepared.cancelled:
                    await prepared.channel.put(frames)
                    prepared.provider = "cache"
                    return "cache"
        if prepared.cancelled:
            return "cancelled"
        if not cb.allow_request():
            log.info("ElevenLabs circuit breaker open; Piper fallback guild=%s", job.guild_id)
            return "pre_audio"
        prepared.provider = "elevenlabs"
        if opus:
            status, _ = await self._stream_ogg_opus_to_channel(
                prepared, voice, elevenlabs.stream_audio(job.text, params), cache_key,
                ttfa_timeout=config.ELEVENLABS_TTFA_TIMEOUT, label="ElevenLabs",
            )
        else:
            status, _ = await self._stream_pcm_to_channel(
                prepared, voice, lambda: elevenlabs.open_stream(job.text, params), cache_key,
                budget=config.ELEVENLABS_TTFA_TIMEOUT, label="ElevenLabs",
            )
        if status == "ok":
            cb.record_success()
        elif isinstance(prepared.error, ElevenLabsRequestError):
            # A missing voice or one too-long message must not pause every
            # ElevenLabs voice.
            log.warning(
                "ElevenLabs cannot voice this message with %s (%s): %s; Piper fallback",
                voice.name, params.voice_id, prepared.error,
            )
        elif status != "cancelled":
            self.tts_dispatcher.record_failure(cb, prepared.error)
        return status

    async def _stream_gemini_to_channel(
        self, prepared: PreparedAudio, voice, cache_key: str,
    ) -> tuple[str, int]:
        """Gemini PCM into the shared PCM path.

        The first-audio budget grows with the text because OpenRouter sends
        nothing until the whole clip is generated.
        """
        job = prepared.job
        return await self._stream_pcm_to_channel(
            prepared, voice, lambda: self.tts_dispatcher.gemini.open_stream(job.text, voice.gemini),
            cache_key,
            budget=config.GEMINI_TTFA_TIMEOUT + config.GEMINI_TTFA_PER_CHAR * len(job.text),
            label="Gemini", volume_db=voice.gemini.volume_db,
        )

    async def _stream_pcm_to_channel(
        self, prepared: PreparedAudio, voice, open_stream, cache_key: str, *,
        budget: float, label: str, volume_db: float = 0.0,
    ) -> tuple[str, int]:
        """Frame raw PCM chunk by chunk into ``prepared.channel``, no ffmpeg.

        ``open_stream()`` returns a stream with ``rate``, ``channels``,
        ``chunks()`` and ``aclose()`` once the response headers are in.
        ``budget`` covers the first frame. Returns (status, frames) with
        status in ok/truncated/pre_audio/cancelled.
        """
        job = prepared.job
        started = time.perf_counter()
        deadline = time.monotonic() + budget
        frame_limit = playback_frame_limit(job.text, voice)
        frames_count = 0
        limit_hit = False
        status = "ok"
        stream = None
        cache = self.tts_dispatcher.cache
        cache_final: Path | None = None
        part_path: Path | None = None
        cache_part = None

        async def emit(frames: list[bytes]) -> None:
            nonlocal frames_count, limit_hit
            if frame_limit is not None and frames_count + len(frames) > frame_limit:
                frames = frames[:max(0, frame_limit - frames_count)]
                limit_hit = True
            if not frames:
                return
            if frames_count == 0:
                now = time.perf_counter()
                log.info(
                    "%s first frame guild=%s job=%s message_to_frame_s=%.3f request_to_frame_s=%.3f chars=%d",
                    label, job.guild_id, job.job_id, now - job.message_ts, now - started, len(job.text),
                )
            await prepared.channel.put(frames)
            frames_count += len(frames)

        try:
            stream = await asyncio.wait_for(open_stream(), timeout=budget)
            framer = PcmFramer(stream.rate, stream.channels, volume_db)
            if cache is not None:
                try:
                    cache_final = cache.cache_path_for(job.text, cache_key, "pcm")
                    cache_final.parent.mkdir(parents=True, exist_ok=True)
                    part_path = cache_final.parent / f"{cache_final.stem}.{uuid.uuid4().hex}.tmp"
                    cache_part = part_path.open("wb")
                    cache_part.write(pcm_cache_header(stream.rate, stream.channels))
                except OSError:
                    cache_final = cache_part = None
            chunks = stream.chunks()
            try:
                while True:
                    try:
                        if frames_count:
                            chunk = await chunks.__anext__()
                        else:
                            chunk = await asyncio.wait_for(
                                chunks.__anext__(), timeout=max(deadline - time.monotonic(), 0),
                            )
                    except StopAsyncIteration:
                        break
                    if prepared.cancelled:
                        status = "cancelled"
                        break
                    if cache_part is not None:
                        try:
                            cache_part.write(chunk)
                        except OSError:
                            cache_part.close()
                            cache_part = None
                    await emit(framer.feed(chunk))
                    if limit_hit:
                        break
            finally:
                await chunks.aclose()
            if status == "ok" and not limit_hit:
                await emit(framer.flush())
        except asyncio.TimeoutError:
            log.warning(
                "%s first audio exceeded %.2fs; Piper fallback guild=%s chars=%d",
                label, budget, job.guild_id, len(job.text),
            )
            status = "truncated" if frames_count else "pre_audio"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            prepared.error = exc
            log.warning(
                "%s stream failed after %d frames (%s: %s)",
                label, frames_count, type(exc).__name__, exc,
            )
            status = "truncated" if frames_count else "pre_audio"
        finally:
            if stream is not None:
                await stream.aclose()
            if cache_part is not None:
                cache_part.close()

        if prepared.cancelled:
            status = "cancelled"
        elif limit_hit:
            status = "truncated"
            log.warning("%s audio length limit hit guild=%s frames=%d", label, job.guild_id, frames_count)
        elif status == "ok" and not frames_count:
            log.warning("%s stream produced no audio; Piper fallback guild=%s", label, job.guild_id)
            prepared.error = RuntimeError(f"{label} returned no audio")
            status = "pre_audio"
        _log_stream_done(label, job, status, frames_count, started)
        if part_path is not None:
            if status == "ok" and cache_final is not None and cache_part is not None:
                try:
                    os.replace(part_path, cache_final)
                    cache.commit_file(job.text, cache_final, cache_key)
                except OSError:
                    part_path.unlink(missing_ok=True)
            else:
                part_path.unlink(missing_ok=True)
        return status, frames_count

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
            pitch = voice.fish.pitch if getattr(voice, "is_fish", False) and prepared.provider in {"fish", "cache"} else 0
            frames = limit_pcm_frames(await self._safe_pcm_frames(filename, pitch), job.text, voice)
            if frames:
                await prepared.channel.put(frames)
        finally:
            if filename.exists():
                try:
                    filename.unlink()
                except OSError:
                    log.exception("Failed to remove temp file: %s", filename)

    async def _stream_to_channel(self, prepared: PreparedAudio, voice) -> tuple[str, int]:
        """Stream MiniMax MP3 into the shared incremental decoder."""
        mm = voice.minimax
        agen = self.tts_dispatcher.cloud.stream_audio(
            prepared.job.text, voice_id=mm.voice_id, model=mm.model, speed=mm.speed,
            vol=mm.vol, pitch=mm.pitch, emotion=mm.emotion, language_boost=mm.language_boost,
        )
        return await self._decode_stream_to_channel(
            prepared, voice, agen, "mp3", voice_cache_key(voice), "mp3",
        )

    async def _stream_fish_opus_to_channel(
        self, prepared: PreparedAudio, voice, ogg_cache_key: str, *, request_config=None,
    ) -> tuple[str, int]:
        """Fish Ogg/Opus into the shared direct-packet path."""
        agen = self.tts_dispatcher.fish.stream_audio(
            prepared.job.text, reference_id=voice.fish.reference_id, params=voice.fish,
            request_config=request_config,
        )
        return await self._stream_ogg_opus_to_channel(
            prepared, voice, agen, ogg_cache_key, ttfa_timeout=config.FISH_TTFA_TIMEOUT, label="Fish",
        )

    async def _stream_ogg_opus_to_channel(
        self, prepared: PreparedAudio, voice, agen, ogg_cache_key: str, *,
        ttfa_timeout: float, label: str,
    ) -> tuple[str, int]:
        """Demux an Ogg/Opus byte stream directly into 20 ms Discord packets.

        ``ttfa_timeout`` covers the first audio packet, not just the first
        HTTP bytes (the Ogg header pages come before any audio). If the
        packets cannot go to Discord as-is, the bytes already received are
        handed to the ffmpeg path instead of paying for a second request.
        """
        job = prepared.job
        started = time.perf_counter()
        cache_key = ogg_cache_key + DISCORD_OPUS_CACHE_SUFFIX
        demuxer = OggOpusDemuxer()
        frame_limit = playback_frame_limit(job.text, voice)
        deadline = time.monotonic() + ttfa_timeout
        frames_count = 0
        limit_hit = False
        to_ffmpeg = False
        received: list[bytes] = []  # bytes before the first packet, for the ffmpeg fallback
        status = "ok"
        cache = self.tts_dispatcher.cache
        cache_final: Path | None = None
        part_path: Path | None = None
        cache_part = None
        if cache is not None:
            try:
                cache_final = cache.cache_path_for(job.text, cache_key, "dopus")
                cache_final.parent.mkdir(parents=True, exist_ok=True)
                part_path = cache_final.parent / f"{cache_final.stem}.{uuid.uuid4().hex}.tmp"
                cache_part = part_path.open("wb")
                cache_part.write(FRAME_CACHE_MAGIC)
            except OSError:
                cache_final = part_path = cache_part = None

        async def next_chunk() -> bytes | None:
            try:
                if frames_count:
                    return await agen.__anext__()
                return await asyncio.wait_for(
                    agen.__anext__(), timeout=max(deadline - time.monotonic(), 0),
                )
            except StopAsyncIteration:
                return None

        async def consume(chunk: bytes) -> None:
            nonlocal frames_count, limit_hit, cache_part
            packets = demuxer.feed(chunk)
            if frame_limit is not None and frames_count + len(packets) > frame_limit:
                packets = packets[:max(0, frame_limit - frames_count)]
                limit_hit = True
            if not packets:
                return
            if frames_count == 0:
                now = time.perf_counter()
                log.info(
                    "%s direct first packet guild=%s job=%s message_to_packet_s=%.3f request_to_packet_s=%.3f",
                    label, job.guild_id, job.job_id, now - job.message_ts, now - started,
                )
            if cache_part is not None:
                try:
                    for packet in packets:
                        write_frame(cache_part, packet)
                except OSError:
                    cache_part.close()
                    cache_part = None
            await prepared.channel.put(packets)
            frames_count += len(packets)

        try:
            while True:
                chunk = await next_chunk()
                if chunk is None:
                    if frames_count:
                        demuxer.finish()
                    else:
                        log.warning("%s stream produced no audio; Piper fallback guild=%s", label, job.guild_id)
                        status = "pre_audio"
                    break
                if prepared.cancelled:
                    status = "cancelled"
                    break
                if not frames_count:
                    received.append(chunk)
                await consume(chunk)
                if limit_hit:
                    break
        except asyncio.TimeoutError:
            log.warning(
                "%s first audio exceeded %.2fs; Piper fallback guild=%s chars=%d",
                label, ttfa_timeout, job.guild_id, len(job.text),
            )
            status = "pre_audio"
        except UnsupportedOpusStream as exc:
            if frames_count:
                log.warning("%s direct Opus stream rejected after %d packets: %s", label, frames_count, exc)
                status = "truncated"
            else:
                log.warning("%s Opus packets not playable directly (%s); decoding with ffmpeg", label, exc)
                to_ffmpeg = True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            prepared.error = exc
            log.warning("%s direct Opus stream failed after %d packets: %s", label, frames_count, exc)
            status = "truncated" if frames_count else "pre_audio"
        finally:
            if not to_ffmpeg:
                await agen.aclose()
            if cache_part is not None:
                cache_part.close()

        if to_ffmpeg:
            if part_path is not None:
                part_path.unlink(missing_ok=True)

            async def replay():
                try:
                    for chunk in received:
                        yield chunk
                    async for chunk in agen:
                        yield chunk
                finally:
                    await agen.aclose()

            return await self._decode_stream_to_channel(
                prepared, voice, replay(), "ogg", ogg_cache_key, "opus",
                ttfa_timeout=max(deadline - time.monotonic(), 0.5),
            )

        if prepared.cancelled:
            status = "cancelled"
        elif limit_hit:
            status = "truncated"
            log.warning("%s direct audio length limit hit guild=%s frames=%d", label, job.guild_id, frames_count)
        _log_stream_done(label, job, status, frames_count, started)
        if part_path is not None:
            if status == "ok" and cache_final is not None and cache_part is not None:
                try:
                    os.replace(part_path, cache_final)
                    cache.commit_file(job.text, cache_final, cache_key)
                except OSError:
                    part_path.unlink(missing_ok=True)
            else:
                part_path.unlink(missing_ok=True)
        return status, frames_count

    async def _stream_fish_to_channel(
        self, prepared: PreparedAudio, voice, cache_key: str, *, request_config=None,
    ) -> tuple[str, int]:
        """Stream Fish Ogg/Opus into the shared incremental decoder."""
        agen = self.tts_dispatcher.fish.stream_audio(
            prepared.job.text, reference_id=voice.fish.reference_id, params=voice.fish,
            request_config=request_config,
        )
        return await self._decode_stream_to_channel(
            prepared, voice, agen, "ogg", cache_key, "opus", ttfa_timeout=config.FISH_TTFA_TIMEOUT,
        )

    async def _decode_stream_to_channel(
        self, prepared: PreparedAudio, voice, agen, input_format: str,
        cache_key: str, cache_suffix: str, *, ttfa_timeout: float | None = None,
    ) -> tuple[str, int]:
        """Tee compressed audio to an atomic cache file and ffmpeg stdin.

        Like _stream_tts_to_source but writes to the prefetch channel (not the
        live player) and honors cancellation. Returns (status, frames) with
        status in ok/truncated/pre_audio/cancelled.
        """
        job = prepared.job
        if ttfa_timeout is None:
            ttfa_timeout = config.TTS_STREAM_TTFA_TIMEOUT
        # The budget runs until the first decoded frame: an Ogg stream's first
        # chunk may be only headers, so the first chunk alone proves nothing.
        deadline = time.monotonic() + ttfa_timeout
        try:
            first_chunk = await asyncio.wait_for(agen.__anext__(), timeout=ttfa_timeout)
        except StopAsyncIteration:
            await agen.aclose()
            log.warning("Stream produced no audio; Piper fallback")
            return ("pre_audio", 0)
        except asyncio.TimeoutError:
            await agen.aclose()
            log.warning("Stream TTFA exceeded %.2fs; Piper fallback", ttfa_timeout)
            return ("pre_audio", 0)
        except Exception as exc:
            await agen.aclose()
            log.warning("Stream failed before first audio (%s: %s); Piper fallback",
                        type(exc).__name__, exc)
            prepared.error = exc
            return ("pre_audio", 0)
        if prepared.cancelled:
            await agen.aclose()
            return ("cancelled", 0)

        try:
            proc = await asyncio.create_subprocess_exec(
                *build_tts_stream_pcm_command(
                    input_format,
                    voice.fish.pitch if input_format == "ogg" and getattr(voice, "is_fish", False) else 0,
                ),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError:
            await agen.aclose()
            log.exception("Could not start ffmpeg for %s stream; Piper fallback", input_format)
            return ("pre_audio", 0)
        mid_error: list[BaseException] = []
        cache = self.tts_dispatcher.cache
        cache_final: Path | None = None
        cache_part = None
        part_path: Path | None = None
        if cache is not None:
            try:
                cache_final = cache.cache_path_for(job.text, cache_key, cache_suffix)
                cache_final.parent.mkdir(parents=True, exist_ok=True)
                part_path = cache_final.parent / f"{cache_final.stem}.{uuid.uuid4().hex}.tmp"
                cache_part = part_path.open("wb")
            except OSError:
                cache_final = None
                cache_part = None
                part_path = None

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
        ttfa_expired = False
        leftover = b""
        try:
            while True:
                read = proc.stdout.read(PCM_FRAME_BYTES * 16)
                if frames_count:
                    data = await read
                else:
                    try:
                        data = await asyncio.wait_for(
                            read, timeout=max(deadline - time.monotonic(), 0),
                        )
                    except asyncio.TimeoutError:
                        ttfa_expired = True
                        feeder.cancel()
                        try:
                            proc.kill()
                        except ProcessLookupError:
                            pass
                        break
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
            await asyncio.gather(feeder, return_exceptions=True)
            if leftover and not prepared.cancelled and not limit_hit and not ttfa_expired:
                await prepared.channel.put(
                    [leftover + b"\x00" * (PCM_FRAME_BYTES - len(leftover))]
                )
                frames_count += 1
            try:
                await proc.wait()
            except Exception:
                pass

        def _discard() -> None:
            if part_path:
                try:
                    part_path.unlink()
                except OSError:
                    pass

        if prepared.cancelled:
            _discard()
            return ("cancelled", frames_count)
        if ttfa_expired:
            _discard()
            log.warning("Stream first audio exceeded %.2fs; Piper fallback", ttfa_timeout)
            return ("pre_audio", 0)
        if limit_hit:
            _discard()  # a stutter loop must never be cached
            return ("truncated", frames_count)
        if mid_error:
            _discard()
            log.warning("Stream failed mid-stream after %d frames (%s); truncated",
                        frames_count, mid_error[0])
            return ("truncated", frames_count)
        if proc.returncode != 0:
            _discard()
            log.warning("ffmpeg stream decode failed rc=%s format=%s", proc.returncode, input_format)
            return ("truncated" if frames_count else "pre_audio", frames_count)
        if frames_count == 0:
            _discard()
            return ("pre_audio", 0)
        if cache is not None and cache_final is not None and part_path:
            try:
                os.replace(part_path, cache_final)
                cache.commit_file(job.text, cache_final, cache_key)
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

                # The legacy single-worker mode still streams Fish, Gemini and
                # ElevenLabs audio. Reuse the same producer/consumer path as prefetch,
                # without changing the continuous Discord player.
                if (
                    getattr(voice, "is_fish", False) or getattr(voice, "is_gemini", False)
                    or getattr(voice, "is_elevenlabs", False)
                ) and self._should_attempt_stream(voice):
                    prepared = PreparedAudio(job=job, channel=asyncio.Queue())
                    self.active_prepared.add(prepared)
                    generator = asyncio.create_task(self._prepare_into(prepared))
                    try:
                        await self._play_prepared(prepared)
                        await generator
                    finally:
                        if not generator.done():
                            generator.cancel()
                            await asyncio.gather(generator, return_exceptions=True)
                        self.active_prepared.discard(prepared)
                    continue

                # Streaming fast path: a MiniMax voice over the continuous
                # stream plays chunks as they arrive (lower Time-To-First-
                # Audio). On a pre-audio failure or an open breaker it returns
                # "fallback" and we drop to the Piper file path below.
                file_voice_name = job.voice_profile
                if getattr(voice, "is_minimax", False) and self._should_attempt_stream(voice):
                    outcome = await self._run_streaming_job(job, voice, worker_started)
                    if outcome == "done":
                        continue
                    # Pre-audio fallback: use Piper directly, never re-hit cloud.
                    file_voice_name = self.voice_registry.fallback_profile

                connect_task = asyncio.create_task(self.ensure_voice(job.voice_channel))
                tts_task = asyncio.create_task(self.generate_tts_file(job.text, filename, file_voice_name))

                try:
                    vc, provider_used = await asyncio.gather(connect_task, tts_task)
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
                    file_voice = (
                        self.voice_registry.get(file_voice_name)
                        or self.voice_registry.fallback_record()
                    )
                    pitch = (
                        file_voice.fish.pitch
                        if getattr(file_voice, "is_fish", False) and provider_used in {"fish", "cache"}
                        else 0
                    )
                    if config.TTS_CONTINUOUS_STREAM:
                        source = self.ensure_continuous_player(vc)
                        frames = limit_pcm_frames(
                            await self.prepare_tts_pcm_frames(filename, pitch), job.text, file_voice
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
                        await self.play_file(vc, filename, pitch)
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
