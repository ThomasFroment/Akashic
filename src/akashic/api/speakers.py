"""Speakers in the API: list the voices, name them, merge two, move a wrong turn elsewhere."""

from __future__ import annotations

import time
from contextlib import contextmanager
from typing import TYPE_CHECKING, Annotated

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from akashic.storage.speakers import Speaker, SpeakerError, StoredTurn

if TYPE_CHECKING:
    from collections.abc import Generator

    from akashic.api.server import Services
    from akashic.storage.speakers import SpeakerStore

type Json = dict[str, object]


class Rename(BaseModel):
    name: str | None = None


class Merge(BaseModel):
    into: int


class Assign(BaseModel):
    speaker_id: int | None = None  # None: a new, unnamed speaker


def _speaker_json(s: Speaker) -> Json:
    return {
        "id": s.id,
        "name": s.name,
        "is_self": s.is_self,
        "turns": s.turns,
        "speech_ms": s.speech_ms,
        "last_ms": s.last_ms,
        "sample": None
        if s.sample is None
        else {
            "segment_id": s.sample.segment_id,
            "start_ms": s.sample.start_ms,
            "end_ms": s.sample.end_ms,
        },
    }


def _turn_json(t: StoredTurn) -> Json:
    return {
        "id": t.id,
        "segment_id": t.segment_id,
        "speaker_id": t.speaker_id,
        "start_ms": t.start_ms,
        "end_ms": t.end_ms,
        "speech_ms": t.speech_ms,
        "at_ms": t.segment_start_ms + t.start_ms,
        "text": t.transcript,
    }


def speaker_routes(services: Services, store: SpeakerStore) -> APIRouter:
    router = APIRouter()

    @router.get("/speakers")
    def speakers() -> list[Json]:  # pyright: ignore[reportUnusedFunction]
        with services.db():
            return [_speaker_json(s) for s in store.all_speakers()]

    @router.get("/speakers/{speaker_id}/turns")
    def turns(  # pyright: ignore[reportUnusedFunction]
        speaker_id: int, limit: Annotated[int, Query(ge=1, le=100)] = 20
    ) -> list[Json]:
        with services.db():
            return [_turn_json(t) for t in store.turns_of(speaker_id, limit)]

    @router.post("/speakers/{speaker_id}/name")
    def rename(speaker_id: int, body: Rename) -> Json:  # pyright: ignore[reportUnusedFunction]
        with services.db(), _translated():
            store.rename(speaker_id, body.name)
        return {"ok": True}

    @router.post("/speakers/{speaker_id}/merge")
    def merge(speaker_id: int, body: Merge) -> Json:  # pyright: ignore[reportUnusedFunction]
        with services.db(), _translated():
            store.merge(speaker_id, body.into)
        return {"ok": True}

    @router.post("/turns/{turn_id}/assign")
    def assign(turn_id: int, body: Assign) -> Json:  # pyright: ignore[reportUnusedFunction]
        with services.db(), _translated():
            target = store.reassign_turn(turn_id, body.speaker_id, time.time_ns() // 1_000_000)
        return {"speaker_id": target}

    return router


@contextmanager
def _translated() -> Generator[None]:
    """A request that makes no sense is a 409, an unknown id a 404."""
    try:
        yield
    except SpeakerError as exc:
        status = 404 if str(exc).startswith("no such") else 409
        raise HTTPException(status, str(exc)) from exc
