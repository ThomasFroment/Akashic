"""Asking questions: queue one for the worker, poll its answer, list and delete past ones.

The API process has no model. ``POST /ask`` stores the question and queues an ``ask`` job; the
page polls ``GET /ask/{id}`` and sees the answer grow while the worker writes it. When the chat
model or runtime has not been downloaded, nothing is queued and the reason is returned so the
page can say so and still show plain search results.

A question may follow up on earlier ones: ``thread`` names the conversation (the id of its first
question). ``GET /ask`` lists conversations, ``GET /ask/thread/{id}`` returns the one a question
belongs to, and deleting the first question of a conversation forgets all of it.
"""

from __future__ import annotations

import json
import re
import time
from typing import TYPE_CHECKING, Annotated, Final

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from akashic.pipeline.ask import ask_job
from akashic.pipeline.jobs import enqueue_in
from akashic.storage import db

if TYPE_CHECKING:
    from sqlite3 import Connection

    from akashic.api.server import Services

type Json = dict[str, object]

MAX_QUESTION: Final = 500
_COLUMNS: Final = (
    "id, asked_ms, text, state, step, plan, answer, sources, model, error, coalesce(thread_id, id)"
)


class AskBody(BaseModel):
    q: str = Field(min_length=1, max_length=MAX_QUESTION, pattern=r"\S")  # not only spaces
    thread: int | None = None  # the conversation this question follows up on


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def _loads(raw: object) -> object:
    return None if raw is None else json.loads(str(raw))


def _row_json(r: tuple[object, ...]) -> Json:
    return {
        "id": r[0],
        "asked_ms": r[1],
        "text": r[2],
        "state": r[3],
        "step": r[4],
        "plan": _loads(r[5]),
        "answer": r[6],
        "sources": _loads(r[7]),
        "model": r[8],
        "error": r[9],
        "thread": r[10],
    }


def _conversation_of(conn: Connection, question_id: int) -> int:
    """The conversation a question belongs to (the id of its first question), or 404."""
    row = conn.execute(
        "SELECT coalesce(thread_id, id) FROM questions WHERE id = ?", (question_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(404, "no such conversation")
    return int(row[0])


def _greeting(question: str) -> str | None:
    """A hello needs no model and no memory: the reply, or None for a real question."""
    if not re.fullmatch(r"(?:hello|hi|hey|bonjour|salut|coucou)[!?. ]*", question, re.IGNORECASE):
        return None
    return "Bonjour !" if question.lower().startswith(("bonjour", "salut", "coucou")) else "Hello!"


def _conversations(conn: Connection, limit: int) -> list[Json]:
    """Conversations, the most recently active first, each named by its first question."""
    rows = conn.execute(
        "SELECT q.id, q.asked_ms, q.text, q.state, count(f.id),"
        " max(q.asked_ms, coalesce(max(f.asked_ms), 0)) AS last_ms"
        " FROM questions q LEFT JOIN questions f ON f.thread_id = q.id"
        " WHERE q.thread_id IS NULL GROUP BY q.id ORDER BY last_ms DESC, q.id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    keys = ("id", "asked_ms", "text", "state", "follow_ups", "last_ms")
    return [dict(zip(keys, r, strict=True)) for r in rows]


def _queue(conn: Connection, question: str, follows: int | None) -> Json:
    """Store a question, in the conversation of ``follows`` if given, and queue its job."""
    now = _now_ms()
    thread = None if follows is None else _conversation_of(conn, follows)
    cursor = conn.execute(
        "INSERT INTO questions (asked_ms, text, thread_id) VALUES (?, ?, ?)",
        (now, question, thread),
    )
    question_id = int(cursor.lastrowid or 0)
    enqueue_in(conn, ask_job(question_id), now)
    return {"id": question_id, "thread": thread or question_id, "blocked": None}


def ask_routes(services: Services) -> APIRouter:
    router = APIRouter()

    @router.post("/ask")
    def ask(body: AskBody) -> Json:  # pyright: ignore[reportUnusedFunction]
        question = body.q.strip()
        reply = _greeting(question)
        if reply is not None:
            return {"id": None, "answer": reply, "blocked": None}
        blocked = services.ask_blocker()
        if blocked is not None:
            return {"id": None, "blocked": blocked}
        with services.db() as conn, db.transaction(conn):
            return _queue(conn, question, body.thread)

    @router.get("/ask/{question_id}")
    def one(question_id: int) -> Json:  # pyright: ignore[reportUnusedFunction]
        with services.db() as conn:
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM questions WHERE id = ?",  # noqa: S608 - constant columns
                (question_id,),
            ).fetchone()
        if row is None:
            raise HTTPException(404, "no such question")
        return _row_json(row)

    @router.get("/ask/thread/{thread_id}")
    def thread(thread_id: int) -> list[Json]:  # pyright: ignore[reportUnusedFunction]
        """The whole conversation a question belongs to, in the order it was asked."""
        with services.db() as conn:
            root = _conversation_of(conn, thread_id)
            rows = conn.execute(
                f"SELECT {_COLUMNS} FROM questions"  # noqa: S608 - constant columns
                " WHERE id = ? OR thread_id = ? ORDER BY id",
                (root, root),
            ).fetchall()
        return [_row_json(r) for r in rows]

    @router.get("/ask")
    def past(  # pyright: ignore[reportUnusedFunction]
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
    ) -> list[Json]:
        with services.db() as conn:
            return _conversations(conn, limit)

    @router.delete("/ask/{question_id}")
    def forget(question_id: int) -> Json:  # pyright: ignore[reportUnusedFunction]
        """Forget a question; the first question of a conversation takes the rest with it."""
        with services.db() as conn, db.transaction(conn):
            gone = conn.execute(
                "DELETE FROM questions WHERE id = ? OR thread_id = ?", (question_id, question_id)
            ).rowcount
        if not gone:
            raise HTTPException(404, "no such question")
        return {"deleted": question_id}

    return router
