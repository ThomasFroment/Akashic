"""Local API and UI server: FastAPI on 127.0.0.1, bearer-token auth, no outside assets.

The session token is random per app start and never shown. The tray opens
``http://127.0.0.1:<port>/open?code=<one-time code>``; that code works once, within two minutes,
and is swapped for an HttpOnly, SameSite=Strict cookie holding the token. So the address bar,
the browser history (which a browser may sync) and Akashic's own screenshots never hold anything
that still opens the UI. Static UI files carry no data and need no session.

Defences: the Host header must be loopback (DNS rebinding), a request that changes something
must not come from another origin, a strict Content-Security-Policy forbids anything but our own
origin, and requests are never logged (URLs carry search text).
"""

from __future__ import annotations

import io
import logging
import secrets
import socket
import threading
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING, Annotated, Final

import uvicorn
from fastapi import APIRouter, Cookie, Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image

from akashic.api.ask import ask_routes
from akashic.api.debug import debug_routes
from akashic.api.memory import memory_routes
from akashic.api.models import model_routes
from akashic.api.settings import settings_routes, theme_of
from akashic.api.speakers import speaker_routes
from akashic.api.stt import stt_routes
from akashic.config import DEFAULT_EMBEDDING_MODEL
from akashic.search import keyword, transcripts
from akashic.search.keyword import Filters, Hit
from akashic.storage.crypto import CryptoError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Generator
    from sqlite3 import Connection

    from akashic.api.debug import DebugSources
    from akashic.api.models import Downloads
    from akashic.storage.audio import AudioStore
    from akashic.storage.frames import FrameStore
    from akashic.storage.speakers import SpeakerStore

log = logging.getLogger("api")

UI_DIR: Final = Path(__file__).resolve().parent.parent / "ui"
CSP: Final = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' blob:; "
    "media-src 'self' blob:; "
    "font-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'none'; "
    "frame-ancestors 'none'"
)
SESSION_COOKIE: Final = "akashic_session"
OPEN_CODE_TTL_S: Final = 120.0
SAFE_METHODS: Final = frozenset({"GET", "HEAD"})
type ThumbKey = tuple[int, str, int]  # frame id, stored image path, width (0 = full size)
THUMB_CACHE_SIZE: Final = 300
MAX_THUMB_PX: Final = 1920
HOST: Final = "127.0.0.1"
REOPEN_PAGE: Final = (
    "<!doctype html><meta charset=utf-8><title>Akashic</title>"
    "<p>This link has been used or has expired. Open Akashic from its tray icon.</p>"
)

type Json = dict[str, object]


@dataclass(frozen=True, slots=True)
class Controls:
    """What the UI may ask the app to do or report about it."""

    states: Callable[[], dict[str, str]]
    pause: Callable[[], None]
    resume: Callable[[], None]
    restart: Callable[[], None] = lambda: None  # restart capture and worker (Settings)
    # Start with Windows (Settings): read and set the user's Run registry value. None where the
    # platform has no such thing (tests on other systems): the setting is then not offered.
    autostart_get: Callable[[], bool] | None = None
    autostart_set: Callable[[bool], None] | None = None
    while_stopped: Callable[[Callable[[], None]], None] | None = None


class Services:
    """Shared read access to the database and media, safe to use from the thread pool."""

    def __init__(  # noqa: PLR0913, PLR0917 - wiring: each argument is a distinct collaborator
        self,
        conn: Connection,
        frames: FrameStore,
        token: str,
        controls: Controls,
        audio: AudioStore | None = None,
        debug: DebugSources | None = None,
        config_file: Path | None = None,
        speakers: SpeakerStore | None = None,
        embedding_model: str = DEFAULT_EMBEDDING_MODEL,
        ask_blocker: Callable[[], str | None] = lambda: None,
        downloads: Downloads | None = None,
    ) -> None:
        self._conn = conn
        self.debug = debug
        self.config_file = config_file
        self._lock = threading.RLock()
        self.frames = frames
        self.audio = audio
        self.speakers = speakers
        self.embedding_model = embedding_model
        self.ask_blocker = ask_blocker  # why a question cannot be answered yet, or None
        self.downloads = downloads  # Settings > Models
        self.token = token
        self.controls = controls
        self._thumbs: OrderedDict[ThumbKey, bytes] = OrderedDict()
        self._codes: dict[str, float] = {}  # one-time open codes -> expiry (monotonic seconds)

    def new_open_code(self, now: float | None = None) -> str:
        """A code that opens the UI once, within ``OPEN_CODE_TTL_S``."""
        code = secrets.token_urlsafe(16)
        with self._lock:
            self._codes[code] = (time_s() if now is None else now) + OPEN_CODE_TTL_S
        return code

    def take_open_code(self, code: str, now: float | None = None) -> bool:
        """True (once) for a code that was issued and has not expired."""
        at = time_s() if now is None else now
        with self._lock:
            for known, expires in list(self._codes.items()):
                if expires < at:
                    del self._codes[known]
            match = next((k for k in self._codes if secrets.compare_digest(k, code)), None)
            if match is None:
                return False
            del self._codes[match]
            return True

    @contextmanager
    def db(self) -> Generator[Connection]:
        with self._lock:
            yield self._conn

    def thumb(self, key: ThumbKey) -> bytes | None:
        with self._lock:
            value = self._thumbs.get(key)
            if value is not None:
                self._thumbs.move_to_end(key)
            return value

    def put_thumb(self, key: ThumbKey, value: bytes) -> None:
        with self._lock:
            self._thumbs[key] = value
            while len(self._thumbs) > THUMB_CACHE_SIZE:
                self._thumbs.popitem(last=False)

    def clear_thumbs(self) -> None:
        with self._lock:
            self._thumbs.clear()


@dataclass
class Page:
    """Shared query parameters: app filter and paging (validated by FastAPI)."""

    process: Annotated[str | None, Query(max_length=260)] = None
    limit: Annotated[int, Query(ge=1, le=keyword.MAX_LIMIT)] = 30
    offset: Annotated[int, Query(ge=0)] = 0


def time_s() -> float:
    return monotonic()


def new_token() -> str:
    return secrets.token_urlsafe(32)


def day_range_ms(day: str) -> tuple[int, int]:
    """Local midnight to next local midnight of ``YYYY-MM-DD``, as UTC milliseconds."""
    try:
        d = datetime.strptime(day, "%Y-%m-%d").date()  # noqa: DTZ007 - made local just below
    except ValueError as exc:
        raise HTTPException(422, "day must be YYYY-MM-DD") from exc
    try:
        start = datetime.combine(d, time.min).astimezone()
        end = datetime.combine(d + timedelta(days=1), time.min).astimezone()
        return int(start.timestamp() * 1000), int(end.timestamp() * 1000)
    except (OSError, OverflowError) as exc:  # dates Windows cannot convert to local time
        raise HTTPException(422, "day is out of range") from exc


def _thumbnail(webp: bytes, width: int) -> bytes:
    with Image.open(io.BytesIO(webp)) as img:
        img.thumbnail((width, width * 4))
        out = io.BytesIO()
        img.save(out, "WEBP", quality=70)
        return out.getvalue()


def _hit_json(hit: Hit) -> Json:
    return {
        "id": hit.id,
        "ts_ms": hit.ts_ms,
        "monitor": hit.monitor,
        "process": hit.process,
        "window_title": hit.window_title,
        "url": hit.url,
        "has_image": hit.has_image,
        "snippet": hit.snippet,
    }


def _utterance_json(u: transcripts.Utterance) -> Json:
    return {
        "id": u.id,
        "stream": u.stream,
        "start_ms": u.start_ms,
        "end_ms": u.end_ms,
        "language": u.language,
        "sources": list(u.sources),
        "snippet": u.snippet,
        "speaker_id": u.speaker_id,
        "speaker": u.speaker,
        "confidence": u.confidence,
    }


def _control_routes(services: Services) -> APIRouter:
    router = APIRouter()

    @router.get("/status")
    def status() -> Json:  # pyright: ignore[reportUnusedFunction]
        states = services.controls.states()
        with services.db() as conn:
            frames = conn.execute("SELECT count(*) FROM frames").fetchone()[0]
            apps = keyword.processes(conn)
            game = (
                conn.execute(
                    "SELECT process FROM game_sessions WHERE closed = 0"
                    " ORDER BY start_ms DESC LIMIT 1"
                ).fetchone()
                if states.get("capture") == "gaming"
                else None
            )
        return {
            "processes": states,
            "paused": states.get("capture") == "paused",
            "frames": frames,
            "apps": apps,
            "game_process": game[0] if game else None,
            "theme": theme_of(services.config_file),
        }

    @router.post("/pause")
    def pause(*, paused: bool = True) -> dict[str, bool]:  # pyright: ignore[reportUnusedFunction]
        (services.controls.pause if paused else services.controls.resume)()
        return {"paused": paused}

    return router


def _search_routes(services: Services) -> APIRouter:
    router = APIRouter()

    @router.get("/search")
    def search(  # pyright: ignore[reportUnusedFunction]
        page: Annotated[Page, Depends()],
        q: Annotated[str, Query(max_length=500)] = "",
        start_ms: int = 0,
        end_ms: int = 2**62,
    ) -> list[Json]:
        filters = Filters(start_ms, end_ms, page.process or None)
        with services.db() as conn:
            hits = (
                keyword.search(conn, q, filters, page.limit, page.offset)
                if q.strip()
                else keyword.browse(conn, filters, page.limit, page.offset)
            )
        return [_hit_json(h) for h in hits]

    @router.get("/timeline")
    def timeline(  # pyright: ignore[reportUnusedFunction]
        page: Annotated[Page, Depends()], day: str
    ) -> list[Json]:
        start, end = day_range_ms(day)
        with services.db() as conn:
            hits = keyword.browse(
                conn,
                Filters(start, end, page.process or None),
                page.limit,
                page.offset,
                newest_first=False,
            )
        return [_hit_json(h) for h in hits]

    return router


def _audio_routes(services: Services) -> APIRouter:
    router = APIRouter()

    @router.get("/transcripts")
    def transcript_list(  # pyright: ignore[reportUnusedFunction]
        q: Annotated[str, Query(max_length=500)] = "",
        start_ms: int = 0,
        end_ms: int = 2**62,
        limit: Annotated[int, Query(ge=1, le=keyword.MAX_LIMIT)] = 30,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> list[Json]:
        with services.db() as conn:
            found = (
                transcripts.search(conn, q, start_ms, end_ms, limit=limit, offset=offset)
                if q.strip()
                else transcripts.browse(conn, start_ms, end_ms, limit=limit, offset=offset)
            )
        return [_utterance_json(u) for u in found]

    @router.get("/audio/{segment_id}")
    def audio_file(segment_id: int) -> Response:  # pyright: ignore[reportUnusedFunction]
        if services.audio is None:
            raise HTTPException(404, "audio is not available")
        with services.db():
            segment = services.audio.get(segment_id)
            if segment is None:
                raise HTTPException(404, "no such segment")
            try:
                data = services.audio.read_file(segment)
            except (FileNotFoundError, CryptoError) as exc:
                raise HTTPException(404, "audio file is missing or unreadable") from exc
        if data is None:
            raise HTTPException(404, "the raw audio was deleted")
        return Response(data, media_type="audio/ogg")

    return router


def _frame_routes(services: Services) -> APIRouter:
    router = APIRouter()

    @router.get("/frames/{frame_id}")
    def frame(frame_id: int) -> Json:  # pyright: ignore[reportUnusedFunction]
        with services.db() as conn:
            stored = services.frames.get(frame_id)
            if stored is None:
                raise HTTPException(404, "no such frame")
            prev_id, next_id = keyword.neighbors(conn, frame_id)
        return {
            "id": stored.id,
            "ts_ms": stored.ts_ms,
            "monitor": stored.monitor,
            "trigger": stored.trigger,
            "process": stored.process,
            "window_title": stored.window_title,
            "url": stored.url,
            "text": stored.text,
            "text_source": stored.text_source,
            "has_image": stored.image_path is not None,
            "prev_id": prev_id,
            "next_id": next_id,
        }

    @router.get("/frames/{frame_id}/image")
    def frame_image(  # pyright: ignore[reportUnusedFunction]
        frame_id: int, w: Annotated[int | None, Query(ge=16, le=MAX_THUMB_PX)] = None
    ) -> Response:
        # Serialize reading, rendering and caching with deletion. Row IDs can be reused
        # afterward, so an in-flight thumbnail must not repopulate a cleared cache, and the
        # file path in the key keeps a reused ID from showing another frame's pixels.
        with services.db():
            stored = services.frames.get(frame_id)
            if stored is None or stored.image_path is None:
                raise HTTPException(404, "no image for this frame")
            cache_key = (frame_id, stored.image_path, w or 0)
            cached = services.thumb(cache_key) if w else None
            if cached is not None:
                return Response(cached, media_type="image/webp")
            try:
                webp = services.frames.image(stored)
            except FileNotFoundError as exc:
                raise HTTPException(404, "image file is missing") from exc
            except CryptoError as exc:
                log.warning("frame %d image failed to decrypt", frame_id)
                raise HTTPException(500, "image could not be decrypted") from exc
            if webp is None:  # pragma: no cover - guarded by image_path above
                raise HTTPException(404, "no image for this frame")
            if w:
                webp = _thumbnail(webp, w)
                services.put_thumb(cache_key, webp)
        return Response(webp, media_type="image/webp")

    return router


def _page_routes(services: Services) -> APIRouter:
    """The page itself and the one-time door into it; no session needed for either."""
    router = APIRouter(include_in_schema=False)

    @router.get("/")
    def index() -> FileResponse:  # pyright: ignore[reportUnusedFunction]
        return FileResponse(UI_DIR / "index.html")

    @router.get("/open")
    def open_ui(  # pyright: ignore[reportUnusedFunction]
        code: Annotated[str, Query(max_length=64)] = "",
    ) -> Response:
        if not services.take_open_code(code):
            return Response(REOPEN_PAGE, status_code=403, media_type="text/html")
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(
            SESSION_COOKIE, services.token, httponly=True, samesite="strict", path="/"
        )
        return response

    return router


def _api_routes(services: Services) -> list[APIRouter]:
    """Every /api router; the optional ones only when their service exists."""
    routers = [
        _control_routes(services),
        _search_routes(services),
        _audio_routes(services),
        _frame_routes(services),
        memory_routes(services),
        ask_routes(services),
        stt_routes(services),
    ]
    if services.speakers is not None:
        routers.append(speaker_routes(services, services.speakers))
    if services.debug is not None:
        routers.append(debug_routes(services, services.debug))
    if services.config_file is not None:
        routers.append(settings_routes(services, services.config_file))
    if services.downloads is not None:
        routers.append(model_routes(services, services.downloads))
    return routers


def create_app(services: Services, port: int) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    allowed_hosts = {f"{HOST}:{port}", f"localhost:{port}"}
    origins = {f"http://{h}" for h in allowed_hosts}

    @app.middleware("http")
    async def guard(  # pyright: ignore[reportUnusedFunction] - registered by the decorator
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if request.headers.get("host", "").lower() not in allowed_hosts:
            return Response("forbidden host", status_code=403)
        origin = request.headers.get("origin")
        if request.method not in SAFE_METHODS and origin is not None and origin not in origins:
            return Response("forbidden origin", status_code=403)
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = CSP
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    def auth(akashic_session: Annotated[str | None, Cookie()] = None) -> None:
        if akashic_session is None or not secrets.compare_digest(akashic_session, services.token):
            raise HTTPException(401, "missing or wrong session")

    app.include_router(_page_routes(services))
    for router in _api_routes(services):
        app.include_router(router, prefix="/api", dependencies=[Depends(auth)])
    app.mount("/static", StaticFiles(directory=UI_DIR), name="static")
    return app


class ApiServer:
    """Runs the app under uvicorn in a background thread, on a socket we bound ourselves."""

    def __init__(self, services: Services) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.bind((HOST, 0))
        self.port: int = self._sock.getsockname()[1]
        config = uvicorn.Config(
            create_app(services, self.port), log_config=None, access_log=False, lifespan="off"
        )
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._run, name="api", daemon=True)
        self._services = services

    def _run(self) -> None:
        self._server.run(sockets=[self._sock])

    def open_url(self) -> str:
        """Address to open in a browser: a fresh one-time code, never the session token."""
        return f"http://{HOST}:{self.port}/open?code={self._services.new_open_code()}"

    def start(self) -> None:
        self._thread.start()
        log.info("listening on %s:%d", HOST, self.port)

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=5)
        self._sock.close()
