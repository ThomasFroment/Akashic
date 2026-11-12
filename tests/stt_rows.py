"""Comparison rows with the columns the schema requires, for tests that seed them directly."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlite3 import Connection


def insert_sample(  # noqa: PLR0913 - one stt_samples row
    conn: Connection,
    segment_id: int,
    reference: str,
    terms: str = "[]",
    *,
    draft_derived: int = 1,
    previous_reference: str = "",
) -> None:
    conn.execute(
        "INSERT INTO stt_samples (segment_id, reference, terms, reference_language, condition,"
        " draft_model, draft_derived, previous_reference) VALUES (?, ?, ?, 'fr', 'quiet', 'draft',"
        " ?, ?)",
        (segment_id, reference, terms, draft_derived, previous_reference),
    )


def insert_run(conn: Connection, vocabulary: str, *, run_id: int | None = None) -> int:
    cursor = conn.execute(
        "INSERT INTO stt_runs (id, created_ms, vocabulary, processing_version)"
        " VALUES (?, 1, ?, 'test')",
        (run_id, vocabulary),
    )
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def insert_result(  # noqa: PLR0913, PLR0917 - one stt_results row
    conn: Connection,
    run_id: int,
    segment_id: int,
    model: str,
    reference: str,
    terms: str,
    *,
    prediction: str | None = None,
    previous_reference: str = "",
    overlap_reference: str = "",
) -> None:
    conn.execute(
        "INSERT INTO stt_results (run_id, segment_id, model, revision, reference, terms,"
        " prediction, reference_language, condition, draft_model, draft_derived,"
        " previous_reference, overlap_reference)"
        " VALUES (?, ?, ?, 'pin', ?, ?, ?, 'fr', 'quiet', 'draft', 1, ?, ?)",
        (
            run_id,
            segment_id,
            model,
            reference,
            terms,
            prediction,
            previous_reference,
            overlap_reference,
        ),
    )
