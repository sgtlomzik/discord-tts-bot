"""MiniMax T2A provider plugin: HTTP client, errors and configuration.

Enabled by MINIMAX_API_KEY; voices come from the registry (voice-add
provider:minimax). Streams MP3 over SSE (decoded by ffmpeg); the file
method serves the voice-add probe.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator, Optional

import httpx

from ttsbot import config as bot_config
from ttsbot.errors import AuthError, QuotaExhaustedError, RateLimitError, RequestError
from ttsbot.httpclient import attach_warmer, provider_limits
from ttsbot.providers import voice_cache_key
from ttsbot.streams import StreamSpec
from ttsbot.textnorm import derive_auto_emotion

log = logging.getLogger("tts_bot.minimax")

NAME = "minimax"


class MiniMaxError(Exception):
    """Base class for MiniMax failures that should trigger fallback.

    Subclasses let the bot log a precise reason without parsing strings.
    """


class MiniMaxAuthError(MiniMaxError, AuthError):
    """Invalid API key / Group ID. Also covers missing config."""


class MiniMaxQuotaError(MiniMaxError, RateLimitError):
    """HTTP 429 (rate-limited) or explicit quota status_code."""


class MiniMaxQuotaExhaustedError(MiniMaxQuotaError, QuotaExhaustedError):
    """Balance or plan exhausted: retrying soon is pointless."""


# 1008 insufficient balance, 2056 Token Plan usage limit.
MINIMAX_EXHAUSTED_STATUS_CODES = frozenset({1008, 2056})


MINIMAX_RATE_LIMIT_STATUS_CODES = frozenset({1039})


def _base_resp_status(payload: object, missing: int = -1) -> tuple[int, str]:
    """Return ``(status_code, status_msg)`` from a MiniMax JSON body.

    ``missing`` is used when there is no ``base_resp``/``status_code`` at all
    (streamed audio events omit it); a malformed code maps to -1.
    """
    base_resp = payload.get("base_resp") if isinstance(payload, dict) else None
    if not isinstance(base_resp, dict) or base_resp.get("status_code") is None:
        return missing, ""
    try:
        code = int(base_resp["status_code"])
    except (TypeError, ValueError):
        code = -1
    return code, str(base_resp.get("status_msg") or "")


class MiniMaxTimeoutError(MiniMaxError):
    """Request exceeded TTS_REQUEST_TIMEOUT."""


class MiniMaxVoiceNotFoundError(MiniMaxError, RequestError):
    """status_code 2054 — the requested voice_id does not exist."""


@dataclass
class MiniMaxConfig:
    api_key: str = field(default="", repr=False)  # never in logs or tracebacks
    group_id: str = ""
    voice_id: str = ""
    model: str = "speech-2.8-turbo"
    base_url: str = "https://api.minimax.io"
    language_boost: str = "Russian"
    timeout_seconds: float = 2.5
    sample_rate: int = 32000
    bitrate: int = 128000


def load_minimax_config_from_env() -> MiniMaxConfig:
    """Build a ``MiniMaxConfig`` from the standard ``MINIMAX_*`` env vars.

    Missing keys collapse to empty strings; the provider raises
    ``MiniMaxAuthError`` at call time if it is invoked without an API
    key or voice_id. This keeps the bot bootable even when only Piper
    is configured.
    """
    base_url = os.getenv("MINIMAX_BASE_URL", "https://api.minimax.io").strip()
    if not base_url:
        base_url = "https://api.minimax.io"
    model = os.getenv("MINIMAX_MODEL", "speech-2.8-turbo").strip()
    if not model:
        model = "speech-2.8-turbo"
    lang = os.getenv("MINIMAX_LANGUAGE_BOOST", "Russian").strip()
    if not lang:
        lang = "Russian"
    return MiniMaxConfig(
        api_key=os.getenv("MINIMAX_API_KEY", "").strip(),
        group_id=os.getenv("MINIMAX_GROUP_ID", "").strip(),
        voice_id=os.getenv("MINIMAX_VOICE_ID", "").strip(),
        model=model,
        base_url=base_url,
        language_boost=lang,
        timeout_seconds=bot_config._number("TTS_REQUEST_TIMEOUT", 2.5, float),
        sample_rate=bot_config._number("MINIMAX_SAMPLE_RATE", 32000, int),
        bitrate=bot_config._number("MINIMAX_BITRATE", 128000, int),
    )


# MiniMax t2a emotion values (besides "" = omit / provider default).
MINIMAX_EMOTIONS = (
    "neutral", "happy", "sad", "angry", "fearful", "disgusted", "surprised",
)


def resolve_emotion(emotion: str, text: str) -> str:
    """Translate the ``"auto"`` sentinel into a concrete emotion for the given
    text; pass any other value through unchanged."""
    return derive_auto_emotion(text) if emotion == "auto" else emotion


class MiniMaxProvider:
    """Calls MiniMax ``POST /v1/t2a_v2`` and writes MP3 to ``filename``.

    Per the rollout plan §11.1 (corrections feedback), this provider
    holds ONE long-lived ``httpx.AsyncClient`` with keep-alive so the
    TLS handshake is paid once per process lifetime, not once per
    message. This trims ~0.3s off every request after the first.

    Tracks a session-level cumulative ``usage_characters`` counter so
    operators can monitor quota burn across bot restarts (counter
    resets only on process restart — by design, since MiniMax
    accounting is per-account, not per-process).
    """

    name = "minimax"

    def __init__(
        self,
        config: MiniMaxConfig,
        *,
        http_client: Optional["httpx.AsyncClient"] = None,
    ) -> None:
        # Imported lazily so the rest of the provider layer remains
        # usable on systems where httpx is missing (e.g. local CI runs
        # that only exercise the local provider).
        import httpx

        self._config = config
        self._owns_client = http_client is None
        # Keep-alive pool sized for the bot's modest concurrency
        # (one or two in-flight requests). The bot's queue is much
        # larger but each request is short, so 4 keep-alives is plenty.
        self._client = http_client or httpx.AsyncClient(
            timeout=httpx.Timeout(config.timeout_seconds),
            limits=provider_limits(max_connections=8, max_keepalive_connections=4),
        )
        # This client has no base_url, so the warm-up targets the API host.
        self.warmer = attach_warmer(self._client, "MiniMax", config.base_url.rstrip("/") + "/")
        # Cumulative chars billed this process lifetime. Logged on
        # every successful synthesis for quota monitoring.
        self._session_chars: int = 0

    @property
    def session_chars(self) -> int:
        """Cumulative MiniMax characters billed since process start."""
        return self._session_chars

    async def aclose(self) -> None:
        """Gracefully close the underlying HTTP client.

        Call from the bot's ``close()`` hook. No-op when the client was
        injected (tests typically pass their own).
        """
        if self._owns_client:
            await self._client.aclose()

    def _build_url(self) -> str:
        base = self._config.base_url.rstrip("/")
        url = f"{base}/v1/t2a_v2"
        # Per MiniMax docs: if the response indicates an invalid api
        # key, retry with ?GroupId=<id>. We always add it when set so
        # the auth state matches what the user copied from the console.
        if self._config.group_id:
            url = f"{url}?GroupId={self._config.group_id}"
        return url

    def _build_body(
        self,
        text: str,
        *,
        voice_id: str,
        model: str,
        speed: float,
        vol: float,
        pitch: int,
        emotion: str,
        language_boost: str,
    ) -> dict:
        cfg = self._config
        # "auto" -> a concrete emotion derived from this text (cache-safe).
        emotion = resolve_emotion(emotion, text)
        voice_setting: dict = {
            "voice_id": voice_id,
            "speed": speed,
            "vol": vol,
            "pitch": pitch,
        }
        # MiniMax rejects an empty emotion; only include it when set.
        if emotion:
            voice_setting["emotion"] = emotion
        return {
            "model": model,
            "text": text,
            "stream": False,
            "language_boost": language_boost,
            "output_format": "hex",
            "voice_setting": voice_setting,
            "audio_setting": {
                "sample_rate": cfg.sample_rate,
                "bitrate": cfg.bitrate,
                "format": "mp3",
                "channel": 1,
            },
        }

    async def synthesize(
        self,
        text: str,
        filename: Path,
        *,
        voice_id: Optional[str] = None,
        model: Optional[str] = None,
        speed: Optional[float] = None,
        vol: Optional[float] = None,
        pitch: Optional[int] = None,
        emotion: Optional[str] = None,
        language_boost: Optional[str] = None,
    ) -> None:
        """Synthesize ``text`` to ``filename``.

        Voice parameters default to the env-seeded config when not passed;
        the dispatcher supplies them per-call from the registry record.
        """
        cfg = self._config
        if not cfg.api_key:
            raise MiniMaxAuthError("MINIMAX_API_KEY is not set")
        eff_voice_id = voice_id or cfg.voice_id
        if not eff_voice_id:
            raise MiniMaxAuthError("MINIMAX voice_id is not set")

        import httpx  # lazy import, see __init__ for rationale

        url = self._build_url()
        headers = {
            "Authorization": f"Bearer {cfg.api_key}",
            "Content-Type": "application/json",
        }
        body = self._build_body(
            text,
            voice_id=eff_voice_id,
            model=model or cfg.model,
            speed=1.0 if speed is None else speed,
            vol=1.0 if vol is None else vol,
            pitch=0 if pitch is None else pitch,
            emotion=emotion or "",
            language_boost=language_boost or cfg.language_boost,
        )

        log.debug("MiniMax POST %s text=%d chars", url, len(text))

        try:
            response = await self._client.post(url, headers=headers, json=body)
        except httpx.TimeoutException as exc:
            raise MiniMaxTimeoutError(
                f"MiniMax request timed out after {cfg.timeout_seconds}s"
            ) from exc
        except httpx.HTTPError as exc:
            # Network failures, connection resets, DNS errors, etc.
            raise MiniMaxError(f"MiniMax network error: {exc}") from exc

        if response.status_code == 429:
            raise MiniMaxQuotaError("MiniMax rate-limited (HTTP 429)")

        if response.status_code >= 400:
            excerpt = response.text[:200] if response.text else ""
            raise MiniMaxError(
                f"MiniMax HTTP {response.status_code}: {excerpt}"
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise MiniMaxError(
                f"MiniMax returned non-JSON body: {response.text[:200]}"
            ) from exc

        status_code, status_msg = _base_resp_status(payload)
        if status_code != 0:
            # 'invalid api key' is the canonical case for adding
            # GroupId per MiniMax docs. Distinct exception type so the
            # bot logs a clear "auth" reason, not a generic failure.
            if "invalid api key" in status_msg.lower() or status_code in (1002, 1004):
                raise MiniMaxAuthError(
                    f"MiniMax auth failed (status_code={status_code}): {status_msg}"
                )
            self._raise_for_status_code(status_code, status_msg)

        data = payload.get("data") or {}
        audio_hex = data.get("audio")
        if not audio_hex:
            raise MiniMaxError("MiniMax response missing data.audio")

        try:
            audio_bytes = bytes.fromhex(audio_hex)
        except ValueError as exc:
            raise MiniMaxError(
                f"MiniMax audio field is not valid hex: {exc}"
            ) from exc

        filename.write_bytes(audio_bytes)
        # Track quota usage. usage_characters is the number MiniMax
        # actually billed for this request (may differ slightly from
        # len(text) due to language_boost normalization).
        usage = 0
        extra = payload.get("extra_info") or {}
        if isinstance(extra, dict):
            try:
                usage = int(extra.get("usage_characters") or 0)
            except (TypeError, ValueError):
                usage = 0
        self._session_chars += usage
        log.info(
            "MiniMax usage chars=%d session_total=%d text_len=%d audio_bytes=%d",
            usage, self._session_chars, len(text), len(audio_bytes),
        )
        log.debug(
            "MiniMaxProvider synthesized text=%d chars audio=%d bytes file=%s",
            len(text), len(audio_bytes), filename,
        )

    def _build_clone_url(self, path: str) -> str:
        base = self._config.base_url.rstrip("/")
        url = f"{base}{path}"
        if self._config.group_id:
            url = f"{url}?GroupId={self._config.group_id}"
        return url

    async def clone_voice(
        self,
        sample: bytes,
        *,
        voice_id: str,
        filename: str = "sample.mp3",
        model: str = "speech-2.8-hd",
    ) -> None:
        """Upload ``sample`` audio and trigger a MiniMax voice clone.

        Two calls, mirroring ``scripts/clone_voice.py`` but reusing this
        provider's keep-alive client: ``POST /v1/files/upload`` (multipart,
        ``purpose=voice_clone``) to obtain an integer ``file_id``, then
        ``POST /v1/voice_clone`` to register ``voice_id``. Both override the
        short realtime-synth timeout with a generous 60s budget — upload and
        the server-side clone are far slower than a t2a request. Raises a
        ``MiniMaxError`` subclass on any failure so the caller can surface a
        precise reason; on success ``voice_id`` is immediately usable via
        :meth:`synthesize`.
        """
        cfg = self._config
        if not cfg.api_key:
            raise MiniMaxAuthError("MINIMAX_API_KEY is not set")
        if not sample:
            raise MiniMaxError("empty audio sample")
        size_mb = len(sample) / (1024 * 1024)
        if size_mb > 20:
            raise MiniMaxError(
                f"sample is {size_mb:.1f} MB; MiniMax limit is 20 MB"
            )

        import httpx  # lazy import, see __init__ for rationale

        # Upload + server-side clone are slow relative to a realtime synth;
        # override the short keep-alive timeout for these two calls only.
        timeout = httpx.Timeout(60.0, connect=10.0)
        auth = {"Authorization": f"Bearer {cfg.api_key}"}

        # --- 1) upload the sample, get an integer file_id --------------
        upload_url = self._build_clone_url("/v1/files/upload")
        files = {"file": (filename, sample, "application/octet-stream")}
        try:
            resp = await self._client.post(
                upload_url,
                headers=auth,
                files=files,
                data={"purpose": "voice_clone"},
                timeout=timeout,
            )
        except httpx.TimeoutException as exc:
            raise MiniMaxTimeoutError("MiniMax upload timed out") from exc
        except httpx.HTTPError as exc:
            raise MiniMaxError(f"MiniMax upload network error: {exc}") from exc
        if resp.status_code >= 400:
            raise MiniMaxError(
                f"MiniMax upload HTTP {resp.status_code}: {resp.text[:200]}"
            )
        try:
            payload = resp.json()
        except ValueError as exc:
            raise MiniMaxError(
                f"MiniMax upload returned non-JSON: {resp.text[:200]}"
            ) from exc
        status_code, status_msg = _base_resp_status(payload, missing=0)
        if status_code != 0:
            self._raise_for_status_code(status_code, status_msg)
        file_id = (payload.get("file") or {}).get("file_id") or payload.get("file_id")
        if not file_id:
            raise MiniMaxError(f"MiniMax upload missing file_id: {payload}")

        # --- 2) trigger the clone --------------------------------------
        clone_url = self._build_clone_url("/v1/voice_clone")
        body = {
            "file_id": int(file_id),  # string file_id -> 2013 invalid params
            "voice_id": voice_id,
            "model": model,
        }
        headers = {**auth, "Content-Type": "application/json"}
        try:
            resp = await self._client.post(
                clone_url, headers=headers, json=body, timeout=timeout
            )
        except httpx.TimeoutException as exc:
            raise MiniMaxTimeoutError("MiniMax voice_clone timed out") from exc
        except httpx.HTTPError as exc:
            raise MiniMaxError(
                f"MiniMax voice_clone network error: {exc}"
            ) from exc
        if resp.status_code >= 400:
            raise MiniMaxError(
                f"MiniMax voice_clone HTTP {resp.status_code}: {resp.text[:200]}"
            )
        try:
            payload = resp.json()
        except ValueError as exc:
            raise MiniMaxError(
                f"MiniMax voice_clone returned non-JSON: {resp.text[:200]}"
            ) from exc
        status_code, status_msg = _base_resp_status(payload)
        if status_code != 0:
            self._raise_for_status_code(status_code, status_msg)
        log.info(
            "MiniMax voice cloned voice_id=%s model=%s file_id=%s sample_bytes=%d",
            voice_id, model, file_id, len(sample),
        )

    def _raise_for_status_code(self, status_code: int, status_msg: str) -> None:
        """Map a non-zero MiniMax status_code to a precise exception."""
        if "invalid api key" in status_msg.lower() or status_code in (1002, 1004):
            raise MiniMaxAuthError(
                f"MiniMax auth failed (status_code={status_code}): {status_msg}"
            )
        if status_code == 2054:
            raise MiniMaxVoiceNotFoundError(
                f"MiniMax voice id not exist (status_code=2054): {status_msg}"
            )
        if status_code in MINIMAX_EXHAUSTED_STATUS_CODES:
            raise MiniMaxQuotaExhaustedError(
                f"MiniMax quota exhausted (status_code={status_code}): {status_msg}"
            )
        if status_code in MINIMAX_RATE_LIMIT_STATUS_CODES:
            raise MiniMaxQuotaError(
                f"MiniMax rate-limited (status_code={status_code}): {status_msg}"
            )
        raise MiniMaxError(f"MiniMax status_code={status_code}: {status_msg}")

    def stream_spec(self, text: str, voice) -> StreamSpec:
        """MP3 chunks over SSE, decoded by ffmpeg."""
        mm = voice.minimax
        return StreamSpec(
            kind="mp3", cache_key=voice_cache_key(voice), ttfa=bot_config.TTS_STREAM_TTFA_TIMEOUT,
            label="MiniMax",
            open=lambda: self.stream_audio(
                text, voice_id=mm.voice_id, model=mm.model, speed=mm.speed, vol=mm.vol,
                pitch=mm.pitch, emotion=mm.emotion, language_boost=mm.language_boost,
            ),
        )

    async def stream_audio(
        self,
        text: str,
        *,
        voice_id: Optional[str] = None,
        model: Optional[str] = None,
        speed: Optional[float] = None,
        vol: Optional[float] = None,
        pitch: Optional[int] = None,
        emotion: Optional[str] = None,
        language_boost: Optional[str] = None,
    ) -> AsyncIterator[bytes]:
        """Yield decoded MP3 chunks from a streaming ``/v1/t2a_v2`` call.

        Uses ``stream: true`` + ``stream_options.exclude_aggregated_audio``
        so the final SSE event does NOT re-send the full clip (otherwise the
        audio would be duplicated). Each ``data:`` event carries a hex chunk
        in ``data.audio``; the final event (``data.status == 2``) carries
        ``extra_info.usage_characters``.

        Errors raised BEFORE the first yielded chunk let the caller fall back
        cleanly to Piper. An error event mid-stream raises as well; the caller
        must handle a partial stream (no clean rollback once audio is playing).
        """
        cfg = self._config
        if not cfg.api_key:
            raise MiniMaxAuthError("MINIMAX_API_KEY is not set")
        eff_voice_id = voice_id or cfg.voice_id
        if not eff_voice_id:
            raise MiniMaxAuthError("MINIMAX voice_id is not set")

        import httpx  # lazy import, see __init__ for rationale

        url = self._build_url()
        headers = {
            "Authorization": f"Bearer {cfg.api_key}",
            "Content-Type": "application/json",
        }
        body = self._build_body(
            text,
            voice_id=eff_voice_id,
            model=model or cfg.model,
            speed=1.0 if speed is None else speed,
            vol=1.0 if vol is None else vol,
            pitch=0 if pitch is None else pitch,
            emotion=emotion or "",
            language_boost=language_boost or cfg.language_boost,
        )
        body["stream"] = True
        # Without this the final event repeats the entire clip -> doubled audio.
        body["stream_options"] = {"exclude_aggregated_audio": True}

        log.debug("MiniMax STREAM POST %s text=%d chars", url, len(text))

        # The first-chunk (TTFA) budget is enforced by the caller. Here we
        # only need a generous per-read (inter-chunk) timeout so a long
        # message that legitimately streams for seconds is not truncated by
        # the short default request timeout; gaps between chunks are tiny in
        # practice, so this just guards against a fully stalled connection.
        stream_timeout = httpx.Timeout(
            connect=cfg.timeout_seconds,
            read=max(cfg.timeout_seconds, 10.0),
            write=cfg.timeout_seconds,
            pool=cfg.timeout_seconds,
        )

        usage = 0
        chunks = 0
        try:
            async with self._client.stream(
                "POST", url, headers=headers, json=body, timeout=stream_timeout
            ) as response:
                if response.status_code == 429:
                    raise MiniMaxQuotaError("MiniMax rate-limited (HTTP 429)")
                if response.status_code >= 400:
                    excerpt = (await response.aread())[:200].decode("utf-8", "replace")
                    raise MiniMaxError(
                        f"MiniMax HTTP {response.status_code}: {excerpt}"
                    )
                # Errors (quota, auth, bad voice) arrive as a plain JSON body,
                # possibly pretty-printed, instead of SSE events. Collect such
                # lines rather than trusting the Content-Type header.
                body_lines: list[str] = []
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        if line.strip() and not line.startswith((":", "event:", "id:", "retry:")):
                            body_lines.append(line)
                        continue
                    payload_str = line[len("data:"):].strip()
                    if not payload_str:
                        continue
                    try:
                        payload = json.loads(payload_str)
                    except ValueError:
                        continue
                    if not isinstance(payload, dict):
                        continue
                    sc, status_msg = _base_resp_status(payload, missing=0)
                    if sc != 0:
                        self._raise_for_status_code(sc, status_msg)
                    extra = payload.get("extra_info") or {}
                    if isinstance(extra, dict) and extra.get("usage_characters"):
                        try:
                            usage = int(extra.get("usage_characters") or 0)
                        except (TypeError, ValueError):
                            pass
                    data = payload.get("data") or {}
                    audio_hex = data.get("audio")
                    if audio_hex:
                        try:
                            chunk = bytes.fromhex(audio_hex)
                        except ValueError:
                            continue
                        if chunk:
                            chunks += 1
                            yield chunk
                if not chunks and body_lines:
                    raw = "\n".join(body_lines)
                    try:
                        payload = json.loads(raw)
                    except ValueError as exc:
                        raise MiniMaxError(
                            f"MiniMax returned non-SSE body: {raw[:200]!r}"
                        ) from exc
                    sc, status_msg = _base_resp_status(payload)
                    if sc != 0:
                        self._raise_for_status_code(sc, status_msg)
                    raise MiniMaxError("MiniMax returned JSON instead of an audio stream")
        except httpx.TimeoutException as exc:
            raise MiniMaxTimeoutError(
                f"MiniMax stream timed out after {cfg.timeout_seconds}s"
            ) from exc
        except httpx.HTTPError as exc:
            raise MiniMaxError(f"MiniMax stream network error: {exc}") from exc
        finally:
            if usage:
                self._session_chars += usage
                log.info(
                    "MiniMax usage (stream) chars=%d session_total=%d text_len=%d chunks=%d",
                    usage, self._session_chars, len(text), chunks,
                )


def create(store=None) -> Optional["MiniMaxProvider"]:
    """The MiniMax provider, or None without MINIMAX_API_KEY (its voices
    then fall to the next voice of their chain)."""
    cfg = load_minimax_config_from_env()
    if not cfg.api_key:
        log.info("MiniMax provider disabled (no MINIMAX_API_KEY)")
        return None
    log.info(
        "MiniMax provider enabled model=%s default_voice_id=%s base_url=%s timeout=%.1fs",
        cfg.model, cfg.voice_id or "(per-record)", cfg.base_url, cfg.timeout_seconds,
    )
    return MiniMaxProvider(cfg)
