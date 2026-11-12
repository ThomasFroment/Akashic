"""C1: shared content-addressed passages with a write-path tag.

frames.text_key = HMAC-SHA256(ctx.hmac_key, text) is written in the same statement as the text
(insert, set_text) and cleared with it (expire_frame_text). Passages live once per key in
text_passages; triggers delete a key's passages when the last frame carrying it goes (delete or
key change), in the statement's own transaction. Guards refuse a text without its key (or a text
change without a key change). Search ranks (text_key, window_title, url) groups instead of frames
and returns one representative frame per group with an "also seen" count/range.

expire_embeddings records a vector floor (vector_floor.cutoff_ms) so the vector side only expands
keys to frames with ts >= floor, keeping today's per-frame semantics for keys that straddle it.
"""

from __future__ import annotations

import hashlib
import hmac
import sys
from pathlib import Path
from typing import Final

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ref_today  # noqa: E402
from ref_today import (  # noqa: E402
    BATCH,
    CHUNK_ROWS,
    FRAME_SLOTS,
    FTS_DEPTH,
    HIGHLIGHT_END,
    HIGHLIGHT_START,
    MIN_TEXT_CHARS,
    RETENTION_BATCH,
    VECTOR_DEPTH,
    build_match,
    fuse,
    transaction,
    windows,
)

KEY_BATCH: Final = 64  # keys deleted per expire_embeddings transaction (each ~1-70 rows)

_COMMON_SCHEMA = ref_today.SCHEMA.read_text("utf-8").split("-- ===== 0018_frame_passages.sql")[0]

_TABLES = """
-- HMAC-SHA256(DB subkey "frame-text-key", utf-8 text): never a plain digest.
ALTER TABLE frames ADD COLUMN text_key BLOB CHECK (text_key IS NULL OR length(text_key) = 32);
CREATE INDEX frames_text_key ON frames (text_key, ts_ms);

CREATE TABLE text_passages (
    text_key BLOB NOT NULL,
    model TEXT NOT NULL,
    start_char INTEGER NOT NULL CHECK (start_char >= 0),
    end_char INTEGER NOT NULL CHECK (end_char > start_char),
    dim INTEGER NOT NULL CHECK (dim > 0),
    vec BLOB NOT NULL CHECK (length(vec) = dim * 2),
    PRIMARY KEY (text_key, model, start_char)
) WITHOUT ROWID;

-- Frames older than cutoff_ms have no vectors of their own (expire_embeddings).
CREATE TABLE vector_floor (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    cutoff_ms INTEGER NOT NULL
);
"""

_TRIGGERS = """
CREATE TRIGGER frames_text_key_ad AFTER DELETE ON frames
WHEN old.text_key IS NOT NULL BEGIN
    DELETE FROM text_passages WHERE text_key = old.text_key
        AND NOT EXISTS (SELECT 1 FROM frames WHERE text_key = old.text_key);
END;

CREATE TRIGGER frames_text_key_au AFTER UPDATE OF text_key ON frames
WHEN old.text_key IS NOT NULL AND old.text_key IS NOT new.text_key BEGIN
    DELETE FROM text_passages WHERE text_key = old.text_key
        AND NOT EXISTS (SELECT 1 FROM frames WHERE text_key = old.text_key);
END;

CREATE TRIGGER frames_text_key_guard_bi BEFORE INSERT ON frames
WHEN (new.text IS NULL) <> (new.text_key IS NULL) BEGIN
    SELECT RAISE(ABORT, 'frames.text_key must accompany frames.text');
END;

CREATE TRIGGER frames_text_key_guard_bu BEFORE UPDATE OF text, text_key ON frames
WHEN (new.text IS NULL) <> (new.text_key IS NULL)
    OR (old.text IS NOT new.text AND old.text_key IS new.text_key) BEGIN
    SELECT RAISE(ABORT, 'frames.text_key must change with frames.text');
END;
"""


def text_key(ctx, text: str | None) -> bytes | None:  # noqa: ANN001
    if text is None:
        return None
    return hmac.new(ctx.hmac_key, text.encode("utf-8", "surrogatepass"), hashlib.sha256).digest()


def _check_pragmas(conn) -> None:  # noqa: ANN001
    # The cleanup triggers rely on non-recursive triggers (default).
    if int(conn.execute("PRAGMA recursive_triggers").fetchone()[0]) != 0:
        msg = "recursive_triggers must be off"
        raise RuntimeError(msg)


def migrate(ctx) -> None:  # noqa: ANN001
    _check_pragmas(ctx.conn)
    ref_today.run_migration(ctx.conn, _COMMON_SCHEMA + _TABLES + _TRIGGERS, 19)


def upgrade_from_today(ctx) -> None:  # noqa: ANN001
    """Migrate a populated ref_today database (user_version 18) in one transaction."""
    conn = ctx.conn
    _check_pragmas(conn)
    with transaction(conn):
        # executescript would COMMIT: run the statements one by one.
        for stmt in _split(_TABLES):
            conn.execute(stmt)
        # Backfill keys without re-indexing FTS (text/title/url do not change).
        fts_au = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'frames_au'").fetchone()[0]
        conn.execute("DROP TRIGGER frames_au")
        rows = conn.execute("SELECT id, text FROM frames WHERE text IS NOT NULL").fetchall()
        conn.executemany(
            "UPDATE frames SET text_key = ? WHERE id = ?",
            [(text_key(ctx, str(t)), int(i)) for i, t in rows],
        )
        conn.execute(fts_au)
        # One complete passage set per (key, model), copied from any frame that has it.
        texts = {int(i): str(t) for i, t in rows}
        have = conn.execute(
            "SELECT f.text_key, p.model, p.frame_id, count(*) FROM frame_passages p"
            " JOIN frames f ON f.id = p.frame_id GROUP BY p.frame_id, p.model"
        ).fetchall()
        done: set[tuple[bytes, str]] = set()
        for key, model, frame_id, n in have:
            pair = (bytes(key), str(model))
            if pair in done or int(n) != len(windows(texts[int(frame_id)])):
                continue
            conn.execute(
                "INSERT INTO text_passages SELECT ?, model, start_char, end_char, dim, vec"
                " FROM frame_passages WHERE frame_id = ? AND model = ?",
                (pair[0], int(frame_id), pair[1]),
            )
            done.add(pair)
        conn.execute("DROP TABLE frame_passages")  # also drops frames_passages_au and its index
        for stmt in _split(_TRIGGERS):  # guards only after the backfill
            conn.execute(stmt)
        conn.execute("PRAGMA user_version = 19")


def _split(script: str) -> list[str]:
    """Split a script into statements (triggers keep their inner ';')."""
    out, buf = [], ""
    for line in script.splitlines():
        if line.strip().startswith("--") or not line.strip():
            continue
        buf += line + "\n"
        if line.rstrip().endswith(";") and (not buf.lstrip().upper().startswith("CREATE TRIGGER") or line.strip() == "END;"):
            out.append(buf)
            buf = ""
    return out


# --- write path -------------------------------------------------------------------------------


def insert_frame(ctx, frame: dict[str, object]) -> int:  # noqa: ANN001
    text = frame["text"]
    key = text_key(ctx, None if text is None else str(text))
    with transaction(ctx.conn) as conn:
        cursor = conn.execute(
            "INSERT INTO frames (ts_ms, monitor, process, window_title, url, text,"
            " text_source, trigger, image_path, focused, image_scope, text_key)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                frame["ts_ms"],
                frame["monitor"],
                frame["process"],
                frame["window_title"],
                frame["url"],
                text,
                frame["text_source"],
                frame["trigger"],
                frame["image_path"],
                int(frame["focused"]),  # type: ignore[arg-type]
                frame["image_scope"],
                key,
            ),
        )
        row_id = cursor.lastrowid
    return int(row_id)


def set_text(ctx, frame_id: int, text: str | None, source: str | None) -> None:  # noqa: ANN001
    with transaction(ctx.conn) as conn:
        conn.execute(
            "UPDATE frames SET text = ?, text_source = ?, text_key = ? WHERE id = ?",
            (text, source, text_key(ctx, text), frame_id),
        )


# --- embedding --------------------------------------------------------------------------------

_MISSING_KEYS: Final = (
    "SELECT f.text_key FROM frames f WHERE f.ts_ms >= :since AND f.text_key IS NOT NULL"
    " AND length(f.text) >= :min"
    " AND NOT EXISTS (SELECT 1 FROM text_passages p"
    "                 WHERE p.text_key = f.text_key AND p.model = :model)"
    " GROUP BY f.text_key ORDER BY min(f.ts_ms), min(f.id) LIMIT :limit"
)


def _floor(conn) -> int:  # noqa: ANN001
    row = conn.execute("SELECT cutoff_ms FROM vector_floor WHERE id = 1").fetchone()
    return int(row[0]) if row else -(2**62)


def _publish(conn, key: bytes, model: str, text: str, vectors) -> bool:  # noqa: ANN001
    rows = [
        (key, model, start, end, len(v), np.ascontiguousarray(v, dtype=np.float16).tobytes())
        for (start, end), v in zip(windows(text), vectors, strict=True)
    ]
    with transaction(conn):
        current = conn.execute(
            "SELECT text FROM frames WHERE text_key = ? LIMIT 1", (key,)
        ).fetchone()
        if current is None or current[0] != text:
            return False
        conn.execute("DELETE FROM text_passages WHERE text_key = ? AND model = ?", (key, model))
        conn.executemany("INSERT INTO text_passages VALUES (?, ?, ?, ?, ?, ?)", rows)
    return True


def _embed_keys(conn, embedder, keys: list[bytes]) -> None:  # noqa: ANN001
    texts: dict[bytes, str] = {}
    for key in keys:  # any frame with the key is a valid representative (guarded invariant)
        row = conn.execute(
            "SELECT text FROM frames WHERE text_key = ? AND text IS NOT NULL LIMIT 1", (key,)
        ).fetchone()
        if row is not None:
            texts[key] = str(row[0])
    todo = [k for k in keys if k in texts]
    if not todo:
        return
    offsets = {k: windows(texts[k]) for k in todo}
    vectors = embedder.embed_documents([texts[k][s:e] for k in todo for s, e in offsets[k]])
    at = 0
    for key in todo:
        end = at + len(offsets[key])
        _publish(conn, key, embedder.model, texts[key], vectors[at:end])
        at = end


def embed_pending(ctx, embedder, *, since_ms: int = 0) -> None:  # noqa: ANN001
    seen: set[tuple[bytes, ...]] = set()
    while True:
        since = max(since_ms, _floor(ctx.conn))
        rows = ctx.conn.execute(
            _MISSING_KEYS,
            {"model": embedder.model, "since": since, "limit": BATCH, "min": MIN_TEXT_CHARS},
        ).fetchall()
        if not rows:
            return
        keys = tuple(bytes(r[0]) for r in rows)
        if keys in seen:
            return
        seen.add(keys)
        _embed_keys(ctx.conn, embedder, list(keys))


# --- query ------------------------------------------------------------------------------------

Group = tuple[bytes | None, str | None, str | None]


def _order(hit: tuple[bytes, float, int, int]) -> tuple[float, bytes]:
    return -hit[1], hit[0]


def _top_keys(conn, model: str, query: np.ndarray, lo: int, hi: int, limit: int) -> list[tuple[bytes, float, int, int]]:  # noqa: ANN001
    cursor = conn.execute(
        "SELECT p.text_key, p.start_char, p.end_char, p.vec"
        " FROM (SELECT DISTINCT text_key FROM frames"
        "       WHERE ts_ms >= :lo AND ts_ms < :hi AND text_key IS NOT NULL) k"
        " JOIN text_passages p ON p.text_key = k.text_key AND p.model = :model",
        {"model": model, "lo": lo, "hi": hi},
    )
    best: dict[bytes, tuple[bytes, float, int, int]] = {}
    while rows := cursor.fetchmany(CHUNK_ROWS):
        rows = [r for r in rows if len(r[3]) == len(query) * 2]
        if not rows:
            continue
        matrix = np.frombuffer(b"".join(bytes(r[3]) for r in rows), dtype=np.float16).reshape(
            len(rows), -1
        ).astype(np.float32)
        for r, score in zip(rows, matrix @ query, strict=True):
            key = bytes(r[0])
            hit = (key, float(score), int(r[1]), int(r[2]))
            if key not in best or hit[1] > best[key][1]:
                best[key] = hit
        if len(best) > 4 * limit:
            best = {h[0]: h for h in sorted(best.values(), key=_order)[:limit]}
    return sorted(best.values(), key=_order)[:limit]


def query_frames(ctx, q_text: str, start_ms: int, end_ms: int) -> list[tuple[int, int, dict]]:  # noqa: ANN001
    conn = ctx.conn
    qvec = ctx.query_vector(q_text)
    # group -> (rep_id, rep_ts, count, first_ms, last_ms)
    lex: dict[Group, tuple[int, int, int, int, int]] = {}
    match = build_match(q_text)
    if match:
        rows = conn.execute(
            "WITH m AS MATERIALIZED (SELECT f.id, f.ts_ms, f.text_key, f.window_title, f.url,"
            " bm25(frames_fts, 1.0, 3.0, 2.0) AS s"
            " FROM frames_fts JOIN frames f ON f.id = frames_fts.rowid"
            " WHERE frames_fts MATCH :match AND f.ts_ms >= :start AND f.ts_ms < :end)"
            # one document per group: equal bm25, so the representative is the latest frame
            " SELECT text_key, window_title, url, 0, max(ts_ms), count(*), min(ts_ms),"
            " total(s) / count(*) AS score FROM m GROUP BY text_key, window_title, url"
            " ORDER BY score, max(ts_ms) DESC LIMIT :depth",
            {"match": match, "start": start_ms, "end": end_ms, "depth": FTS_DEPTH},
        ).fetchall()
        for r in rows:
            group = (None if r[0] is None else bytes(r[0]), r[1], r[2])
            lex[group] = (int(r[3]), int(r[4]), int(r[5]), int(r[6]), int(r[4]))
    sem: dict[Group, tuple[int, int, int, int, int]] = {}
    passage: dict[bytes, tuple[int, int]] = {}
    sem_order: list[Group] = []
    if qvec is not None:
        lo = max(start_ms, _floor(conn))
        hits = _top_keys(conn, ctx.model, qvec, lo, end_ms, VECTOR_DEPTH)
        if hits:
            keys = [h[0] for h in hits]
            passage = {h[0]: (h[2], h[3]) for h in hits}
            marks = ",".join("?" * len(keys))
            expanded: dict[bytes, list[tuple[int, Group, tuple[int, int, int, int, int]]]] = {}
            for r in conn.execute(
                "SELECT text_key, window_title, url, 0, max(ts_ms), count(*), min(ts_ms)"  # noqa: S608
                f" FROM frames WHERE text_key IN ({marks}) AND ts_ms >= ? AND ts_ms < ?"
                " GROUP BY text_key, window_title, url",
                (*keys, lo, end_ms),
            ):
                group = (bytes(r[0]), r[1], r[2])
                info = (int(r[3]), int(r[4]), int(r[5]), int(r[6]), int(r[4]))
                expanded.setdefault(group[0], []).append((int(r[4]), group, info))
            for key in keys:
                for _, group, info in sorted(expanded.get(key, []), key=lambda e: -e[0]):
                    sem[group] = info
                    sem_order.append(group)
    fused = fuse(list(lex), sem_order)
    order = sorted(fused, key=lambda g: -fused[g])[:FRAME_SLOTS]
    if not order:
        return []
    reps = {g: _with_id(conn, g, lex.get(g) or sem[g]) for g in order}
    ids = [reps[g][0] for g in order]
    marks = ",".join("?" * len(ids))
    texts = {
        int(r[0]): str(r[1] or "")
        for r in conn.execute(f"SELECT id, text FROM frames WHERE id IN ({marks})", ids)  # noqa: S608
    }
    words: dict[int, str] = {}
    need = [reps[g][0] for g in order if g[0] is None or g[0] not in passage]
    if need and match:
        words = {
            int(r[0]): str(r[1] or "")
            for r in conn.execute(
                "SELECT rowid, snippet(frames_fts, -1, ?, ?, '…', 24) FROM frames_fts"  # noqa: S608
                f" WHERE frames_fts MATCH ? AND rowid IN ({','.join('?' * len(need))})",
                (HIGHLIGHT_START, HIGHLIGHT_END, match, *need),
            )
        }
    out = []
    for g in order:
        rep_id, rep_ts, count, first, last = reps[g]
        if g[0] is not None and g[0] in passage:
            s, e = passage[g[0]]
            snippet = texts.get(rep_id, "")[s:e]
        else:
            snippet = words.get(rep_id, "")
        out.append((rep_id, rep_ts, {"snippet": snippet, "also_seen": count, "first_ms": first, "last_ms": last}))
    return out


def _with_id(conn, group: Group, info: tuple[int, int, int, int, int]) -> tuple[int, int, int, int, int]:  # noqa: ANN001
    """Resolve the representative frame (latest of the group in the window) to its id."""
    key, title, url = group
    row = conn.execute(
        "SELECT id FROM frames WHERE ts_ms = ? AND text_key IS ? AND window_title IS ? AND url IS ?"
        " ORDER BY id DESC LIMIT 1",
        (info[1], key, title, url),
    ).fetchone()
    return (int(row[0]), *info[1:])


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
                "UPDATE frames SET text = NULL, text_source = NULL, text_key = NULL WHERE id IN"
                " (SELECT id FROM frames WHERE ts_ms < ? AND text IS NOT NULL LIMIT ?)",
                (cutoff_ms, RETENTION_BATCH),
            ).rowcount
        total += changed
        if changed < RETENTION_BATCH:
            return total


def expire_embeddings(ctx, cutoff_ms: int) -> int:  # noqa: ANN001
    """Delete keys with no frame at or after the cutoff; record the floor for the vector side."""
    conn = ctx.conn
    total = 0
    first = True
    while True:
        with transaction(conn):
            if first:
                conn.execute(
                    "INSERT INTO vector_floor (id, cutoff_ms) VALUES (1, ?) ON CONFLICT (id)"
                    " DO UPDATE SET cutoff_ms = max(cutoff_ms, excluded.cutoff_ms)",
                    (cutoff_ms,),
                )
            keys = [
                bytes(r[0])
                for r in conn.execute(
                    "SELECT DISTINCT f.text_key FROM frames f"
                    " WHERE f.ts_ms < :cut AND f.text_key IS NOT NULL"
                    " AND NOT EXISTS (SELECT 1 FROM frames g"
                    "                 WHERE g.text_key = f.text_key AND g.ts_ms >= :cut)"
                    " AND EXISTS (SELECT 1 FROM text_passages p WHERE p.text_key = f.text_key)"
                    " LIMIT :n",
                    {"cut": cutoff_ms, "n": KEY_BATCH},
                )
            ]
            removed = 0
            if keys:
                removed = conn.execute(
                    f"DELETE FROM text_passages WHERE text_key IN ({','.join('?' * len(keys))})",  # noqa: S608
                    keys,
                ).rowcount
        first = False
        total += removed
        if len(keys) < KEY_BATCH:
            return total


def delete_all(ctx) -> dict[str, int]:  # noqa: ANN001
    with transaction(ctx.conn) as conn:
        removed = {
            "screenshots": int(
                conn.execute("SELECT count(*) FROM frames WHERE image_path IS NOT NULL").fetchone()[0]
            )
        }
        conn.execute("DELETE FROM text_passages")
        removed["frames"] = conn.execute("DELETE FROM frames").rowcount
        conn.execute("DELETE FROM vector_floor")
    return removed
