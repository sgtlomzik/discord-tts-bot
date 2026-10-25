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
from ttsbot.elevenlabs import ElevenLabsError
from ttsbot.fish import FishError
from ttsbot.gemini import GeminiError
from ttsbot.health import FailureKind, classify_failure
from ttsbot.pcm import PcmFramer, pcm_cache_header, pcm_to_frames, read_pcm_cache
from ttsbot.minimax import MiniMaxError, MiniMaxVoiceNotFoundError
from ttsbot import config
from ttsbot.audio import (
    PCM_FRAME_BYTES,
    PCM_FRAME_MS,
    build_tts_stream_pcm_command,
)
from ttsbot.models import PreparedAudio, VOICE_PROFILES, VoiceProfile, piper_default_profile
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
            # Preload the Piper ONNX model so the first real request
            # (including a fallback to Piper) is not cold. Cloud providers
            # are HTTP: nothing to warm here, and it would cost a request.
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
                provider = self._speaking_provider(record)
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

    async def _provider_health_worker(self) -> None:
        """Check paused providers on a timer (see TTSDispatcher.check_health)."""
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                await self.tts_dispatcher.check_health()
            except Exception:
                log.exception("Provider health check failed")
            await asyncio.sleep(config.TTS_HEALTH_CHECK_INTERVAL)

    def warm_tts_connection(self, guild_id: int, user_id: int):
        """Warm the HTTP connection of the user's cloud voice (on typing).

        Returns the warm-up task, or None when the voice is local or its
        connection was used in the last 30 s.
        """
        record = self.voice_registry.get(self.config_store.voice_for_user(guild_id, user_id))
        provider = self._speaking_provider(record)
        warmer = getattr(provider, "warmer", None)
        return warmer.maybe_warm() if warmer is not None else None

    def _speaking_provider(self, record):
        """The cloud provider that will speak for ``record`` now: its own,
        or, while that one is paused, the next one of its fallback chain
        (None when Piper speaks)."""
        if record is None:
            return None
        dispatcher = self.tts_dispatcher
        for voice in self.voice_registry.chain_for(record):
            provider = dispatcher.provider_for(voice.provider)
            if provider is None:
                if voice.is_piper:
                    return None
                continue
            breaker = dispatcher.breaker_for(voice.provider)
            if breaker is None or breaker.state.value == "closed":
                return provider
        return None

    def _resolve_piper_profile(self, voice_profile: str | None) -> VoiceProfile:
        """Resolve a voice name to a Piper ``VoiceProfile`` via the registry.

        Falls back to the registry ``fallback_profile`` then ``VOICE_PROFILES``
        so a missing or non-Piper name still yields a usable Piper voice.
        """
        name = (
            voice_profile
            or self.voice_registry.fallback_profile
            or piper_default_profile()
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
        return VOICE_PROFILES.get(name) or VOICE_PROFILES[piper_default_profile()]

    def persist_voice_registry(self) -> None:
        """Save the current catalog (one transaction).

        On failure the in-memory catalog goes back to what is on disk, so
        a command never leaves a voice in effect that was not saved; callers
        only report the error (a StateWriteError, which is an OSError).
        """
        try:
            voice_registry.save_registry(self.state_db, self.voice_registry)
        except Exception:
            saved = getattr(self, "_saved_registry", None)
            if saved is not None:
                self.voice_registry.restore(saved)
            raise
        self._saved_registry = self.voice_registry.snapshot()

    async def validate_minimax_voice(self, voice_id: str) -> tuple[bool, str]:
        """Probe a MiniMax voice_id with a short phrase.

        Returns (ok, error_message). ok=True means status_code 0; a 2054
        (voice id not exist) yields a clear rejection. Used by voice-add
        before persisting a new voice.
        """
        cloud = self.tts_dispatcher.provider_for("minimax")
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
        fish = self.tts_dispatcher.provider_for("fish")
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
            log.exception("Failed to save the voice catalog after adding Fish voice %s", name)
            return f"не удалось сохранить: {exc}"
        return ""

    async def register_gemini_voice(
        self, *, name: str, voice: str, label: str, description: str,
    ) -> str:
        """Probe a Gemini prebuilt ``voice`` and save it as voice ``name``.

        Returns "" on success or a short reason (lowercase, for a message
        prefix). The probe is one short billed request.
        """
        gemini = self.tts_dispatcher.provider_for("gemini")
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
            log.exception("Failed to save the voice catalog after adding Gemini voice %s", name)
            return f"не удалось сохранить: {exc}"
        return ""

    async def register_elevenlabs_voice(
        self, *, name: str, voice_id: str, label: str, description: str,
    ) -> str:
        """Probe an ElevenLabs ``voice_id`` and save it as voice ``name``.

        Returns "" on success or a short reason (lowercase, for a message
        prefix). The probe is one short billed request (~8 credits).
        """
        elevenlabs = self.tts_dispatcher.provider_for("elevenlabs")
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
            log.exception("Failed to save the voice catalog after adding ElevenLabs voice %s", name)
            return f"не удалось сохранить: {exc}"
        return ""

    async def clone_fish_voice(
        self, *, name: str, sample: bytes, filename: str, description: str,
    ) -> tuple[bool, str]:
        """Create, probe, and register a Fish voice for /voicebot voice-clone.

        On success the second tuple member is the new reference_id. On a
        post-creation failure the error includes the id so it can be recovered.
        """
        fish = self.tts_dispatcher.provider_for("fish")
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

    def voice_chain(self, voice) -> list:
        """``voice`` and its fallback voices (see VoiceRegistry.chain_for)."""
        return list(self.voice_registry.chain_for(voice))

    def _should_attempt_stream(self, voice) -> bool:
        """True when the voice's cloud provider is configured (it streams)."""
        return voice is not None and self.tts_dispatcher.provider_for(getattr(voice, "provider", None)) is not None

    # ------------------------------------------------------------------
    # Prefetch pipeline (TTS_PREFETCH_ENABLED): generation_worker produces
    # PreparedAudio ahead of playback_worker, which plays strictly FIFO.
    # ------------------------------------------------------------------

    async def _generation_worker(self, session) -> None:
        """Start generating each of the guild's messages in order.

        A message's generation starts as soon as it fits the prefetch window
        (``session.ready``, which also fixes the play order), so the next
        message is generated while the current one is still downloading or
        playing.
        """
        await self.wait_until_ready()
        log.info("TTS generation worker started guild=%s lookahead=%d",
                 session.guild_id, config.TTS_PREFETCH_LOOKAHEAD)
        while not self.is_closed():
            job = await session.queue.get()
            prepared = PreparedAudio(job=job, channel=asyncio.Queue())
            session.active.add(prepared)
            try:
                # Backpressure: waits while `lookahead` messages are ready ahead.
                await session.ready.put(prepared)
            except BaseException:
                prepared.channel.put_nowait(None)
                raise
            finally:
                session.queue.task_done()
            # wait_s: time in the queue before generation began.
            log.info(
                "Generation start guild=%s job=%s voice=%s wait_s=%.3f",
                job.guild_id, job.job_id, job.voice_profile, time.perf_counter() - job.queued_at,
            )
            task = asyncio.create_task(self._generate(prepared), name=f"tts-generate:{job.guild_id}:{job.job_id}")
            session.generating.add(task)
            task.add_done_callback(session.generating.discard)

    async def _generate(self, prepared: PreparedAudio) -> None:
        """Generate one message; its channel always ends with the sentinel."""
        try:
            await self._prepare_into(prepared)
        except asyncio.CancelledError:
            prepared.channel.put_nowait(None)
            raise
        except Exception:
            log.exception("TTS generation pipeline error job=%s", prepared.job.job_id)
            await prepared.channel.put(None)

    async def _prepare_into(self, prepared: PreparedAudio) -> None:
        """Generate a job's audio into ``prepared.channel`` (frame batches).

        Walks the voice's fallback chain: each cloud voice streams in turn
        until one produces audio; a failure before any audio moves on to the
        next voice. A provider that failed for a reason other than this one
        message (or is paused) is skipped for the rest of the chain, and so
        is a voice whose provider is not configured. The first Piper voice
        is synthesized to a file and ends the chain.
        """
        job = prepared.job
        voice = self.voice_registry.get(job.voice_profile) or self.voice_registry.fallback_record()
        try:
            if prepared.cancelled:
                return
            chain = self.voice_chain(voice) or [voice]
            skipped: set[str] = set()
            for index, candidate in enumerate(chain):
                if prepared.cancelled:
                    return
                if getattr(candidate, "provider", None) in skipped:
                    continue
                if candidate is None or candidate.is_piper:
                    await self._generate_file_into(prepared, candidate)
                    return
                if not self._should_attempt_stream(candidate):
                    continue  # its provider is not configured
                prepared.error = None
                status = await self._stream_voice_into(prepared, candidate)
                if status != "pre_audio":
                    return  # ok / truncated / cache / cancelled
                if classify_failure(prepared.error) is not FailureKind.REQUEST:
                    skipped.add(candidate.provider)
                nxt = next((v for v in chain[index + 1:] if v.provider not in skipped), None)
                self._log_fallback(job, candidate, nxt, prepared.error)
            log.error("No voice could speak job=%s guild=%s chain=%s", job.job_id, job.guild_id,
                      ",".join(v.name for v in chain))
        finally:
            # Always terminate the channel so the consumer never hangs.
            await prepared.channel.put(None)

    async def _stream_voice_into(self, prepared: PreparedAudio, voice) -> str:
        """Stream one voice into ``prepared.channel``: cache, breaker, API.

        Works for any registered provider: it describes the stream
        (``stream_spec``), this method does the rest. Returns ok,
        truncated, pre_audio (nothing played: the next voice speaks),
        cache or cancelled.
        """
        job = prepared.job
        dispatcher = self.tts_dispatcher
        provider = dispatcher.provider_for(voice.provider)
        breaker = dispatcher.breaker_for(voice.provider)
        spec = provider.stream_spec(job.text, voice)
        frames = await self._cached_frames(spec, job.text, voice)
        if frames and not prepared.cancelled:
            await prepared.channel.put(frames)
            prepared.provider = "cache"
            return "cache"
        if prepared.cancelled:
            return "cancelled"
        if not breaker.allow_request():
            log.info("%s circuit breaker open; next voice guild=%s", spec.label, job.guild_id)
            return "pre_audio"
        # Set before streaming so the playback worker logs the provider on
        # the first frame.
        prepared.provider = voice.provider
        status, _ = await self._run_stream(prepared, voice, spec)
        if status == "ok" or (status == "truncated" and prepared.error is None):
            # Cut without an error (the length limit on a stutter loop, an
            # Opus stream that changed format): the provider answered.
            breaker.record_success()
        elif status != "cancelled":
            # A request-only error (missing voice, one message too long) is
            # not counted: it must not pause the provider's other voices.
            dispatcher.record_failure(breaker, prepared.error)
        return status

    async def _cached_frames(self, spec, text: str, voice) -> list[bytes]:
        """The cached audio of ``spec`` as frames, or [] (miss or unreadable)."""
        cache = self.tts_dispatcher.cache
        if cache is None:
            return []
        if spec.kind == "opus":
            cached = cache.lookup(text, spec.cache_key + DISCORD_OPUS_CACHE_SUFFIX)
        else:
            cached = cache.lookup(text, spec.cache_key)
        if cached is None:
            return []
        try:
            if spec.kind == "opus":
                frames = list(read_frame_cache(cached))
            elif spec.kind == "pcm":
                rate, channels, pcm = read_pcm_cache(cached)
                frames = pcm_to_frames(pcm, rate, channels, spec.volume_db)
            else:
                frames = await self._safe_pcm_frames(cached, spec.pitch)
        except (OSError, ValueError) as exc:  # UnsupportedOpusStream is a ValueError
            log.warning("Invalid %s cache file %s (%s); regenerating", spec.label, cached, exc)
            return []
        return limit_pcm_frames(frames, text, voice)

    async def _run_stream(self, prepared: PreparedAudio, voice, spec) -> tuple[str, int]:
        """Play ``spec``'s stream by its kind; (status, frames)."""
        if spec.kind == "opus":
            return await self._stream_ogg_opus_to_channel(
                prepared, voice, spec.open(), spec.cache_key, ttfa_timeout=spec.ttfa, label=spec.label,
            )
        if spec.kind == "pcm":
            return await self._stream_pcm_to_channel(
                prepared, voice, spec.open, spec.cache_key,
                budget=spec.ttfa, label=spec.label, volume_db=spec.volume_db,
            )
        return await self._decode_stream_to_channel(
            prepared, voice, spec.open(), "ogg" if spec.kind == "ogg" else "mp3", spec.cache_key,
            "opus" if spec.kind == "ogg" else "mp3", ttfa_timeout=spec.ttfa, pitch=spec.pitch,
        )

    def _log_fallback(self, job, voice, nxt, error) -> None:
        """One line per fallback step; quiet (INFO) while the provider is paused."""
        breaker = self.tts_dispatcher.breaker_for(voice.provider)
        paused = error is None and breaker is not None and breaker.state.value != "closed"
        if error is not None:
            reason = f"{type(error).__name__}: {error}"
        elif paused:
            reason = breaker.describe()
        else:
            reason = "no audio in time"
        log.log(
            logging.INFO if paused else logging.WARNING,
            "Fallback guild=%s job=%s voice=%s -> %s reason=%s",
            job.guild_id, job.job_id, voice.name, nxt.name if nxt else "none", reason[:200],
        )

    async def _safe_pcm_frames(self, path: Path, pitch: int = 0) -> list[bytes]:
        try:
            return await self.prepare_tts_pcm_frames(path, pitch)
        except Exception:
            log.exception("PCM decode failed for %s", path)
            return []

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
        """Synthesize the Piper ``voice`` to a file and put its frames into the channel."""
        if prepared.cancelled:
            return
        job = prepared.job
        filename = config.TMP_DIR / f"tts_{uuid.uuid4().hex}.wav"
        try:
            prepared.provider = await self.tts_dispatcher.synthesize_piper(job.text, filename, voice)
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

    async def _decode_stream_to_channel(
        self, prepared: PreparedAudio, voice, agen, input_format: str,
        cache_key: str, cache_suffix: str, *, ttfa_timeout: float | None = None, pitch: int = 0,
    ) -> tuple[str, int]:
        """Tee compressed audio to an atomic cache file and ffmpeg stdin.

        Writes to the prefetch channel (not the
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
                *build_tts_stream_pcm_command(input_format, pitch),
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

        def _tee(chunk: bytes) -> None:
            # A failed cache write (a full disk) only stops caching this
            # message: the audio keeps going and the provider is not blamed.
            nonlocal cache_part, cache_final
            if cache_part is None:
                return
            try:
                cache_part.write(chunk)
            except OSError as exc:
                log.warning("TTS cache write failed (%s); not caching this message", exc)
                try:
                    cache_part.close()
                except OSError:
                    pass
                cache_part = cache_final = None

        async def _feed() -> None:
            try:
                proc.stdin.write(first_chunk)
                await proc.stdin.drain()
                _tee(first_chunk)
                async for chunk in agen:
                    if prepared.cancelled:
                        break
                    proc.stdin.write(chunk)
                    await proc.stdin.drain()
                    _tee(chunk)
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
        except BaseException:
            # Cancelled (shutdown) or failed: a feeder waiting on a stalled
            # stream would otherwise keep this task and ffmpeg alive forever.
            feeder.cancel()
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            raise
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
            prepared.error = mid_error[0]  # counts against the provider
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
        else:
            _discard()  # caching stopped part-way: drop the partial file
        return ("ok", frames_count)
