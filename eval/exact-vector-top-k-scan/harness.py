"""Correctness, per-row cost and memory harness for the exact vector top-k scans.

Usage:  python harness.py <candidate.py> <outdir> [--sizes 100k,1m] [--queries 300] [--runs 9]
                                                  [--no-today]

The candidate module defines (same signatures and return types as today, see ref_today.py):
    passages_top(conn, model, query, *, start_ms, end_ms, only=None, limit=100,
                 kind="segment", apps=()) -> list of objects with .owner_id .score .start_char
                 .end_char (int, float, int, int), best first
    embeddings_top(conn, owner_kind, model, query, *, start_ms=0, end_ms=2**62, only=None,
                   exclude=(), limit=30) -> list of objects with .owner_id .score, best first

Correctness: per DB size, --queries seeded random queries (default 300) over passages
(segment and frame) and embeddings (segment and episode) with time windows (unwindowed, 1 h,
day, week, month, exact-timestamp edges, empty, around the NaN/inf rows), only (None, subsets,
small sets with absent ids, empty; set/list/tuple/frozenset/generator), exclude, apps, limits
1/30/100/200, and query vectors that copy stored tie vectors (exact top-of-list ties, ties
across 4,000-row chunk boundaries). Today's results (ref_today.py) are cached in cache/.
A query is "ok" when the owner sequence (and, for passages, the char offsets) equals today's.
Otherwise each differing position is classified with exact float64 scores read back from the
DB under the query's own filters:
    hard       a different owner/passage whose exact score differs by >= 1e-6, an ineligible
               owner, a different length, an exception, or a wrong return type;
    tie_order  equal exact scores in another order than today's tie rule
               (passages: owner_id; max passage: first in scan order; embeddings: scan order);
    near_tie   exact scores differ by < 1e-6 (allowed: float32 accumulation order).
mismatches = hard + tie_order + errors + type errors, counted outside NaN scope; queries where
the embeddings NaN row is eligible are reported separately (nan_scope_mismatches), together
with whether today's own answer there equals a NaN-dropped stable oracle.
max_abs_dscore = max |score_cand - score_today| over owners present in both lists (owners
only in the candidate's list: vs their exact score), worst case printed.

Cost (per scenario: unwindowed passages segment all/only-50 %, frame with apps, embeddings
segment all/only+exclude; one fixed query): 1 warm-up then --runs interleaved runs, median of
    t_total   the call;
    t_fetch   the same SQL iterated with fetchmany(4000), nothing done (cursor floor: tuples,
              per-column objects, bytes copy, SQLCipher decode);
    t_decode  an aggregate count(...)/sum(length(vec)) over the same FROM/JOIN/WHERE (SQLite
              step + page decryption, no Python per row);
per scanned row (rows = rows the cursor returns): total, fetch floor, decode and
after_fetch = (t_total - t_fetch) / rows. Today's numbers are measured in the same run (or
read from cache/today_timing_<size>.json with --no-today).
Memory: tracemalloc peak minus start during one call (numpy buffers are traced too) on the
unwindowed passages-segment and embeddings-segment scenarios of the largest size.

Outputs in <outdir>: metrics.txt (one line), details.json, run.log, side.png.
"""

from __future__ import annotations

import gc
import hashlib
import importlib.util
import inspect
import json
import math
import pickle
import re
import statistics
import sys
import time
import tracemalloc
import traceback
from pathlib import Path

import matplotlib
import numpy as np

matplotlib.use("Agg")
import matplotlib.pyplot as plt

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import build_db  # noqa: E402
import ref_today  # noqa: E402

REPO = HERE.parents[1]
FAR = 2**62
DAY, HOUR = build_db.DAY_MS, build_db.HOUR_MS
NEAR = 1e-6
BUDGET_NS = 500.0
CHUNK = 4_000
QUERY_VERSION = 3  # bump when make_queries changes (invalidates the cached ref results)
SEED = 4242


class Tee:
    def __init__(self, path: Path) -> None:
        self.f = path.open("w", encoding="utf-8")

    def __call__(self, *a: object) -> None:
        s = " ".join(str(x) for x in a)
        print(s, flush=True)
        self.f.write(s + "\n")
        self.f.flush()


def load(path: str):
    spec = importlib.util.spec_from_file_location(Path(path).stem + "_candidate", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # dataclasses look their module up here
    spec.loader.exec_module(mod)
    return mod


def verbatim_check() -> list[str]:
    """ref_today bodies must equal the repo's top() bodies (def line excepted)."""

    def block(src: str, name: str) -> list[str]:
        lines = src.splitlines()
        i = next(k for k, ln in enumerate(lines) if ln.startswith(f"def {name}("))
        j = next((k for k in range(i + 1, len(lines)) if lines[k] and not lines[k][0].isspace()
                  and not lines[k].startswith(")")), len(lines))  # fmt: skip
        return lines[i + 1 : j]

    out = []
    ref_src = Path(ref_today.__file__).read_text("utf-8")
    for repo_file, name in (("passages.py", "passages_top"), ("embeddings.py", "embeddings_top")):
        repo = (REPO / "src" / "akashic" / "storage" / repo_file).read_text("utf-8")
        a, b = block(repo, "top"), block(ref_src, name)
        a = [ln for ln in a if ln.strip()]
        b = [ln for ln in b if ln.strip()]
        out.append(f"{repo_file}: {'verbatim' if a == b else 'DIFFERS FROM REPO'}")
    return out


# --- SQL mirrors of today's filters ---------------------------------------------------------


def passage_where(kind: str, apps: tuple[str, ...]) -> tuple[str, str]:
    table, column, source, text_column, ts = build_db.PASSAGE_TABLES[kind]
    frm = f" FROM {table} p JOIN {source} s ON s.id = p.{column}"
    where = (f" WHERE p.model = :model AND s.{ts} >= :start AND s.{ts} < :end"
             f" AND s.{text_column} IS NOT NULL")  # fmt: skip
    if kind == "frame" and apps:
        where += (" AND s.image_scope = 'window' AND replace(lower(s.process), '.exe', '')"
                  " IN (SELECT replace(lower(value), '.exe', '') FROM json_each(:apps))")  # fmt: skip
    return frm, where


def passage_params(model: str, q: dict) -> dict[str, object]:
    p: dict[str, object] = {"model": model, "start": q["start"], "end": q["end"]}
    if q["kind"] == "frame" and q["apps"]:
        p["apps"] = json.dumps(tuple(q["apps"]))
    return p


def scan_sqls(model: str, q: dict) -> tuple[str, object, str, object]:
    """(fetch sql, params, decode-aggregate sql, params) for a query."""
    if q["fn"] == "passages":
        column = build_db.PASSAGE_TABLES[q["kind"]][1]
        frm, where = passage_where(q["kind"], tuple(q["apps"]))
        p = passage_params(model, q)
        fetch = f"SELECT p.{column}, p.start_char, p.end_char, p.vec" + frm + where
        agg = (f"SELECT count(p.{column}), count(p.start_char), count(p.end_char),"
               " sum(length(p.vec))" + frm + where)  # fmt: skip
        return fetch, p, agg, p
    p2 = (q["owner_kind"], model, q["start"], q["end"])
    agg = ("SELECT count(owner_id), sum(length(vec)) FROM embeddings WHERE owner_kind = ?"
           " AND model = ? AND start_ms >= ? AND start_ms < ?")  # fmt: skip
    return build_db.EMB_SQL, p2, agg, p2


# --- queries --------------------------------------------------------------------------------


def make_queries(conn, meta: dict, n: int) -> list[dict]:  # noqa: C901, PLR0912, PLR0915
    rng = np.random.default_rng(SEED + int(meta["n"]))
    model = meta["model"]
    t0, t1 = int(meta["t0"]), int(meta["t_end"])
    special = meta["embeddings"]["special"]

    def vec_from(sql: str, args: tuple) -> np.ndarray | None:
        row = conn.execute(sql, args).fetchone()
        if row is None or len(row[0]) != build_db.DIMS * 2:
            return None
        return np.frombuffer(bytes(row[0]), dtype=np.float16).astype(np.float32)

    def random_stored(table: str) -> np.ndarray:
        hi = conn.execute(f"SELECT max(rowid) FROM {table}").fetchone()[0]  # noqa: S608
        while True:
            v = vec_from(f"SELECT vec FROM {table} WHERE rowid = ?",  # noqa: S608
                         (int(rng.integers(1, hi + 1)),))  # fmt: skip
            if v is not None and np.isfinite(v).all():
                return v

    def tie_vec(fn: str, kind: str) -> np.ndarray:
        if fn == "passages":
            table, column = build_db.PASSAGE_TABLES[kind][:2]
            rows = meta[f"passages_{kind}"]["tie_rows"]
            sql = f"SELECT vec FROM {table} WHERE {column} = ? AND model = ? AND start_char = ?"  # noqa: S608
            while True:
                o, sc = rows[int(rng.integers(len(rows)))]
                v = vec_from(sql, (o, model, sc))
                if v is not None:
                    return v
        rows = [r for r in meta["embeddings"]["tie_rows"] if r[0] == kind]
        while True:
            k, o = rows[int(rng.integers(len(rows)))]
            v = vec_from("SELECT vec FROM embeddings WHERE owner_kind = ? AND owner_id = ?"
                         " AND model = ?", (k, o, model))  # fmt: skip
            if v is not None:
                return v

    def query_vec(fn: str, kind: str, table: str) -> tuple[np.ndarray, str]:
        r = rng.random()
        if r < 0.35:
            return tie_vec(fn, kind), "tie_copy"
        if r < 0.45:
            v = random_stored(table)
            return (v + rng.standard_normal(v.size).astype(np.float32) * np.float32(0.01)), "near_copy"
        if r < 0.85:
            v = random_stored(table) + rng.standard_normal(build_db.DIMS).astype(np.float32) * np.float32(0.06)
            v /= np.linalg.norm(v)
            return v.astype(np.float32), "topic"
        v = rng.standard_normal(build_db.DIMS).astype(np.float32)
        return (v / np.linalg.norm(v)).astype(np.float32), "random"

    def ts_of(fn: str, kind: str) -> int:
        if fn == "passages":
            _, _, source, _, ts = build_db.PASSAGE_TABLES[kind]
            hi = conn.execute(f"SELECT max(id) FROM {source}").fetchone()[0]  # noqa: S608
            while True:
                r = conn.execute(f"SELECT {ts} FROM {source} WHERE id = ?",  # noqa: S608
                                 (int(rng.integers(1, hi + 1)),)).fetchone()  # fmt: skip
                if r:
                    return int(r[0])
        hi = conn.execute("SELECT max(rowid) FROM embeddings").fetchone()[0]
        while True:
            r = conn.execute("SELECT start_ms FROM embeddings WHERE rowid = ?",
                             (int(rng.integers(1, hi + 1)),)).fetchone()  # fmt: skip
            if r:
                return int(r[0])

    def window(fn: str, kind: str) -> tuple[int, int, str]:
        r = rng.random()
        lengths = [("1h", HOUR), ("day", DAY), ("week", 7 * DAY), ("month", 30 * DAY)]
        if r < 0.15:
            return 0, FAR, "all"
        if r < 0.85:
            name, length = lengths[int(rng.integers(len(lengths)))]
            s = int(rng.integers(t0 - DAY, t1))
            return s, s + length, name
        if r < 0.95:
            a, b = sorted((ts_of(fn, kind), ts_of(fn, kind)))
            if b - a > 20 * DAY:
                b = a + int(rng.integers(HOUR, 3 * DAY))
            return a, max(b, a + 1), "edge"
        if fn == "embeddings" and kind == "segment":
            sp = special["nan" if rng.random() < 0.5 else "inf"]
            return sp["start_ms"] - DAY, sp["start_ms"] + DAY, "special_row"
        return t1 + 10 * DAY, t1 + 11 * DAY, "empty"

    def owners_in(fn: str, q: dict) -> list[int]:
        if fn == "passages":
            column = build_db.PASSAGE_TABLES[q["kind"]][1]
            frm, where = passage_where(q["kind"], tuple(q["apps"]))
            sql = f"SELECT DISTINCT p.{column}" + frm + where
            return sorted(int(r[0]) for r in conn.execute(sql, passage_params(model, q)))
        return sorted(int(r[0]) for r in conn.execute(
            "SELECT owner_id FROM embeddings WHERE owner_kind = ? AND model = ? AND start_ms >= ?"
            " AND start_ms < ?", (q["owner_kind"], model, q["start"], q["end"])))  # fmt: skip

    def subset(ids: list[int], lo: float, hi: float) -> list[int]:
        if not ids:
            return []
        frac = rng.uniform(lo, hi)
        k = max(1, int(len(ids) * frac))
        return sorted(rng.choice(ids, k, replace=False).tolist())

    def only_choice(ids: list[int], probs: tuple[float, float, float, float]) -> tuple[list | None, str]:
        r = rng.random()
        p_none, p_sub, p_small, _p_empty = probs
        if r < p_none:
            return None, "none"
        if r < p_none + p_sub:
            return subset(ids, 0.1, 0.9), "subset"
        if r < p_none + p_sub + p_small:
            small = subset(ids, 0.0, 0.01)[:50]
            absent = [10**9 + int(x) for x in rng.integers(0, 1000, 5)]
            return sorted(set(small + absent)), "small+absent"
        return [], "empty"

    apps_pool = [(), (), (), ("chrome",), ("Discord.exe", "code"), ("CHROME.EXE", "Zen"),
                 ("nonexistent",), ("explorer", "windowsterminal.exe")]  # fmt: skip
    containers = ["set"] * 14 + ["list", "tuple", "frozenset", "generator"] + ["set"] * 2
    plan = ["pseg"] * 30 + ["pframe"] * 25 + ["eseg"] * 30 + ["eep"] * 15
    out = []
    for k in range(n):
        typ = plan[int(rng.integers(len(plan)))] if k >= 4 else ["pseg", "pframe", "eseg", "eep"][k]
        fn = "passages" if typ[0] == "p" else "embeddings"
        kind = {"pseg": "segment", "pframe": "frame", "eseg": "segment", "eep": "episode"}[typ]
        table = build_db.PASSAGE_TABLES[kind][0] if fn == "passages" else "embeddings"
        vec, vtag = query_vec(fn, kind, table)
        s, e, wtag = window(fn, kind)
        limit = [1, 30, 100, 200][int(rng.integers(4))]
        q: dict = {"i": k, "fn": fn, "q": vec, "start": s, "end": e, "limit": limit,
                   "tags": [typ, vtag, wtag, f"limit{limit}"]}  # fmt: skip
        if fn == "passages":
            q["kind"] = kind
            q["apps"] = apps_pool[int(rng.integers(len(apps_pool)))] if kind == "frame" else ()
            ids = owners_in(fn, q)
            probs = (0.45, 0.30, 0.15, 0.10) if kind == "segment" else (0.85, 0.1, 0.03, 0.02)
            q["only"], otag = only_choice(ids, probs)
            q["tags"] += [f"only_{otag}", f"apps{len(q['apps'])}"]
        else:
            q["owner_kind"] = kind
            q["model"] = model
            ids = owners_in(fn, q)
            if kind == "episode":
                q["only"], otag = only_choice(ids, (0.3, 0.6, 0.07, 0.03))
                q["exclude"], etag = [], "none"
            else:
                q["only"], otag = only_choice(ids, (0.5, 0.3, 0.12, 0.08))
                if rng.random() < 0.5:
                    base = q["only"] if q["only"] and rng.random() < 0.5 else ids
                    q["exclude"], etag = subset(base, 0.2, 0.6), "subset"
                else:
                    q["exclude"], etag = [], "none"
            q["tags"] += [f"only_{otag}", f"exclude_{etag}"]
        q["container"] = containers[int(rng.integers(len(containers)))]
        if limit in (30, 100) and rng.random() < 0.2:
            q["default_limit"] = (fn == "passages" and limit == 100) or (fn == "embeddings" and limit == 30)
        out.append(q)
    # Deterministic extras: unwindowed scans with exact top ties across chunk boundaries.
    for fn, kind in (("passages", "segment"), ("passages", "frame"), ("embeddings", "segment")):
        for limit in (1, 200):
            vec = tie_vec(fn, kind)
            q = {"i": len(out), "fn": fn, "q": vec, "start": 0, "end": FAR, "limit": limit,
                 "tags": ["extra_tie_all", f"limit{limit}"], "only": None, "container": "set"}  # fmt: skip
            if fn == "passages":
                q.update(kind=kind, apps=())
            else:
                q.update(owner_kind=kind, model=model, exclude=[])
            out.append(q)
    return out


def container(q: dict, ids):
    if ids is None:
        return None
    c = q["container"]
    return {"set": set, "list": list, "tuple": tuple, "frozenset": frozenset,
            "generator": lambda x: (i for i in list(x))}[c](ids)  # fmt: skip


def call(mod, model: str, q: dict):
    kw: dict = {"start_ms": q["start"], "end_ms": q["end"], "only": container(q, q["only"])}
    if not q.get("default_limit"):
        kw["limit"] = q["limit"]
    if q["fn"] == "passages":
        if q["kind"] != "segment":
            kw["kind"] = q["kind"]
        if q["apps"]:
            kw["apps"] = tuple(q["apps"])
        return mod.passages_top(conn_holder[0], model, q["q"], **kw)
    if q["exclude"]:
        kw["exclude"] = container(q, q["exclude"]) if q["container"] != "generator" else set(q["exclude"])
    return mod.embeddings_top(conn_holder[0], q["owner_kind"], model, q["q"], **kw)


conn_holder: list = [None]


def normalize(fn: str, res) -> tuple[list[tuple], int]:
    """(rows, type_errors). rows: (owner, score[, start_char, end_char])."""
    bad = 0 if isinstance(res, list) else 1
    out = []
    for r in res:
        if fn == "passages":
            vals = (r.owner_id, r.score, r.start_char, r.end_char)
            bad += not (type(vals[0]) is int and type(vals[1]) is float
                        and type(vals[2]) is int and type(vals[3]) is int)  # fmt: skip
            out.append((int(vals[0]), float(vals[1]), int(vals[2]), int(vals[3])))
        else:
            bad += not (type(r.owner_id) is int and type(r.score) is float)
            out.append((int(r.owner_id), float(r.score)))
    return out, bad


# --- exact oracle ---------------------------------------------------------------------------


def exact_dot(blob: bytes, q64: np.ndarray) -> float:
    """Correctly rounded dot of a float16 vector with the float32 query: each float16 x float32
    product is exact in float64 (11 + 24 bits), math.fsum sums them exactly."""
    v = np.frombuffer(bytes(blob), np.float16).astype(np.float64)
    with np.errstate(invalid="ignore", over="ignore"):
        prods = v * q64
    if not np.isfinite(prods).all():
        return float(prods.sum())  # inf or NaN, as float64 arithmetic gives it
    return math.fsum(prods.tolist())


def exact_rows(conn, model: str, q: dict, owner: int) -> list[tuple[int, int, float]]:
    """Eligible rows of one owner under the query's filters: (start_char, end_char, exact)."""
    only = q.get("_only_set")
    if only is not None and owner not in only:
        return []
    q64 = q["q"].astype(np.float64)
    want = len(q["q"]) * 2
    if q["fn"] == "passages":
        column = build_db.PASSAGE_TABLES[q["kind"]][1]
        frm, where = passage_where(q["kind"], tuple(q["apps"]))
        sql = "SELECT p.start_char, p.end_char, p.vec" + frm + where + f" AND p.{column} = :owner"
        rows = conn.execute(sql, {**passage_params(model, q), "owner": owner}).fetchall()
        return sorted((r[0], r[1], exact_dot(r[2], q64)) for r in rows if len(r[2]) == want)
    if owner in q["_exclude_set"]:
        return []
    rows = conn.execute(build_db.EMB_SQL + " AND owner_id = ?",
                        (q["owner_kind"], model, q["start"], q["end"], owner)).fetchall()  # fmt: skip
    return [(0, 0, exact_dot(r[1], q64)) for r in rows if len(r[1]) == want]


def same(a: float, b: float) -> bool:
    return a == b or (math.isnan(a) and math.isnan(b))


def gap(a: float | None, b: float | None) -> str:
    """'same' (exact tie), 'near' (< 1e-6 apart) or 'hard'."""
    if a is None or b is None:
        return "hard"
    if same(a, b):
        return "same"
    if not (math.isfinite(a) and math.isfinite(b)):
        return "hard"
    return "near" if abs(a - b) < NEAR else "hard"


# today_noise: the candidate differs from today only where exact scores are equal and the
# candidate follows the tie rule, i.e. today itself broke its rule (float32 rounding of
# identical vectors depends on their row position inside the BLAS gemv).
RANK = {"ok": 0, "today_noise": 1, "near_tie": 2, "tie_order": 3, "hard": 4, "error": 5}


class Oracle:
    def __init__(self, conn, model: str, q: dict) -> None:
        self.conn, self.model, self.q = conn, model, q
        self.rows: dict[int, list] = {}
        self._pos: dict[int, int] | None = None

    def ex(self, owner: int) -> list:
        if owner not in self.rows:
            self.rows[owner] = exact_rows(self.conn, self.model, self.q, owner)
        return self.rows[owner]

    def owner_max(self, owner: int) -> float | None:
        rows = self.ex(owner)
        if not rows:
            return None
        return max((r[2] for r in rows), key=lambda v: (not math.isnan(v), v))

    def passage(self, owner: int, sc: int) -> float | None:
        return next((r[2] for r in self.ex(owner) if r[0] == sc), None)

    def score_of(self, row: tuple) -> float | None:
        """Exact score of a returned row (the returned passage for passages)."""
        if self.q["fn"] == "passages":
            return self.passage(row[0], row[2])
        return self.owner_max(row[0])

    def key(self, owner: int) -> int:
        """Tie rule key: passages owner_id; embeddings position in today's scan order."""
        if self.q["fn"] == "passages":
            return owner
        if self._pos is None:
            q = self.q
            cur = self.conn.execute(build_db.EMB_SQL,
                                    (q["owner_kind"], self.model, q["start"], q["end"]))  # fmt: skip
            self._pos = {int(r[0]): k for k, r in enumerate(cur)}
        return self._pos[owner]


def compare(conn, model: str, q: dict, ref: list[tuple], cand: list[tuple]) -> tuple[str, list[float], dict]:  # noqa: C901, PLR0912, PLR0915
    o = Oracle(conn, model, q)
    rs = {r[0]: r[1] for r in ref}
    deltas: list[float] = []
    info: dict = {}
    for r in cand:
        base = rs.get(r[0])
        if base is None:
            base = o.owner_max(r[0])
            if base is None:
                return "hard", deltas, {"why": f"owner {r[0]} is not eligible"}
        d = 0.0 if same(r[1], base) else abs(r[1] - base)
        if math.isnan(d):
            d = math.inf
        deltas.append(d)
        if d > info.get("max_d", -1.0):
            info.update(max_d=d, max_owner=r[0], cand_score=r[1], ref_score=base)
    # Score error against the exact value (both lists), finite scores only.
    for name, lst in (("today_err", ref), ("cand_err", cand)):
        errs = [abs(r[1] - e) for r in lst if (e := o.score_of(r)) is not None
                and math.isfinite(e) and math.isfinite(r[1])]  # fmt: skip
        info[name] = max(errs, default=0.0)
    if len({r[0] for r in cand}) != len(cand):
        return "hard", deltas, {**info, "why": "duplicate owners"}
    if [r[:1] + r[2:] for r in ref] == [r[:1] + r[2:] for r in cand]:
        return "ok", deltas, info
    if len(ref) != len(cand):
        return "hard", deltas, {**info, "why": f"length {len(cand)} != today {len(ref)}"}
    worst = "ok"

    def bump(cls: str, why: str) -> None:
        nonlocal worst
        if RANK[cls] > RANK[worst]:
            worst = cls
            info["why"] = why

    e_c = [o.owner_max(r[0]) for r in cand]
    for k, (a, b) in enumerate(zip(ref, cand, strict=True)):
        if a[0] != b[0]:
            g = gap(o.owner_max(a[0]), e_c[k])
            if g == "hard":
                bump("hard", f"pos {k}: today owner {a[0]}, cand owner {b[0]}")
            elif g == "near":
                bump("near_tie", f"pos {k}: today owner {a[0]}, cand owner {b[0]} (< 1e-6)")
            else:
                bump("today_noise", f"pos {k}: exact tie today {a[0]} / cand {b[0]}")
        elif a[2:] != b[2:]:
            pa, pb = o.passage(a[0], a[2]), o.passage(b[0], b[2])
            g = gap(pa, pb)
            why = f"pos {k}: owner {a[0]} offsets today {a[2:]} cand {b[2:]}"
            if g == "hard":
                bump("hard", why)
            elif g == "near":
                bump("near_tie", why + " (< 1e-6)")
            else:
                first = min(r[0] for r in o.ex(b[0]) if same(r[2], pb))
                bump("today_noise" if b[2] == first else "tie_order", why + " (exact tie)")
    # Tie rule inside the candidate's own list: equal exact scores in key order.
    for k in range(1, len(cand)):
        if e_c[k] is not None and e_c[k - 1] is not None and same(e_c[k], e_c[k - 1]):
            if o.key(cand[k][0]) < o.key(cand[k - 1][0]):
                bump("tie_order", f"pos {k}: exact tie {cand[k - 1][0]} before {cand[k][0]}")
    # A cut through a tie group keeps the smaller keys.
    only_c = {r[0] for r in cand} - set(rs)
    only_r = set(rs) - {r[0] for r in cand}
    for x in only_c:
        ex_x = o.owner_max(x)
        for y in only_r:
            if ex_x is not None and gap(ex_x, o.owner_max(y)) == "same" and o.key(x) > o.key(y):
                bump("tie_order", f"cut: cand keeps {x}, today keeps {y} (exact tie)")
    return worst, deltas, info


def nan_eligible(q: dict, meta: dict) -> bool:
    if q["fn"] != "embeddings" or q["owner_kind"] != "segment":
        return False
    sp = meta["embeddings"]["special"]["nan"]
    if not q["start"] <= sp["start_ms"] < q["end"]:
        return False
    oid = sp["owner_id"]
    return (q["only"] is None or oid in set(q["only"])) and oid not in set(q["exclude"])


def nan_dropped_oracle(conn, model: str, q: dict) -> list[int]:
    """Stable top-limit over the scan with float32 scores, NaN rows dropped."""
    only = None if q["only"] is None else set(q["only"])
    excl = set(q["exclude"])
    ids, vecs = [], []
    for o, v in conn.execute(build_db.EMB_SQL, (q["owner_kind"], model, q["start"], q["end"])):
        if len(v) == len(q["q"]) * 2 and (only is None or o in only) and o not in excl:
            ids.append(o)
            vecs.append(bytes(v))
    if not ids:
        return []
    m = np.frombuffer(b"".join(vecs), np.float16).reshape(len(ids), -1).astype(np.float32)
    with np.errstate(invalid="ignore", over="ignore"):
        s = m @ q["q"].astype(np.float32)
    keep = ~np.isnan(s)
    order = np.argsort(-s[keep], kind="stable")[: q["limit"]]
    kept = np.asarray(ids)[keep]
    return [int(x) for x in kept[order]]


# --- timing ---------------------------------------------------------------------------------


def scenarios(conn, meta: dict) -> list[dict]:
    rng = np.random.default_rng(SEED + 1)
    model = meta["model"]
    v = rng.standard_normal(build_db.DIMS).astype(np.float32)
    row = conn.execute("SELECT vec FROM segment_passages WHERE rowid = 1000").fetchone()[0]
    v = np.frombuffer(bytes(row), np.float16).astype(np.float32) + 0.06 * v
    v = (v / np.linalg.norm(v)).astype(np.float32)

    def ids(sql: str, args) -> list[int]:
        return sorted({int(r[0]) for r in conn.execute(sql, args)})

    seg_owners = ids("SELECT segment_id FROM segment_passages WHERE model = ?", (model,))
    emb_owners = ids("SELECT owner_id FROM embeddings WHERE owner_kind = 'segment' AND model = ?",
                     (model,))  # fmt: skip
    pick = lambda xs, f: sorted(rng.choice(xs, int(len(xs) * f), replace=False).tolist())  # noqa: E731
    base = {"q": v, "start": 0, "end": FAR, "container": "set", "exclude": [], "apps": ()}
    return [
        {**base, "name": "passages/segment_all", "fn": "passages", "kind": "segment",
         "only": None, "limit": 100},
        {**base, "name": "passages/segment_only50", "fn": "passages", "kind": "segment",
         "only": pick(seg_owners, 0.5), "limit": 100},
        {**base, "name": "passages/frame_apps", "fn": "passages", "kind": "frame",
         "only": None, "limit": 100, "apps": ("chrome", "Discord.exe", "code")},
        {**base, "name": "embeddings/segment_all", "fn": "embeddings", "owner_kind": "segment",
         "only": None, "limit": 30},
        {**base, "name": "embeddings/segment_only60_excl20", "fn": "embeddings",
         "owner_kind": "segment", "only": pick(emb_owners, 0.6), "limit": 30,
         "exclude": pick(emb_owners, 0.2)},
    ]  # fmt: skip


def time_scenario(conn, model: str, sc: dict, cand, today, runs: int, log) -> dict:
    fetch_sql, fp, agg_sql, ap = scan_sqls(model, sc)

    def fetch() -> int:
        n = 0
        cur = conn.execute(fetch_sql, fp)
        while rows := cur.fetchmany(CHUNK):
            n += len(rows)
        return n

    def decode() -> None:
        conn.execute(agg_sql, ap).fetchone()

    fns = {"cand": lambda: call(cand, model, sc), "fetch": fetch, "decode": decode}
    if today is not None:
        fns["today"] = lambda: call(today, model, sc)
    rows = fetch()
    times: dict[str, list[float]] = {k: [] for k in fns}
    names = list(fns)
    for r in range(runs + 1):
        for name in names[r % len(names):] + names[: r % len(names)]:
            gc.collect()
            t = time.perf_counter()
            fns[name]()
            dt = time.perf_counter() - t
            if r:
                times[name].append(dt)
    med = {k: statistics.median(v) for k, v in times.items()}
    ns = lambda t: t / max(rows, 1) * 1e9  # noqa: E731
    out = {"rows": rows, "runs": runs, "median_s": med,
           "fetch_ns": ns(med["fetch"]), "decode_ns": ns(med["decode"]),
           "cand_total_ns": ns(med["cand"]), "cand_after_fetch_ns": ns(med["cand"] - med["fetch"])}  # fmt: skip
    if today is not None:
        out["today_total_ns"] = ns(med["today"])
        out["today_after_fetch_ns"] = ns(med["today"] - med["fetch"])
    log(f"  {sc['name']:38s} rows={rows:>8} decode={out['decode_ns']:7.0f} fetch={out['fetch_ns']:7.0f}"
        f" cand_total={out['cand_total_ns']:7.0f} cand_after_fetch={out['cand_after_fetch_ns']:7.0f}"
        + (f" today_total={out['today_total_ns']:7.0f} today_after_fetch={out['today_after_fetch_ns']:7.0f}"
           if today is not None else "") + " ns/row")  # fmt: skip
    return out


def peak_mb(model: str, sc: dict, mod) -> float:
    gc.collect()
    tracemalloc.start()
    tracemalloc.reset_peak()
    base = tracemalloc.get_traced_memory()[0]
    res = call(mod, model, sc)
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    del res
    return (peak - base) / 2**20


# --- main -----------------------------------------------------------------------------------


def arg(name: str, default: str) -> str:
    if name in sys.argv:
        return sys.argv[sys.argv.index(name) + 1]
    return default


def main() -> None:  # noqa: C901, PLR0912, PLR0915
    cand_path, out = sys.argv[1], Path(sys.argv[2])
    out.mkdir(parents=True, exist_ok=True)
    log = Tee(out / "run.log")
    sizes = arg("--sizes", "100k,1m").split(",")
    n_queries = int(arg("--queries", "300"))
    runs = int(arg("--runs", "9"))
    no_today = "--no-today" in sys.argv
    cand = load(cand_path)
    for line in verbatim_check():
        log("ref_today vs repo:", line)
    log(f"candidate={cand_path} sizes={sizes} queries={n_queries} runs={runs}")
    details: dict = {"candidate": str(cand_path), "sizes": {}}
    all_deltas: list[float] = []
    worst = {"d": -1.0}
    totals = {"mismatches": 0, "hard": 0, "tie_order": 0, "near_tie": 0, "today_noise": 0,
              "errors": 0, "type_errors": 0, "nan_scope_mismatches": 0, "queries": 0,
              "today_err_vs_exact": 0.0, "cand_err_vs_exact": 0.0}  # fmt: skip
    timing: dict[str, dict] = {}
    peaks: dict[str, float] = {}
    for size in sizes:
        meta = build_db.ensure(size)
        conn = build_db.connect(build_db.db_path(size))
        conn_holder[0] = conn
        model = meta["model"]
        t = time.perf_counter()
        queries = make_queries(conn, meta, n_queries)
        log(f"[{size}] {len(queries)} queries built in {time.perf_counter() - t:.1f}s")
        h = hashlib.sha256()
        h.update(Path(ref_today.__file__).read_bytes())
        h.update(build_db.meta_path(size).read_bytes())
        h.update(f"{QUERY_VERSION}:{n_queries}".encode())
        for q in queries:
            h.update(q["q"].tobytes())
            h.update(repr({k: v for k, v in q.items() if k != "q"}).encode())
        cache = build_db.CACHE / f"ref_{size}_{h.hexdigest()[:16]}.pkl"
        if cache.exists():
            refs = pickle.loads(cache.read_bytes())
            log(f"[{size}] today's results from {cache.name}")
        else:
            t = time.perf_counter()
            refs = [normalize(q["fn"], call(ref_today, model, q))[0] for q in queries]
            cache.write_bytes(pickle.dumps(refs))
            log(f"[{size}] today's results computed in {time.perf_counter() - t:.1f}s")
        for q in queries:  # after hashing: oracle helpers
            q["_only_set"] = None if q["only"] is None else set(q["only"])
            q["_exclude_set"] = set(q.get("exclude", ()))
        t = time.perf_counter()
        per = {k: 0 for k in RANK}
        by_tag: dict[str, dict[str, int]] = {}
        failures = []
        nan_info = []
        for q, ref in zip(queries, refs, strict=True):
            try:
                res, bad = normalize(q["fn"], call(cand, model, q))
                cls, deltas, info = compare(conn, model, q, ref, res)
            except Exception:  # noqa: BLE001
                cls, deltas, info, bad = "error", [], {"why": traceback.format_exc(limit=3)}, 0
            in_nan = nan_eligible(q, meta)
            if in_nan:
                oracle = nan_dropped_oracle(conn, model, q)
                nan_info.append({"i": q["i"], "today_equals_nan_dropped_oracle":
                                 [r[0] for r in ref] == oracle, "class": cls})  # fmt: skip
            per[cls] += 1
            for tag in q["tags"]:
                by_tag.setdefault(tag, {}).setdefault(cls, 0)
                by_tag[tag][cls] += 1
            totals["type_errors"] += bad
            bad_cls = cls in ("hard", "tie_order", "error")
            if in_nan and bad_cls:
                totals["nan_scope_mismatches"] += 1
            elif bad_cls or bad:
                totals["mismatches"] += 1
            if cls != "ok":
                totals["errors" if cls == "error" else cls] += 1
            if cls != "ok" or bad:
                failures.append({"size": size, "i": q["i"], "class": cls, "type_errors": bad,
                                 "tags": q["tags"], "nan_scope": in_nan, **{k: v for k, v in info.items()}})  # fmt: skip
            all_deltas.extend(deltas)
            for e in ("today_err", "cand_err"):
                totals[e + "_vs_exact"] = max(totals[e + "_vs_exact"], info.get(e, 0.0))
            if info.get("max_d", -1.0) > worst["d"]:
                worst = {"d": info["max_d"], "size": size, "query": q["i"], "tags": q["tags"],
                         "owner": info["max_owner"], "cand": info["cand_score"], "today": info["ref_score"]}  # fmt: skip
        totals["queries"] += len(queries)
        log(f"[{size}] compared in {time.perf_counter() - t:.1f}s: {per}")
        for f in failures[:15]:
            log("   ", json.dumps(f, default=str)[:400])
        if nan_info:
            ok = sum(x["today_equals_nan_dropped_oracle"] for x in nan_info)
            log(f"[{size}] NaN-row scope: {len(nan_info)} queries; today == NaN-dropped stable"
                f" oracle in {ok}/{len(nan_info)}")  # fmt: skip
        # Timing.
        today_cache = build_db.CACHE / f"today_timing_{size}.json"
        cached_today = json.loads(today_cache.read_text("utf-8")) if no_today and today_cache.exists() else None
        log(f"[{size}] timing ({runs} runs, median; ns per scanned row)")
        timing[size] = {}
        for sc in scenarios(conn, meta):
            r = time_scenario(conn, model, sc, cand, None if cached_today else ref_today, runs, log)
            if cached_today:
                r["today_total_ns"] = cached_today[sc["name"]]["today_total_ns"]
                r["today_after_fetch_ns"] = cached_today[sc["name"]]["today_after_fetch_ns"]
                r["today_from_cache"] = True
            timing[size][sc["name"]] = r
        if not cached_today:
            today_cache.write_text(json.dumps({k: {kk: vv for kk, vv in v.items() if kk.startswith("today")}
                                               for k, v in timing[size].items()}), "utf-8")  # fmt: skip
        if size == sizes[-1]:
            for sc in scenarios(conn, meta):
                if sc["name"] in ("passages/segment_all", "embeddings/segment_all"):
                    peaks[sc["name"]] = peak_mb(model, sc, cand)
                    peaks["today:" + sc["name"]] = peak_mb(model, sc, ref_today)
            log(f"[{size}] peak extra MB (tracemalloc): " + ", ".join(f"{k}={v:.1f}" for k, v in peaks.items()))
        details["sizes"][size] = {"per_class": per, "by_tag": by_tag, "failures": failures[:200],
                                  "nan_scope": nan_info, "timing": timing[size],
                                  "plans": {k: meta[k]["plan_unwindowed"] for k in
                                            ("passages_segment", "passages_frame", "embeddings")}}  # fmt: skip
        conn.close()

    # metrics line
    def worst_after(size: str, fn: str, who: str = "cand") -> dict:
        rows = [v for k, v in timing[size].items() if k.startswith(fn)]
        return max(rows, key=lambda r: r[f"{who}_after_fetch_ns"])

    parts = [f"mismatches={totals['mismatches']}", f"hard={totals['hard']}",
             f"tie_order={totals['tie_order']}", f"near_tie={totals['near_tie']}",
             f"today_noise={totals['today_noise']}",
             f"errors={totals['errors']}", f"type_errors={totals['type_errors']}",
             f"nan_scope_mismatches={totals['nan_scope_mismatches']}",
             f"max_abs_dscore={max(worst['d'], 0):.3g}",
             f"cand_err_vs_exact={totals['cand_err_vs_exact']:.3g}",
             f"today_err_vs_exact={totals['today_err_vs_exact']:.3g}"]  # fmt: skip
    for size in sizes:
        for fn in ("passages", "embeddings"):
            w = worst_after(size, fn)
            parts.append(f"{fn}_after_fetch_ns_{size}={w['cand_after_fetch_ns']:.0f}")
            wt = worst_after(size, fn, "today")
            parts.append(f"{fn}_today_after_fetch_ns_{size}={wt['today_after_fetch_ns']:.0f}")
            parts.append(f"{fn}_fetch_floor_ns_{size}={w['fetch_ns']:.0f}")
            parts.append(f"{fn}_decode_ns_{size}={w['decode_ns']:.0f}")
    pk = [v for k, v in peaks.items() if not k.startswith("today:")]
    pkt = [v for k, v in peaks.items() if k.startswith("today:")]
    parts.append(f"peak_MB={max(pk) if pk else float('nan'):.1f}")
    parts.append(f"today_peak_MB={max(pkt) if pkt else float('nan'):.1f}")
    line = " ".join(parts)
    (out / "metrics.txt").write_text(line + "\n", "utf-8")
    details.update(totals=totals, worst=worst, peaks_MB=peaks, metrics=line)
    (out / "details.json").write_text(json.dumps(details, indent=1, default=str), "utf-8")
    log("worst |dscore|:", json.dumps(worst, default=str))
    log("METRICS", line)
    plot(out, sizes, timing, all_deltas, worst, Path(cand_path).name)


def plot(out: Path, sizes: list[str], timing: dict, deltas: list[float], worst: dict, name: str) -> None:  # noqa: C901
    plt.style.use("dark_background")
    fig, (ax, bx) = plt.subplots(1, 2, figsize=(14, 5.2), gridspec_kw={"width_ratios": [1.5, 1]})
    colors = {"decode": "#4c78a8", "cursor": "#9d9d9d", "after": "#f58518"}
    x, labels = 0.0, []
    ticks = []
    for size in sizes:
        for fn in ("passages", "embeddings"):
            rows = {k: v for k, v in timing[size].items() if k.startswith(fn)}
            wname = max(rows, key=lambda k: rows[k]["cand_after_fetch_ns"])
            w = rows[wname]  # the candidate's worst scenario; today on the same scenario
            for k, (who, r) in enumerate((("today", w), ("cand", w))):
                dec = r["decode_ns"] / 1e3
                cur = max(r["fetch_ns"] - r["decode_ns"], 0) / 1e3
                aft = r[f"{who}_after_fetch_ns"] / 1e3
                xi = x + k * 0.38
                ax.bar(xi, dec, 0.36, color=colors["decode"], label="SQLCipher step+decode" if x == 0 and k == 0 else None)
                ax.bar(xi, cur, 0.36, bottom=dec, color=colors["cursor"], label="cursor objects (fetch floor - decode)" if x == 0 and k == 0 else None)
                ax.bar(xi, aft, 0.36, bottom=dec + cur, color=colors["after"], alpha=0.55 if who == "today" else 1.0,
                       label="Python after fetch" if x == 0 and k == 1 else None)  # fmt: skip
                ax.text(xi, dec + cur + aft, f"{aft:.2f}", ha="center", va="bottom", fontsize=7)
                ticks.append((xi, who))
            floor = (w["fetch_ns"]) / 1e3
            ax.plot([x - 0.2, x + 0.58], [floor + 0.5, floor + 0.5], "--", color="#e45756", lw=1.2,
                    label="budget: fetch floor + 0.5 us" if x == 0 else None)  # fmt: skip
            labels.append((x + 0.19, f"{fn} {size}\n{wname.split('/')[1]}"))
            x += 1.1
    ax.set_xticks([t[0] for t in ticks], [t[1] for t in ticks], fontsize=7)
    for xm, lab in labels:
        ax.text(xm, -0.12, lab, transform=ax.get_xaxis_transform(), ha="center", va="top", fontsize=9)
    ax.set_ylabel("us per scanned row (candidate's worst scenario per group)")
    ax.set_title(f"per-row cost: today vs {name}")
    ax.set_ylim(0, ax.get_ylim()[1] * 1.3)
    ax.legend(fontsize=7, loc="upper center", ncol=2)
    d = np.asarray(deltas, dtype=np.float64)
    zeros = int((d == 0).sum())
    nz = d[(d > 0) & np.isfinite(d)]
    inf = int(np.isinf(d).sum())
    bins = np.logspace(-12, 0, 49)
    if nz.size:
        bx.hist(np.clip(nz, 1e-12, 1), bins=bins, color="#72b7b2")
    bx.set_xscale("log")
    bx.set_xlim(1e-12, 1)  # same axes for every candidate
    bx.axvline(NEAR, color="#e45756", ls="--", lw=1.2, label="contract 1e-6")
    bx.set_title(f"|score cand - today| per returned owner\nzero: {zeros}, nonzero: {nz.size}, inf/nan: {inf}, "
                 f"max {max(worst['d'], 0):.2g}", fontsize=9)  # fmt: skip
    bx.set_xlabel("|delta score| (exact zeros counted in title)")
    bx.legend(fontsize=7)
    fig.tight_layout()
    fig.savefig(out / "side.png", dpi=90, bbox_inches="tight")


if __name__ == "__main__":
    main()
