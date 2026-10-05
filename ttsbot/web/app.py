"""aiohttp application: routes, errors, auth, CORS and the background upkeep.

Every API error is ``{"error": {"code": ..., "message": ...}}`` with a
matching status, so a frontend handles them in one place. Responses carry
``Cache-Control: no-store`` (the recordings are private) except audio,
which browsers fetch in ranges and may keep for the session.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import hmac
import logging
import zoneinfo
from pathlib import Path

from aiohttp import web

from ttsbot.archive.media import BuildFailed, MediaCache, TooBusy
from ttsbot.archive.retention import RetentionPolicy
from ttsbot.archive.service import API_PREFIX, ArchiveService, AudioUnavailable, SessionRecording
from ttsbot.archive.store import RecordingStore, SessionNotFound

log = logging.getLogger("tts_web")

SERVICE: web.AppKey[ArchiveService] = web.AppKey("service", ArchiveService)
_MAX_LIMIT = 200


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, headers: dict | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.headers = headers or {}


def _error(status: int, code: str, message: str, headers: dict | None = None) -> web.Response:
    response = web.json_response({"error": {"code": code, "message": message}}, status=status, headers=headers)
    response.headers["Cache-Control"] = "no-store"
    return response


@web.middleware
async def errors_middleware(request: web.Request, handler) -> web.StreamResponse:
    try:
        return await handler(request)
    except ApiError as exc:
        return _error(exc.status, exc.code, exc.message, dict(exc.headers))
    except SessionNotFound:
        return _error(404, "session_not_found", "No such session")
    except AudioUnavailable:
        return _error(404, "audio_unavailable", "This session has no audio left")
    except BuildFailed:  # details are in the log, not for clients
        return _error(503, "audio_build_failed", "The audio could not be prepared", {"Retry-After": "300"})
    except TooBusy:
        return _error(503, "busy", "Too many audio files are being prepared", {"Retry-After": "30"})
    except SessionRecording:
        return _error(409, "session_recording", "Available once the recording has ended")
    except web.HTTPException as exc:
        if not request.path.startswith("/api/") or exc.status < 400:
            raise
        kept = {k: v for k, v in exc.headers.items() if k.lower() in ("allow", "retry-after")}
        return _error(exc.status, exc.reason.lower().replace(" ", "_"), exc.reason, kept)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("Request failed %s %s", request.method, request.path)
        return _error(500, "internal_error", "Internal error")


def auth_middleware(username: str, password: str):
    """HTTP Basic auth when a password is set (browsers ask for it once,
    and send it with <audio> requests too, unlike a token header)."""

    @web.middleware
    async def middleware(request: web.Request, handler) -> web.StreamResponse:
        if password and request.method != "OPTIONS" and request.path != f"{API_PREFIX}/health":
            if not _authorized(request.headers.get("Authorization", ""), username, password):
                return _error(401, "unauthorized", "Authentication required",
                              {"WWW-Authenticate": 'Basic realm="recordings", charset="UTF-8"'})
        return await handler(request)

    return middleware


def _authorized(header: str, username: str, password: str) -> bool:
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "basic":
        return False
    try:
        user, _, given = base64.b64decode(value, validate=True).decode("utf-8").partition(":")
    except (binascii.Error, UnicodeDecodeError):
        return False
    user_ok = not username or hmac.compare_digest(user.encode(), username.encode())
    return hmac.compare_digest(given.encode(), password.encode()) and user_ok


def cors_middleware(origins: tuple[str, ...]):
    @web.middleware
    async def middleware(request: web.Request, handler) -> web.StreamResponse:
        origin = request.headers.get("Origin", "").rstrip("/")
        allowed = origin and origin in origins and request.path.startswith("/api/")
        if allowed and request.method == "OPTIONS":
            response: web.StreamResponse = web.Response(status=204)
        else:
            response = await handler(request)
        if allowed:
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Access-Control-Allow-Credentials"] = "true"
            response.headers["Access-Control-Allow-Headers"] = "Authorization, Range, Content-Type"
            response.headers["Access-Control-Allow-Methods"] = "GET, HEAD, OPTIONS"
            response.headers["Access-Control-Expose-Headers"] = "Content-Range, Accept-Ranges, Content-Length, Retry-After"
            response.headers["Vary"] = "Origin"
        return response

    return middleware


# The page runs only its own scripts (so a transcript can never inject one),
# talks only to its own origin, and may load the two web fonts it asks for.
# Inline styles are allowed: the interface sets colours per speaker.
PAGE_POLICY = (
    "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src https://fonts.gstatic.com; img-src 'self' data:; media-src 'self'; connect-src 'self'; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)


@web.middleware
async def headers_middleware(request: web.Request, handler) -> web.StreamResponse:
    response = await handler(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    if request.path.startswith("/api/"):
        if not isinstance(response, web.FileResponse):
            response.headers.setdefault("Cache-Control", "no-store")
    else:
        response.headers.setdefault("Content-Security-Policy", PAGE_POLICY)
    return response


# -- handlers ----------------------------------------------------------


def _session_id(request: web.Request) -> str:
    return f"{request.match_info['guild']}/{request.match_info['session']}"


def _int(request: web.Request, name: str, default: int, low: int, high: int) -> int:
    raw = request.query.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ApiError(400, "bad_parameter", f"{name} must be an integer") from None
    return min(high, max(low, value))


def _float(request: web.Request, name: str) -> float | None:
    raw = request.query.get(name)
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        raise ApiError(400, "bad_parameter", f"{name} must be a number (epoch seconds)") from None


def _json(data: dict) -> web.Response:
    response = web.json_response(data)
    response.enable_compression()
    return response


async def health(request: web.Request) -> web.Response:
    data = await request.app[SERVICE].health()
    return web.json_response(data, status=200 if data["status"] == "ok" else 503)


async def info(request: web.Request) -> web.Response:
    return _json(request.app[SERVICE].info())


async def guilds(request: web.Request) -> web.Response:
    return _json(await request.app[SERVICE].guilds())


async def sessions(request: web.Request) -> web.Response:
    query = request.query
    for name in ("guild", "user"):
        if name in query and not query[name].isdigit():
            raise ApiError(400, "bad_parameter", f"{name} must be a Discord id")
    return _json(await request.app[SERVICE].list_sessions(
        guild=query.get("guild"), user=query.get("user"),
        since=_float(request, "since"), until=_float(request, "until"),
        limit=_int(request, "limit", 50, 1, _MAX_LIMIT), offset=_int(request, "offset", 0, 0, 10**9),
    ))


async def session(request: web.Request) -> web.Response:
    return _json(await request.app[SERVICE].session(_session_id(request)))


async def audio(request: web.Request) -> web.StreamResponse:
    found = await request.app[SERVICE].audio_file(_session_id(request), request.query.get("v"))
    # A versioned URL always gets the same bytes: cache it for good. The
    # plain URL follows the latest mix, so the browser has to revalidate.
    cache = "private, max-age=604800, immutable" if found.exact else "private, no-cache"
    return _file(request, found.path, "audio/ogg", f"{request.match_info['session']}.ogg", cache)


async def audio_mp3(request: web.Request) -> web.StreamResponse:
    path = await request.app[SERVICE].mp3_file(_session_id(request))
    if path is None:
        response = web.json_response({"status": "building"}, status=202, headers={"Retry-After": "10"})
        return response
    return _file(request, path, "audio/mpeg", f"{request.match_info['session']}.mp3", "private, no-cache")


async def transcript_markdown(request: web.Request) -> web.StreamResponse:
    path = await request.app[SERVICE].transcript_markdown(_session_id(request))
    return _file(request, path, "text/markdown; charset=utf-8", f"{request.match_info['session']}.md", "no-store")


def _file(request: web.Request, path: Path, content_type: str, filename: str, cache: str) -> web.FileResponse:
    headers = {"Content-Type": content_type, "Cache-Control": cache}
    if request.query.get("download"):
        headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    return web.FileResponse(path, headers=headers)


def _static_routes(app: web.Application, folder: Path) -> None:
    """A single page app: real files as they are, any other page path gets index.html."""
    index = folder / "index.html"

    async def page(request: web.Request) -> web.StreamResponse:
        tail = request.match_info.get("tail", "")
        if tail.startswith("api/"):
            raise web.HTTPNotFound()
        candidate = (folder / tail).resolve()
        if tail and candidate.is_file() and candidate.is_relative_to(folder.resolve()):
            # revalidated on every load (cheap 304s), so a new frontend shows up at once
            return web.FileResponse(candidate, headers={"Cache-Control": "no-cache"})
        return web.FileResponse(index, headers={"Cache-Control": "no-cache"})

    app.router.add_get("/{tail:.*}", page)


async def _root(request: web.Request) -> web.Response:
    return web.json_response({"api": f"{API_PREFIX}/info", "frontend": None})


# -- assembly ----------------------------------------------------------


def create_app(service: ArchiveService, *, username: str = "", password: str = "",
               cors_origins: tuple[str, ...] = (), static_dir: Path | None = None,
               maintenance_seconds: float = 0, prebuild_recent: int = 0) -> web.Application:
    # Outermost first: CORS headers go on every answer (401s too) and
    # preflights skip auth; errors become JSON inside all of them.
    middlewares = [headers_middleware, auth_middleware(username, password), errors_middleware]
    if cors_origins:
        middlewares.insert(0, cors_middleware(cors_origins))
    app = web.Application(middlewares=middlewares)
    app[SERVICE] = service
    prefix = API_PREFIX
    app.router.add_get(f"{prefix}/health", health)
    app.router.add_get(f"{prefix}/info", info)
    app.router.add_get(f"{prefix}/guilds", guilds)
    app.router.add_get(f"{prefix}/sessions", sessions)
    session_path = f"{prefix}/sessions/{{guild:\\d+}}/{{session:[0-9]{{8}}-[0-9]{{6}}Z-[0-9]+}}"
    app.router.add_get(session_path, session)
    app.router.add_get(f"{session_path}/audio", audio)
    app.router.add_get(f"{session_path}/audio.mp3", audio_mp3)
    app.router.add_get(f"{session_path}/transcript.md", transcript_markdown)
    if static_dir is not None and (static_dir / "index.html").is_file():
        _static_routes(app, static_dir)
    else:
        app.router.add_get("/", _root)

    async def upkeep(app: web.Application):
        await asyncio.to_thread(service.cache.setup)
        task = None
        if maintenance_seconds > 0:
            task = asyncio.create_task(_maintenance(service, maintenance_seconds, prebuild_recent),
                                       name="archive-maintenance")
        yield
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await service.cache.aclose()

    app.cleanup_ctx.append(upkeep)
    return app


async def _maintenance(service: ArchiveService, every: float, prebuild_recent: int) -> None:
    while True:
        try:
            await service.maintain(prebuild_recent)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Archive maintenance failed")
        await asyncio.sleep(every)


def build_from_config() -> web.Application:
    from ttsbot import config

    try:
        tz = zoneinfo.ZoneInfo(config.TRANSCRIPT_TIMEZONE)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        log.warning("Unknown TRANSCRIPT_TIMEZONE=%s; using UTC", config.TRANSCRIPT_TIMEZONE)
        tz = zoneinfo.ZoneInfo("UTC")
    cache = MediaCache(config.WEB_CACHE_DIR, max_bytes=config.WEB_CACHE_MAX_MB * 1_048_576)
    service = ArchiveService(RecordingStore(config.RECORDING_DIR), cache, RetentionPolicy.from_config(),
                             tz=tz, gap_ms=config.WEB_MIX_GAP_MS)
    if "*" in config.WEB_CORS_ORIGINS:
        raise SystemExit("WEB_CORS_ORIGINS=* would let any website read the recordings: list the origins")
    if not config.WEB_PASSWORD:
        if not config.WEB_ALLOW_ANONYMOUS:
            raise SystemExit("WEB_PASSWORD is empty: set it, or WEB_ALLOW_ANONYMOUS=1 if only trusted "
                             "machines can reach the port")
        log.warning("WEB_PASSWORD is empty: anyone who can reach port %s can read the recordings", config.WEB_PORT)
    log.info("Recordings web root=%s cache=%s cache_max_mb=%d static=%s cors=%s",
             config.RECORDING_DIR, config.WEB_CACHE_DIR, config.WEB_CACHE_MAX_MB,
             config.WEB_STATIC_DIR or "-", ",".join(config.WEB_CORS_ORIGINS) or "-")
    return create_app(service, username=config.WEB_USERNAME, password=config.WEB_PASSWORD,
                      cors_origins=config.WEB_CORS_ORIGINS,
                      static_dir=Path(config.WEB_STATIC_DIR) if config.WEB_STATIC_DIR else None,
                      maintenance_seconds=config.WEB_MAINTENANCE_SECONDS,
                      prebuild_recent=config.WEB_PREBUILD_RECENT)
