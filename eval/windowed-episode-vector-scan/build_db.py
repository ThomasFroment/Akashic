"""Disposable encrypted SQLCipher DB: 40,000 episodes over ~2 years with episode vectors.

Run: python build_db.py [--force]. The DB is cached under cache/ and reused by every candidate.
Connection settings copy src/akashic/storage/db.py (raw key, WAL, synchronous NORMAL, ...).

Synthetic content (seeded, deterministic):
- 40,000 episodes, ~55 per day over 730 days; durations mostly 30 s to 2 h (lognormal by kind),
  ~3 % of 2-12 h, ~0.4 % heavy tail of 1-60 days (paused media/game sessions), and 3 forced
  60-day episodes (used by the straddle scenario).
- ~3 % of episodes share their start_ms with another (exact equal start_ms).
- Episode ids are only roughly chronological (built late / rebuilt); embedding rowids follow a
  different, shuffled order with ~3 % re-embedded at the end (INSERT OR REPLACE).
- 256-dim float16 vectors (normalised in float32 then cast, like embeddings.put) around 64
  topics; ~700 duplicate groups copy a vector's bytes exactly (exact score ties), some between
  episodes with equal start_ms; ~1 % of episodes have no vector yet; 5 have a 384-dim vector
  under the main model (damaged rows, skipped by size); 300 other-model rows; 30 'query' rows.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import sqlcipher3

HERE = Path(__file__).resolve().parent
CACHE = HERE / "cache"
DB_PATH = CACHE / "episodes40k.db"
META_PATH = CACHE / "meta.json"
KEY = bytes(range(32))  # disposable test DB: fixed raw key
MODEL = "qwen3-embedding-0.6b-q8"
OTHER_MODEL = "qwen3-embedding-0.6b-q8-old"
DIMS = 256
N_EPISODES = 40_000
DAYS = 730
DAY_MS = 86_400_000
HOUR_MS = 3_600_000
T0 = 1_731_196_800_000  # 2024-11-10 00:00 UTC
SEED = 20261110
BUSY_TIMEOUT_MS = 5_000


def connect(path: Path = DB_PATH) -> sqlcipher3.Connection:
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


def _unit(rng: np.random.Generator, centers: np.ndarray, topic: np.ndarray) -> np.ndarray:
    v = centers[topic] + 1.2 * rng.standard_normal((len(topic), DIMS)).astype(np.float32) / 16.0
    v /= np.linalg.norm(v, axis=1, keepdims=True)
    return v.astype(np.float32).astype(np.float16)  # embeddings.put stores float16


def _durations(rng: np.random.Generator, kinds: np.ndarray) -> np.ndarray:
    median_min = {"video": 12, "music": 35, "conversation": 40, "browsing": 8, "work": 30,
                  "game": 60}  # fmt: skip
    med = np.array([median_min[k] for k in kinds], dtype=np.float64)
    dur = np.exp(rng.normal(np.log(med), 0.9)) * 60_000
    dur = np.clip(dur, 30_000, 2 * HOUR_MS)
    n = len(kinds)
    long = rng.random(n) < 0.03
    dur[long] = rng.uniform(2, 12, long.sum()) * HOUR_MS
    media = np.isin(kinds, ["video", "music", "game"])
    tail = (rng.random(n) < 0.004 / 0.45) & media  # ~0.4 % of all episodes
    dur[tail] = np.exp(rng.uniform(np.log(1), np.log(60), tail.sum())) * DAY_MS
    return dur.astype(np.int64)


def build(path: Path = DB_PATH) -> dict[str, object]:
    rng = np.random.default_rng(SEED)
    kinds_all = np.array(["video", "music", "conversation", "browsing", "work", "game"])
    kinds = rng.choice(kinds_all, N_EPISODES, p=[0.25, 0.10, 0.15, 0.25, 0.15, 0.10])
    day = rng.integers(0, DAYS, N_EPISODES)
    # Time of day: mostly 08:00-02:00, peaking in the afternoon and evening.
    tod_h = np.clip(rng.normal(16.5, 4.5, N_EPISODES), 7.5, 25.9)
    start = T0 + day * DAY_MS + (tod_h * HOUR_MS).astype(np.int64) + rng.integers(0, 1000, N_EPISODES)
    dur = _durations(rng, kinds)
    # Three forced 60-day episodes for the straddle scenario.
    forced = np.array([1000, 2000, 3000])
    for j, d in zip(forced, (200, 420, 600), strict=True):
        kinds[j], start[j], dur[j] = "game", T0 + d * DAY_MS + 13 * HOUR_MS, 60 * DAY_MS
    order = np.argsort(start, kind="stable")
    # Equal start_ms: ~3 % copy the start of their chronological predecessor.
    same = rng.random(N_EPISODES) < 0.03
    same[forced] = False
    for pos in range(1, N_EPISODES):
        if same[order[pos]] and order[pos - 1] not in forced:
            start[order[pos]] = start[order[pos - 1]]
    end = start + dur
    # Episode ids: roughly chronological by end time, with noise and ~1 % built much later.
    built = end + rng.integers(0, 2 * HOUR_MS, N_EPISODES)
    late = rng.random(N_EPISODES) < 0.01
    built[late] += rng.integers(DAY_MS, 30 * DAY_MS, late.sum())
    idx_by_built = np.argsort(built, kind="stable")
    ids = np.empty(N_EPISODES, dtype=np.int64)
    ids[idx_by_built] = np.arange(1, N_EPISODES + 1)

    centers = rng.standard_normal((64, DIMS)).astype(np.float32)
    centers /= np.linalg.norm(centers, axis=1, keepdims=True)
    topic = rng.integers(0, 64, N_EPISODES)
    vecs = _unit(rng, centers, topic)

    # Duplicate vectors (exact ties): ~700 groups of 2-5.
    dup_groups = 0
    pos_of = np.empty(N_EPISODES, dtype=np.int64)
    pos_of[order] = np.arange(N_EPISODES)
    for _ in range(700):
        src = int(rng.integers(0, N_EPISODES))
        k = int(rng.integers(1, 5))
        if rng.random() < 0.6:  # rewatch / reopen near in time
            p = pos_of[src]
            near = order[max(0, p - 150) : p + 150]
            others = rng.choice(near, k, replace=False)
        else:
            others = rng.integers(0, N_EPISODES, k)
        vecs[others] = vecs[src]
        dup_groups += 1
    # Some equal-start pairs also share their vector: tie on score and start_ms.
    eq_pairs = 0
    for pos in range(1, N_EPISODES):
        a, b = order[pos - 1], order[pos]
        if start[a] == start[b] and rng.random() < 0.3:
            vecs[b] = vecs[a]
            eq_pairs += 1

    # Which episodes have a vector, and in which order the vectors were written (rowids).
    no_vec = np.zeros(N_EPISODES, dtype=bool)
    no_vec[order[-200:]] = True  # the newest are not embedded yet
    no_vec[rng.choice(N_EPISODES, 100, replace=False)] = True
    no_vec[forced] = False
    damaged = rng.choice(np.flatnonzero(~no_vec), 5, replace=False)
    embedded = np.flatnonzero(~no_vec)
    write_key = built[embedded] + rng.integers(0, 6 * HOUR_MS, len(embedded))
    write_order = embedded[np.argsort(write_key, kind="stable")]
    reembed = rng.choice(write_order, int(0.03 * len(write_order)), replace=False)

    rows_emb: list[tuple[object, ...]] = []
    created = 0

    def emb_row(i: int, model: str, vec: bytes, dim: int) -> tuple[object, ...]:
        nonlocal created
        created += 1
        return ("episode", int(ids[i]), model, dim, vec, int(start[i]), int(end[i]) + created)

    other_model_set = set(rng.choice(embedded, 300, replace=False).tolist())
    other_vecs = _unit(rng, centers, rng.integers(0, 64, N_EPISODES))
    damaged_set = set(damaged.tolist())
    query_at = set(rng.choice(len(write_order), 30, replace=False).tolist())
    for n, i in enumerate(write_order):
        i = int(i)
        if i in damaged_set:
            bad = rng.standard_normal(384).astype(np.float16)
            rows_emb.append(emb_row(i, MODEL, bad.tobytes(), 384))
        else:
            rows_emb.append(emb_row(i, MODEL, vecs[i].tobytes(), DIMS))
        if i in other_model_set:
            rows_emb.append(emb_row(i, OTHER_MODEL, other_vecs[i].tobytes(), DIMS))
        if n in query_at:
            q = _unit(rng, centers, rng.integers(0, 64, 1))[0]
            rows_emb.append(("query", int(rng.integers(1, 2**61)), MODEL, DIMS, q.tobytes(),
                             int(start[i]), int(start[i])))  # fmt: skip

    apps = {"video": ["firefox.exe", "zen.exe", "vlc.exe"], "music": ["Spotify.exe"],
            "conversation": ["Discord.exe", "Discord.exe", "steam.exe"],
            "browsing": ["firefox.exe", "zen.exe"], "work": ["Code.exe", "WindowsTerminal.exe"],
            "game": ["eldenring.exe", "Factorio.exe", "cs2.exe"]}  # fmt: skip
    ep_rows = []
    for i in range(N_EPISODES):
        k = str(kinds[i])
        app = apps[k][int(rng.integers(0, len(apps[k])))]
        url = f"https://example.invalid/{i}" if k in ("video", "browsing") else None
        ep_rows.append((int(ids[i]), k, f"{k}:{i}", int(start[i]), int(end[i]),
                        f"{k} topic {int(topic[i])}", app, url, ""))  # fmt: skip
    ep_rows.sort(key=lambda r: r[0])
    speakers = [(1, None, 1, 0, 0)] + [(s, f"person{s}", 0, 0, 0) for s in range(2, 13)]
    people = []
    for i in np.flatnonzero(kinds == "conversation"):
        for s in rng.choice(np.arange(2, 13), int(rng.integers(1, 4)), replace=False):
            people.append((int(ids[i]), int(s), int(rng.integers(1000, 600_000))))

    path.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        Path(str(path) + suffix).unlink(missing_ok=True)
    conn = connect(path)
    conn.executescript((HERE / "schema.sql").read_text("utf-8"))
    conn.execute("BEGIN IMMEDIATE")
    conn.executemany("INSERT INTO speakers (id, name, is_self, vec_n, created_ms)"
                     " VALUES (?, ?, ?, ?, ?)", speakers)  # fmt: skip
    conn.executemany("INSERT INTO episodes (id, kind, ref, start_ms, end_ms, title, app, url, body)"
                     " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", ep_rows)  # fmt: skip
    conn.executemany("INSERT INTO episode_people VALUES (?, ?, ?)", people)
    conn.executemany("INSERT INTO embeddings (owner_kind, owner_id, model, dim, vec, start_ms,"
                     " created_ms) VALUES (?, ?, ?, ?, ?, ?, ?)", rows_emb)  # fmt: skip
    # Re-embedded rows: INSERT OR REPLACE gives them a new, larger rowid.
    conn.executemany(
        "INSERT OR REPLACE INTO embeddings (owner_kind, owner_id, model, dim, vec, start_ms,"
        " created_ms) VALUES ('episode', ?, ?, ?, ?, ?, ?)",
        [(int(ids[i]), MODEL, DIMS, vecs[i].tobytes(), int(start[i]), int(end[i]) + 10**7)
         for i in reembed if int(i) not in damaged_set],
    )  # fmt: skip
    conn.execute("COMMIT")
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()

    meta = {
        "db": str(path), "key_hex": KEY.hex(), "model": MODEL, "seed": SEED, "t0": T0,
        "t_end": int(T0 + DAYS * DAY_MS), "episodes": N_EPISODES,
        "embedded_main_model": int(len(embedded)), "damaged_main_model": len(damaged_set),
        "other_model_rows": len(other_model_set), "query_rows": len(query_at),
        "reembedded": int(len(reembed)), "duplicate_groups": dup_groups,
        "equal_start_vector_pairs": eq_pairs,
        "equal_start_episodes": int(len(start) - len(np.unique(start))),
        "tail_ge_1day": int((dur >= DAY_MS).sum()), "ge_2h": int((dur > 2 * HOUR_MS).sum()),
        "forced_60day_ids": [int(ids[j]) for j in forced],
        "forced_60day_spans": [[int(start[j]), int(end[j])] for j in forced],
        "max_duration_ms": int(dur.max()),
    }  # fmt: skip
    META_PATH.write_text(json.dumps(meta, indent=1), "utf-8")
    return meta


def ensure() -> dict[str, object]:
    if DB_PATH.exists() and META_PATH.exists():
        return json.loads(META_PATH.read_text("utf-8"))
    return build()


if __name__ == "__main__":
    meta = build() if "--force" in sys.argv else ensure()
    print(json.dumps(meta, indent=1))
