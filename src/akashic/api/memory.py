"""Memory in the API: episodes by day, one episode with what was said in it, and search."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Annotated, Final

from fastapi import APIRouter, HTTPException, Query

from akashic.capture.media import process_for_app
from akashic.search import hybrid, planner, semantic
from akashic.storage import monitors

if TYPE_CHECKING:
    from akashic.api.server import Services
    from akashic.search.keyword import Hit

type Json = dict[str, object]

MAX_LINES: Final = 400
KINDS: Final = ("video", "music", "conversation", "browsing", "work", "game")


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def hit_json(hit: Hit) -> Json:
    """A screen search hit; ``id`` is the frame, ``window_id`` the window that matched."""
    return {
        "id": hit.frame_id,
        "window_id": hit.window_id,
        "ts_ms": hit.ts_ms,
        "monitor_id": hit.monitor_id,
        "process": hit.process,
        "title": hit.title,
        "url": hit.url,
        "has_image": hit.has_image,
        "snippet": hit.snippet,
    }


def _episode_json(e: hybrid.EpisodeHit) -> Json:
    return {
        "id": e.id,
        "kind": e.kind,
        "start_ms": e.start_ms,
        "end_ms": e.end_ms,
        "title": e.title,
        "app": e.app,
        "url": e.url,
        "people": list(e.people),
        "unnamed": e.unnamed,
        "snippet": e.snippet,
    }


def _line_json(line: hybrid.LineHit) -> Json:
    return {
        "segment_id": line.segment_id,
        "stream": line.stream,
        "start_ms": line.start_ms,
        "end_ms": line.end_ms,
        "speaker_id": line.speaker_id,
        "speaker": line.speaker,
        "text": line.text,
        "episode_id": line.episode_id,
        "confidence": line.confidence,
    }


def memory_routes(services: Services) -> APIRouter:
    router = APIRouter()

    @router.get("/memory/search")
    def memory_search(  # pyright: ignore[reportUnusedFunction]
        q: Annotated[str, Query(min_length=1, max_length=500)],
        limit: Annotated[int, Query(ge=1, le=50)] = 10,
    ) -> Json:
        with services.db() as conn:
            plan = planner.heuristic_plan(q, _now_ms(), hybrid.known_names(conn))
        qvec = semantic.query_vector(services.db, q, services.embedding_model, _now_ms)
        with services.db() as conn:
            found = hybrid.search_memory(conn, plan, qvec, services.embedding_model, limit=limit)
        return {
            "plan": {
                "time": None
                if plan.time is None
                else {
                    "start_ms": plan.time.start_ms,
                    "end_ms": plan.time.end_ms,
                    "label": plan.time.label,
                },
                "people": list(plan.people),
                "kinds": list(plan.kinds),
                "keywords": list(plan.keywords),
            },
            "semantic": found.semantic,
            "episodes": [_episode_json(e) for e in found.episodes],
            "lines": [_line_json(line) for line in found.lines],
            "frames": [hit_json(f) for f in found.frames],
        }

    @router.get("/episodes")
    def episodes(  # pyright: ignore[reportUnusedFunction]
        start_ms: int,
        end_ms: int,
        kind: Annotated[str | None, Query(max_length=20)] = None,
    ) -> list[Json]:
        if kind is not None and kind not in KINDS:
            raise HTTPException(422, "unknown kind")
        with services.db() as conn:
            rows = conn.execute(
                "SELECT id FROM episodes WHERE start_ms < ? AND end_ms > ?"
                " AND (? IS NULL OR kind = ?) ORDER BY start_ms, id LIMIT 500",
                (end_ms, start_ms, kind, kind),
            ).fetchall()
            ids = [int(r[0]) for r in rows]
            details = hybrid.episode_hits(conn, ids) if ids else []
        return [_episode_json(e) for e in details]

    @router.get("/episodes/{episode_id}")
    def episode(episode_id: int) -> Json:  # pyright: ignore[reportUnusedFunction]
        with services.db() as conn:
            found = hybrid.episode_hits(conn, [episode_id])
            if not found:
                raise HTTPException(404, "no such episode")
            lines = hybrid.episode_lines(conn, episode_id, MAX_LINES)
            evidence = monitors.episode_evidence(conn, episode_id, _now_ms(), process_for_app)
            moment = None if evidence is None else monitors.frame_moment(conn, evidence[0])
        return {
            **_episode_json(found[0]),
            "lines": [_line_json(line) for line in lines],
            "frame_id": None if evidence is None else evidence[0],
            "window_id": None if evidence is None else evidence[1],
            "frame_ms": None if moment is None else moment[0],
            "monitor_id": None if moment is None else moment[1],
        }

    return router
