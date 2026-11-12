"""C3: link table (hard-link / inode analogy), capture write path untouched.

Every frame whose text is embedded owns one link row (frame_id, model) -> key, where
key = HMAC-SHA256(ctx.hmac_key, text). Vectors live once per (key, model) in text_passages.
The last link of a key takes the key's vectors with it (AFTER DELETE trigger), so every
deletion path (frame delete cascade, text change/expiry, expire_embeddings, delete_all)
removes derived vectors in the same statement/transaction as today.

Query: FTS rows get their key from the link (or an HMAC computed here for unlinked rows);
vectors are scored once per key linked from frames in the window; RRF fuses rankings of
groups (key, window_title, url). Each result is a real frame of its group (best-bm25 frame,
else the latest), with its own (frame_id, ts_ms); the other frames of the group in the
window are returned in ``extra["also_seen"]``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
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

VERSION: Final = 19
ALSO_SEEN_CAP: Final = 50

# Migration 0019: shared passages + per-frame links. Statements run inside one transaction.
DDL: Final = (
    """CREATE TABLE IF NOT EXISTS frame_text_links (
        frame_id INTEGER NOT NULL REFERENCES frames(id) ON DELETE CASCADE,
        model TEXT NOT NULL,
        key BLOB NOT NULL CHECK (length(key) = 32),
        PRIMARY KEY (frame_id, model)
    )""",
    "CREATE INDEX IF NOT EXISTS frame_text_links_key ON frame_text_links(key, model)",
    """CREATE TABLE IF NOT EXISTS text_passages (
        key BLOB NOT NULL,
        model TEXT NOT NULL,
        start_char INTEGER NOT NULL CHECK (start_char >= 0),
        end_char INTEGER NOT NULL CHECK (end_char > start_char),
        dim INTEGER NOT NULL CHECK (dim > 0),
        vec BLOB NOT NULL CHECK (length(vec) = dim * 2),
        PRIMARY KEY (key, model, start_char)
    )""",
    # The last link of (key, model) takes the vectors with it. Fires for cascades from frames
    # (foreign_keys = ON) and from the frames update trigger below (nested, not recursive).
    """CREATE TRIGGER IF NOT EXISTS frame_text_links_ad AFTER DELETE ON frame_text_links
    WHEN NOT EXISTS (SELECT 1 FROM frame_text_links WHERE key = old.key AND model = old.model)
    BEGIN
        DELETE FROM text_passages WHERE key = old.key AND model = old.model;
    END""",
    """CREATE TRIGGER IF NOT EXISTS frames_text_links_au AFTER UPDATE OF text ON frames
    WHEN old.text IS NOT new.text BEGIN
        DELETE FROM frame_text_links WHERE frame_id = new.id;
    END""",
)
DROP_TODAY: Final = (
    "DROP TRIGGER IF EXISTS frames_passages_au",
    "DROP TABLE IF EXISTS frame_passages",
)


def text_key(ctx, text: str) -> bytes:  # noqa: ANN001
    return hmac.new(ctx.hmac_key, text.encode("utf-8"), hashlib.sha256).digest()


# --- schema -----------------------------------------------------------------------------------


def migrate(ctx) -> None:  # noqa: ANN001
    ref_today.migrate(ctx)  # 0001..0018
    with transaction(ctx.conn) as conn:
        for stmt in DDL + DROP_TODAY:
            conn.execute(stmt)
        conn.execute(f"PRAGMA user_version = {VERSION}")


def upgrade_from_today(ctx) -> dict[str, int]:  # noqa: ANN001
    """Populated v18 database -> v19 in one transaction: hash only frames that already have
    frame_passages, copy one passage set per (key, model), then drop frame_passages."""
    conn = ctx.conn
    links = copied = 0
    with transaction(conn):
        for stmt in DDL:
            conn.execute(stmt)
        cursor = conn.execute(
            "SELECT f.id, f.text, m.model FROM frames f"
            " JOIN (SELECT DISTINCT frame_id, model FROM frame_passages) m ON m.frame_id = f.id"
            " WHERE f.text IS NOT NULL"
        )
        done: set[tuple[bytes, str]] = set()
        for frame_id, text, model in cursor:
            key = text_key(ctx, str(text))
            if (key, model) not in done:
                conn.execute(
                    "INSERT OR IGNORE INTO text_passages"
                    " SELECT ?, model, start_char, end_char, dim, vec FROM frame_passages"
                    " WHERE frame_id = ? AND model = ?",
                    (key, frame_id, model),
                )
                done.add((key, model))
                copied += 1
            conn.execute(
                "INSERT OR IGNORE INTO frame_text_links VALUES (?, ?, ?)", (frame_id, model, key)
            )
            links += 1
        for stmt in DROP_TODAY:
            conn.execute(stmt)
        conn.execute(f"PRAGMA user_version = {VERSION}")
    return {"links": links, "keys": copied}


# --- capture write path: unchanged ------------------------------------------------------------

insert_frame = ref_today.insert_frame
set_text = ref_today.set_text


# --- embed worker -----------------------------------------------------------------------------

_MISSING: Final = (
    "SELECT f.id FROM frames f WHERE f.ts_ms >= :since AND length(f.text) >= :min"
    " AND NOT EXISTS (SELECT 1 FROM frame_text_links l"
    "                 WHERE l.frame_id = f.id AND l.model = :model)"
    " ORDER BY f.ts_ms, f.id LIMIT :limit"
)
_HAS_KEY: Final = "SELECT 1 FROM text_passages WHERE key = ? AND model = ? AND start_char = 0"


def _embed_frames(ctx, embedder, ids: list[int]) -> None:  # noqa: ANN001
    conn, model = ctx.conn, embedder.model
    marks = ",".join("?" * len(ids))
    sql = f"SELECT id, text FROM frames WHERE id IN ({marks}) AND text IS NOT NULL"  # noqa: S608
    texts = {int(r[0]): str(r[1]) for r in conn.execute(sql, ids)}
    if not texts:
        return
    keys = {i: text_key(ctx, t) for i, t in texts.items()}
    first: dict[bytes, str] = {}
    for i, t in texts.items():
        first.setdefault(keys[i], t)
    need = [k for k in first if conn.execute(_HAS_KEY, (k, model)).fetchone() is None]
    offsets = {k: windows(first[k]) for k in need}
    flat = [first[k][s:e] for k in need for s, e in offsets[k]]
    vectors = embedder.embed_documents(flat) if flat else []
    rows: dict[bytes, list[tuple]] = {}
    at = 0
    for k in need:
        end = at + len(offsets[k])
        rows[k] = [
            (k, model, s, e, len(v), np.ascontiguousarray(v, dtype=np.float16).tobytes())
            for (s, e), v in zip(offsets[k], vectors[at:end], strict=True)
        ]
        at = end
    with transaction(conn):
        current = {int(r[0]): r[1] for r in conn.execute(sql, ids)}
        live = [i for i in texts if current.get(i) == texts[i]]  # today's text-unchanged check
        publish = {keys[i] for i in live}
        for k in publish:
            if k in rows:
                conn.executemany("INSERT OR IGNORE INTO text_passages VALUES (?, ?, ?, ?, ?, ?)", rows[k])
            elif conn.execute(_HAS_KEY, (k, model)).fetchone() is None:
                publish.discard(k)  # vectors left since the check: the next pass embeds it
        conn.executemany(
            "INSERT OR IGNORE INTO frame_text_links VALUES (?, ?, ?)",
            [(i, model, keys[i]) for i in live if keys[i] in publish],
        )


def embed_pending(ctx, embedder, *, since_ms: int = 0) -> None:  # noqa: ANN001
    seen: set[tuple[int, ...]] = set()
    while True:
        rows = ctx.conn.execute(
            _MISSING,
            {"model": embedder.model, "since": since_ms, "limit": BATCH, "min": MIN_TEXT_CHARS},
        ).fetchall()
        if not rows:
            return
        ids = tuple(int(r[0]) for r in rows)
        if ids in seen:
            return
        seen.add(ids)
        _embed_frames(ctx, embedder, list(ids))


# --- query ------------------------------------------------------------------------------------


def _top_keys(conn, model: str, query: np.ndarray, start_ms: int, end_ms: int) -> dict[bytes, tuple[float, int, int]]:  # noqa: ANN001
    """Max-passage score per key linked from a frame in the window (one scan per key)."""
    cursor = conn.execute(
        "SELECT p.key, p.start_char, p.end_char, p.vec FROM text_passages p"
        " WHERE p.model = :model AND p.key IN ("
        "   SELECT l.key FROM frames f JOIN frame_text_links l ON l.frame_id = f.id"
        "   WHERE l.model = :model AND f.ts_ms >= :start AND f.ts_ms < :end)",
        {"model": model, "start": start_ms, "end": end_ms},
    )
    best: dict[bytes, tuple[float, int, int]] = {}
    while rows := cursor.fetchmany(CHUNK_ROWS):
        rows = [r for r in rows if len(r[3]) == len(query) * 2]
        if not rows:
            continue
        matrix = np.vstack([np.frombuffer(bytes(r[3]), dtype=np.float16) for r in rows]).astype(np.float32)
        for r, score in zip(rows, matrix @ query, strict=True):
            key = bytes(r[0])
            if key not in best or score > best[key][0]:
                best[key] = (float(score), int(r[1]), int(r[2]))
    ranked = sorted(best.items(), key=lambda kv: (-kv[1][0], kv[0]))[:VECTOR_DEPTH]
    return dict(ranked)


def query_frames(ctx, q_text: str, start_ms: int, end_ms: int) -> list[tuple[int, int, dict]]:  # noqa: ANN001
    conn, model = ctx.conn, ctx.model
    qvec = ctx.query_vector(q_text)
    window = {"start": start_ms, "end": end_ms, "model": model}

    # group id -> frames (id -> ts) seen for it; best FTS frame and snippet per group
    members: dict[tuple, dict[int, int]] = {}
    best_fts: dict[tuple, tuple[int, str]] = {}
    words: list[tuple] = []
    match = build_match(q_text)
    if match:
        rows = conn.execute(
            "SELECT f.id, f.ts_ms, f.window_title, f.url, l.key,"
            " CASE WHEN l.key IS NULL THEN f.text END,"
            " snippet(frames_fts, -1, :open, :close, '…', 24)"
            " FROM frames_fts JOIN frames f ON f.id = frames_fts.rowid"
            " LEFT JOIN frame_text_links l ON l.frame_id = f.id AND l.model = :model"
            " WHERE frames_fts MATCH :match AND f.ts_ms >= :start AND f.ts_ms < :end"
            " ORDER BY bm25(frames_fts, 1.0, 3.0, 2.0), f.ts_ms DESC, f.id DESC LIMIT :depth",
            {**window, "match": match, "depth": FTS_DEPTH, "open": HIGHLIGHT_START, "close": HIGHLIGHT_END},
        ).fetchall()
        for fid, ts, title, url, key, text, snip in rows:
            if key is not None:
                k: object = bytes(key)
            elif text is not None:  # not embedded yet / past vector retention / short text
                k = text_key(ctx, str(text))
            else:  # expired text, matched by title/url: never merged with other frames
                k = ("frame", int(fid))
            gid = (k, title, url)
            members.setdefault(gid, {})[int(fid)] = int(ts)
            if gid not in best_fts:
                best_fts[gid] = (int(fid), str(snip or ""))
                words.append(gid)

    meaning: list[tuple] = []
    passages: dict[bytes, tuple[float, int, int]] = {}
    if qvec is not None:
        passages = _top_keys(conn, model, qvec, start_ms, end_ms)
    linked: dict[bytes, list[tuple[int, int, object, object]]] = {}
    if passages or words:
        for key, fid, ts, title, url in conn.execute(
            "SELECT l.key, f.id, f.ts_ms, f.window_title, f.url FROM frames f"
            " JOIN frame_text_links l ON l.frame_id = f.id"
            " WHERE l.model = :model AND f.ts_ms >= :start AND f.ts_ms < :end",
            window,
        ):
            linked.setdefault(bytes(key), []).append((int(fid), int(ts), title, url))
    for key in passages:
        frames = sorted(linked.get(key, ()), key=lambda r: (-r[1], -r[0]))
        for _, _, title, url in frames:
            gid = (key, title, url)
            if gid not in meaning:
                meaning.append(gid)
    meaning = meaning[:VECTOR_DEPTH]

    fused = fuse(words, meaning)
    order = sorted(fused, key=lambda g: -fused[g])[:FRAME_SLOTS]
    if not order:
        return []

    picks: list[tuple[tuple, int, int, list[tuple[int, int]]]] = []
    for gid in order:
        k, title, url = gid
        seen = dict(members.get(gid, {}))
        if isinstance(k, bytes):
            for fid, ts, t, u in linked.get(k, ()):
                if t == title and u == url:
                    seen[fid] = ts
        if gid in best_fts:
            rep = best_fts[gid][0]
        else:
            rep = max(seen, key=lambda i: (seen[i], i))
        also = sorted(((i, ts) for i, ts in seen.items() if i != rep), key=lambda x: x[1])
        picks.append((gid, rep, seen[rep], also))

    texts = dict(
        conn.execute(
            "SELECT id, coalesce(text, '') FROM frames WHERE id IN (SELECT value FROM json_each(?))",
            (json.dumps([p[1] for p in picks]),),
        ).fetchall()
    )
    out: list[tuple[int, int, dict]] = []
    for gid, rep, ts, also in picks:
        if rep not in texts:
            continue
        hit = passages.get(gid[0]) if isinstance(gid[0], bytes) else None
        snippet = str(texts[rep])[hit[1] : hit[2]] if hit is not None else best_fts[gid][1]
        out.append(
            (rep, ts, {"snippet": snippet, "also_seen": also[:ALSO_SEEN_CAP], "also_seen_count": len(also)})
        )
    return out


# --- deletion paths ---------------------------------------------------------------------------

delete_frame = ref_today.delete_frame  # cascade -> links -> last-link trigger -> vectors
expire_frame_text = ref_today.expire_frame_text  # text -> NULL -> links -> vectors


def expire_embeddings(ctx, cutoff_ms: int) -> int:  # noqa: ANN001
    """Today's shape: drop the links of frames older than the cutoff; a key's vectors go with
    its last link, so per-frame vector reach is unchanged."""
    conn = ctx.conn
    total = 0
    while True:
        with transaction(conn):
            changed = conn.execute(
                "DELETE FROM frame_text_links WHERE rowid IN"
                " (SELECT l.rowid FROM frame_text_links l JOIN frames f"
                " ON f.id = l.frame_id WHERE f.ts_ms < ? LIMIT ?)",
                (cutoff_ms, RETENTION_BATCH),
            ).rowcount
        total += changed
        if changed < RETENTION_BATCH:
            return total


def delete_all(ctx) -> dict[str, int]:  # noqa: ANN001
    with transaction(ctx.conn) as conn:
        removed = {
            "screenshots": int(
                conn.execute("SELECT count(*) FROM frames WHERE image_path IS NOT NULL").fetchone()[0]
            )
        }
        removed["frames"] = conn.execute("DELETE FROM frames").rowcount  # links cascade, vectors follow
        removed["text_passages"] = conn.execute("DELETE FROM text_passages").rowcount  # 0 if consistent
    return removed
