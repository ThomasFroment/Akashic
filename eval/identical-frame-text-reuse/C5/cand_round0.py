"""C5: shared passages per keyed text + per-sighting collapse of identical frames.

Storage (write-path tag, shared passages):
- frames.text_key = HMAC-SHA256(ctx.hmac_key, text utf-8)[:16], written with the text by
  insert_frame / set_text, nulled with it by expire_frame_text. Guard triggers abort any write
  where text and text_key disagree (NULL-ness, or text changed while the key did not).
- text_passages(text_key, model, start_char, end_char, dim, vec), PK(text_key, model, start_char),
  WITHOUT ROWID: one passage set per distinct text, whatever the number of frames showing it.
- frame_vec_horizon: the vector-retention cutoff (and how far expire_embeddings finished).
  Frames older than the cutoff are never reached through vectors, even when their key is shared
  with a newer frame. Absent row = no cutoff (delete_all leaves no row).
- AFTER DELETE / AFTER UPDATE OF text_key triggers delete a key's passages when no frame at or
  after the cutoff still carries it: same transaction as the frame change.
- Indexes: frames(text_key, ts_ms) for key -> frames expansion; frames_ts widened to
  (ts_ms, text_key) so window -> keys is index-only (no overflow-page walk past a large text).

Query: FTS per frame (as today) and vector per key, both collapsed to groups
(text_key, window_title, url), fused by RRF on groups. Slots take sightings: for each top group,
its frames in the window (ordered index range scan) are split into runs at gaps > G = 15 min;
at most R = 2 runs per group; runs inherit the group's score and, within a group, are ordered by
best FTS frame then recency. Representative = the run's best-bm25 frame, else its latest.
Frames without text (expired) stay one group per frame (title/url FTS hits only).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Final

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

if TYPE_CHECKING:
    from sqlite3 import Connection

KEY_BYTES: Final = 16
GAP_MS: Final = 15 * 60_000  # G
RUNS_PER_GROUP: Final = 2  # R
NO_CUTOFF: Final = -(2**63)
VERSION: Final = 19
_HORIZON: Final = f"coalesce((SELECT cutoff_ms FROM frame_vec_horizon WHERE id = 1), {NO_CUTOFF})"

_SCHEMA_PRE: Final = """
ALTER TABLE frames ADD COLUMN text_key BLOB;
CREATE TABLE text_passages (
    text_key BLOB NOT NULL,
    model TEXT NOT NULL,
    start_char INTEGER NOT NULL CHECK (start_char >= 0),
    end_char INTEGER NOT NULL CHECK (end_char > start_char),
    dim INTEGER NOT NULL CHECK (dim > 0),
    vec BLOB NOT NULL CHECK (length(vec) = dim * 2),
    PRIMARY KEY (text_key, model, start_char)
) WITHOUT ROWID;
CREATE TABLE frame_vec_horizon (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    cutoff_ms INTEGER NOT NULL,
    done_ms INTEGER NOT NULL
);
"""

_SCHEMA_POST: Final = f"""
CREATE INDEX frames_key_ts ON frames (text_key, ts_ms);
DROP INDEX frames_ts;
CREATE INDEX frames_ts ON frames (ts_ms, text_key);
INSERT OR IGNORE INTO text_passages
    SELECT f.text_key, p.model, p.start_char, p.end_char, p.dim, p.vec
    FROM frame_passages p JOIN frames f ON f.id = p.frame_id WHERE f.text_key IS NOT NULL;
DROP TRIGGER frames_passages_au;
DROP TABLE frame_passages;
CREATE TRIGGER frames_tp_ad AFTER DELETE ON frames
WHEN old.text_key IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM frames WHERE text_key = old.text_key AND ts_ms >= {_HORIZON})
BEGIN
    DELETE FROM text_passages WHERE text_key = old.text_key;
END;
CREATE TRIGGER frames_tp_au AFTER UPDATE OF text_key ON frames
WHEN old.text_key IS NOT NULL AND old.text_key IS NOT new.text_key AND NOT EXISTS (
    SELECT 1 FROM frames WHERE text_key = old.text_key AND ts_ms >= {_HORIZON})
BEGIN
    DELETE FROM text_passages WHERE text_key = old.text_key;
END;
CREATE TRIGGER frames_key_guard_ai BEFORE INSERT ON frames
WHEN (new.text IS NULL) <> (new.text_key IS NULL)
BEGIN
    SELECT RAISE(ABORT, 'frames.text_key out of sync with text');
END;
CREATE TRIGGER frames_key_guard_au BEFORE UPDATE OF text, text_key ON frames
WHEN (new.text IS NULL) <> (new.text_key IS NULL)
  OR (new.text IS NOT old.text AND new.text_key IS old.text_key)
  OR (new.text IS old.text AND new.text_key IS NOT old.text_key)
BEGIN
    SELECT RAISE(ABORT, 'frames.text_key out of sync with text');
END;
"""


def text_key(ctx, text: str | None) -> bytes | None:  # noqa: ANN001
    if text is None:
        return None
    return hmac.new(ctx.hmac_key, text.encode("utf-8"), hashlib.sha256).digest()[:KEY_BYTES]


# --- schema -----------------------------------------------------------------------------------


def upgrade_from_today(ctx, vec_cutoff_ms: int | None = None) -> None:  # noqa: ANN001
    """Today's schema (v18, populated or empty) -> v19, atomically. Keys are backfilled before
    the guard triggers exist; one passage set per key is kept from frame_passages."""
    conn = ctx.conn
    conn.execute("BEGIN IMMEDIATE")
    try:
        for stmt in _split(_SCHEMA_PRE):
            conn.execute(stmt)
        last = -1
        while True:
            rows = conn.execute(
                "SELECT id, text FROM frames WHERE id > ? AND text IS NOT NULL ORDER BY id LIMIT 1000",
                (last,),
            ).fetchall()
            if not rows:
                break
            conn.executemany(
                "UPDATE frames SET text_key = ? WHERE id = ?",
                [(text_key(ctx, str(t)), int(i)) for i, t in rows],
            )
            last = int(rows[-1][0])
        for stmt in _split(_SCHEMA_POST):
            conn.execute(stmt)
        if vec_cutoff_ms is not None:  # the app passes its vector-retention cutoff here
            conn.execute(
                "INSERT INTO frame_vec_horizon VALUES (1, ?, ?)", (vec_cutoff_ms, NO_CUTOFF)
            )
        conn.execute(f"PRAGMA user_version = {VERSION}")
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def _split(script: str) -> list[str]:
    """Split on ';' at statement ends, keeping trigger bodies whole."""
    out, buf = [], []
    for line in script.strip().splitlines():
        buf.append(line)
        joined = "\n".join(buf).strip()
        if joined.endswith(";") and (
            not joined.upper().startswith("CREATE TRIGGER") or joined.upper().endswith("END;")
        ):
            out.append(joined)
            buf = []
    return [s for s in out if s.strip(";").strip()]


def migrate(ctx) -> None:  # noqa: ANN001
    ref_today.migrate(ctx)
    upgrade_from_today(ctx)


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


def set_text(ctx, frame_id: int, text: str, source: str) -> None:  # noqa: ANN001
    key = text_key(ctx, text)
    with transaction(ctx.conn) as conn:
        conn.execute(
            "UPDATE frames SET text = ?, text_source = ?, text_key = ? WHERE id = ?",
            (text, source, key, frame_id),
        )


# --- horizon ----------------------------------------------------------------------------------


def _horizon(conn: Connection) -> tuple[int, int]:
    row = conn.execute("SELECT cutoff_ms, done_ms FROM frame_vec_horizon WHERE id = 1").fetchone()
    return (NO_CUTOFF, NO_CUTOFF) if row is None else (int(row[0]), int(row[1]))


# --- embedding --------------------------------------------------------------------------------

_MISSING_KEYS: Final = (
    "SELECT f.text_key, max(f.id) FROM frames f"
    " WHERE f.ts_ms >= :since AND f.text_key IS NOT NULL"
    " AND NOT EXISTS (SELECT 1 FROM text_passages p WHERE p.text_key = f.text_key AND p.model = :model)"
    " AND length(f.text) >= :min"
    " GROUP BY f.text_key ORDER BY min(f.ts_ms) LIMIT :limit"
)


def _publish(conn: Connection, key: bytes, model: str, offsets, vectors) -> bool:  # noqa: ANN001
    rows = [
        (key, model, s, e, len(v), np.ascontiguousarray(v, dtype=np.float16).tobytes())
        for (s, e), v in zip(offsets, vectors, strict=True)
    ]
    with transaction(conn):
        cutoff = _horizon(conn)[0]
        alive = conn.execute(
            "SELECT 1 FROM frames WHERE text_key = ? AND ts_ms >= ? LIMIT 1", (key, cutoff)
        ).fetchone()
        if alive is None:  # every frame with this text left (or expired) during inference
            return False
        conn.execute("DELETE FROM text_passages WHERE text_key = ? AND model = ?", (key, model))
        conn.executemany("INSERT INTO text_passages VALUES (?, ?, ?, ?, ?, ?)", rows)
    return True


def embed_pending(ctx, embedder, *, since_ms: int = 0) -> None:  # noqa: ANN001
    """One representative frame per missing key; each distinct text is embedded once."""
    conn = ctx.conn
    seen: set[tuple[bytes, ...]] = set()
    while True:
        since = max(since_ms, _horizon(conn)[0])
        rows = conn.execute(
            _MISSING_KEYS,
            {"model": embedder.model, "since": since, "limit": BATCH, "min": MIN_TEXT_CHARS},
        ).fetchall()
        if not rows:
            return
        keys = tuple(bytes(r[0]) for r in rows)
        if keys in seen:
            return
        seen.add(keys)
        reps = {bytes(r[0]): int(r[1]) for r in rows}
        marks = ",".join("?" * len(reps))
        texts = {
            int(r[0]): str(r[1])
            for r in conn.execute(
                f"SELECT id, text FROM frames WHERE id IN ({marks}) AND text IS NOT NULL",  # noqa: S608
                list(reps.values()),
            )
        }
        todo = [(k, texts[i]) for k, i in reps.items() if i in texts and text_key(ctx, texts[i]) == k]
        if not todo:
            continue
        offsets = [windows(t) for _, t in todo]
        vectors = embedder.embed_documents(
            [t[s:e] for (_, t), offs in zip(todo, offsets, strict=True) for s, e in offs]
        )
        at = 0
        for (key, _), offs in zip(todo, offsets, strict=True):
            _publish(conn, key, embedder.model, offs, vectors[at : at + len(offs)])
            at += len(offs)


# --- query ------------------------------------------------------------------------------------


def _top_keys(conn: Connection, model: str, query: np.ndarray, lo: int, hi: int, limit: int):  # noqa: ANN202
    """Max-passage score per key over keys with a frame in [lo, hi). -> [(key, score, s, e)]."""
    cursor = conn.execute(
        "SELECT p.text_key, p.start_char, p.end_char, p.vec FROM text_passages p"
        " WHERE p.model = :model AND p.text_key IN"
        " (SELECT text_key FROM frames WHERE ts_ms >= :lo AND ts_ms < :hi AND text_key IS NOT NULL)",
        {"model": model, "lo": lo, "hi": hi},
    )
    best: dict[bytes, tuple[bytes, float, int, int]] = {}
    want = len(query) * 2
    while rows := cursor.fetchmany(CHUNK_ROWS):
        rows = [r for r in rows if len(r[3]) == want]
        if not rows:
            continue
        matrix = np.frombuffer(b"".join(bytes(r[3]) for r in rows), dtype=np.float16).reshape(
            len(rows), -1
        ).astype(np.float32)
        for r, score in zip(rows, matrix @ query, strict=True):
            key = bytes(r[0])
            s = float(score)
            if key not in best or s > best[key][1]:
                best[key] = (key, s, int(r[1]), int(r[2]))
    return sorted(best.values(), key=lambda h: -h[1])[:limit]


_RUNS: Final = (
    "WITH g AS (SELECT ts_ms FROM frames WHERE text_key = :k AND ts_ms >= :lo AND ts_ms < :hi"
    "           AND window_title IS :t AND url IS :u),"
    " x AS (SELECT ts_ms, CASE WHEN ts_ms - lag(ts_ms) OVER (ORDER BY ts_ms) > :gap"
    "        THEN 1 ELSE 0 END AS brk FROM g),"
    " y AS (SELECT ts_ms, sum(brk) OVER (ORDER BY ts_ms ROWS UNBOUNDED PRECEDING) AS run FROM x)"
    " SELECT min(ts_ms), max(ts_ms), count(*) FROM y GROUP BY run"
)


def runs_of(conn: Connection, key: bytes, title, url, lo: int, hi: int) -> list[tuple[int, int, int]]:  # noqa: ANN001
    """Sightings of one group in [lo, hi): (first_ts, last_ts, count), ts order."""
    rows = conn.execute(
        _RUNS, {"k": key, "lo": lo, "hi": hi, "t": title, "u": url, "gap": GAP_MS}
    ).fetchall()
    return sorted((int(a), int(b), int(c)) for a, b, c in rows)


def query_frames(ctx, q_text: str, start_ms: int, end_ms: int) -> list[tuple[int, int, dict]]:  # noqa: ANN001, C901, PLR0912, PLR0915
    conn = ctx.conn
    qvec = ctx.query_vector(q_text)
    cutoff = _horizon(conn)[0]
    vlo = max(start_ms, cutoff)

    # groups: gid -> (key, title, url); FTS facts per group
    info: dict[tuple, tuple] = {}
    fts_hits: dict[tuple, list[tuple[int, int, int]]] = {}  # gid -> [(rank, id, ts)]
    fts_snip: dict[tuple, str] = {}
    latest: dict[tuple, int] = {}
    fts_order: list[tuple] = []
    match = build_match(q_text)
    if match:
        rows = conn.execute(
            "SELECT f.id, f.ts_ms, f.text_key, f.window_title, f.url,"
            " snippet(frames_fts, -1, :open, :close, '…', 24)"
            " FROM frames_fts JOIN frames f ON f.id = frames_fts.rowid"
            " WHERE frames_fts MATCH :match AND f.ts_ms >= :start AND f.ts_ms < :end"
            " ORDER BY bm25(frames_fts, 1.0, 3.0, 2.0), f.ts_ms DESC, f.id DESC LIMIT :depth",
            {"match": match, "start": start_ms, "end": end_ms, "depth": FTS_DEPTH,
             "open": HIGHLIGHT_START, "close": HIGHLIGHT_END},
        ).fetchall()
        for rank, (fid, ts, key, title, url, snip) in enumerate(rows):
            gid = (bytes(key), title, url) if key is not None else ("frame", int(fid))
            if gid not in info:
                info[gid] = (bytes(key) if key is not None else None, title, url, int(fid))
                fts_order.append(gid)
                fts_snip[gid] = str(snip or "")
            fts_hits.setdefault(gid, []).append((rank, int(fid), int(ts)))
            latest[gid] = max(latest.get(gid, ts), int(ts))

    passage: dict[bytes, tuple[int, int]] = {}
    vec_order: list[tuple] = []
    if qvec is not None and vlo < end_ms:
        hits = _top_keys(conn, ctx.model, qvec, vlo, end_ms, VECTOR_DEPTH)
        if hits:
            score = {h[0]: h[1] for h in hits}
            passage = {h[0]: (h[2], h[3]) for h in hits}
            marks = ",".join("?" * len(hits))
            grows = conn.execute(
                "SELECT text_key, window_title, url, max(ts_ms) FROM frames"
                f" WHERE text_key IN ({marks}) AND ts_ms >= ? AND ts_ms < ?"  # noqa: S608
                " GROUP BY text_key, window_title, url",
                [h[0] for h in hits] + [vlo, end_ms],
            ).fetchall()
            vg = []
            for key, title, url, last in grows:
                gid = (bytes(key), title, url)
                info.setdefault(gid, (bytes(key), title, url, None))
                latest[gid] = max(latest.get(gid, int(last)), int(last))
                vg.append((-score[bytes(key)], -int(last), gid))
            vec_order = [g for *_, g in sorted(vg, key=lambda v: (v[0], v[1]))][:VECTOR_DEPTH]

    fused = fuse(fts_order, vec_order)
    fts_rank = {g: hits[0][0] for g, hits in fts_hits.items()}
    order = sorted(fused, key=lambda g: (-fused[g], fts_rank.get(g, 1 << 30), -latest.get(g, 0)))

    slots: list[tuple[tuple, int, int, int, int, int]] = []  # gid, id, ts, first, last, n
    for gid in order:
        if len(slots) >= FRAME_SLOTS:
            break
        key, title, url, fid = info[gid]
        ghits = fts_hits.get(gid, [])
        if key is None:  # textless frame: its own group
            _, hid, hts = ghits[0]
            slots.append((gid, hid, hts, hts, hts, 1))
            continue
        lo = start_ms if ghits else vlo
        runs = runs_of(conn, key, title, url, lo, end_ms)
        ranked = []
        for first, last, n in runs:
            inside = [h for h in ghits if first <= h[2] <= last]
            best = min(inside) if inside else None
            ranked.append(((0, best[0]) if best else (1, -last), first, last, n, best))
        ranked.sort(key=lambda r: r[0])
        for _, first, last, n, best in ranked[:RUNS_PER_GROUP]:
            if len(slots) >= FRAME_SLOTS:
                break
            if best is not None:
                rid, rts = best[1], best[2]
            else:
                rid = int(conn.execute(
                    "SELECT id FROM frames WHERE text_key = ? AND ts_ms = ? AND window_title IS ?"
                    " AND url IS ? ORDER BY id DESC LIMIT 1",
                    (key, last, title, url),
                ).fetchone()[0])
                rts = last
            slots.append((gid, rid, rts, first, last, n))

    if not slots:
        return []
    need = [s[1] for s in slots if s[0][0] in passage]
    texts: dict[int, str] = {}
    if need:
        texts = {
            int(r[0]): str(r[1])
            for r in conn.execute(
                "SELECT id, coalesce(text, '') FROM frames WHERE id IN (SELECT value FROM json_each(?))",
                (json.dumps(need),),
            )
        }
    out = []
    for gid, rid, rts, first, last, n in slots:
        span = passage.get(gid[0]) if isinstance(gid[0], bytes) else None
        snippet = texts.get(rid, "")[span[0] : span[1]] if span else fts_snip.get(gid, "")
        out.append((rid, rts, {"snippet": snippet, "first_ts": first, "last_ts": last, "count": n}))
    return out


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
                " (SELECT id FROM frames WHERE ts_ms < ? AND text_key IS NOT NULL LIMIT ?)",
                (cutoff_ms, RETENTION_BATCH),
            ).rowcount
        total += changed
        if changed < RETENTION_BATCH:
            return total


def expire_embeddings(ctx, cutoff_ms: int) -> int:  # noqa: ANN001
    """Raise the vector horizon (old frames leave vector search at once), then delete, in
    batches of keys, the passages of keys with no frame at or after it. Scans only frames in
    [last completed cutoff, cutoff): the triggers keep that invariant between runs."""
    conn = ctx.conn
    total = 0
    while True:
        with transaction(conn):
            old_cut, done = _horizon(conn)
            cut = max(cutoff_ms, old_cut)
            conn.execute(
                "INSERT INTO frame_vec_horizon VALUES (1, ?, ?) ON CONFLICT(id) DO UPDATE"
                " SET cutoff_ms = excluded.cutoff_ms",
                (cut, done),
            )
            keys = [
                bytes(r[0])
                for r in conn.execute(
                    "SELECT DISTINCT f.text_key FROM frames f"
                    " WHERE f.ts_ms >= :done AND f.ts_ms < :cut AND f.text_key IS NOT NULL"
                    " AND NOT EXISTS (SELECT 1 FROM frames g WHERE g.text_key = f.text_key"
                    "                 AND g.ts_ms >= :cut)"
                    " AND EXISTS (SELECT 1 FROM text_passages p WHERE p.text_key = f.text_key)"
                    " LIMIT :n",
                    {"done": done, "cut": cut, "n": RETENTION_BATCH},
                )
            ]
            if keys:
                marks = ",".join("?" * len(keys))
                total += conn.execute(
                    f"DELETE FROM text_passages WHERE text_key IN ({marks})", keys  # noqa: S608
                ).rowcount
            if len(keys) < RETENTION_BATCH:
                conn.execute("UPDATE frame_vec_horizon SET done_ms = cutoff_ms WHERE id = 1")
                return total


def delete_all(ctx) -> dict[str, int]:  # noqa: ANN001
    with transaction(ctx.conn) as conn:
        removed = {
            "screenshots": int(
                conn.execute("SELECT count(*) FROM frames WHERE image_path IS NOT NULL").fetchone()[0]
            )
        }
        removed["text_passages"] = conn.execute("DELETE FROM text_passages").rowcount
        conn.execute("DELETE FROM frame_vec_horizon")
        removed["frames"] = conn.execute("DELETE FROM frames").rowcount
    return removed
