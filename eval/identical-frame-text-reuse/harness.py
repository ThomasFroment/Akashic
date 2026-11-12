"""Measurement harness for identical-frame-text-reuse candidates.

    python harness.py <candidate.py> <outdir> [--keep]

The candidate module provides the function set documented in ref_today.py. Today's pipeline
(ref_today.py) is always run alongside it as the baseline (once, if the candidate is the
reference). Writes <outdir>/metrics.txt (one line), <outdir>/metrics.json (details) and
<outdir>/side.png. Databases are encrypted SQLCipher files under <outdir>/_work, removed at the
end unless --keep.

Measured (structural: the fake embedder stands in for llama-server):
- embedding work: texts and characters sent per fixture vs the ideal (sum of windows over
  distinct texts); stored vector rows/bytes (every BLOB whose length is a multiple of 512, i.e.
  256-dim float16 vectors, in every non-FTS table) and their reduction vs today;
- deletion: orphan vectors after delete_frame / set_text / expire_frame_text /
  expire_embeddings / delete_all (a vector is an orphan when no window of a still-eligible text
  explains it at cosine >= 0.999); rollback: each path interrupted (progress handler) at 20/50/80 %
  must leave single-transaction paths unchanged and batched paths consistent, and a rerun must
  reach the uninterrupted final state;
- findability: 50 sampled frames, each queried with the window [ts, ts+1) by words and by meaning
  only, must come back with their own id and timestamp;
- result slots: distinct (text, title, url) groups in the top 5 == min(5, groups in window);
- cost: added write cost per stored frame (p50/p99 over 10k inserts, interleaved with today);
  query p50/p95 (median of 9 per query) vs today;
- privacy: no unkeyed digest (sha256/sha1/md5/blake2b of any fixture text or window) in any
  decrypted DB value; raw DB/WAL bytes start encrypted and contain no fixture sentinel.
"""

from __future__ import annotations

import hashlib
import hmac
import importlib.util
import json
import re
import shutil
import sys
import time
from pathlib import Path
from types import ModuleType

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import fake_embed  # noqa: E402
import fixtures  # noqa: E402
import ref_today  # noqa: E402

MODEL = "fake-qwen3-256"
DIM = fake_embed.DIM
VEC_BYTES = DIM * 2
COS_MATCH = 0.999
MIN_TEXT_CHARS = 12
EMBED_EVERY = 60  # housekeeping cadence in the fixtures: one embed pass per 5 min of capture
_TOKEN = re.compile(r"\w+", re.UNICODE)
NONSENSE = "zzqqxvk"
DATA_KEY = hashlib.sha256(b"identical-frame-text-reuse harness data key").digest()


# --- keys, context ----------------------------------------------------------------------------


def hkdf(key: bytes, purpose: str, length: int = 32) -> bytes:
    """HKDF-SHA256 (RFC 5869), zero salt, info "akashic/<purpose>/v1" like crypto.subkey."""
    prk = hmac.new(b"\x00" * 32, key, hashlib.sha256).digest()
    out, block, i = b"", b"", 1
    while len(out) < length:
        block = hmac.new(prk, block + f"akashic/{purpose}/v1".encode() + bytes([i]), hashlib.sha256).digest()
        out += block
        i += 1
    return out[:length]


DB_KEY = hkdf(DATA_KEY, "database")
_QVEC: dict[str, np.ndarray] = {}


def query_vector(q_text: str) -> np.ndarray:
    vec = _QVEC.get(q_text)
    if vec is None:
        vec = _QVEC[q_text] = fake_embed.clean_vector(q_text)
    return vec


class Ctx:
    def __init__(self, conn, path: Path) -> None:  # noqa: ANN001
        self.conn = conn
        self.path = path
        self.model = MODEL
        self.hmac_key = hkdf(DATA_KEY, "frame-text-key")
        self.query_vector = query_vector

    @staticmethod
    def subkey(purpose: str) -> bytes:
        return hkdf(DATA_KEY, purpose)


def load(path: str | Path) -> ModuleType:
    path = Path(path).resolve()
    spec = importlib.util.spec_from_file_location(f"cand_{path.stem}", path)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def open_db(mod: ModuleType, work: Path, name: str, *, schema_of: ModuleType | None = None) -> Ctx:
    path = work / f"{name}.db"
    for suffix in ("", "-wal", "-shm"):
        Path(str(path) + suffix).unlink(missing_ok=True)
    ctx = Ctx(ref_today.connect(path, DB_KEY), path)
    (schema_of or mod).migrate(ctx)
    return ctx


def close(ctx: Ctx) -> None:
    ctx.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    ctx.conn.close()


# --- the expected state, kept in Python (candidate-agnostic) ------------------------------------


class Model:
    def __init__(self) -> None:
        self.frames: dict[int, dict[str, object]] = {}
        self.vec_cutoff = -(2**62)

    def eligible_texts(self) -> set[str]:
        return {
            str(f["text"])
            for f in self.frames.values()
            if f["text"] is not None and len(str(f["text"])) >= MIN_TEXT_CHARS
            and int(f["ts_ms"]) >= self.vec_cutoff  # type: ignore[arg-type]
        }

    def group(self, frame_id: int) -> tuple[object, object, object]:
        f = self.frames[frame_id]
        return (f["text"], f["window_title"], f["url"])


def populate(mod: ModuleType, ctx: Ctx, frames: list[dict[str, object]], embedder, *, embed: bool = True) -> Model:  # noqa: ANN001
    model = Model()
    for n, frame in enumerate(frames, start=1):
        frame_id = mod.insert_frame(ctx, dict(frame))
        model.frames[frame_id] = dict(frame)
        if embed and n % EMBED_EVERY == 0:
            mod.embed_pending(ctx, embedder)
    if embed:
        mod.embed_pending(ctx, embedder)
    return model


# --- structural scans ---------------------------------------------------------------------------


def user_tables(conn, *, with_shadow: bool = False) -> list[str]:  # noqa: ANN001
    rows = conn.execute("SELECT name, sql FROM sqlite_master WHERE type = 'table'").fetchall()
    virtual = [n for n, sql in rows if sql and sql.upper().startswith("CREATE VIRTUAL TABLE")]
    out = []
    for name, _ in rows:
        if name.startswith("sqlite_") or name in virtual:
            continue
        if not with_shadow and any(name.startswith(v + "_") for v in virtual):
            continue
        out.append(name)
    return sorted(out)


def fts_tables(conn) -> list[str]:  # noqa: ANN001
    rows = conn.execute("SELECT name, sql FROM sqlite_master WHERE type = 'table'").fetchall()
    return [n for n, sql in rows if sql and "USING FTS5" in sql.upper()]


def columns(conn, table: str) -> list[str]:  # noqa: ANN001
    return [str(r[1]) for r in conn.execute(f'PRAGMA table_info("{table}")')]


def vectors(conn) -> tuple[np.ndarray, int]:  # noqa: ANN001
    """Every stored 256-dim float16 vector (BLOBs of length k*512 in non-FTS tables)."""
    parts: list[np.ndarray] = []
    total = 0
    for table in user_tables(conn):
        for col in columns(conn, table):
            for (blob,) in conn.execute(
                f'SELECT "{col}" FROM "{table}" WHERE typeof("{col}") = \'blob\''  # noqa: S608
                f' AND length("{col}") % {VEC_BYTES} = 0 AND length("{col}") > 0'
            ):
                raw = bytes(blob)
                total += len(raw)
                parts.append(np.frombuffer(raw, dtype=np.float16).reshape(-1, DIM))
    if not parts:
        return np.zeros((0, DIM), np.float32), 0
    return np.vstack(parts).astype(np.float32), total


def db_bytes(conn) -> int:  # noqa: ANN001
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    page = int(conn.execute("PRAGMA page_size").fetchone()[0])
    count = int(conn.execute("PRAGMA page_count").fetchone()[0])
    free = int(conn.execute("PRAGMA freelist_count").fetchone()[0])
    return int((count - free) * page)


def orphans(conn, model: Model) -> int:  # noqa: ANN001
    stored, _ = vectors(conn)
    if not len(stored):
        return 0
    wins = {t[s:e] for t in model.eligible_texts() for s, e in fake_embed.windows(t)}
    if not wins:
        return len(stored)
    ref = np.vstack([fake_embed.clean_vector(w) for w in wins])
    ref /= np.linalg.norm(ref, axis=1, keepdims=True)
    stored = stored / np.maximum(np.linalg.norm(stored, axis=1, keepdims=True), 1e-12)
    best = np.full(len(stored), -1.0, np.float32)
    for at in range(0, len(ref), 4096):
        best = np.maximum(best, (stored @ ref[at : at + 4096].T).max(axis=1))
    return int((best < COS_MATCH).sum())


def fts_ok(conn) -> bool:  # noqa: ANN001
    try:
        for t in fts_tables(conn):
            conn.execute(f"INSERT INTO \"{t}\" (\"{t}\") VALUES ('integrity-check')")  # noqa: S608
    except Exception:  # noqa: BLE001
        return False
    return True


def fingerprint(conn, *, vec_bytes: bool = True) -> str:  # noqa: ANN001
    """Hash of every non-FTS table's rows (order-independent). Vector BLOBs optionally reduced to
    their count (re-embedding after an interrupt gives a different jitter)."""
    h = hashlib.sha256()
    for table in user_tables(conn):
        rows = []
        for row in conn.execute(f'SELECT * FROM "{table}"'):  # noqa: S608
            cells = []
            for v in row:
                if isinstance(v, (bytes, memoryview)) and len(v) % VEC_BYTES == 0 and not vec_bytes:
                    cells.append(f"vec*{len(v) // VEC_BYTES}")
                else:
                    cells.append(repr(bytes(v)) if isinstance(v, memoryview) else repr(v))
            rows.append("|".join(cells))
        h.update(table.encode() + b"\n" + "\n".join(sorted(rows)).encode() + b"\n")
    return h.hexdigest()


def rows_left(conn) -> int:  # noqa: ANN001
    return sum(conn.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0] for t in user_tables(conn))  # noqa: S608


# --- queries ------------------------------------------------------------------------------------


def kw_query(text: str, rng: np.random.Generator) -> str:
    tokens = _TOKEN.findall(text)
    i = int(rng.integers(0, max(1, len(tokens) - 2)))
    return " ".join(tokens[i : i + 2])


def sem_query(text: str, rng: np.random.Generator) -> str:
    """Words of the text plus a token found nowhere: FTS matches nothing, meaning still does."""
    tokens = _TOKEN.findall(text)
    i = int(rng.integers(0, max(1, len(tokens) - 6)))
    return " ".join([*tokens[i : i + 6], NONSENSE])


def query_set(fx: dict[str, list[dict[str, object]]], seed: int = 7) -> tuple[list[tuple[str, int, int]], list[tuple[str, int, int]]]:
    """(timed queries, extra group-metric queries): (q_text, start_ms, end_ms)."""
    rng = np.random.default_rng(seed)
    timed: list[tuple[str, int, int]] = []
    extra: list[tuple[str, int, int]] = []
    for name in fixtures.NAMES:
        frames = fx[name]
        lo, hi = int(frames[0]["ts_ms"]), int(frames[-1]["ts_ms"]) + fixtures.STEP_MS  # type: ignore[arg-type]
        for make in (kw_query, sem_query):
            f = frames[int(rng.integers(0, len(frames)))]
            timed.append((make(str(f["text"]), rng), lo, hi))
            s = int(rng.integers(lo, max(lo + 1, hi - 600_000)))
            sub = [g for g in frames if s <= int(g["ts_ms"]) < s + 600_000] or frames  # type: ignore[arg-type]
            f = sub[int(rng.integers(0, len(sub)))]
            timed.append((make(str(f["text"]), rng), s, s + 600_000))
            for _ in range(2):
                s = int(rng.integers(lo, max(lo + 1, hi - 120_000)))
                sub = [g for g in frames if s <= int(g["ts_ms"]) < s + 120_000] or frames  # type: ignore[arg-type]
                f = sub[int(rng.integers(0, len(sub)))]
                extra.append((make(str(f["text"]), rng), s, s + 120_000))
    every = [f for name in fixtures.NAMES for f in fx[name]]
    day = (fixtures.DAY0_MS, fixtures.DAY0_MS + 6 * 3_600_000)
    cross = (fixtures.DAY0_MS + 50 * 60_000, fixtures.DAY0_MS + 90 * 60_000)  # video end + static + mixed start
    for make in (kw_query, sem_query):
        timed.append((make(str(every[int(rng.integers(0, len(every)))]["text"]), rng), *day))
        static = fx["static"][0]
        timed.append((make(str(static["text"]), rng), *cross))
    return timed, extra


def timed_query(mod: ModuleType, ctx: Ctx, q: tuple[str, int, int], reps: int = 9) -> tuple[float, list]:
    result = mod.query_frames(ctx, *q)  # warm-up (and memoised query vector)
    times = []
    for _ in range(reps):
        t = time.perf_counter()
        mod.query_frames(ctx, *q)
        times.append(time.perf_counter() - t)
    return float(np.median(times)) * 1000, result


# --- phases -------------------------------------------------------------------------------------


def phase_storage(mod: ModuleType, work: Path, fx: dict[str, list[dict[str, object]]], tag: str) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for name in fixtures.NAMES:
        emb = fake_embed.FakeEmbedder(MODEL)
        ctx = open_db(mod, work, f"{tag}_fx_{name}")
        populate(mod, ctx, fx[name], emb)
        stored, nbytes = vectors(ctx.conn)
        distinct = {str(f["text"]) for f in fx[name] if len(str(f["text"])) >= MIN_TEXT_CHARS}
        out[name] = {
            "frames": len(fx[name]),
            "distinct_texts": len(distinct),
            "sent": emb.texts,
            "sent_chars": emb.chars,
            "calls": emb.calls,
            "ideal": sum(len(fake_embed.windows(t)) for t in distinct),
            "today_windows": sum(len(fake_embed.windows(str(f["text"]))) for f in fx[name]),
            "vec_rows": len(stored),
            "vec_bytes": nbytes,
            "db_bytes": db_bytes(ctx.conn),
        }
        close(ctx)
    return out


def _straddling_cutoff(model: Model) -> int:
    """A cutoff inside a run of identical video frames (a text group older and newer than it)."""
    ids = sorted(model.frames, key=lambda i: int(model.frames[i]["ts_ms"]))  # type: ignore[arg-type]
    for a, b, c in zip(ids, ids[1:], ids[2:], strict=False):
        fa, fb, fc = model.frames[a], model.frames[b], model.frames[c]
        if fa["process"] == "zen.exe" and fa["text"] == fb["text"] == fc["text"] and int(fa["ts_ms"]) > fixtures.DAY0_MS + 200_000:  # type: ignore[arg-type]
            return int(fb["ts_ms"])  # type: ignore[arg-type]
    return int(model.frames[ids[len(ids) // 2]]["ts_ms"])  # type: ignore[arg-type]


def _found(mod: ModuleType, ctx: Ctx, frame_id: int, ts: int, q: str) -> bool:
    return any(int(r[0]) == frame_id and int(r[1]) == ts for r in mod.query_frames(ctx, q, ts, ts + 1))


def _dup_frame(model: Model) -> tuple[int, list[int]]:
    """A video frame whose text group has other members; returns (frame, the other members)."""
    by_text: dict[object, list[int]] = {}
    for i, f in model.frames.items():
        if f["process"] == "zen.exe" and f["text"]:
            by_text.setdefault(model.group(i), []).append(i)
    members = next(v for v in by_text.values() if len(v) >= 4)  # noqa: PLR2004
    return members[1], [m for m in members if m != members[1]]


def phase_deletion(mod: ModuleType, work: Path, small: list[dict[str, object]], tag: str) -> dict[str, object]:
    res: dict[str, object] = {}
    rng = np.random.default_rng(11)

    def fresh(name: str, *, embed: bool = True) -> tuple[Ctx, Model, fake_embed.FakeEmbedder]:
        emb = fake_embed.FakeEmbedder(MODEL)
        ctx = open_db(mod, work, f"{tag}_del_{name}")
        return ctx, populate(mod, ctx, small, emb, embed=embed), emb

    # delete one frame of a duplicate group: survivors stay findable
    ctx, model, _ = fresh("dup")
    victim, mates = _dup_frame(model)
    mod.delete_frame(ctx, victim)
    model.frames.pop(victim)
    res["delete_frame_dup_orphans"] = orphans(ctx.conn, model)
    res["survivors_findable"] = sum(
        _found(mod, ctx, m, int(model.frames[m]["ts_ms"]), sem_query(str(model.frames[m]["text"]), rng))  # type: ignore[arg-type]
        for m in mates[:5]
    )
    res["survivors_checked"] = min(5, len(mates))
    close(ctx)

    # delete every frame of a unique-text group (the static page) and 5 unique mixed frames
    ctx, model, _ = fresh("group")
    static_ids = [i for i, f in model.frames.items() if "docs.python.org" in str(f["url"])]
    counts: dict[object, int] = {}
    for i in model.frames:
        counts[model.frames[i]["text"]] = counts.get(model.frames[i]["text"], 0) + 1
    unique_mixed = [i for i, f in model.frames.items() if f["process"] != "zen.exe" and counts[f["text"]] == 1][:5]
    for i in static_ids + unique_mixed:
        mod.delete_frame(ctx, i)
        model.frames.pop(i)
    res["delete_frame_group_orphans"] = orphans(ctx.conn, model)
    res["delete_frame_group_fts_ok"] = fts_ok(ctx.conn)
    close(ctx)

    # set_text on a duplicate frame: old vectors leave with the old text, the new text embeds
    ctx, model, emb = fresh("settext")
    victim, mates = _dup_frame(model)
    new_text = "Texte remplacé par OCR différé " + str(model.frames[victim]["text"])[200:3000]
    mod.set_text(ctx, victim, new_text, "ocr")
    model.frames[victim]["text"] = new_text
    res["set_text_orphans_before_embed"] = orphans(ctx.conn, model)
    mod.embed_pending(ctx, emb)
    res["set_text_orphans"] = orphans(ctx.conn, model)
    ts = int(model.frames[victim]["ts_ms"])  # type: ignore[arg-type]
    res["set_text_findable"] = int(_found(mod, ctx, victim, ts, sem_query(new_text, rng)))
    res["set_text_mates_findable"] = sum(
        _found(mod, ctx, m, int(model.frames[m]["ts_ms"]), sem_query(str(model.frames[m]["text"]), rng))  # type: ignore[arg-type]
        for m in mates[:3]
    )
    close(ctx)

    # expire_frame_text with a cutoff inside a group of identical frames
    ctx, model, _ = fresh("expire_text")
    cutoff = _straddling_cutoff(model)
    mod.expire_frame_text(ctx, cutoff)
    old = [i for i, f in model.frames.items() if int(f["ts_ms"]) < cutoff]  # type: ignore[arg-type]
    texts_before = {i: str(model.frames[i]["text"]) for i in old}
    for i in old:
        model.frames[i]["text"] = None
    res["expire_frame_text_orphans"] = orphans(ctx.conn, model)
    sample = old[-10:]
    res["expired_frames_returned"] = sum(
        bool(mod.query_frames(ctx, sem_query(texts_before[i], rng), int(model.frames[i]["ts_ms"]), int(model.frames[i]["ts_ms"]) + 1))  # type: ignore[arg-type]
        for i in sample
    )
    close(ctx)

    # expire_embeddings with a cutoff inside a group of identical frames
    ctx, model, _ = fresh("expire_vec")
    cutoff = _straddling_cutoff(model)
    mod.expire_embeddings(ctx, cutoff)
    model.vec_cutoff = cutoff
    res["expire_embeddings_orphans"] = orphans(ctx.conn, model)
    old = [i for i, f in model.frames.items() if int(f["ts_ms"]) < cutoff][-10:]  # type: ignore[arg-type]
    res["stale_semantic_hits"] = sum(
        _found(mod, ctx, i, int(model.frames[i]["ts_ms"]), sem_query(str(model.frames[i]["text"]), rng))  # type: ignore[arg-type]
        for i in old
    )
    close(ctx)

    # full delete
    ctx, model, _ = fresh("all")
    mod.delete_all(ctx)
    model.frames.clear()
    stored, _ = vectors(ctx.conn)
    res["delete_all_vectors_left"] = len(stored)
    res["delete_all_rows_left"] = rows_left(ctx.conn)
    res["delete_all_fts_ok"] = fts_ok(ctx.conn)
    close(ctx)

    res["orphans_total"] = sum(
        int(res[k]) for k in (
            "delete_frame_dup_orphans", "delete_frame_group_orphans", "set_text_orphans_before_embed",
            "set_text_orphans", "expire_frame_text_orphans", "expire_embeddings_orphans",
            "delete_all_vectors_left",
        )
    )
    return res


class _Interrupt:
    """Progress handler: counts VM callbacks, interrupts once at ``at`` (0 = never)."""

    def __init__(self, at: int = 0) -> None:
        self.at, self.n, self.fired = at, 0, False

    def __call__(self) -> int:
        self.n += 1
        if self.at and not self.fired and self.n >= self.at:
            self.fired = True
            return 1
        return 0


def phase_rollback(mod: ModuleType, work: Path, small: list[dict[str, object]], tag: str) -> dict[str, object]:
    def setup(name: str, *, embed: bool) -> tuple[Ctx, Model, fake_embed.FakeEmbedder]:
        emb = fake_embed.FakeEmbedder(MODEL)
        ctx = open_db(mod, work, f"{tag}_rb_{name}")
        return ctx, populate(mod, ctx, small, emb, embed=embed), emb

    def op_delete_frame(ctx: Ctx, model: Model, emb) -> None:  # noqa: ANN001
        victim, _ = _dup_frame(model)
        mod.delete_frame(ctx, victim)

    def op_set_text(ctx: Ctx, model: Model, emb) -> None:  # noqa: ANN001
        victim, _ = _dup_frame(model)
        mod.set_text(ctx, victim, "nouveau texte " + str(model.frames[victim]["text"])[100:2000], "ocr")

    def op_expire_text(ctx: Ctx, model: Model, emb) -> None:  # noqa: ANN001
        mod.expire_frame_text(ctx, _straddling_cutoff(model))

    def op_expire_vec(ctx: Ctx, model: Model, emb) -> None:  # noqa: ANN001
        mod.expire_embeddings(ctx, _straddling_cutoff(model))

    def op_delete_all(ctx: Ctx, model: Model, emb) -> None:  # noqa: ANN001
        mod.delete_all(ctx)

    def op_embed(ctx: Ctx, model: Model, emb) -> None:  # noqa: ANN001
        mod.embed_pending(ctx, emb)

    # (name, op, single transaction?, build embedded?, compare vector bytes?)
    paths = (
        ("delete_frame", op_delete_frame, True, True, True),
        ("set_text", op_set_text, True, True, True),
        ("delete_all", op_delete_all, True, True, True),
        ("expire_frame_text", op_expire_text, False, True, True),
        ("expire_embeddings", op_expire_vec, False, True, True),
        ("embed_pending", op_embed, False, False, False),
    )
    detail: dict[str, object] = {}
    ok = total = 0
    for name, op, single, embedded, vec_bytes in paths:
        ctx, model, emb = setup(name, embed=embedded)
        counter = _Interrupt()
        ctx.conn.set_progress_handler(counter, 64)
        op(ctx, model, emb)
        ctx.conn.set_progress_handler(None, 0)
        final = fingerprint(ctx.conn, vec_bytes=vec_bytes)
        steps = counter.n
        close(ctx)
        for frac in (0.2, 0.5, 0.8):
            ctx, model, emb = setup(name, embed=embedded)
            before = fingerprint(ctx.conn, vec_bytes=vec_bytes)
            handler = _Interrupt(max(1, int(steps * frac)))
            ctx.conn.set_progress_handler(handler, 64)
            raised = False
            try:
                op(ctx, model, emb)
            except Exception:  # noqa: BLE001
                raised = True
            ctx.conn.set_progress_handler(None, 0)
            open_tx = ctx.conn.in_transaction
            if open_tx:
                ctx.conn.execute("ROLLBACK")
            after = fingerprint(ctx.conn, vec_bytes=vec_bytes)
            consistent = orphans(ctx.conn, model) == 0 and fts_ok(ctx.conn)
            if single:
                unchanged = after == before if raised else after == final
            else:
                unchanged = True
            op(ctx, model, emb)  # retry
            converged = fingerprint(ctx.conn, vec_bytes=vec_bytes) == final
            passed = unchanged and consistent and converged and not open_tx
            detail[f"{name}@{frac}"] = {
                "raised": raised, "unchanged": unchanged, "consistent": consistent,
                "converged": converged, "left_open_tx": open_tx, "steps": steps,
            }
            ok += passed
            total += 1
            close(ctx)
    return {"rollback_ok": ok, "rollback_total": total, "rollback_detail": detail}


def phase_upgrade(mod: ModuleType, work: Path, small: list[dict[str, object]], tag: str) -> dict[str, object]:
    if not hasattr(mod, "upgrade_from_today") or mod is ref_today:
        return {"upgrade": "n/a"}
    rng = np.random.default_rng(13)
    ctx = open_db(ref_today, work, f"{tag}_upgrade")
    model = populate(ref_today, ctx, small, fake_embed.FakeEmbedder(MODEL))
    emb = fake_embed.FakeEmbedder(MODEL, seed=0)
    t = time.perf_counter()
    mod.upgrade_from_today(ctx)
    mod.embed_pending(ctx, emb)
    seconds = time.perf_counter() - t
    sample = list(model.frames)[:: max(1, len(model.frames) // 20)][:20]
    found = sum(
        _found(mod, ctx, i, int(model.frames[i]["ts_ms"]), sem_query(str(model.frames[i]["text"]), rng))  # type: ignore[arg-type]
        and _found(mod, ctx, i, int(model.frames[i]["ts_ms"]), kw_query(str(model.frames[i]["text"]), rng))  # type: ignore[arg-type]
        for i in sample
    )
    out = {
        "upgrade": "ok",
        "upgrade_orphans": orphans(ctx.conn, model),
        "upgrade_findable": f"{found}/{len(sample)}",
        "upgrade_embed_texts": emb.texts,
        "upgrade_seconds": round(seconds, 3),
        "upgrade_vec_rows": len(vectors(ctx.conn)[0]),
    }
    close(ctx)
    return out


def phase_write(mod: ModuleType, work: Path, n: int = 10_000) -> dict[str, float]:
    stream = fixtures.write_stream(n)
    ctx_ref = open_db(ref_today, work, "write_ref")
    ctx_cand = open_db(mod, work, "write_cand")
    t_ref: list[int] = []
    t_cand: list[int] = []
    block = 500
    for at in range(0, n, block):
        for ctx, m, sink in ((ctx_ref, ref_today, t_ref), (ctx_cand, mod, t_cand)):
            for frame in stream[at : at + block]:
                t = time.perf_counter_ns()
                m.insert_frame(ctx, dict(frame))
                sink.append(time.perf_counter_ns() - t)
    close(ctx_ref)
    close(ctx_cand)
    r, c = np.array(t_ref) / 1e6, np.array(t_cand) / 1e6
    return {
        "write_today_p50_ms": float(np.percentile(r, 50)),
        "write_today_p99_ms": float(np.percentile(r, 99)),
        "write_p50_ms": float(np.percentile(c, 50)),
        "write_p99_ms": float(np.percentile(c, 99)),
        "write_add_p50_ms": float(np.percentile(c, 50) - np.percentile(r, 50)),
        "write_add_p99_ms": float(np.percentile(c, 99) - np.percentile(r, 99)),
    }


def phase_queries(mod: ModuleType, work: Path, fx: dict[str, list[dict[str, object]]], same: bool) -> dict[str, object]:
    every = [f for name in fixtures.NAMES for f in fx[name]]
    ctx_ref = open_db(ref_today, work, "combined_ref")
    model_ref = populate(ref_today, ctx_ref, every, fake_embed.FakeEmbedder(MODEL))
    if same:
        ctx_cand, model = ctx_ref, model_ref
    else:
        ctx_cand = open_db(mod, work, "combined_cand")
        model = populate(mod, ctx_cand, every, fake_embed.FakeEmbedder(MODEL))
    timed, extra = query_set(fx)

    t_ref, t_cand = [], []
    results_ref, results_cand = [], []
    for q in timed:
        ms, res = timed_query(ref_today, ctx_ref, q)
        t_ref.append(ms)
        results_ref.append(res)
        if same:
            t_cand.append(ms)
            results_cand.append(res)
        else:
            ms, res = timed_query(mod, ctx_cand, q)
            t_cand.append(ms)
            results_cand.append(res)
    for q in extra:
        results_ref.append(ref_today.query_frames(ctx_ref, *q))
        results_cand.append(results_ref[-1] if same else mod.query_frames(ctx_cand, *q))

    by_ts = {int(f["ts_ms"]): i for i, f in model.frames.items()}  # type: ignore[arg-type]
    groups_ok = 0
    mean_groups = []
    window_violations = ts_mismatch = 0
    top1_agree = 0
    per_query = []
    for q, rc, rr in zip(timed + extra, results_cand, results_ref, strict=True):
        _, lo, hi = q
        in_window = {model.group(i) for i, f in model.frames.items() if lo <= int(f["ts_ms"]) < hi and f["text"]}  # type: ignore[arg-type]
        got = []
        for frame_id, ts, _extra in rc:
            if not lo <= int(ts) < hi:
                window_violations += 1
            if int(frame_id) not in model.frames or int(model.frames[int(frame_id)]["ts_ms"]) != int(ts):  # type: ignore[arg-type]
                ts_mismatch += 1
                continue
            got.append(model.group(int(frame_id)))
        distinct = len(set(got))
        expect = min(5, len(in_window))
        groups_ok += distinct == expect
        mean_groups.append(distinct)
        ref_top = model_ref.group(int(rr[0][0])) if rr else None
        cand_top = got[0] if got else None
        top1_agree += ref_top == cand_top
        per_query.append({"window_min": (hi - lo) / 60_000, "groups": distinct, "expect": expect, "n": len(rc)})

    rng = np.random.default_rng(5)
    picks = []
    for name, k in (("video", 15), ("static", 10), ("mixed", 15), ("twovid", 10)):
        picks.extend(fx[name][int(i)] for i in rng.choice(len(fx[name]), k, replace=False))
    kw = sem = 0
    for f in picks:
        ts = int(f["ts_ms"])  # type: ignore[arg-type]
        fid = by_ts[ts]
        kw += _found(mod, ctx_cand, fid, ts, kw_query(str(f["text"]), rng))
        sem += _found(mod, ctx_cand, fid, ts, sem_query(str(f["text"]), rng))

    out: dict[str, object] = {
        "q_p50_ms": float(np.percentile(t_cand, 50)),
        "q_p95_ms": float(np.percentile(t_cand, 95)),
        "q_today_p50_ms": float(np.percentile(t_ref, 50)),
        "q_today_p95_ms": float(np.percentile(t_ref, 95)),
        "q_ratio_p50": float(np.percentile(t_cand, 50) / np.percentile(t_ref, 50)),
        "q_ratio_p95": float(np.percentile(t_cand, 95) / np.percentile(t_ref, 95)),
        "groups_ok": groups_ok,
        "groups_total": len(per_query),
        "mean_groups_top5": float(np.mean(mean_groups)),
        "top1_agree": top1_agree,
        "window_violations": window_violations,
        "ts_mismatch": ts_mismatch,
        "findable_kw": kw,
        "findable_sem": sem,
        "findable_n": len(picks),
        "per_query": per_query,
        "q_times_ms": {"cand": t_cand, "today": t_ref},
    }
    out.update(privacy_scan(ctx_cand, fx))
    path = ctx_cand.path
    close(ctx_cand)
    if not same:
        close(ctx_ref)
    out.update(raw_file_scan(path))
    return out


# --- privacy ------------------------------------------------------------------------------------


def _digest_sets(fx: dict[str, list[dict[str, object]]]) -> tuple[set[bytes], set[str], set[int]]:
    texts = {str(f["text"]) for frames in fx.values() for f in frames if f["text"]}
    wins = {t[s:e] for t in texts for s, e in fake_embed.windows(t)}
    raw: set[bytes] = set()
    for t in texts | wins:
        data = t.encode("utf-8")
        for algo in (hashlib.sha256, hashlib.sha1, hashlib.md5, hashlib.blake2b, hashlib.blake2s):
            raw.add(algo(data).digest())
        raw.add(hashlib.blake2b(data, digest_size=32).digest())
        raw.add(hashlib.sha256(t.encode("utf-16-le")).digest())
    hexes = {d.hex() for d in raw}
    ints = set()
    for d in raw:
        for order in ("big", "little"):
            ints.add(int.from_bytes(d[:8], order, signed=True))  # type: ignore[arg-type]
    return raw, hexes, ints


def privacy_scan(ctx: Ctx, fx: dict[str, list[dict[str, object]]]) -> dict[str, int]:
    raw, hexes, ints = _digest_sets(fx)
    sha256_only = {d for d in raw if len(d) == 32}  # noqa: PLR2004
    hexre = re.compile(r"[0-9a-fA-F]{32,128}")
    hits = 0
    conn = ctx.conn
    own = set(user_tables(conn))
    for table in user_tables(conn, with_shadow=True):
        for row in conn.execute(f'SELECT * FROM "{table}"'):  # noqa: S608
            for v in row:
                if isinstance(v, (bytes, memoryview)):
                    b = bytes(v)
                    if b in raw:
                        hits += 1
                    elif table in own and len(b) % VEC_BYTES and len(b) <= 1 << 20:
                        # a digest embedded inside a larger value (e.g. key || something)
                        hits += sum(b[i : i + 32] in sha256_only for i in range(len(b) - 31))
                elif isinstance(v, str):
                    for m in hexre.findall(v):
                        lo = m.lower()
                        hits += lo in hexes or any(lo[:n] in hexes for n in (32, 40, 64, 128))
                elif isinstance(v, int) and v in ints:
                    hits += 1
    return {"unkeyed_hash_hits": hits}


def raw_file_scan(path: Path) -> dict[str, int]:
    leaks = 0
    header_plain = 0
    for p in (path, Path(str(path) + "-wal")):
        if not p.exists():
            continue
        data = p.read_bytes()
        if p == path and data.startswith(b"SQLite format 3\x00"):
            header_plain = 1
        leaks += sum(s.encode() in data for s in fixtures.SENTINELS)
    return {"raw_plaintext_header": header_plain, "raw_sentinel_hits": leaks}


# --- report -------------------------------------------------------------------------------------


def plot(path: Path, name: str, storage: dict, storage_ref: dict, q: dict, w: dict) -> None:
    import matplotlib  # noqa: PLC0415

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt  # noqa: PLC0415

    plt.style.use("dark_background")
    fig, ax = plt.subplots(1, 2, figsize=(12, 4.2), gridspec_kw={"width_ratios": [1.4, 1]})
    names = list(fixtures.NAMES)
    x = np.arange(len(names))
    today = [storage_ref[n]["vec_rows"] for n in names]
    cand = [storage[n]["vec_rows"] for n in names]
    ax[0].bar(x - 0.2, today, 0.4, label="today", color="#8a8f98")
    ax[0].bar(x + 0.2, cand, 0.4, label=name, color="#4ea1ff")
    for i, (t, c) in enumerate(zip(today, cand, strict=True)):
        red = 100 * (1 - c / t) if t else 0.0
        ax[0].text(i + 0.2, max(c, 1), f"{c:,}\n-{red:.0f}%", ha="center", va="bottom", fontsize=8)
    ax[0].set_yscale("log")
    ax[0].set_ylim(1, max(today + cand) * 8)
    ax[0].set_xticks(x, names)
    ax[0].set_ylabel("stored vector rows (log)")
    ax[0].set_title("vector rows per fixture vs today")
    ax[0].legend(loc="upper left", fontsize=8, ncols=2)

    labels = ["query p50\n(ratio to today)", "query p95\n(ratio to today)", "write added p99\n(/ 1 ms budget)"]
    vals = [q["q_ratio_p50"], q["q_ratio_p95"], max(w["write_add_p99_ms"], 0.0) / 1.0]
    notes = [
        f"{q['q_p50_ms']:.1f} vs {q['q_today_p50_ms']:.1f} ms",
        f"{q['q_p95_ms']:.1f} vs {q['q_today_p95_ms']:.1f} ms",
        f"+{w['write_add_p99_ms']:.3f} ms (p99 {w['write_p99_ms']:.2f})",
    ]
    colors = ["#4ea1ff" if v <= 1.0 else "#ff6b6b" for v in vals]
    ax[1].barh(range(3), vals, color=colors)
    ax[1].axvline(1.0, color="#e0e0e0", ls="--", lw=1)
    for i, (v, n) in enumerate(zip(vals, notes, strict=True)):
        ax[1].text(v + 0.02, i, n, va="center", fontsize=8)
    ax[1].set_yticks(range(3), labels, fontsize=8)
    ax[1].set_xlim(0, max(2.0, max(vals) * 1.6))
    ax[1].invert_yaxis()
    ax[1].set_title("cost vs budget (dashed = budget)")
    fig.suptitle(f"identical-frame-text-reuse: {name}", fontsize=10)
    fig.tight_layout()
    fig.savefig(path, dpi=90)
    plt.close(fig)


def main() -> None:
    if len(sys.argv) < 3:  # noqa: PLR2004
        print(__doc__)
        sys.exit(2)
    cand_path = Path(sys.argv[1]).resolve()
    out = Path(sys.argv[2]).resolve()
    keep = "--keep" in sys.argv[3:]
    out.mkdir(parents=True, exist_ok=True)
    work = out / "_work"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir()
    same = cand_path == Path(ref_today.__file__).resolve()
    mod = ref_today if same else load(cand_path)
    name = cand_path.stem
    t0 = time.perf_counter()

    fx = fixtures.build()
    small = fixtures.small()
    storage_ref = phase_storage(ref_today, work, fx, "ref")
    storage = storage_ref if same else phase_storage(mod, work, fx, "cand")
    deletion = phase_deletion(mod, work, small, "cand")
    rollback = phase_rollback(mod, work, small, "cand")
    upgrade = phase_upgrade(mod, work, small, "cand")
    write = phase_write(mod, work)
    queries = phase_queries(mod, work, fx, same)

    for n in fixtures.NAMES:
        t = storage_ref[n]["vec_rows"]
        storage[n]["reduction_vs_today"] = 1 - storage[n]["vec_rows"] / t if t else 0.0
    details = {
        "candidate": name, "storage": storage, "storage_today": storage_ref, "deletion": deletion,
        "rollback": rollback, "upgrade": upgrade, "write": write, "queries": queries,
        "seconds": round(time.perf_counter() - t0, 1),
    }
    (out / "metrics.json").write_text(json.dumps(details, indent=1, default=str), "utf-8")

    parts = [f"cand={name}"]
    for n in fixtures.NAMES:
        s = storage[n]
        parts.append(
            f"{n}:sent={s['sent']}/ideal={s['ideal']} rows={s['vec_rows']} MB={s['vec_bytes'] / 1e6:.2f}"
            f" red={100 * s['reduction_vs_today']:.1f}%"
        )
    parts += [
        f"orphans={deletion['orphans_total']}",
        f"rollback={rollback['rollback_ok']}/{rollback['rollback_total']}",
        f"survivors={deletion['survivors_findable']}/{deletion['survivors_checked']}",
        f"expired_returned={deletion['expired_frames_returned']}",
        f"stale_sem={deletion['stale_semantic_hits']}",
        f"rows_after_delete_all={deletion['delete_all_rows_left']}",
        f"findable_kw={queries['findable_kw']}/{queries['findable_n']}",
        f"findable_sem={queries['findable_sem']}/{queries['findable_n']}",
        f"groups_ok={queries['groups_ok']}/{queries['groups_total']}",
        f"mean_groups={queries['mean_groups_top5']:.2f}",
        f"top1_agree={queries['top1_agree']}/{queries['groups_total']}",
        f"win_viol={queries['window_violations']} ts_mismatch={queries['ts_mismatch']}",
        f"write_add_p50={write['write_add_p50_ms']:.3f}ms write_add_p99={write['write_add_p99_ms']:.3f}ms",
        f"q_p50={queries['q_p50_ms']:.1f}ms(today {queries['q_today_p50_ms']:.1f})",
        f"q_p95={queries['q_p95_ms']:.1f}ms(today {queries['q_today_p95_ms']:.1f})",
        f"unkeyed_hash={queries['unkeyed_hash_hits']}",
        f"raw_leak={queries['raw_sentinel_hits'] + queries['raw_plaintext_header']}",
        f"upgrade={upgrade.get('upgrade')}",
    ]
    line = " ".join(parts)
    (out / "metrics.txt").write_text(line + "\n", "utf-8")
    plot(out / "side.png", name, storage, storage_ref, queries, write)
    if not keep:
        shutil.rmtree(work, ignore_errors=True)
    print(line)
    print(f"({details['seconds']} s)")


if __name__ == "__main__":
    main()
