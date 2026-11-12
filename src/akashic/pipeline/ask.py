"""The ``ask`` job: one question, from words to a cited answer. Runs in the worker, which owns
the models; the API process only queues the job and polls the ``questions`` row.

Steps, each visible to the page through ``questions.step``:

1. *planning*: the chat model turns the question into a filter (grammar-constrained JSON);
   time is resolved in Python. Any trouble falls back to the rule-based plan.
2. *searching*: embed the planned topic, then filter and rank by words and meaning. Follow-ups
   retain the previous/current question pair. Both models may stay loaded (``models_max=2``).
3. *answering*: the chat model writes the answer from the numbered sources only, streamed into
   the row. Citations to sources that do not exist are removed. With no sources at all the
   model is not called.

The job is light: the user is waiting for it, so it runs even during a game or while the GPU
is busy. The worker gives the VRAM back right after when a game is running.

Content never reaches the log: only counts and the exception type.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Final, Protocol, cast

from akashic.pipeline.jobs import NewJob, PermanentJobError
from akashic.pipeline.llm import LlamaError, strip_thinking
from akashic.search import answer, hybrid, planner, semantic
from akashic.search.timeparse import TimeRange
from akashic.storage import db

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping
    from sqlite3 import Connection

    import numpy as np
    from numpy.typing import NDArray

    from akashic.pipeline.jobs import Job
    from akashic.search.planner import Plan

    type Vector = NDArray[np.float32]

log = logging.getLogger(__name__)

ASK: Final = "ask"
FLUSH_S: Final = 0.25  # how often the growing answer is written to the row
LIMIT: Final = 10  # results per kind handed to the source builder


class Chat(Protocol):
    model: str

    def available(self) -> bool: ...

    def complete_json(
        self, system: str, user: str, schema: Mapping[str, object], max_tokens: int = ...
    ) -> str: ...

    def stream(self, system: str, user: str, max_tokens: int = ...) -> Iterator[str]: ...

    def unload_if_idle(self, *, force: bool = False) -> None: ...


class QueryEmbedder(Protocol):
    model: str

    def available(self) -> bool: ...

    def embed_query(self, text: str) -> Vector: ...


def ask_job(question_id: int) -> NewJob:
    """The job for a question. ``data_ms`` 0 puts it ahead of any backlog."""
    return NewJob(ASK, f"{ASK}:{question_id}", 0, {"question_id": question_id})


def plan_json(plan: Plan, *, semantic: bool) -> str:
    """What was understood, as the page shows it."""
    return json.dumps(
        {
            "time": None
            if plan.time is None
            else {
                "start_ms": plan.time.start_ms,
                "end_ms": plan.time.end_ms,
                "label": plan.time.label,
            },
            "people": list(plan.people),
            "kinds": list(plan.kinds),
            "apps": list(plan.apps),
            "latest": plan.latest,
            "overview": plan.overview,
            "keywords": list(plan.keywords),
            "semantic_query": plan.semantic_query,
            "semantic": semantic,
        }
    )


def stored_plan(raw: object) -> Plan | None:
    """A plan as ``plan_json`` stored it, or None when there is none or it does not read."""
    if not isinstance(raw, str):
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    fields = cast("dict[str, object]", data)

    def strings(key: str) -> tuple[str, ...]:
        value = fields.get(key)
        items = cast("list[object]", value) if isinstance(value, list) else []
        return tuple(x for x in items if isinstance(x, str))

    time_range = None
    when = fields.get("time")
    topic = fields.get("semantic_query")
    if isinstance(when, dict):
        span = cast("dict[str, object]", when)
        start, end, label = span.get("start_ms"), span.get("end_ms"), span.get("label")
        if isinstance(start, int) and isinstance(end, int):
            time_range = TimeRange(start, end, label if isinstance(label, str) else "")
    return planner.Plan(
        time=time_range,
        people=strings("people"),
        kinds=strings("kinds"),
        apps=strings("apps"),
        latest=fields.get("latest") is True,
        overview=fields.get("overview") is True,
        keywords=strings("keywords"),
        semantic_query=topic if isinstance(topic, str) else "",
    )


@dataclass(frozen=True, slots=True)
class Earlier:
    """An earlier question of the conversation: what was asked and answered, what it searched."""

    turn: answer.Turn
    plan: Plan | None


def sources_json(sources: list[answer.Source]) -> str:
    return json.dumps(
        [
            {
                "n": s.n,
                "kind": s.kind,
                "id": s.ref_id,
                "start_ms": s.start_ms,
                "end_ms": s.end_ms,
                "label": s.label,
                "text": s.text,
                "episode_id": s.episode_id,
                "segment_ids": s.segment_ids,
            }
            for s in sources
        ]
    )


class AskHandler:
    kind: ClassVar[str] = ASK
    heavy: ClassVar[bool] = False  # asked by the user just now: never waits for a game

    def __init__(  # noqa: PLR0913, PLR0917 - the database, both models, and their presence
        self,
        conn: Connection,
        chat: Chat,
        embedder: QueryEmbedder,
        chat_present: Callable[[], bool],
        embedding_present: Callable[[], bool],
        clock_ms: Callable[[], int] = lambda: time.time_ns() // 1_000_000,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._conn = conn
        self._chat = chat
        self._embedder = embedder
        self._chat_present = chat_present
        self._embedding_present = embedding_present
        self._clock_ms = clock_ms
        self._monotonic = monotonic

    def blocked(self) -> str | None:
        if not self._chat.available():
            return "llama-server not downloaded yet"
        if not self._chat_present():
            return "chat model not downloaded yet"
        return None

    # --- the row -----------------------------------------------------------------------------

    def _set(self, question_id: int, **columns: object) -> bool:
        names = ", ".join(f"{c} = ?" for c in columns)
        with db.transaction(self._conn) as conn:
            changed = conn.execute(
                f"UPDATE questions SET {names} WHERE id = ? AND state != 'failed'",  # noqa: S608
                (*columns.values(), question_id),
            ).rowcount
        return changed == 1

    def _save_sources(self, question_id: int, sources: list[answer.Source]) -> bool:
        """Publish excerpts and their provenance together, unless deletion cancelled this job."""
        with db.transaction(self._conn) as conn:
            if (
                conn.execute(
                    "UPDATE questions SET step = 'answering', sources = ?"
                    " WHERE id = ? AND state = 'working'",
                    (sources_json(sources), question_id),
                ).rowcount
                != 1
            ):
                return False
            segment_ids = {i for s in sources for i in s.segment_ids}
            segment_ids.update(s.ref_id for s in sources if s.kind == "line")
            for source in sources:
                if source.kind == "episode":
                    segment_ids.update(
                        int(r[0])
                        for r in conn.execute(
                            "SELECT item_id FROM episode_items WHERE episode_id = ?"
                            " AND item_kind IN ('segment', 'video_segment')",
                            (source.ref_id,),
                        )
                    )
            conn.executemany(
                "INSERT OR IGNORE INTO question_segments VALUES (?, ?)",
                [(question_id, i) for i in segment_ids],
            )
            conn.executemany(
                "INSERT OR IGNORE INTO question_frames VALUES (?, ?)",
                [(question_id, s.ref_id) for s in sources if s.kind == "frame"],
            )
            # Keep inherited provenance even if an earlier turn is later forgotten separately.
            for lineage, column in (
                ("question_segments", "segment_id"),
                ("question_frames", "frame_id"),
            ):
                conn.execute(
                    f"INSERT OR IGNORE INTO {lineage} SELECT ?, p.{column}"  # noqa: S608 - fixed names
                    f" FROM {lineage} p JOIN questions earlier ON earlier.id = p.question_id"
                    " JOIN questions current ON current.id = ? WHERE earlier.id < current.id"
                    " AND coalesce(earlier.thread_id, earlier.id)"
                    " = coalesce(current.thread_id, current.id)",
                    (question_id, question_id),
                )
        return True

    # --- the steps ---------------------------------------------------------------------------

    def _vector(self, text: str) -> Vector | None:
        """The question's vector, or None (no embedding model, no vectors yet, or it failed)."""
        model = self._embedder.model
        if not (self._embedder.available() and self._embedding_present()):
            return None
        if not semantic.semantic_ready(self._conn, model):
            return None
        try:
            return self._embedder.embed_query(text)
        except LlamaError:
            return None

    def _plan(self, question: str, now: int, earlier: list[Earlier]) -> Plan:
        names = hybrid.known_names(self._conn)
        system, user = planner.plan_prompt(question, names, [e.turn.question for e in earlier])
        try:
            raw = self._chat.complete_json(system, user, planner.PLAN_SCHEMA)
        except LlamaError:
            plan = planner.heuristic_plan(question, now, names)
        else:
            plan = planner.plan_from_model(raw, question, now, names)
        last = next((e.plan for e in reversed(earlier) if e.plan is not None), None)
        return plan if last is None else planner.follow_up(plan, last)

    def _earlier(self, question_id: int, thread_id: int | None) -> list[Earlier]:
        """The answered questions before this one in its conversation, oldest first."""
        if thread_id is None:
            return []
        rows = self._conn.execute(
            "SELECT text, answer, plan FROM questions WHERE (id = ? OR thread_id = ?)"
            " AND id < ? AND state = 'done' ORDER BY id DESC LIMIT ?",
            (thread_id, thread_id, question_id, answer.MAX_TURNS),
        ).fetchall()
        return [
            Earlier(answer.Turn(str(r[0]), str(r[1] or "")), stored_plan(r[2]))
            for r in reversed(rows)
        ]

    def _write_answer(  # noqa: PLR0913 - the question, its plan, its evidence, the conversation
        self,
        question_id: int,
        question: str,
        plan: Plan,
        sources: list[answer.Source],
        now: int,
        *,
        history: list[answer.Turn],
        overview_total: int | None,
    ) -> str:
        system, user = answer.answer_prompt(
            question, plan, sources, now, history, overview_total=overview_total
        )
        text, last_flush = "", self._monotonic()
        for piece in self._chat.stream(system, user):
            text += piece
            if self._monotonic() - last_flush >= FLUSH_S:
                last_flush = self._monotonic()
                if not self._set(question_id, answer=strip_thinking(text)):
                    return ""
        return strip_thinking(text)

    # --- the job -----------------------------------------------------------------------------

    def run(self, job: Job) -> None:
        question_id = job.payload.get("question_id")
        if not isinstance(question_id, int):
            msg = "ask job without a question"
            raise PermanentJobError(msg)
        row = self._conn.execute(
            "SELECT text, state, thread_id FROM questions WHERE id = ?", (question_id,)
        ).fetchone()
        if row is None or row[1] in {"done", "failed"}:
            return  # deleted meanwhile, or already answered by an earlier run
        question = str(row[0])
        earlier = self._earlier(question_id, None if row[2] is None else int(row[2]))
        log.info("answering queued question (%d earlier turn(s))", len(earlier))
        now = self._clock_ms()
        if not self._set(question_id, state="working", step="planning", answer="", error=None):
            return
        plan = self._plan(question, now, earlier)
        # Time/app/person constraints belong in filters; their phrasing can overwhelm a
        # cross-language topic match. Preserve full question context for terse follow-ups.
        self._set(question_id, step="searching")
        query = (
            f"{earlier[-1].turn.question}\n{question}"
            if earlier
            else plan.semantic_query or question
        )
        qvec = self._vector(query)
        self._set(question_id, step="searching", plan=plan_json(plan, semantic=qvec is not None))
        found = hybrid.search_memory(self._conn, plan, qvec, self._embedder.model, limit=LIMIT)
        sources = answer.build_sources(found, overview=plan.overview)
        log.info("retrieved %d source(s) for question", len(sources))
        if not sources:
            self._set(
                question_id,
                state="done",
                step="",
                answer=answer.no_evidence(question),
                sources="[]",
                model=None,
                finished_ms=self._clock_ms(),
            )
            return
        if not self._save_sources(question_id, sources):
            return
        text = self._write_answer(
            question_id,
            question,
            plan,
            sources,
            now,
            history=[e.turn for e in earlier],
            overview_total=found.overview_total,
        )
        cleaned, _ = answer.clean_answer(text, sources)
        self._set(
            question_id,
            state="done",
            step="",
            answer=cleaned or answer.no_evidence(question),
            model=self._chat.model,
            finished_ms=self._clock_ms(),
        )

    def give_up(self, job: Job) -> None:
        question_id = job.payload.get("question_id")
        if isinstance(question_id, int):
            self._set(
                question_id,
                state="failed",
                step="",
                error="the model could not answer",
                finished_ms=self._clock_ms(),
            )

    def idle(self, *, force: bool = False) -> None:
        self._chat.unload_if_idle(force=force)
