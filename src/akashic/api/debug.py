"""Routes behind the Debug page: what each process is doing, the job queue, and the logs.

Behind the same per-run session and loopback binding as the rest of the API. Logs hold no
captured content (see ``akashic.log``). Disk use per type is in Settings.
"""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Final

from fastapi import APIRouter, HTTPException, Query

from akashic.api import logview
from akashic.storage import db
from akashic.storage.debuginfo import ProcessInfo

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from akashic.api.server import Services

type Json = dict[str, object]  # FastAPI reads return annotations at runtime, so no TYPE_CHECKING

RECENT_JOBS: Final = 25
RECENT_SEGMENTS: Final = 15


@dataclass(frozen=True, slots=True)
class DebugSources:
    """Where the Debug page looks, set up by the app that owns the paths and the config."""

    data_dir: Path
    logs_dir: Path
    settings: Callable[[], dict[str, object]]  # config values worth seeing, read fresh each time


def _overview(services: Services, sources: DebugSources) -> Json:
    now_ms = time.time_ns() // 1_000_000
    with services.db() as conn:
        infos = ProcessInfo(conn).read_all()
        job_counts: list[tuple[str, str, int]] = conn.execute(
            "SELECT kind, state, count(*) FROM jobs GROUP BY kind, state ORDER BY kind, state"
        ).fetchall()
        jobs: list[tuple[int, str, str, int, int, int, str | None]] = conn.execute(
            "SELECT id, kind, state, attempts, data_ms, run_after_ms, error FROM jobs"
            " WHERE state != 'done' ORDER BY data_ms, id LIMIT ?",
            (RECENT_JOBS,),
        ).fetchall()
        seg_counts: list[tuple[str, str, int]] = conn.execute(
            "SELECT stream, status, count(*) FROM audio_segments GROUP BY stream, status"
        ).fetchall()
        segments: list[tuple[int, str, int, int, str, str, str | None]] = conn.execute(
            "SELECT id, stream, start_ms, speech_ms, sources, status, language"
            " FROM audio_segments ORDER BY start_ms DESC LIMIT ?",
            (RECENT_SEGMENTS,),
        ).fetchall()
        frame_row: tuple[int, int | None, int] = conn.execute(
            "SELECT count(*), max(ts_ms), sum(ts_ms >= ?) FROM frames", (now_ms - 60_000,)
        ).fetchone()
        schema = db.schema_version(conn)
    try:
        free_gb = round(shutil.disk_usage(sources.data_dir).free / 1e9, 1)
    except OSError:
        free_gb = None
    db_file = sources.data_dir / "db" / "akashic.db"
    return {
        "now_ms": now_ms,
        "schema": schema,
        "processes": services.controls.states(),
        "info": {i.name: {"age_ms": now_ms - i.updated_ms, "data": i.data} for i in infos},
        "jobs": {
            "counts": [{"kind": k, "state": s, "n": n} for k, s, n in job_counts],
            "open": [
                {
                    "id": i,
                    "kind": k,
                    "state": s,
                    "attempts": a,
                    "data_ms": d,
                    "wait_ms": max(0, r - now_ms),
                    "error": e,
                }
                for i, k, s, a, d, r, e in jobs
            ],
        },
        "audio": {
            "counts": [{"stream": s, "status": st, "n": n} for s, st, n in seg_counts],
            "recent": [
                {
                    "id": i,
                    "stream": s,
                    "start_ms": t,
                    "speech_ms": sp,
                    "sources": src,
                    "status": st,
                    "language": lang,
                }
                for i, s, t, sp, src, st, lang in segments
            ],
        },
        "frames": {
            "total": frame_row[0],
            "latest_ts_ms": frame_row[1],
            "last_minute": frame_row[2] or 0,
        },
        "disk": {
            "free_gb": free_gb,
            "db_mb": round(db_file.stat().st_size / 1e6, 1) if db_file.exists() else None,
        },
        "settings": sources.settings(),
    }


def _log_routes(sources: DebugSources) -> APIRouter:
    router = APIRouter()

    @router.get("/logs/files")
    def log_files() -> list[Json]:  # pyright: ignore[reportUnusedFunction]
        return [
            {"name": f.name, "size": f.size, "modified_ms": f.modified_ms}
            for f in logview.list_files(sources.logs_dir)
        ]

    @router.get("/logs")
    def logs(  # pyright: ignore[reportUnusedFunction]
        name: str = logview.MERGED,
        level: str = "DEBUG",
        q: Annotated[str, Query(max_length=200)] = "",
        limit: Annotated[int, Query(ge=1, le=5000)] = 300,
    ) -> Json:
        entries = logview.read_logs(sources.logs_dir, name)
        if entries is None:
            raise HTTPException(404, "no such log")
        chosen = logview.select(entries, min_level=level.upper(), query=q, limit=limit)
        return {
            "name": name,
            "total": len(entries),
            "entries": [
                {
                    "src": e.source,
                    "ts": e.ts,
                    "pid": e.pid,
                    "logger": e.logger,
                    "level": e.level,
                    "msg": e.message,
                }
                for e in chosen
            ],
        }

    return router


def _action_routes(services: Services) -> APIRouter:
    router = APIRouter()

    @router.post("/jobs/retry")
    def retry_jobs() -> Json:  # pyright: ignore[reportUnusedFunction]
        """Make every waiting or failed job runnable now (skips retry delays, resets attempts)."""
        with services.db() as conn, db.transaction(conn) as tx:
            changed = tx.execute(
                "UPDATE jobs SET state = 'pending', run_after_ms = 0, attempts = 0, error = NULL"
                " WHERE state IN ('pending', 'failed')"
            ).rowcount
        return {"retried": changed}

    @router.post("/segments/{segment_id}/retranscribe")
    def retranscribe(segment_id: int) -> Json:  # pyright: ignore[reportUnusedFunction]
        """Forget a segment's transcript and queue it for Whisper again."""
        if services.audio is None:
            raise HTTPException(404, "audio is not available")
        with services.db():
            if not services.audio.requeue(segment_id, time.time_ns() // 1_000_000):
                raise HTTPException(404, "no such segment")
        return {"queued": segment_id}

    return router


def _system_routes(services: Services, sources: DebugSources) -> APIRouter:
    router = APIRouter()

    @router.get("/overview")
    def overview() -> Json:  # pyright: ignore[reportUnusedFunction]
        return _overview(services, sources)

    return router


def debug_routes(services: Services, sources: DebugSources) -> APIRouter:
    router = APIRouter(prefix="/debug")
    for part in (
        _system_routes(services, sources),
        _log_routes(sources),
        _action_routes(services),
    ):
        router.include_router(part)
    return router
