"""C5 sighting checks and run-expansion cost (own bench; harness.py untouched).

A. Two sightings: page P seen 09:00-09:10 and 18:00-18:10 (5 s frames) among other browsing.
   Whole-day window -> 2 slots for P; morning window -> 1 slot. Keyword and meaning-only queries.
B. Run-expansion cost on one key visible all day (17,280 frames, 5 s, same text/title/url):
   runs_of() alone and full query_frames (whole day), vs today's query_frames on the same data.
C. FAILS WHEN illustrations: revisits every 20 min (many runs, cap R=2), and one continuous
   viewing captured every 16 min (gaming-throttled) split into several runs.
"""
import shutil
import statistics
import sys
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import fake_embed  # noqa: E402
import fixtures  # noqa: E402
import harness  # noqa: E402
import ref_today  # noqa: E402

cand = harness.load(HERE / "cand.py")
work = HERE / "_sbench"
shutil.rmtree(work, ignore_errors=True)
work.mkdir()
MIDNIGHT = fixtures.DAY0_MS - 8 * 3_600_000  # 2026-11-09T00:00Z
H, M, S5 = 3_600_000, 60_000, 5_000
MARK = "sightingmarker"
g = fixtures._Gen(42)  # noqa: SLF001
P_TEXT = "Patch notes " + MARK + " quête serveur\n" + g.block(3_000)
P_TITLE, P_URL = "Patch notes 3.2 - Zen", "https://example.org/patch-3-2"
lines: list[str] = []


def frame(ts: int, text: str, title: str, url: str | None) -> dict:
    return fixtures._frame(ts, text, title, url, "zen.exe")  # noqa: SLF001


def span(t0: int, t1: int, step: int = S5) -> range:
    return range(t0, t1 + 1, step)


def timed(fn, reps: int = 9) -> tuple[float, object]:  # noqa: ANN001
    out = fn()
    ts = []
    for _ in range(reps):
        t = time.perf_counter()
        fn()
        ts.append(time.perf_counter() - t)
    return statistics.median(ts) * 1000, out


def p_slots(ctx, res) -> list[tuple]:  # noqa: ANN001
    out = []
    for fid, ts, extra in res:
        row = ctx.conn.execute("SELECT text, window_title FROM frames WHERE id = ?", (fid,)).fetchone()
        if row[0] == P_TEXT and row[1] == P_TITLE:
            out.append((fid, ts, extra))
    return out


def hm(ts: int) -> str:
    m = (ts - MIDNIGHT) // M
    return f"{m // 60:02d}:{m % 60:02d}:{(ts - MIDNIGHT) % M // 1000:02d}"


# --- A. two sightings ---------------------------------------------------------------------------
filler = fixtures.build(seed=0)["mixed"][:400]
frames = [frame(t, P_TEXT, P_TITLE, P_URL) for t in span(MIDNIGHT + 9 * H, MIDNIGHT + 9 * H + 10 * M)]
frames += [{**f, "ts_ms": MIDNIGHT + 12 * H + i * S5} for i, f in enumerate(filler)]
frames += [frame(t, P_TEXT, P_TITLE, P_URL) for t in span(MIDNIGHT + 18 * H, MIDNIGHT + 18 * H + 10 * M)]
frames.sort(key=lambda f: f["ts_ms"])
ctx = harness.open_db(cand, work, "sight")
emb = fake_embed.FakeEmbedder(harness.MODEL)
model = harness.populate(cand, ctx, frames, emb)
rng = np.random.default_rng(3)
queries = {"keyword": MARK, "meaning-only": harness.sem_query(P_TEXT[40:], rng)}
windows_ = {"whole day": (MIDNIGHT, MIDNIGHT + 24 * H), "morning": (MIDNIGHT, MIDNIGHT + 12 * H)}
expect = {"whole day": 2, "morning": 1}
lines.append(f"A. two sightings: P = 121 frames 09:00-09:10 + 121 frames 18:00-18:10; 400 filler frames 12:00-12:33; "
             f"embed texts sent={emb.texts} (P embedded once: {len(fake_embed.windows(P_TEXT))} windows)")
ok_all = True
for qn, q in queries.items():
    for wn, (lo, hi) in windows_.items():
        res = cand.query_frames(ctx, q, lo, hi)
        ps = p_slots(ctx, res)
        ok = len(ps) == expect[wn]
        ok_all &= ok
        desc = "; ".join(
            f"rep {hm(ts)} run {hm(e['first_ts'])}-{hm(e['last_ts'])} n={e['count']}" for _, ts, e in ps
        )
        lines.append(f"  {qn:12s} {wn:9s}: slots={len(res)} P-slots={len(ps)} (expect {expect[wn]}) {'PASS' if ok else 'FAIL'} | {desc}")
# a single P frame is still reachable at its own timestamp
pid = next(i for i, f in model.frames.items() if f["text"] == P_TEXT and f["ts_ms"] == MIDNIGHT + 9 * H + 5 * M)
r = cand.query_frames(ctx, MARK, MIDNIGHT + 9 * H + 5 * M, MIDNIGHT + 9 * H + 5 * M + 1)
lines.append(f"  [ts, ts+1) at 09:05:00 -> {[(x[0], hm(x[1])) for x in r]} own id {pid}: {'PASS' if r and r[0][0] == pid else 'FAIL'}")
lines.append(f"  A overall: {'PASS' if ok_all else 'FAIL'}")
harness.close(ctx)

# --- B. one key visible all day ----------------------------------------------------------------
K_TEXT = "Discord vocal général " + g.block(2_000)
allday = [frame(t, K_TEXT, "general - Discord", None) for t in range(MIDNIGHT, MIDNIGHT + 24 * H, S5)]
others = [{**f, "ts_ms": f["ts_ms"] + 1} for f in fixtures.build(seed=0)["mixed"][:200]]  # interleaved
day = sorted(allday + others, key=lambda f: f["ts_ms"])
lines.append(f"B. run expansion: key visible all day = {len(allday)} frames (5 s, 24 h) + {len(others)} other frames")
res_b = {}
for name, mod in (("C5", cand), ("today", ref_today)):
    c = harness.open_db(mod, work, f"allday_{name}")
    e = fake_embed.FakeEmbedder(harness.MODEL)
    t = time.perf_counter()
    for f in day:
        mod.insert_frame(c, dict(f))
    t_ins = time.perf_counter() - t
    t = time.perf_counter()
    mod.embed_pending(c, e)
    t_emb = time.perf_counter() - t
    rows = len(harness.vectors(c.conn)[0])
    q_kw, q_sem = "vocal général", harness.sem_query(K_TEXT, np.random.default_rng(1))
    lo, hi = MIDNIGHT, MIDNIGHT + 24 * H
    ms_kw, out_kw = timed(lambda: mod.query_frames(c, q_kw, lo, hi))  # noqa: B023
    ms_sem, out_sem = timed(lambda: mod.query_frames(c, q_sem, lo, hi))  # noqa: B023
    extra = ""
    if mod is cand:
        key = cand.text_key(c, K_TEXT)
        ms_runs, runs = timed(lambda: cand.runs_of(c.conn, key, "general - Discord", None, lo, hi))  # noqa: B023
        ms_raw, _ = timed(lambda: c.conn.execute(  # noqa: B023
            "SELECT ts_ms FROM frames WHERE text_key = ? AND ts_ms >= ? AND ts_ms < ?", (key, lo, hi)).fetchall())  # noqa: B023
        k_slots = [x for x in out_kw if x[2]["count"] > 1]
        extra = (f" runs_of={ms_runs:.2f} ms -> {len(runs)} run(s) {[(hm(a), hm(b), n) for a, b, n in runs]};"
                 f" plain key range fetch (no title/url filter, no window fn)={ms_raw:.2f} ms;"
                 f" kw slots: {[(hm(x[1]), x[2]['count']) for x in out_kw]}")
    lines.append(f"  {name:5s}: insert {t_ins / len(day) * 1000:.3f} ms/frame, embed {t_emb:.1f} s, vec rows={rows},"
                 f" embed texts={e.texts}; query kw p50={ms_kw:.1f} ms, sem p50={ms_sem:.1f} ms{extra}")
    harness.close(c)

# --- C. FAILS WHEN illustrations ----------------------------------------------------------------
c = harness.open_db(cand, work, "revisit")
rev = []
for k in range(72):  # 1 min every 20 min, all day
    t0 = MIDNIGHT + k * 20 * M
    rev += [frame(t, P_TEXT, P_TITLE, P_URL) for t in span(t0, t0 + M)]
harness.populate(cand, c, rev, fake_embed.FakeEmbedder(harness.MODEL))
key = cand.text_key(c, P_TEXT)
runs = cand.runs_of(c.conn, key, P_TITLE, P_URL, MIDNIGHT, MIDNIGHT + 24 * H)
res = cand.query_frames(c, MARK, MIDNIGHT, MIDNIGHT + 24 * H)
lines.append(f"C1. revisits 1 min every 20 min: {len(runs)} runs in the day, slots shown={len(res)} "
             f"({[hm(x[1]) for x in res]}); {len(runs) - len(res)} sightings hidden by R=2 (only reachable with a narrower window)")
harness.close(c)
c = harness.open_db(cand, work, "throttle")
thr = [frame(t, P_TEXT, P_TITLE, P_URL) for t in span(MIDNIGHT + 20 * H, MIDNIGHT + 21 * H, 16 * M)]
harness.populate(cand, c, thr, fake_embed.FakeEmbedder(harness.MODEL))
runs = cand.runs_of(c.conn, key, P_TITLE, P_URL, MIDNIGHT, MIDNIGHT + 24 * H)
res = cand.query_frames(c, MARK, MIDNIGHT, MIDNIGHT + 24 * H)
lines.append(f"C2. one 1 h viewing captured every 16 min (throttled): {len(thr)} frames -> {len(runs)} runs; "
             f"slots={[(hm(x[1]), hm(x[2]['first_ts']), x[2]['count']) for x in res]} (one viewing shown as {len(res)} sightings)")
harness.close(c)

shutil.rmtree(work, ignore_errors=True)
out = "\n".join(lines)
print(out)
(HERE / "bench_sighting.txt").write_text(out + "\n", "utf-8")
