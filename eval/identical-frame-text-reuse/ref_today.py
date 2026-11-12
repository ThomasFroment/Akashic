"""Reference candidate: today's frame text pipeline, copied from src (frame paths only).

Sources (verbatim logic; only the parts that concern frames are kept, see notes):
- connect/transaction:       src/akashic/storage/db.py
- schema:                    schema_today.sql (migrations 0001, 0002, 0015, 0018)
- insert_frame / set_text:   src/akashic/storage/frames.py FrameStore.add / set_text
                             (minus media write and the gaming-mode OCR job)
- embed_pending:             src/akashic/pipeline/embed.py enqueue_missing(_MISSING_FRAMES) +
                             EmbedHandler.run (frame branch), looped until nothing is missing
- windows/publish/top:       src/akashic/storage/passages.py (kind="frame")
- query_frames:              src/akashic/search/hybrid.py _ranked_frames + fuse +
                             _semantic_coverage, called with limit min(10, 5) = 5 like
                             search_memory (plan: keywords = q_text words, no people/kinds/apps,
                             latest=False); build_match from src/akashic/search/keyword.py
- delete_frame:              the frame-row DELETE used by retention.delete_spent_frames, for one id
- expire_frame_text:         src/akashic/storage/retention.py expire_frame_text
- expire_embeddings:         src/akashic/storage/retention.py expire_embeddings (frame_passages
                             statement only: embeddings / segment_passages are not in this schema)
- delete_all:                src/akashic/storage/retention.py delete_all_recorded_data
                             (frames table only; no media store)

Candidate interface (every candidate module provides the same names):
    migrate(ctx)                                   create the schema on an empty database
    insert_frame(ctx, frame: dict) -> int          the capture write path
    set_text(ctx, frame_id, text, source)          deferred OCR text
    embed_pending(ctx, embedder)                   embed everything missing, then return
    query_frames(ctx, q_text, start_ms, end_ms)    -> list[(frame_id, ts_ms, extra)], <= 5
    delete_frame(ctx, frame_id)
    expire_frame_text(ctx, cutoff_ms)
    expire_embeddings(ctx, cutoff_ms)
    delete_all(ctx)
    optional: upgrade_from_today(ctx)              migrate a populated ref_today database
``ctx`` has: conn (sqlcipher3, pragmas of db.connect), model (str), hmac_key (32-byte DB
subkey, HKDF of the data key, purpose "frame-text-key"), subkey(purpose) -> bytes,
query_vector(q_text) -> float32 unit vector (memoised; its cost is not timed).
"""

from __future__ import annotations

import json
import re
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Final

import numpy as np
import sqlcipher3

if TYPE_CHECKING:
    from collections.abc import Generator, Iterable
    from sqlite3 import Connection

BUSY_TIMEOUT_MS: Final = 5_000
SCHEMA: Final = Path(__file__).with_name("schema_today.sql")

# passages.py
CHARS: Final = 400
STEP: Final = 300
# embeddings.py
CHUNK_ROWS: Final = 4_000
# embed.py
BATCH: Final = 32
MIN_TEXT_CHARS: Final = 12
# hybrid.py
RRF_K: Final = 60
FTS_DEPTH: Final = 200
VECTOR_DEPTH: Final = 100
FRAME_SLOTS: Final = 5  # search_memory: _ranked_frames(conn, scope, min(limit, 5))
# keyword.py
HIGHLIGHT_START: Final = "\x02"
HIGHLIGHT_END: Final = "\x03"
MAX_TERMS: Final = 12
_WORD = re.compile(r"\w+", re.UNICODE)
# retention.py
RETENTION_BATCH: Final = 500


# --- db.py ------------------------------------------------------------------------------------


def connect(path: Path, db_key: bytes) -> Connection:
    """Open (or create) the encrypted database with ``db_key`` as the raw SQLCipher key."""
    if len(db_key) != 32:  # noqa: PLR2004
        msg = "database key must be 32 bytes"
        raise ValueError(msg)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlcipher3.connect(
        str(path), timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None, check_same_thread=False
    )
    conn.execute(f"PRAGMA key = \"x'{db_key.hex()}'\"")
    conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA temp_store = MEMORY")
    return conn


@contextmanager
def transaction(conn: Connection) -> Generator[Connection]:
    """A short write transaction. Takes the write lock up front to avoid upgrade deadlocks."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def run_migration(conn: Connection, sql: str, version: int) -> None:
    """db.migrate() for one script: atomic, with its own BEGIN/COMMIT."""
    script = f"BEGIN IMMEDIATE;\n{sql}\nPRAGMA user_version = {version};\nCOMMIT;"
    try:
        conn.executescript(script)
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def migrate(ctx) -> None:  # noqa: ANN001
    run_migration(ctx.conn, SCHEMA.read_text("utf-8"), 18)


# --- frames.py --------------------------------------------------------------------------------


def insert_frame(ctx, frame: dict[str, object]) -> int:  # noqa: ANN001
    with transaction(ctx.conn) as conn:
        cursor = conn.execute(
            "INSERT INTO frames (ts_ms, monitor, process, window_title, url, text,"
            " text_source, trigger, image_path, focused, image_scope)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                frame["ts_ms"],
                frame["monitor"],
                frame["process"],
                frame["window_title"],
                frame["url"],
                frame["text"],
                frame["text_source"],
                frame["trigger"],
                frame["image_path"],
                int(frame["focused"]),  # type: ignore[arg-type]
                frame["image_scope"],
            ),
        )
        row_id = cursor.lastrowid
    return int(row_id)


def set_text(ctx, frame_id: int, text: str, source: str) -> None:  # noqa: ANN001
    with transaction(ctx.conn) as conn:
        conn.execute(
            "UPDATE frames SET text = ?, text_source = ? WHERE id = ?", (text, source, frame_id)
        )


# --- passages.py ------------------------------------------------------------------------------


def windows(text: str) -> list[tuple[int, int]]:
    if not text:
        return []
    return [
        (start, min(start + CHARS, len(text)))
        for start in range(0, max(len(text) - CHARS, 0) + STEP, STEP)
    ]


def publish(conn: Connection, owner_id: int, model: str, text: str, vectors) -> bool:  # noqa: ANN001
    """Publish a complete set only if the source text has not changed during inference."""
    offsets = windows(text)
    rows = [
        (
            owner_id,
            model,
            start,
            end,
            len(vector),
            np.ascontiguousarray(vector, dtype=np.float16).tobytes(),
        )
        for (start, end), vector in zip(offsets, vectors, strict=True)
    ]
    with transaction(conn):
        current = conn.execute("SELECT text FROM frames WHERE id = ?", (owner_id,)).fetchone()
        if current is None or current[0] != text:
            return False
        conn.execute("DELETE FROM frame_passages WHERE frame_id = ? AND model = ?", (owner_id, model))
        conn.executemany("INSERT INTO frame_passages VALUES (?, ?, ?, ?, ?, ?)", rows)
    return True


def _order(hit: tuple[int, float, int, int]) -> tuple[float, int]:
    return -hit[1], hit[0]


def top(
    conn: Connection,
    model: str,
    query: np.ndarray,
    *,
    start_ms: int,
    end_ms: int,
    limit: int = 100,
) -> list[tuple[int, float, int, int]]:
    """passages.top(kind="frame", apps=(), only=None): max-passage score per frame.
    Returns (owner_id, score, start_char, end_char)."""
    sql = (
        "SELECT p.frame_id, p.start_char, p.end_char, p.vec FROM frame_passages p"
        " JOIN frames s ON s.id = p.frame_id WHERE p.model = :model"
        " AND s.ts_ms >= :start AND s.ts_ms < :end"
        " AND s.text IS NOT NULL"
    )
    cursor = conn.execute(sql, {"model": model, "start": start_ms, "end": end_ms})
    best: dict[int, tuple[int, float, int, int]] = {}
    while rows := cursor.fetchmany(CHUNK_ROWS):
        rows = [r for r in rows if len(r[3]) == len(query) * 2]
        if not rows:
            continue
        matrix = np.vstack([np.frombuffer(bytes(r[3]), dtype=np.float16) for r in rows]).astype(
            np.float32
        )
        for r, score in zip(rows, matrix @ query, strict=True):
            owner_id = int(r[0])
            hit = (owner_id, float(score), int(r[1]), int(r[2]))
            if owner_id not in best or hit[1] > best[owner_id][1]:
                best[owner_id] = hit
        retained = sorted(best.values(), key=_order)[:limit]
        best = {hit[0]: hit for hit in retained}
    return sorted(best.values(), key=_order)


# --- embed.py ---------------------------------------------------------------------------------

_MISSING_FRAMES: Final = (
    "SELECT f.id, f.ts_ms FROM frames f WHERE f.ts_ms >= :since AND length(f.text) >= :min"
    " AND NOT EXISTS (SELECT 1 FROM frame_passages p"
    "                 WHERE p.frame_id = f.id AND p.model = :model)"
    " ORDER BY f.ts_ms, f.id LIMIT :limit"
)


def _embed_frames(conn: Connection, embedder, ids: list[int]) -> None:  # noqa: ANN001
    """EmbedHandler.run, kind == 'frame'."""
    model = embedder.model
    marks = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT id, ts_ms, text FROM frames WHERE id IN ({marks})"  # noqa: S608
        " AND text IS NOT NULL",
        ids,
    ).fetchall()
    texts = {int(r[0]): (int(r[1]), str(r[2])) for r in rows}
    todo = [i for i in ids if i in texts]
    if not todo:
        return
    offsets = {i: windows(texts[i][1]) for i in todo}
    vectors = embedder.embed_documents(
        [texts[i][1][start:end] for i in todo for start, end in offsets[i]]
    )
    at = 0
    for owner_id in todo:
        _, text = texts[owner_id]
        end = at + len(offsets[owner_id])
        publish(conn, owner_id, model, text, vectors[at:end])
        at = end


def embed_pending(ctx, embedder, *, since_ms: int = 0) -> None:  # noqa: ANN001
    """Housekeeping's enqueue_missing + the embed job, repeated until nothing is missing."""
    seen: set[tuple[int, ...]] = set()
    while True:
        rows = ctx.conn.execute(
            _MISSING_FRAMES,
            {"model": embedder.model, "since": since_ms, "limit": BATCH, "min": MIN_TEXT_CHARS},
        ).fetchall()
        if not rows:
            return
        ids = tuple(int(r[0]) for r in rows)
        if ids in seen:  # a batch that cannot publish would be re-queued forever
            return
        seen.add(ids)
        _embed_frames(ctx.conn, embedder, list(ids))


# --- hybrid.py --------------------------------------------------------------------------------


def build_match(query: str) -> str | None:
    words = _WORD.findall(query)[:MAX_TERMS]
    if not words:
        return None
    terms = [f'"{w}"' for w in words]
    terms[-1] += "*"
    return " ".join(terms)


def fuse(*rankings: Iterable[int]) -> dict[int, float]:
    scores: dict[int, float] = {}
    for ranking in rankings:
        for rank, item in enumerate(ranking, start=1):
            scores[item] = scores.get(item, 0.0) + 1.0 / (RRF_K + rank)
    return scores


def _semantic_coverage(order: list[int], meaning: list[int], *, latest: bool) -> list[int]:
    if not latest or not order:
        return order
    return list(dict.fromkeys([order[0], *meaning[:3], *order]))


def query_frames(ctx, q_text: str, start_ms: int, end_ms: int) -> list[tuple[int, int, str]]:  # noqa: ANN001
    conn, limit = ctx.conn, FRAME_SLOTS
    qvec = ctx.query_vector(q_text)
    params: dict[str, object] = {
        "start": start_ms,
        "end": end_ms,
        "depth": FTS_DEPTH,
        "apps": json.dumps(()),
    }
    filters = " AND f.ts_ms >= :start AND f.ts_ms < :end"
    words: dict[int, str] = {}
    match = build_match(q_text)
    if match:
        params["match"] = match
        rows = conn.execute(
            "SELECT f.id, snippet(frames_fts, -1, :open, :close, '…', 24)"  # noqa: S608
            " FROM frames_fts JOIN frames f ON f.id = frames_fts.rowid"
            " WHERE frames_fts MATCH :match"
            + filters
            + " ORDER BY bm25(frames_fts, 1.0, 3.0, 2.0), f.ts_ms DESC, f.id DESC LIMIT :depth",
            {**params, "open": HIGHLIGHT_START, "close": HIGHLIGHT_END},
        ).fetchall()
        words = {int(r[0]): str(r[1] or "") for r in rows}
    meaning: dict[int, tuple[int, float, int, int]] = {}
    if qvec is not None:
        meaning = {
            hit[0]: hit
            for hit in top(conn, ctx.model, qvec, start_ms=start_ms, end_ms=end_ms, limit=VECTOR_DEPTH)
        }
    fused = fuse(list(words), list(meaning))
    order = sorted(fused, key=lambda i: -fused[i])
    ids = _semantic_coverage(order, list(meaning), latest=False)[:limit]
    if not ids:
        return []
    rows = conn.execute(
        "SELECT id, ts_ms, monitor, process, window_title, url, image_path IS NOT NULL,"
        " coalesce(text, ''), image_scope FROM frames"
        " WHERE id IN (SELECT value FROM json_each(?))",
        (json.dumps(ids),),
    ).fetchall()
    found: dict[int, tuple[int, int, str]] = {}
    for row in rows:
        frame_id = int(row[0])
        passage = meaning.get(frame_id)
        snippet = str(row[7])[passage[2] : passage[3]] if passage is not None else words[frame_id]
        found[frame_id] = (frame_id, int(row[1]), snippet)
    return [found[i] for i in ids if i in found]


# --- deletion paths ---------------------------------------------------------------------------


def delete_frame(ctx, frame_id: int) -> None:  # noqa: ANN001
    with transaction(ctx.conn) as conn:
        conn.execute("DELETE FROM frames WHERE id = ?", (frame_id,))


def expire_frame_text(ctx, cutoff_ms: int) -> int:  # noqa: ANN001
    conn = ctx.conn
    total = 0
    while True:
        with transaction(conn):
            changed = conn.execute(
                "UPDATE frames SET text = NULL, text_source = NULL WHERE id IN"
                " (SELECT id FROM frames WHERE ts_ms < ? AND text IS NOT NULL LIMIT ?)",
                (cutoff_ms, RETENTION_BATCH),
            ).rowcount
        total += changed
        if changed < RETENTION_BATCH:
            return total


def expire_embeddings(ctx, cutoff_ms: int) -> int:  # noqa: ANN001
    conn = ctx.conn
    total = 0
    while True:
        with transaction(conn):
            frame_rows = conn.execute(
                "DELETE FROM frame_passages WHERE rowid IN"
                " (SELECT p.rowid FROM frame_passages p JOIN frames f"
                " ON f.id = p.frame_id WHERE f.ts_ms < ? LIMIT ?)",
                (cutoff_ms, RETENTION_BATCH),
            ).rowcount
        total += frame_rows
        if frame_rows < RETENTION_BATCH:
            return total


def delete_all(ctx) -> dict[str, int]:  # noqa: ANN001
    with transaction(ctx.conn) as conn:
        removed = {
            "screenshots": int(
                conn.execute("SELECT count(*) FROM frames WHERE image_path IS NOT NULL").fetchone()[0]
            )
        }
        # Child rows (frame_passages) and FTS entries go through the schema's cascade/triggers.
        for table in ("frames",):
            removed[table] = conn.execute(f"DELETE FROM {table}").rowcount  # noqa: S608
    return removed
