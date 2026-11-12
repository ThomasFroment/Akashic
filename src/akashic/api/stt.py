"""User-selected speech comparisons; references/predictions stay in SQLCipher."""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from akashic.config import Paths, load_config
from akashic.pipeline.jobs import NewJob, enqueue_in
from akashic.pipeline.models import SPEECH_CANDIDATES, is_ready
from akashic.pipeline.stt_compare import STT_COMPARE, term_score
from akashic.storage import db

if TYPE_CHECKING:
    from akashic.api.server import Services

type Json = dict[str, object]
MAX_SAMPLES = 12
MAX_TERM_CHARS = 100
RESULT_KEYS = (
    "segment_id",
    "model",
    "reference",
    "prediction",
    "language",
    "elapsed_ms",
    "errors",
    "words",
    "term_hits",
    "term_total",
    "error",
    "revision",
    "reference_language",
    "condition",
    "draft_model",
    "draft_derived",
    "previous_reference",
    "overlap_reference",
    "char_errors",
    "chars",
)
COUNTERS = ("clips", "completed", "failed", "pending")
METRICS = ("errors", "words", "char_errors", "chars", "term_hits", "term_total")
INDEPENDENT_OF_DRAFT = "independent of draft"


class SampleBody(BaseModel):
    segment_id: int = Field(gt=0)
    reference: str = Field(max_length=6000)
    terms: list[str] = Field(default_factory=list, max_length=20)
    reference_language: str = Field(pattern="^(fr|en|mixed|none)$")
    condition: str = Field(min_length=1, max_length=80)
    draft_derived: bool = False
    previous_reference: str = Field(default="", max_length=6000)
    overlap_reference: str = Field(default="", max_length=6000)


class CompareBody(BaseModel):
    models: list[str] = Field(min_length=1, max_length=4)


def comparison_scores(results: list[Json]) -> list[Json]:
    """Explicit full-deletion denominator, with pending clips excluded and counted separately."""
    groups: dict[tuple[str, str, str], Json] = {}
    for row in results:
        labels = [
            ("all", "all"),
            ("language", str(row["reference_language"])),
            ("condition", str(row["condition"])),
        ]
        if not row["draft_derived"]:
            labels.append(("provenance", INDEPENDENT_OF_DRAFT))
        for dimension, label in labels:
            key = (str(row["model"]), dimension, label)
            group = groups.setdefault(
                key,
                {
                    "model": key[0],
                    "dimension": dimension,
                    "label": label,
                    **dict.fromkeys((*COUNTERS, *METRICS), 0),
                    "denominator": (
                        "failed decodes count as full deletion; pending excluded; "
                        "zero-reference insertions retained"
                    ),
                },
            )
            group["clips"] = int(str(group["clips"])) + 1
            pending = row["prediction"] is None and row["error"] is None
            count_key = "pending" if pending else "failed" if row["error"] else "completed"
            group[count_key] = int(str(group[count_key])) + 1
            if not pending:
                for metric in METRICS:
                    group[metric] = int(str(group[metric])) + int(str(row[metric] or 0))
    for model, dimension, _label in list(groups):
        independent = (model, "provenance", INDEPENDENT_OF_DRAFT)
        if dimension == "all" and independent not in groups:
            empty = dict(groups[model, "all", "all"])
            empty.update(dimension="provenance", label=INDEPENDENT_OF_DRAFT)
            empty.update(dict.fromkeys((*COUNTERS, *METRICS), 0))
            groups[independent] = empty
    return list(groups.values())


def stt_routes(services: Services) -> APIRouter:  # noqa: C901, PLR0915 - independent HTTP handlers
    router = APIRouter(prefix="/stt")

    def paths() -> Paths:
        if services.config_file is None:
            raise HTTPException(503, "settings unavailable")
        return Paths(services.config_file.parent)

    @router.get("")
    def overview() -> Json:  # pyright: ignore[reportUnusedFunction]
        folders = paths()
        with services.db() as conn:
            samples = conn.execute(
                "SELECT "
                "s.segment_id,a.start_ms,a.stream,s.reference,s.terms,"
                "s.reference_language,s.condition,s.draft_derived,s.draft_model"
                " FROM stt_samples s JOIN audio_segments a ON a.id=s.segment_id ORDER BY a.start_ms"
            ).fetchall()
            runs: list[Json] = []
            for run_id, state, created, version in conn.execute(
                "SELECT id,state,created_ms,processing_version "
                "FROM stt_runs ORDER BY id DESC LIMIT 3"
            ).fetchall():
                rows = conn.execute(
                    f"SELECT {','.join(RESULT_KEYS)} FROM stt_results WHERE run_id=?"  # noqa: S608 - fixed column names
                    " ORDER BY segment_id,model",
                    (run_id,),
                ).fetchall()
                results: list[Json] = [dict(zip(RESULT_KEYS, r, strict=True)) for r in rows]
                runs.append(
                    {
                        "id": run_id,
                        "state": state,
                        "created_ms": created,
                        "processing_version": version,
                        "results": results,
                        "scores": comparison_scores(results),
                    }
                )
        return {
            "models": [
                {
                    "key": k,
                    "label": s.description,
                    "ready": is_ready(folders.models_dir, folders.runtime_dir, s),
                }
                for k, s in SPEECH_CANDIDATES.items()
            ],
            "samples": [
                {
                    "segment_id": r[0],
                    "start_ms": r[1],
                    "stream": r[2],
                    "reference": r[3],
                    "terms": json.loads(r[4]),
                    "reference_language": r[5],
                    "condition": r[6],
                    "draft_derived": bool(r[7]),
                    "draft_model": r[8],
                }
                for r in samples
            ],
            "runs": runs,
        }

    @router.get("/recordings")
    def recordings(segment_id: int | None = None) -> list[Json]:  # pyright: ignore[reportUnusedFunction]
        with services.db() as conn:
            rows = conn.execute(
                "SELECT id,start_ms,stream,transcript,transcription_model FROM audio_segments"
                " WHERE audio_path IS NOT NULL AND status IN ('done','empty')"
                " AND (? IS NULL OR id=?) ORDER BY start_ms DESC LIMIT 24",
                (segment_id, segment_id),
            ).fetchall()
        return [
            {
                "segment_id": r[0],
                "start_ms": r[1],
                "stream": r[2],
                "transcript": (r[3] or "")[:6000],
                "draft_model": r[4] or "unknown (capture model was not recorded)",
            }
            for r in rows
        ]

    @router.post("/samples")
    def save_sample(body: SampleBody) -> Json:  # pyright: ignore[reportUnusedFunction]
        terms = list(dict.fromkeys(t.strip() for t in body.terms if t.strip()))
        if not body.condition.strip() or any(len(t) > MAX_TERM_CHARS for t in terms):
            raise HTTPException(422, "provide a condition and short important terms")
        _, present = term_score(terms, body.reference, body.reference)
        if present != len(terms):
            raise HTTPException(422, "important terms must appear in the corrected reference")
        with services.db() as conn, db.transaction(conn):
            row = conn.execute(
                "SELECT audio_path,transcription_model FROM audio_segments "
                "WHERE id=? AND status IN ('done','empty')",
                (body.segment_id,),
            ).fetchone()
            if row is None or row[0] is None:
                raise HTTPException(404, "recording unavailable")
            count = conn.execute("SELECT count(*) FROM stt_samples").fetchone()[0]
            exists = conn.execute(
                "SELECT 1 FROM stt_samples WHERE segment_id=?",
                (body.segment_id,),
            ).fetchone()
            if count >= MAX_SAMPLES and not exists:
                raise HTTPException(422, "keep at most twelve comparison clips")
            conn.execute(
                "INSERT INTO stt_samples(segment_id,reference,terms,reference_language,condition,"
                "draft_derived,previous_reference,overlap_reference,draft_model) "
                "VALUES (?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(segment_id) DO UPDATE SET "
                "reference=excluded.reference,terms=excluded.terms,"
                "reference_language=excluded.reference_language,condition=excluded.condition,"
                "draft_derived=excluded.draft_derived,previous_reference=excluded.previous_reference,"
                "overlap_reference=excluded.overlap_reference,draft_model=excluded.draft_model",
                (
                    body.segment_id,
                    body.reference.strip(),
                    json.dumps(terms),
                    body.reference_language,
                    body.condition.strip(),
                    body.draft_derived,
                    body.previous_reference,
                    body.overlap_reference,
                    row[1] or "unknown",
                ),
            )
        return {"saved": body.segment_id}

    @router.delete("/samples/{segment_id}")
    def delete_sample(segment_id: int) -> Json:  # pyright: ignore[reportUnusedFunction]
        with services.db() as conn, db.transaction(conn):
            conn.execute("DELETE FROM stt_samples WHERE segment_id=?", (segment_id,))
        return {"deleted": segment_id}

    @router.post("/runs")
    def compare(body: CompareBody) -> Json:  # pyright: ignore[reportUnusedFunction]
        folders = paths()
        models = list(dict.fromkeys(body.models))
        if any(k not in SPEECH_CANDIDATES for k in models):
            raise HTTPException(422, "unknown speech model")
        if any(
            not is_ready(folders.models_dir, folders.runtime_dir, SPEECH_CANDIDATES[k])
            for k in models
        ):
            raise HTTPException(409, "download the chosen models first")
        vocabulary = load_config(folders).audio.vocabulary
        now = time.time_ns() // 1_000_000
        with services.db() as conn, db.transaction(conn):
            if conn.execute(
                "SELECT 1 FROM stt_runs WHERE state IN ('queued','running')"
            ).fetchone():
                raise HTTPException(409, "a comparison is already waiting or running")
            samples = conn.execute(
                "SELECT "
                "segment_id,reference,terms,reference_language,condition,draft_model,draft_derived,"
                "previous_reference,overlap_reference FROM stt_samples ORDER BY segment_id"
            ).fetchall()
            if not samples:
                raise HTTPException(422, "add a corrected reference clip first")
            cursor = conn.execute(
                "INSERT INTO stt_runs(created_ms,vocabulary,processing_version) "
                "VALUES (?,?,'production-boundary-v1')",
                (now, json.dumps(vocabulary)),
            )
            run_id = cursor.lastrowid
            for (
                segment,
                reference,
                terms,
                language,
                condition,
                draft,
                derived,
                previous,
                overlap,
            ) in samples:
                for model in models:
                    conn.execute(
                        "INSERT INTO stt_results(run_id,segment_id,model,revision,reference,terms,"
                        "reference_language,condition,draft_model,draft_derived,previous_reference,overlap_reference)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            run_id,
                            segment,
                            model,
                            SPEECH_CANDIDATES[model].base_url,
                            reference,
                            terms,
                            language,
                            condition,
                            draft,
                            derived,
                            previous,
                            overlap,
                        ),
                    )
            enqueue_in(
                conn, NewJob(STT_COMPARE, f"stt_compare:{run_id}", 0, {"run_id": run_id}), now
            )
        return {"id": run_id}

    return router
