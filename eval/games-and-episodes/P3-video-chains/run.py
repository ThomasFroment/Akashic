"""Driver for P3 (video chains): P sweep, incremental derivation check, synthetic adversarial set,
per-video figures, then the harness's side.png / metrics.txt / metrics.json for the chosen P with
chain counts appended. Writes only into this folder. Prints numbers, app names and harness ids only.

    uv run python eval/games-and-episodes/P3-video-chains/run.py
"""

from __future__ import annotations

import json
import statistics
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
HARNESS = HERE.parent / "harness"
sys.path.insert(0, str(HARNESS))
sys.path.insert(0, str(HERE))

import ge  # noqa: E402
import harness  # noqa: E402

import candidate as cand  # noqa: E402
from akashic.pipeline import episodes as production  # noqa: E402

SWEEP = (30_000, 180_000, 600_000)
CHOSEN = 180_000
STEP_MS = 5 * 60_000
CLOSE_DELAY_MS = 2_000  # a row is closed about one media poll after its end
IOU = 0.5

_cache: dict[str, ge.Run] = {}
_orig_load = ge.load


def cached_load(rid):
    if rid not in _cache:
        _cache[rid] = _orig_load(rid)
    return _cache[rid]


ge.load = cached_load


def iou(a0, a1, b0, b1):
    inter = max(0, min(a1, b1) - max(a0, b0))
    return inter / max(1, (a1 - a0) + (b1 - b0) - inter)


# --- real runs: per-P scoring -------------------------------------------------------------------


def video_detail(run, eps):
    out = []
    for v in run.videos:
        cands = [(iou(e[2], e[3], v.start, v.end), e) for e in eps if e[1] == "video"]
        overl = [e for e in eps if e[1] in ("video", "music") and e[2] < v.end and e[3] > v.start]
        best = max(cands, default=(0.0, None), key=lambda x: x[0])
        e = best[1]
        out.append({
            "vid": v.vid, "truth_s": round((v.end - v.start) / 1000), "episodes_overlapping": len(overl),
            "best_iou": round(best[0], 3),
            "matched": best[0] >= IOU,
            "title_ok": bool(e and best[0] >= IOU and v.title.lower() in (e[4] or "").lower()),
            "start_err_s": round(abs(e[2] - v.start) / 1000, 1) if e else None,
            "end_err_s": round(abs(e[3] - v.end) / 1000, 1) if e else None,
            "end_signed_s": round((e[3] - v.end) / 1000, 1) if e else None,
        })
    return out


def score_p(rid, p_ms):
    cand.P_MS = p_ms
    cand.STATS["calls"].clear()
    run = ge.load(rid)
    res = harness.score_run(run, cand)
    calls = [c for c in cand.STATS["calls"] if c["run"] == run.ctx.run_id]
    mg = statistics.median(c["media_ms"] + c["games_ms"] for c in calls)
    detail = video_detail(run, res["_eps"])
    return {
        "f1_video": res["f1_video"], "f1_game": res["f1_game"], "title_acc": res["title_acc"],
        "video_title_acc": sum(d["title_ok"] for d in detail) / len(detail) if detail else None,
        "video_eps": sum(1 for e in res["_eps"] if e[1] == "video"),
        "media_rows": run.shadow.execute("SELECT count(*) FROM media_sessions").fetchone()[0],
        "counts": calls[-1]["counts"], "media_game_ms": mg, "media_game_ms_per_day": mg / run.hours * 24,
        "media_ms_per_day": statistics.median(c["media_ms"] for c in calls) / run.hours * 24,
        "videos": detail, "_eps": res["_eps"],
    }


def production_media_eps(run):
    conn = harness.episode_conn(run, [tuple(r) for r in run.ctx.stored_games])
    xs = []
    for _ in range(5):
        c = ge.fresh(conn)
        t = time.perf_counter()
        production.build_media(c, production.horizon_ms(c, run.ctx.now_ms))
        xs.append((time.perf_counter() - t) * 1000)
    eps = c.execute("SELECT id, kind, start_ms, end_ms, title FROM episodes").fetchall()
    return eps, statistics.median(xs) / run.hours * 24


# --- incremental derivation ---------------------------------------------------------------------


def snapshot_rows(rows, events, gaps, t):
    ms, ev = [], []
    for r in rows:
        rid_, app_id, app, title, artist, url, s, e, played, _closed, *extra = r
        if s > t:
            continue
        e2 = min(e, t)
        closed = 1 if t >= e + CLOSE_DELAY_MS else 0
        p2 = played if closed or e <= s else int(played * (e2 - s) / (e - s))
        ms.append((rid_, app_id, app, title, artist, url, s, e2, p2, closed, *extra))
    keep = {m[0] for m in ms}
    ev = [x for x in events if x[1] in keep and x[2] <= t]
    gp = [(i, k, rsn, a, b if b is not None and b <= t else None) for i, k, rsn, a, b in gaps if a <= t]
    return ms, ev, gp


def episodes_snapshot(conn):
    eps = conn.execute("SELECT id, kind, ref, start_ms, end_ms, title FROM episodes WHERE kind IN ('video', 'music')").fetchall()
    items = conn.execute("SELECT episode_id, item_id FROM episode_items WHERE item_kind = 'media_session'").fetchall()
    by = {}
    for eid, iid in items:
        by.setdefault(eid, []).append(iid)
    return {ref: (kind, a, b, title, tuple(sorted(by.get(eid, [])))) for eid, kind, ref, a, b, title in eps}


def media_tables(conn):
    cols = [r[1] for r in conn.execute("PRAGMA table_info(media_sessions)")]
    rows = conn.execute(f"SELECT {', '.join(cols)} FROM media_sessions ORDER BY id").fetchall()  # noqa: S608
    events = conn.execute("SELECT id, media_session_id, ts_ms, state FROM media_events").fetchall()
    gaps = conn.execute("SELECT id, kind, reason, start_ms, end_ms FROM capture_gaps").fetchall()
    return cols, rows, events, gaps


def clear(conn):
    for t in ("episode_people", "episode_items", "episodes", "media_events", "media_sessions", "capture_gaps"):
        conn.execute(f"DELETE FROM {t}")  # noqa: S608
    conn.execute("DELETE FROM state WHERE key = ?", (cand.CHAINS_SINCE,))
    conn.execute("DELETE FROM jobs")


def incremental(base_conn, t_start, t_end, p_ms, **kw):
    """Derive every STEP_MS over [t_start, t_end + 1 h] on a growing store; compare with one pass."""
    cols, rows, events, gaps = media_tables(base_conn)
    marks = ",".join("?" * len(cols))
    inc = ge.fresh(base_conn)
    clear(inc)
    passes, attempts_ok, totals, first_seen = 0, 0, {}, {}
    t = t_start + STEP_MS
    while t <= t_end + 3_600_000:
        ms, ev, gp = snapshot_rows(rows, events, gaps, t)
        for tb in ("media_events", "media_sessions", "capture_gaps"):
            inc.execute(f"DELETE FROM {tb}")  # noqa: S608
        inc.executemany(f"INSERT INTO media_sessions ({', '.join(cols)}) VALUES ({marks})", ms)  # noqa: S608
        inc.executemany("INSERT INTO media_events (id, media_session_id, ts_ms, state) VALUES (?, ?, ?, ?)", ev)
        inc.executemany("INSERT INTO capture_gaps (id, kind, reason, start_ms, end_ms) VALUES (?, ?, ?, ?, ?)", gp)
        counts: dict = {}
        attempts_ok += cand.build_media_chains(inc, t - production.CLOSE_GAP_MS, p_ms=p_ms, counts=counts, **kw)
        for k, v in counts.items():
            totals[k] = totals.get(k, 0) + v
        for ref, b in inc.execute("SELECT ref, end_ms FROM episodes WHERE kind IN ('video', 'music')"):
            first_seen.setdefault(ref, (t - b) / 1000)
        passes += 1
        t += STEP_MS
    got = episodes_snapshot(inc)
    one = ge.fresh(base_conn)
    for tb in ("episode_people", "episode_items", "episodes"):
        one.execute(f"DELETE FROM {tb}")  # noqa: S608
    one.execute("DELETE FROM state WHERE key = ?", (cand.CHAINS_SINCE,))
    one.execute("DELETE FROM jobs")
    cand.build_media_chains(one, t_end + 86_400_000, p_ms=p_ms, **kw)
    want = episodes_snapshot(one)
    row_uses = {}
    for v in got.values():
        for i in v[4]:
            row_uses[i] = row_uses.get(i, 0) + 1
    return {
        "passes": passes, "episodes_incremental": len(got), "episodes_one_pass": len(want),
        "rows_in_two_episodes": sum(1 for n in row_uses.values() if n > 1),
        "late_extensions": totals.get("late_extension", 0),
        "differs_from_one_pass": sum(1 for k in set(got) | set(want) if got.get(k) != want.get(k)),
        "same_as_one_pass": got == want,
        "emit_latency_s_max": max(first_seen.values(), default=None),  # pass time minus episode end
    }


# --- adversarial set ----------------------------------------------------------------------------

MIN = 60_000
APP = "Google Chrome"


def adversarial_cases():
    """(name, rows, gaps, expected spans). A row: (title, artist, url, start, end, played, last_state, spos, epos, dur)
    with times in ms from the case start; positions None when unknown. Expected: the episodes a correct
    derivation gives."""
    cases = []

    def r(title, a, b, artist="Chan A", url=None, played=None, last="playing", spos=None, epos=None, dur=None):
        return (title, artist, url, a, b, b - a if played is None else played, last, spos, epos, dur)

    # 1 two different videos, same generic title, one app, close together (gap 20 s / 120 s / 400 s)
    for gap_s in (20, 120, 400):
        g = gap_s * 1000
        cases.append((f"same title, other channel, gap {gap_s}s",
                      [r("Episode 1", 0, 20 * MIN, "Chan A", spos=0, epos=20 * MIN, dur=20 * MIN, last="paused"),
                       r("Episode 1", 20 * MIN + g, 45 * MIN + g, "Chan B", spos=0, epos=25 * MIN, dur=25 * MIN)],
                      [], [(0, 20 * MIN), (20 * MIN + g, 45 * MIN + g)]))
        cases.append((f"same title+channel, urls differ, gap {gap_s}s",
                      [r("Episode 1", 0, 20 * MIN, url="https://v.example/a", spos=0, epos=20 * MIN, dur=20 * MIN, last="paused"),
                       r("Episode 1", 20 * MIN + g, 45 * MIN + g, url="https://v.example/b", spos=0, epos=25 * MIN, dur=25 * MIN)],
                      [], [(0, 20 * MIN), (20 * MIN + g, 45 * MIN + g)]))
        cases.append((f"same title+channel, no url, 1st finished, gap {gap_s}s",
                      [r("Episode 1", 0, 20 * MIN, spos=0, epos=20 * MIN, dur=20 * MIN, last="paused"),
                       r("Episode 1", 20 * MIN + g, 45 * MIN + g, spos=0, epos=25 * MIN, dur=25 * MIN)],
                      [], [(0, 20 * MIN), (20 * MIN + g, 45 * MIN + g)]))
        cases.append((f"same title+channel, no url, 1st abandoned, gap {gap_s}s",
                      [r("Episode 1", 0, 12 * MIN, spos=0, epos=12 * MIN, dur=30 * MIN),
                       r("Episode 1", 12 * MIN + g, 37 * MIN + g, spos=0, epos=25 * MIN, dur=25 * MIN)],
                      [], [(0, 12 * MIN), (12 * MIN + g, 37 * MIN + g)]))
    # 2 rewatch right after the end, in a new row (page reloaded) and inside the same row (today's capture)
    cases.append(("rewatch after end, new row (10 s)",
                  [r("Some video", 0, 15 * MIN, url="https://v.example/x", spos=0, epos=15 * MIN, dur=15 * MIN, last="paused"),
                   r("Some video", 15 * MIN + 10_000, 30 * MIN + 10_000, url="https://v.example/x", spos=0, epos=15 * MIN, dur=15 * MIN)],
                  [], [(0, 15 * MIN), (15 * MIN + 10_000, 30 * MIN + 10_000)]))
    cases.append(("rewatch after end, same row (capture keeps it)",
                  [r("Some video", 0, 30 * MIN + 10_000, url="https://v.example/x", played=30 * MIN, spos=0, epos=15 * MIN, dur=15 * MIN)],
                  [], [(0, 15 * MIN), (15 * MIN + 10_000, 30 * MIN + 10_000)]))
    # 3 live stream whose title changes (row closes on title change, reopens 3 s later)
    for with_url in (True, False):
        u = "https://live.example/chan" if with_url else None
        cases.append((f"live stream title change, url {'same' if with_url else 'none'}",
                      [r("Live: speedrun part 1", 0, 40 * MIN, url=u),
                       r("Live: speedrun part 2", 40 * MIN + 3_000, 80 * MIN, url=u),
                       r("Live: speedrun part 3", 80 * MIN + 3_000, 100 * MIN, url=u)],
                      [], [(0, 100 * MIN)]))
    # 4 Pause mid-video: 2 h (not bridged by design: 2 recorded pieces) and 20 min (bridged)
    for pause_min, expect_split in ((120, True), (20, False)):
        pa, pb = 20 * MIN, 20 * MIN + pause_min * MIN
        rows = [r("Long video", 0, pa, url="https://v.example/l", spos=0, epos=pa, dur=200 * MIN),
                r("Long video", pb + 5_000, pb + 30 * MIN, url="https://v.example/l", spos=pb + 5_000, epos=pb + 30 * MIN, dur=200 * MIN)]
        exp = [(0, pa), (pb + 5_000, pb + 30 * MIN)] if expect_split else [(0, pb + 30 * MIN)]
        cases.append((f"Pause {pause_min} min mid-video", rows, [("all", "paused", pa, pb)], exp))
    # 5 guard false-split check: video paused by the user, recording paused 5 min, resumed
    cases.append(("user pause + 5 min Pause, resumed",
                  [r("Talk", 0, 10 * MIN, url="https://v.example/t", spos=0, epos=10 * MIN, dur=60 * MIN, last="paused"),
                   r("Talk", 15 * MIN, 40 * MIN, url="https://v.example/t", spos=10 * MIN, epos=35 * MIN, dur=60 * MIN)],
                  [("all", "paused", 10 * MIN + 2_000, 15 * MIN - 1_000)], [(0, 40 * MIN)]))
    return cases


def adversarial_store(template, with_pos: bool):
    conn = ge.fresh(template)
    clear(conn)
    conn.execute("DELETE FROM audio_segments")
    if with_pos:
        for c in ("start_pos_ms", "end_pos_ms", "duration_ms"):
            conn.execute(f"ALTER TABLE media_sessions ADD COLUMN {c} INTEGER")
    base, sid, eid, spans = 10_000_000_000, 0, 0, []
    for k, (name, rows, gaps, exp) in enumerate(adversarial_cases()):
        t0 = base + k * 6 * 3_600_000
        for title, artist, url, a, b, played, last, spos, epos, dur in rows:
            sid += 1
            if with_pos:
                conn.execute("INSERT INTO media_sessions (id, app_id, app, title, artist, url, start_ms, end_ms, played_ms, closed,"
                             " start_pos_ms, end_pos_ms, duration_ms) VALUES (?, 'x', ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)",
                             (sid, APP, title, artist, url, t0 + a, t0 + b, played, spos, epos, dur))
            else:
                conn.execute("INSERT INTO media_sessions (id, app_id, app, title, artist, url, start_ms, end_ms, played_ms, closed)"
                             " VALUES (?, 'x', ?, ?, ?, ?, ?, ?, ?, 1)", (sid, APP, title, artist, url, t0 + a, t0 + b, played))
            for st, ts in (("playing", a), (last, b - 1 if last == "paused" else None), ("stopped", b)):
                if ts is not None:
                    eid += 1
                    conn.execute("INSERT INTO media_events (id, media_session_id, ts_ms, state) VALUES (?, ?, ?, ?)", (eid, sid, t0 + ts, st))
        for kind, reason, a, b in gaps:
            conn.execute("INSERT INTO capture_gaps (kind, reason, start_ms, end_ms) VALUES (?, ?, ?, ?)", (kind, reason, t0 + a, t0 + b))
        spans.append((name, t0, t0 + 6 * 3_600_000, [(t0 + a, t0 + b) for a, b in exp]))
    return conn, spans


def score_adversarial(template, with_pos, p_ms, proxy=False, url_identity=False):
    conn, spans = adversarial_store(template, with_pos)
    cand.build_media_chains(conn, 10**13, p_ms=p_ms, proxy=proxy, url_identity=url_identity)
    eps = conn.execute("SELECT start_ms, end_ms FROM episodes").fetchall()
    out = []
    for name, lo, hi, exp in spans:
        got = sorted((a, b) for a, b in eps if lo <= a < hi)
        merged = sum(1 for g in got if sum(1 for x in exp if x[0] < g[1] and g[0] < x[1]) > 1)
        split = sum(1 for x in exp if sum(1 for g in got if x[0] < g[1] and g[0] < x[1]) > 1)
        ok = len(got) == len(exp) and all(any(iou(*g, *x) >= 0.9 for g in got) for x in exp)
        out.append({"case": name, "expected": len(exp), "episodes": len(got), "merged": merged, "split": split, "ok": ok})
    inc = incremental(conn, min(s[1] for s in spans), max(s[2] for s in spans), p_ms, proxy=proxy, url_identity=url_identity)
    return out, inc


# --- figures ------------------------------------------------------------------------------------

BG, FG, DIM, GRID = (18, 18, 22), (225, 225, 230), (120, 120, 130), (40, 40, 48)
GREEN, GREY, RED, BLUE, AMBER, PURPLE = (120, 210, 120), (110, 110, 120), (230, 80, 80), (90, 170, 250), (240, 180, 60), (200, 120, 230)


def font(size):
    from PIL import ImageFont

    try:
        return ImageFont.truetype("consola.ttf", size)
    except OSError:
        return ImageFont.load_default()


def fig_video(path, rid, run, v, sweep_eps, prod_eps):
    from PIL import Image, ImageDraw

    f, fb = font(13), font(15)
    pad = 5 * MIN
    t0, t1 = v.start - pad, v.end + pad
    W, X0, X1 = 1400, 210, 1380
    lanes = [("truth (playing)", "truth"), ("recording off", "gaps"), ("stored media rows", "rows"),
             ("episodes: production", "prod")] + [(f"episodes: P={p // 1000}s", p) for p in SWEEP]
    H = 60 + 26 * len(lanes) + 50
    im = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(im)
    sx = lambda t: X0 + (X1 - X0) * (min(max(t, t0), t1) - t0) / (t1 - t0)  # noqa: E731
    d.text((10, 10), f"{rid}  video {v.vid}  truth {round((v.end - v.start) / 60000, 1)} min"
                     "  (green: this video's rows/episodes, grey: other media)", fill=FG, font=fb)
    rows = run.shadow.execute("SELECT title, start_ms, end_ms FROM media_sessions WHERE start_ms < ? AND end_ms > ? ORDER BY start_ms",
                              (t1, t0)).fetchall()
    for li, (label, key) in enumerate(lanes):
        y = 40 + li * 26
        d.text((10, y + 2), label, fill=DIM, font=f)
        d.line((X0, y + 9, X1, y + 9), fill=GRID)
        spans = []
        if key == "truth":
            spans = [(a, b, GREEN) for a, b in v.stretches]
        elif key == "gaps":
            spans = [(a, b, AMBER) for a, b, _r in run.ctx.gaps]
        elif key == "rows":
            spans = [(a, b, GREEN if (t or "").lower() == v.title.lower() else GREY) for t, a, b in rows]
        else:
            eps = prod_eps if key == "prod" else sweep_eps[key]
            for e in eps:
                if e[1] in ("video", "music") and e[2] < t1 and e[3] > t0:
                    good = iou(e[2], e[3], v.start, v.end) >= IOU
                    spans.append((e[2], e[3], GREEN if good else (RED if (e[4] or "").lower() == v.title.lower() else GREY)))
        for k, (a, b, col) in enumerate(spans):
            if b < t0 or a > t1:
                continue
            xa, xb = sx(a), max(sx(b), sx(a) + 2)
            d.rectangle((xa, y + 2, xb, y + 16), fill=col, outline=BG if k % 2 else None)
            if key == "rows" or key == "prod" or isinstance(key, int):
                d.line((xa, y + 1, xa, y + 17), fill=BG, width=2)
    ya = 40 + len(lanes) * 26 + 4
    span_min = (t1 - t0) / MIN
    step = 5 if span_min < 40 else (15 if span_min < 150 else 30)
    m = 0
    while m <= span_min:
        x = sx(t0 + m * MIN)
        d.line((x, 36, x, ya), fill=GRID)
        d.text((x - 10, ya + 2), f"{int(m - pad / MIN)}m", fill=DIM, font=f)
        m += step
    d.text((10, ya + 22), "red: an episode of this video below IoU 0.5 (a fragment); x axis: minutes from the truth start",
           fill=DIM, font=f)
    im.save(path)


def fig_adversarial(path, results):
    from PIL import Image, ImageDraw

    f, fb = font(13), font(15)
    keys = list(results)
    names = [r["case"] for r in results[keys[0]]]
    W = 470 + 120 * len(keys)
    H = 70 + 20 * len(names) + 40
    im = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(im)
    d.text((10, 10), "adversarial set: episodes / expected  (green ok, red merged, amber split)", fill=FG, font=fb)
    for j, k in enumerate(keys):
        d.text((470 + 120 * j, 40), k, fill=DIM, font=f)
    for i, n in enumerate(names):
        y = 62 + 20 * i
        d.text((10, y), n, fill=FG, font=f)
        for j, k in enumerate(keys):
            r = results[k][i]
            col = GREEN if r["ok"] else (RED if r["merged"] else AMBER)
            x = 470 + 120 * j
            d.rectangle((x, y + 2, x + 12, y + 14), fill=col)
            d.text((x + 18, y), f"{r['episodes']}/{r['expected']}", fill=FG, font=f)
    im.save(path)


# --- main ---------------------------------------------------------------------------------------


def main():
    out = {"sweep": {}, "incremental": {}, "adversarial": {}, "production_media_ms_per_day": {}}
    sweep_eps, prod = {}, {}
    for rid in ge.RUNS:
        run = ge.load(rid)
        prod[rid], out["production_media_ms_per_day"][rid] = production_media_eps(run)
        sweep_eps[rid] = {}
        for p in SWEEP:
            res = score_p(rid, p)
            sweep_eps[rid][p] = res.pop("_eps")
            out["sweep"].setdefault(str(p // 1000), {})[rid] = res
            print(f"P={p // 1000}s {rid[-4:]}: F1 video {res['f1_video']:.2f} game {res['f1_game']:.2f} M11.2 {res['title_acc']:.2f}"
                  f" video titles {res['video_title_acc']:.2f} eps {res['video_eps']} rows {res['media_rows']}"
                  f" media+game {res['media_game_ms_per_day']:.2f} ms/day counts {res['counts']}"
                  f" | {[(d['vid'], d['best_iou'], d['start_err_s'], d['end_signed_s']) for d in res['videos']]}", flush=True)
        for p in SWEEP:
            inc = incremental(ge.fresh(run.shadow), run.ctx.first_frame, run.tr.t1, p)
            out["incremental"].setdefault(str(p // 1000), {})[rid] = inc
            print(f"  incremental P={p // 1000}s {rid[-4:]}: {inc}", flush=True)
        for v in run.videos:
            fig_video(HERE / f"fig-{rid[-4:]}-{v.vid}.png", rid, run, v, sweep_eps[rid], prod[rid])
    template = ge.load(ge.RUNS[0]).shadow
    adv_figs = {}
    variants = [(f"P{p // 1000} today", False, p, False, False) for p in SWEEP]
    variants += [(f"P{p // 1000} +pos", True, p, False, False) for p in SWEEP]
    variants += [("P180 proxy", False, CHOSEN, True, False), ("P180 url-id", False, CHOSEN, False, True)]
    for name, with_pos, p, proxy, urlid in variants:
        res, inc = score_adversarial(template, with_pos, p, proxy, urlid)
        out["adversarial"][name] = {"cases": res, "incremental": inc,
                                    "merged": sum(r["merged"] for r in res), "split": sum(r["split"] for r in res),
                                    "ok": sum(r["ok"] for r in res), "n": len(res)}
        adv_figs[name] = res
        print(f"adversarial {name}: ok {out['adversarial'][name]['ok']}/{len(res)} merged {out['adversarial'][name]['merged']}"
              f" split {out['adversarial'][name]['split']} incremental same {inc['same_as_one_pass']} rows_in_two {inc['rows_in_two_episodes']}",
              flush=True)
        for r in res:
            if not r["ok"]:
                print(f"    {r}")
    fig_adversarial(HERE / "fig-adversarial.png", adv_figs)
    # harness outputs for the chosen P
    cand.P_MS = CHOSEN
    cand.STATS["calls"].clear()
    harness.load_module = lambda _p: cand
    sys.argv = ["harness.py", str(HERE / "candidate.py"), *ge.RUNS]
    harness.main()
    chosen = out["sweep"][str(CHOSEN // 1000)]
    inc = out["incremental"][str(CHOSEN // 1000)]
    adv = out["adversarial"][f"P{CHOSEN // 1000} today"]
    advp = out["adversarial"][f"P{CHOSEN // 1000} +pos"]
    rows = sum(r["media_rows"] for r in chosen.values())
    chains = sum(r["counts"]["chains"] for r in chosen.values())
    multi = sum(r["counts"]["multi_row_chains"] for r in chosen.values())
    berr = [d for r in chosen.values() for d in r["videos"]]
    worst_start = max((d["start_err_s"] for d in berr if d["start_err_s"] is not None), default=None)
    worst_end = max((d["end_err_s"] for d in berr if d["end_err_s"] is not None), default=None)
    extra = (f" | P={CHOSEN // 1000}s chains: media rows {rows} -> chains {chains} (multi-row {multi},"
             f" bridged obs {sum(r['counts']['bridged_obs'] for r in chosen.values())}"
             f" rec {sum(r['counts']['bridged_rec'] for r in chosen.values())},"
             f" dropped<min {sum(r['counts']['dropped_min_played'] for r in chosen.values())})"
             f" video titles min={min(r['video_title_acc'] for r in chosen.values()):.2f}"
             f" bounds max start={worst_start}s end={worst_end}s"
             f" media+game max={max(r['media_game_ms_per_day'] for r in chosen.values()):.2f} ms/day"
             f" | incremental dup rows={sum(i['rows_in_two_episodes'] for i in inc.values())}"
             f" late_ext={sum(i['late_extensions'] for i in inc.values())}"
             f" same_as_one_pass={all(i['same_as_one_pass'] for i in inc.values())}"
             f" | adversarial today ok={adv['ok']}/{adv['n']} merged={adv['merged']} split={adv['split']}"
             f"; +pos ok={advp['ok']}/{advp['n']} merged={advp['merged']} split={advp['split']}")
    line = (HERE / "metrics.txt").read_text(encoding="utf-8").strip() + extra
    (HERE / "metrics.txt").write_text(line + "\n", encoding="utf-8")
    data = json.loads((HERE / "metrics.json").read_text(encoding="utf-8"))
    data["p3"] = out
    data["p3"]["chosen_p_s"] = CHOSEN // 1000
    ge.write_json(HERE / "metrics.json", data)
    # side.png again, with the chain counts in its footer (same layout as baseline/side.png)
    cand.P_MS = CHOSEN
    runs = {rid: (ge.load(rid), harness.score_run(ge.load(rid), cand)) for rid in ge.RUNS}
    harness.side_png(HERE / "side.png", cand.NAME, runs, harness.detection(cand), line)
    print(line)


if __name__ == "__main__":
    main()
