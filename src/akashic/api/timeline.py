"""The timeline in the API: the desk at a moment (every monitor, its state and frame) and the
strip of app bands of a day. Reads only the database; no image is touched here."""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from datetime import time as day_time
from typing import TYPE_CHECKING, Annotated

from fastapi import APIRouter, HTTPException, Query

from akashic.pipeline.sessions import app_bands
from akashic.storage import monitors

if TYPE_CHECKING:
    from sqlite3 import Connection

    from akashic.api.server import Services
    from akashic.storage.frames import StoredFrame

type Json = dict[str, object]

MAX_TS_MS = 2**62


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def day_range_ms(day: str) -> tuple[int, int]:
    """Local midnight to next local midnight of ``YYYY-MM-DD``, as UTC milliseconds."""
    try:
        d = datetime.strptime(day, "%Y-%m-%d").date()  # noqa: DTZ007 - made local just below
    except ValueError as exc:
        raise HTTPException(422, "day must be YYYY-MM-DD") from exc
    try:
        start = datetime.combine(d, day_time.min).astimezone()
        end = datetime.combine(d + timedelta(days=1), day_time.min).astimezone()
        return int(start.timestamp() * 1000), int(end.timestamp() * 1000)
    except (OSError, OverflowError) as exc:  # dates Windows cannot convert to local time
        raise HTTPException(422, "day is out of range") from exc


def _windows_json(frame: StoredFrame) -> list[Json]:
    """The app windows front to back; hidden and shell rows, hwnd, url and text stay out."""
    return [
        {
            "id": w.id,
            "process": w.process,
            "title": w.title,
            "rect": [w.left, w.top, w.right, w.bottom],
        }
        for w in frame.windows
        if w.role == "app"
    ]


def _neighbour_ms(conn: Connection, ts: int) -> tuple[int | None, int | None]:
    """The nearest stored frame times (any monitor) before and after ``ts``."""
    before = conn.execute("SELECT max(ts_ms) FROM frames WHERE ts_ms < ?", (ts,)).fetchone()[0]
    after = conn.execute("SELECT min(ts_ms) FROM frames WHERE ts_ms > ?", (ts,)).fetchone()[0]
    return before, after


def _stage_json(services: Services, conn: Connection, ts: int) -> Json:
    stage = monitors.stage_at(conn, ts, _now_ms())
    frames = {
        m.monitor_id: services.frames.get(m.frame_id) for m in stage if m.frame_id is not None
    }
    focused_ts = max(
        (f.ts_ms for f in frames.values() if f is not None and any(w.focused for w in f.windows)),
        default=None,
    )
    entries: list[Json] = []
    for m in stage:
        frame = frames.get(m.monitor_id)
        entries.append(
            {
                "id": m.monitor_id,
                "name": m.name,
                "rect": list(m.rect),
                "scale": m.scale,
                "state": m.state,
                "frame_id": m.frame_id,
                "checked_ms": m.checked_ms,
                "focused": frame is not None
                and frame.ts_ms == focused_ts
                and any(w.focused for w in frame.windows),
                "windows": [] if frame is None else _windows_json(frame),
            }
        )
    prev_ms, next_ms = _neighbour_ms(conn, ts)
    return {"ts_ms": ts, "monitors": entries, "prev_ms": prev_ms, "next_ms": next_ms}


def timeline_routes(services: Services) -> APIRouter:
    router = APIRouter()

    @router.get("/stage")
    def stage(ts: Annotated[int, Query(ge=0, le=MAX_TS_MS)]) -> Json:  # pyright: ignore[reportUnusedFunction]
        with services.db() as conn:
            return _stage_json(services, conn, ts)

    @router.get("/strip")
    def strip(  # pyright: ignore[reportUnusedFunction]
        day: str, process: Annotated[str, Query(max_length=260)] = ""
    ) -> Json:
        start, end = day_range_ms(day)
        with services.db() as conn:
            bands = app_bands(conn, start, end, process or None)
        return {
            "bands": [
                {"start_ms": b.start_ms, "end_ms": b.end_ms, "process": b.process, "title": b.title}
                for b in bands
            ]
        }

    return router
