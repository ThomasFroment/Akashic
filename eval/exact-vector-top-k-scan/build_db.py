"""Disposable encrypted SQLCipher DBs for the exact vector top-k scans.

Run: python build_db.py [100k|1m|all] [--force]. DBs are cached under cache/ (vec_<size>.db and
vec_<size>.json) and reused by every candidate. Connection settings copy
src/akashic/storage/db.py (raw 32-byte key, WAL, synchronous NORMAL, foreign keys, temp_store
MEMORY). Schema: schema.sql (migrations 0001/0002/0003/0006/0009/0013/0015/0018, no FTS).
No ANALYZE is run (the app runs none either).

Per size N (100,000 or 1,000,000), seeded and deterministic:
- segment_passages and frame_passages: N main-model rows each, 256-dim float16 unit vectors
  around 64 topics; owners have 1-60 passages (60 % 1-5, 40 % 6-60), char offsets
  (300 * j, min(300 * j + 400, len)); ~8 % of source ids have no passages; 1 % of sources have
  a NULL text (their passages are filtered by SQL); ~0.1 % of owners have 384- or 128-dim
  passages under the main model (wrong size for the query, skipped); 4 % of owners also have
  other-model passages; owners are published in block-shuffled order and 3 % republished last,
  so rowid order differs from owner order; 2 % of sources share their timestamp with the
  previous one.
- embeddings: N rows: ~90 % 'segment', ~10 % 'episode' under the main model, plus 1.5 %
  other-model rows, 30 'query' rows, 9 wrong-size main-model rows (lengths 0, 2, 384, 510,
  511, 513, 768 bytes), one row with a NaN half and one with a +inf half (owner_kind
  'segment'); start_ms equal for 2 % of rows; 3 % re-embedded with INSERT OR REPLACE.
- vector edge cases, in every table: exact duplicates within an owner (3 % of owners) and
  across owners (2 % of rows), +-0 halves (3 % of rows have 8 zero halves of random sign) and
  their sign-flipped twins (equal scores, different bytes), float16 subnormals (2 % of rows
  have 16 subnormal halves), and, at every 4,000-row chunk boundary of the unwindowed scan,
  the first row of the new chunk gets the bytes of the last row of the previous chunk (same
  owner: an intra-owner tie across chunks; different owners: a cross-owner tie).
The JSON meta lists the special rows, plans of the unwindowed scans and tie rows used to build
queries with exact top-of-list ties.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np
import sqlcipher3

HERE = Path(__file__).resolve().parent
CACHE = HERE / "cache"
SIZES = {"100k": 100_000, "1m": 1_000_000}
KEY = bytes(range(32))  # disposable synthetic DB: fixed raw key
MODEL = "qwen3-embedding-0.6b-q8"
OTHER_MODEL = "qwen3-embedding-0.6b-q8-old"
DIMS = 256
CHUNK = 4_000
DAY_MS = 86_400_000
HOUR_MS = 3_600_000
SPAN_DAYS = 180
T0 = 1_762_041_600_000  # 2025-11-02 00:00 UTC
FAR = 2**62
BUSY_TIMEOUT_MS = 5_000
SEED = 20261110

PASSAGE_TABLES = {
    "segment": ("segment_passages", "segment_id", "audio_segments", "transcript", "start_ms"),
    "frame": ("frame_passages", "frame_id", "frames", "text", "ts_ms"),
}
PROCESSES = ["chrome.exe", "Chrome.EXE", "Discord.exe", "discord", "Code.exe", "firefox.exe",
             "zen.exe", "explorer.exe", "WindowsTerminal.exe", None]  # fmt: skip


def db_path(label: str) -> Path:
    return CACHE / f"vec_{label}.db"


def meta_path(label: str) -> Path:
    return CACHE / f"vec_{label}.json"


def connect(path: Path) -> sqlcipher3.Connection:
    """Same settings as akashic.storage.db.connect (minus mkdir and key-error wrapping)."""
    conn = sqlcipher3.connect(
        str(path), timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None, check_same_thread=False
    )
    conn.execute(f"PRAGMA key = \"x'{KEY.hex()}'\"")
    conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA temp_store = MEMORY")
    return conn


def passage_sql(kind: str) -> str:
    """Today's passages.top SQL without the apps filter (scan order of the unwindowed query)."""
    table, column, source, text_column, ts = PASSAGE_TABLES[kind]
    return (
        f"SELECT p.{column}, p.start_char, p.end_char, p.vec FROM {table} p"
        f" JOIN {source} s ON s.id = p.{column} WHERE p.model = :model"
        f" AND s.{ts} >= :start AND s.{ts} < :end AND s.{text_column} IS NOT NULL"
    )


EMB_SQL = (
    "SELECT owner_id, vec FROM embeddings WHERE owner_kind = ? AND model = ?"
    " AND start_ms >= ? AND start_ms < ?"
)


# --- vectors --------------------------------------------------------------------------------


def unit_f16(rng: np.random.Generator, centers: np.ndarray, n: int, dims: int = DIMS):
    out = np.empty((n, dims), dtype=np.float16)
    for a in range(0, n, 100_000):
        b = min(n, a + 100_000)
        topic = rng.integers(0, len(centers), b - a)
        v = centers[topic, :dims] + rng.standard_normal((b - a, dims), dtype=np.float32) * np.float32(
            1.2 / 16
        )
        v /= np.linalg.norm(v, axis=1, keepdims=True)
        out[a:b] = v.astype(np.float16)  # like embeddings.put / passages.publish
    return out


def apply_edges(rng: np.random.Generator, h: np.ndarray) -> dict[str, np.ndarray]:
    """In place: +-0 halves, subnormals, cross-row exact duplicates and +-0 twins."""
    n = len(h)
    u = h.view(np.uint16)
    zero_rows = rng.choice(n, max(2, n * 3 // 100), replace=False)
    cols = rng.integers(0, DIMS, (len(zero_rows), 8))
    u[zero_rows[:, None], cols] = np.where(rng.random(cols.shape) < 0.5, 0x0000, 0x8000)
    sub_rows = rng.choice(n, max(2, n * 2 // 100), replace=False)
    cols = rng.integers(0, DIMS, (len(sub_rows), 16))
    sign = np.where(rng.random(cols.shape) < 0.5, 0, 0x8000)
    u[sub_rows[:, None], cols] = (rng.integers(1, 0x400, cols.shape) | sign).astype(np.uint16)
    dup_dst = rng.integers(0, n, max(2, n * 2 // 100))
    dup_src = rng.integers(0, n, len(dup_dst))
    # Half of the copies land near their source (same or nearby owner, same chunk or the next).
    near = rng.random(len(dup_dst)) < 0.5
    dup_dst[near] = np.clip(dup_src[near] + rng.integers(-6000, 6000, near.sum()), 0, n - 1)
    h[dup_dst] = h[dup_src]
    twin_src = rng.choice(zero_rows, max(2, n // 200))
    twin_dst = rng.integers(0, n, len(twin_src))
    for a, b in zip(twin_src.tolist(), twin_dst.tolist(), strict=True):
        row = u[a].copy()
        row[(row & 0x7FFF) == 0] ^= 0x8000
        u[b] = row
    return {"dup_src": dup_src, "dup_dst": dup_dst, "twin_src": twin_src, "twin_dst": twin_dst,
            "zero_rows": zero_rows, "sub_rows": sub_rows}  # fmt: skip


def source_times(rng: np.random.Generator, n: int) -> np.ndarray:
    t = np.sort(rng.uniform(0, SPAN_DAYS * DAY_MS, n)) + rng.normal(0, 10 * 60_000, n)
    t = (T0 + np.clip(t, 0, SPAN_DAYS * DAY_MS - 1)).astype(np.int64)
    same = np.flatnonzero(rng.random(n) < 0.02)
    same = same[same > 0]
    t[same] = t[same - 1]
    return t


# --- passages -------------------------------------------------------------------------------


def build_passages(conn, rng, centers, n_rows: int, kind: str) -> dict[str, object]:  # noqa: PLR0915
    table, column, source, _, _ = PASSAGE_TABLES[kind]
    est = int(n_rows / 13) + 100
    counts = np.where(rng.random(est * 2) < 0.6, rng.integers(1, 6, est * 2),
                      rng.integers(6, 61, est * 2))  # fmt: skip
    counts[rng.choice(min(1000, est), 5, replace=False)] = 60  # a few maximal owners
    cum = np.cumsum(counts)
    n_owner = int(np.searchsorted(cum, n_rows)) + 1  # cum[n_owner - 1] >= n_rows
    counts = counts[:n_owner].copy()
    counts[-1] -= int(cum[n_owner - 1] - n_rows)  # stays >= 1
    assert counts.min() >= 1 and counts.sum() == n_rows, (counts.min(), counts.sum())
    row_start = np.concatenate(([0], np.cumsum(counts)[:-1]))

    n_src = int(n_owner * 1.08)
    src_ids = np.arange(1, n_src + 1, dtype=np.int64)
    times = source_times(rng, n_src)
    null_text = rng.random(n_src) < 0.01
    owner_pos = np.sort(rng.choice(n_src, n_owner, replace=False))  # ids with passages
    owners = src_ids[owner_pos]

    # Source rows.
    if kind == "segment":
        rows = ((int(i), "mic" if i % 3 else "system", int(t), int(t) + 20_000, 15_000, "done",
                 None if nt else "t") for i, t, nt in zip(src_ids, times, null_text, strict=True))  # fmt: skip
        conn.executemany(
            "INSERT INTO audio_segments (id, stream, start_ms, end_ms, speech_ms, status,"
            " transcript) VALUES (?, ?, ?, ?, ?, ?, ?)", rows)  # fmt: skip
    else:
        procs = rng.integers(0, len(PROCESSES), n_src)
        scope = rng.random(n_src) < 0.8
        rows = ((int(i), int(t), int(i % 3), PROCESSES[p], None if nt else "t", "click",
                 "window" if sc else "monitor")
                for i, t, nt, p, sc in zip(src_ids, times, null_text, procs, scope, strict=True))  # fmt: skip
        conn.executemany(
            "INSERT INTO frames (id, ts_ms, monitor, process, text, trigger, image_scope)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)", rows)  # fmt: skip

    h = unit_f16(rng, centers, n_rows)
    edges = apply_edges(rng, h)
    # Exact duplicates inside an owner.
    multi = np.flatnonzero(counts >= 2)
    within = rng.choice(multi, max(2, len(multi) * 3 // 100), replace=False)
    for o in within.tolist():
        a, b = rng.choice(counts[o], 2, replace=False)
        h[row_start[o] + b] = h[row_start[o] + a]
    # Wrong-size owners (whole published set has another dim).
    wrong = rng.choice(n_owner, max(3, n_owner // 1000), replace=False)
    wrong_dim = {int(o): (384 if k % 2 else 128) for k, o in enumerate(wrong)}
    other = set(rng.choice(n_owner, n_owner * 4 // 100, replace=False).tolist())
    lens = 300 * (counts - 1) + rng.integers(1, 401, n_owner)

    blocks = np.arange(n_owner) // 256
    order = np.lexsort((rng.random(n_owner), blocks + rng.integers(0, 3, n_owner)))
    republish = rng.choice(n_owner, n_owner * 3 // 100, replace=False)
    rep_set = set(republish.tolist())
    seq = [o for o in order.tolist() if o not in rep_set] + republish.tolist()

    def gen():
        for o in seq:
            oid = int(owners[o])
            k, r0, length = int(counts[o]), int(row_start[o]), int(lens[o])
            d = wrong_dim.get(o)
            if d is not None:
                bad = unit_f16(rng, centers, k, d) if d <= DIMS else rng.standard_normal(
                    (k, d)).astype(np.float16)  # fmt: skip
            for j in range(k):
                s = 300 * j
                vec = h[r0 + j].tobytes() if d is None else bad[j].tobytes()
                yield (oid, MODEL, s, min(s + 400, length), d or DIMS, vec)
            if o in other:
                ov = unit_f16(rng, centers, k)
                for j in range(k):
                    s = 300 * j
                    yield (oid, OTHER_MODEL, s, min(s + 400, length), DIMS, ov[j].tobytes())

    conn.executemany(f"INSERT INTO {table} VALUES (?, ?, ?, ?, ?, ?)", gen())  # noqa: S608

    # Chunk-boundary ties in the unwindowed scan order.
    scan = [(r[0], r[1], len(r[3])) for r in conn.execute(
        passage_sql(kind), {"model": MODEL, "start": 0, "end": FAR})]  # fmt: skip
    same_owner = cross_owner = straddling = 0
    boundary_rows = []
    for p in range(CHUNK, len(scan), CHUNK):
        a, b = scan[p - 1], scan[p]
        if a[0] == b[0]:
            straddling += 1
        if a[2] != DIMS * 2 or b[2] != DIMS * 2:
            continue
        vec = conn.execute(f"SELECT vec FROM {table} WHERE {column} = ? AND model = ?"  # noqa: S608
                           " AND start_char = ?", (a[0], MODEL, a[1])).fetchone()[0]  # fmt: skip
        conn.execute(f"UPDATE {table} SET vec = ? WHERE {column} = ? AND model = ?"  # noqa: S608
                     " AND start_char = ?", (vec, b[0], MODEL, b[1]))  # fmt: skip
        if a[0] == b[0]:
            same_owner += 1
        else:
            cross_owner += 1
        boundary_rows.append([int(a[0]), int(a[1])])

    def ref(i: int) -> list[int]:
        o = int(np.searchsorted(row_start, i, side="right") - 1)
        return [int(owners[o]), int(300 * (i - row_start[o]))]

    tie_rows = boundary_rows + [ref(int(i)) for i in edges["dup_dst"][:300]]
    tie_rows += [ref(int(row_start[o])) for o in within[:100].tolist()]
    tie_rows += [ref(int(i)) for i in edges["twin_dst"][:100]]
    plan = [r[3] for r in conn.execute("EXPLAIN QUERY PLAN " + passage_sql(kind),
                                       {"model": MODEL, "start": 0, "end": FAR})]  # fmt: skip
    return {
        "table": table, "main_rows": n_rows, "owners": n_owner, "sources": n_src,
        "null_text_sources": int(null_text.sum()), "wrong_dim_owners": len(wrong_dim),
        "other_model_owners": len(other), "republished_owners": len(rep_set),
        "within_owner_dup_owners": len(within), "scan_rows_unwindowed": len(scan),
        "boundary_same_owner_ties": same_owner, "boundary_cross_owner_ties": cross_owner,
        "boundary_straddling_owners": straddling, "t_min": int(times.min()),
        "t_max": int(times.max()), "plan_unwindowed": plan, "tie_rows": tie_rows,
    }  # fmt: skip


# --- embeddings -----------------------------------------------------------------------------


def build_embeddings(conn, rng, centers, n_rows: int) -> dict[str, object]:  # noqa: PLR0915
    n_ep = n_rows // 10
    n_seg = n_rows - n_ep
    seg_ids = rng.choice(np.arange(1, int(n_seg * 1.1) + 1), n_seg, replace=False)
    ep_ids = rng.choice(np.arange(1, int(n_ep * 1.05) + 1), n_ep, replace=False)
    kinds = np.array(["segment"] * n_seg + ["episode"] * n_ep)
    ids = np.concatenate((seg_ids, ep_ids)).astype(np.int64)
    start = T0 + rng.integers(0, SPAN_DAYS * DAY_MS, n_rows)
    same = rng.choice(n_rows, n_rows // 50, replace=False)
    start[same] = start[rng.integers(0, n_rows, len(same))]
    h = unit_f16(rng, centers, n_rows)
    edges = apply_edges(rng, h)
    # Equal start_ms AND equal vector: ties broken only by rowid in the scan.
    eq = rng.choice(n_rows, n_rows // 200, replace=False)
    src = rng.integers(0, n_rows, len(eq))
    keep = kinds[eq] == kinds[src]
    eq, src = eq[keep], src[keep]
    start[eq] = start[src]
    h[eq] = h[src]
    created = start + rng.integers(0, 6 * HOUR_MS, n_rows)
    order = np.argsort(created, kind="stable")

    def row(i: int, model: str = MODEL, vec: bytes | None = None) -> tuple[object, ...]:
        v = h[i].tobytes() if vec is None else vec
        return (str(kinds[i]), int(ids[i]), model, len(v) // 2, v, int(start[i]), int(created[i]))

    conn.executemany("INSERT INTO embeddings VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (row(int(i)) for i in order))  # fmt: skip
    other = rng.choice(n_rows, n_rows * 15 // 1000, replace=False)
    ov = unit_f16(rng, centers, len(other))
    conn.executemany("INSERT INTO embeddings VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (row(int(i), OTHER_MODEL, ov[k].tobytes()) for k, i in enumerate(other)))  # fmt: skip
    qv = unit_f16(rng, centers, 30)
    conn.executemany("INSERT INTO embeddings VALUES ('query', ?, ?, ?, ?, ?, ?)",
                     ((int(rng.integers(1, 2**61)), MODEL, DIMS, qv[k].tobytes(),
                       int(T0 + rng.integers(0, SPAN_DAYS * DAY_MS)), 0) for k in range(30)))  # fmt: skip
    # Re-embedded: INSERT OR REPLACE gives a new, larger rowid (same bytes).
    reembed = rng.choice(n_rows, n_rows * 3 // 100, replace=False)
    conn.executemany("INSERT OR REPLACE INTO embeddings VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (row(int(i)) for i in reembed))  # fmt: skip
    # Damaged / wrong-size rows under the main model (embeddings has no length CHECK).
    next_seg = int(seg_ids.max()) + 1
    wrong = []
    for k, nbytes in enumerate((0, 2, 384, 510, 511, 513, 768, 768, 1024)):
        oid = next_seg + k
        t = int(T0 + rng.integers(0, SPAN_DAYS * DAY_MS))
        blob = rng.integers(0, 256, nbytes, dtype=np.uint8).tobytes()
        conn.execute("INSERT INTO embeddings VALUES ('segment', ?, ?, ?, ?, ?, ?)",
                     (oid, MODEL, nbytes // 2, blob, t, t))  # fmt: skip
        wrong.append([oid, nbytes, t])
    ep_wrong = int(ep_ids.max()) + 1
    conn.execute("INSERT INTO embeddings VALUES ('episode', ?, ?, 384, ?, ?, ?)",
                 (ep_wrong, MODEL, bytes(768), T0 + DAY_MS, T0 + DAY_MS))  # fmt: skip
    special = {}
    for k, (name, bits, comp, day) in enumerate((("nan", 0x7E00, 7, 90), ("inf", 0x7C00, 11, 91))):
        v = h[int(rng.integers(0, n_rows))].copy()
        v.view(np.uint16)[comp] = bits
        oid = next_seg + 100 + k
        t = T0 + day * DAY_MS + 12 * HOUR_MS
        conn.execute("INSERT INTO embeddings VALUES ('segment', ?, ?, ?, ?, ?, ?)",
                     (oid, MODEL, DIMS, v.tobytes(), t, t))  # fmt: skip
        special[name] = {"owner_id": oid, "start_ms": int(t), "component": comp}

    boundary = {}
    for kind in ("segment", "episode"):
        scan = [(r[0], len(r[1])) for r in conn.execute(EMB_SQL, (kind, MODEL, 0, FAR))]
        n_tie = 0
        for p in range(CHUNK, len(scan), CHUNK):
            a, b = scan[p - 1], scan[p]
            if a[1] != DIMS * 2 or b[1] != DIMS * 2:
                continue
            vec = conn.execute("SELECT vec FROM embeddings WHERE owner_kind = ? AND owner_id = ?"
                               " AND model = ?", (kind, a[0], MODEL)).fetchone()[0]  # fmt: skip
            conn.execute("UPDATE embeddings SET vec = ? WHERE owner_kind = ? AND owner_id = ?"
                         " AND model = ?", (vec, kind, b[0], MODEL))  # fmt: skip
            n_tie += 1
        boundary[kind] = {"scan_rows": len(scan), "boundary_ties": n_tie}

    def ref(i: int) -> list[object]:
        return [str(kinds[i]), int(ids[i])]

    tie_rows = [ref(int(i)) for i in edges["dup_dst"][:300]] + [ref(int(i)) for i in eq[:200]]
    tie_rows += [ref(int(i)) for i in edges["twin_dst"][:100]]
    plan = [r[3] for r in conn.execute("EXPLAIN QUERY PLAN " + EMB_SQL, ("segment", MODEL, 0, FAR))]
    return {
        "rows_main": n_rows, "segment_rows": n_seg, "episode_rows": n_ep,
        "other_model_rows": len(other), "query_rows": 30, "reembedded": len(reembed),
        "wrong_size_rows": wrong, "wrong_size_episode": ep_wrong, "special": special,
        "equal_start_and_vector_rows": len(eq), "boundary": boundary, "plan_unwindowed": plan,
        "tie_rows": tie_rows,
    }  # fmt: skip


def build(label: str) -> dict[str, object]:
    n = SIZES[label]
    path = db_path(label)
    CACHE.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        Path(str(path) + suffix).unlink(missing_ok=True)
    t0 = time.perf_counter()
    rng = np.random.default_rng(SEED + n)
    centers = rng.standard_normal((64, 384)).astype(np.float32)
    centers /= np.linalg.norm(centers, axis=1, keepdims=True)
    conn = connect(path)
    conn.executescript((HERE / "schema.sql").read_text("utf-8"))
    meta: dict[str, object] = {"label": label, "n": n, "db": str(path), "key_hex": KEY.hex(),
                               "model": MODEL, "other_model": OTHER_MODEL, "dims": DIMS,
                               "t0": T0, "t_end": T0 + SPAN_DAYS * DAY_MS, "seed": SEED + n}  # fmt: skip
    for kind in ("segment", "frame"):
        conn.execute("BEGIN IMMEDIATE")
        meta[f"passages_{kind}"] = build_passages(conn, rng, centers, n, kind)
        conn.execute("COMMIT")
        print(f"[{label}] {kind} passages done {time.perf_counter() - t0:.0f}s", flush=True)
    conn.execute("BEGIN IMMEDIATE")
    meta["embeddings"] = build_embeddings(conn, rng, centers, n)
    conn.execute("COMMIT")
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    meta["build_s"] = round(time.perf_counter() - t0, 1)
    meta["db_bytes"] = path.stat().st_size
    meta_path(label).write_text(json.dumps(meta, indent=1), "utf-8")
    print(f"[{label}] built in {meta['build_s']} s, {meta['db_bytes'] / 1e6:.0f} MB", flush=True)
    return meta


def ensure(label: str, *, force: bool = False) -> dict[str, object]:
    if not force and db_path(label).exists() and meta_path(label).exists():
        return json.loads(meta_path(label).read_text("utf-8"))
    return build(label)


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    labels = list(SIZES) if not args or args == ["all"] else args
    for lab in labels:
        m = ensure(lab, force="--force" in sys.argv)
        brief = {k: v for k, v in m.items() if not isinstance(v, dict)}
        for k, v in m.items():
            if isinstance(v, dict):
                brief[k] = {kk: vv for kk, vv in v.items() if kk != "tie_rows"}
        print(json.dumps(brief, indent=1))
