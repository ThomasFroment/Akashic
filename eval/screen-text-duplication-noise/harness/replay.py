"""Replay harness: run a candidate over today's stored window texts and the synthetic set.

    uv run python eval/screen-text-duplication-noise/harness/replay.py CAND.py OUT_DIR
           [--runs smoke,standard,day] [--reps 9] [--no-synth]

Writes OUT_DIR/metrics.txt (one line), OUT_DIR/details.json (numbers only) and OUT_DIR/side.png.
Baseline numbers for the plot and the *_x ratios come from <study>/baseline/details.json.
Stored text never leaves memory: the candidate FTS is an in-memory SQLite database.
"""

from __future__ import annotations

import argparse
import bisect
import heapq
import importlib.util
import json
import math
import random
import statistics
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import shared as S  # noqa: E402

INDEXED = ("content", "chrome")
MAX_CER_ROWS = 3000  # score.MAX_TEXT_ROWS
NEAR_SAMPLE = 5000


def load_module(path: str):
    spec = importlib.util.spec_from_file_location(Path(path).stem + "_cand", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def norm(s: str) -> str:
    return " ".join(s.split())


# --- running a candidate --------------------------------------------------------------------------


def fresh_windows(wins: list[tuple[list[S.Obs], S.Ctx]]) -> list[tuple[list[S.Obs], S.Ctx]]:
    return [(h, S.Ctx(c.run, c.hwnd, c.process, c.app, c.title, c.url, c.monitor)) for h, c in wins]


def run_candidate(mod, wins, reps: int, span_ms: int) -> tuple[dict, dict]:
    """First rep's output per hwnd, and timings (median over reps)."""
    write_path = bool(getattr(mod, "WRITE_PATH", False))
    t_defer, step_p50, step_p99, step_mean = [], [], [], []
    out = None
    for rep in range(max(1, reps)):
        ws = fresh_windows(wins)
        t0 = time.perf_counter()
        shared = mod.prepare(ws) if hasattr(mod, "prepare") else None
        t_prep = time.perf_counter() - t0
        res, t_proc, per_frame = {}, 0.0, []
        for h, c in ws:
            c.shared = shared
            t1 = time.perf_counter()
            res[c.hwnd] = mod.process(h, c)
            dt = time.perf_counter() - t1
            t_proc += dt
            if write_path and h:
                per_frame += [dt / len(h)] * len(h)
        if hasattr(mod, "step"):
            for h, c in fresh_windows(wins):
                c.shared = shared
                state = None
                for o in h:
                    t2 = time.perf_counter_ns()
                    state = mod.step(state, o, c)
                    per_frame.append((time.perf_counter_ns() - t2) / 1e9)
        t_defer.append(t_prep + (0.0 if write_path else t_proc))
        if per_frame:
            per_frame.sort()
            step_p50.append(per_frame[len(per_frame) // 2])
            step_p99.append(per_frame[min(len(per_frame) - 1, int(0.99 * len(per_frame)))])
            step_mean.append(statistics.fmean(per_frame))
        if out is None:
            out = res
    med = lambda xs: statistics.median(xs) if xs else 0.0  # noqa: E731
    timing = {"defer_s": med(t_defer), "defer_pct": med(t_defer) / (span_ms / 1000) * 100, "reps": max(1, reps),
              "write_us_p50": med(step_p50) * 1e6, "write_us_p99": med(step_p99) * 1e6, "write_us_mean": med(step_mean) * 1e6}
    return out, timing


# --- output -> what FTS would index -----------------------------------------------------------------


class Index:
    def __init__(self, out: dict[int, S.StoredText], obs_at: dict[tuple[int, int], S.Obs]) -> None:
        self.docs: dict = {}
        self.text_bytes = 0
        self.n_lines = 0
        for hwnd, st in out.items():
            for kl in st.lines:
                self.n_lines += 1
                self.text_bytes += len(kl.text.encode("utf-8")) + 1
                d = self.docs.get(kl.doc)
                if d is None:
                    meta = st.doc_meta.get(kl.doc)
                    if meta is None:
                        o = obs_at.get((hwnd, kl.frame_id))
                        meta = (o.title, o.url) if o else (None, None)
                    d = self.docs[kl.doc] = {"hwnd": hwnd, "ts": kl.first_ts, "frame": kl.frame_id, "title": meta[0], "url": meta[1], "lines": []}
                if kl.kind in INDEXED:
                    d["lines"].append(kl.text)
        self.fts = S.new_fts()
        self.info: dict[int, tuple] = {}
        for i, d in enumerate(self.docs.values(), 1):
            if not d["lines"] and not d["title"] and not d["url"]:
                continue
            text = "\n".join(d["lines"])
            self.fts.execute("INSERT INTO t (rowid, text, title, url) VALUES (?, ?, ?, ?)", (i, text, d["title"], d["url"]))
            self.info[i] = (d["hwnd"], d["ts"], hash((text, d["title"], d["url"])))
        self.fts_bytes = S.fts_bytes(self.fts)

    def has(self, nonce: str) -> bool:
        return self.fts.execute("SELECT 1 FROM t WHERE t MATCH ? LIMIT 1", (f'"{nonce}"',)).fetchone() is not None

    def top5(self, match: str) -> list[tuple[int, int]]:
        """Today's keyword.search ranking on this index: bm25(1, 3, 2), newest first among equals, and a
        matching row that repeats the previous matching row of its window (same text/title/url) dropped."""
        rows = self.fts.execute("SELECT rowid, bm25(t, 1.0, 3.0, 2.0) FROM t WHERE t MATCH ?", (match,)).fetchall()
        by_win: dict[int, list] = defaultdict(list)
        for rid, score in rows:
            hwnd, ts, key = self.info[rid]
            by_win[hwnd].append((ts, rid, key, score))
        kept = []
        for hwnd, lst in by_win.items():
            lst.sort()
            prev = None
            for ts, rid, key, score in lst:
                if prev is None or key != prev:
                    kept.append((score, -ts, -rid, hwnd, ts))
                prev = key
        kept.sort()
        return [(k[3], k[4]) for k in kept[:5]]


# --- real-run metrics ---------------------------------------------------------------------------------


def m32(out: dict[int, S.StoredText]) -> float:
    dup = tot = 0
    for st in out.values():
        seen: set[str] = set()
        for kl in st.lines:
            for ln in S.text_lines(kl.text):
                tot += len(ln)
                if ln in seen:
                    dup += len(ln)
                seen.add(ln)
    return dup / tot if tot else 0.0


def m31(store: S.Store, truth: S.RunTruth, out: dict[int, S.StoredText], cache: dict) -> dict:
    by_id = {o.window_id: (h, o) for h, o in store.order}
    rows = [by_id[i] for i in store.score_order]
    sample = rows[:: max(1, len(rows) // MAX_CER_ROWS)] if rows else []
    want: dict[int, list[tuple[int, int, str]]] = defaultdict(list)  # hwnd -> (ts, k, source)
    for k, (h, o) in enumerate(sample):
        want[h].append((o.ts_ms, k, o.source))
    per_row: dict[int, tuple[float, str]] = {}
    for h, reqs in want.items():
        st = out.get(h)
        lines = sorted(((kl.first_ts, kl.last_ts, kl.text) for kl in st.lines), key=lambda x: x[0]) if st else []
        wid = truth.hwnd_to_wid.get(h)
        active: list = []  # heap of (last_ts, n, text)
        j = n = 0
        for ts, k, src in sorted(reqs):
            while j < len(lines) and lines[j][0] <= ts:
                heapq.heappush(active, (lines[j][1], n, lines[j][2]))
                j += 1
                n += 1
            while active and active[0][0] < ts:
                heapq.heappop(active)
            shown = [truth.line_by_id[x] for x in truth.visible_at(wid, ts) if x in truth.line_by_id]
            if not shown:
                continue
            hyp = list({S.fold(x) for _, _, t in active for x in t.splitlines() if x.strip()})
            per_row[k] = (statistics.fmean(S.best_line_cer(ln.folded, hyp, cache) for ln in shown), src)
    out_d: dict[str, list[float]] = defaultdict(list)
    for cer, src in per_row.values():
        out_d["all"].append(cer)
        out_d[f"source={src}"].append(cer)
    return {k: {"cer": statistics.fmean(v), "n": len(v)} for k, v in out_d.items()}


def coverage(lines_by_hwnd: dict[int, set[str]], truth: S.RunTruth, lb: S.Labeler) -> dict[str, float]:
    spans: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for h, lines in lines_by_hwnd.items():
        wid = truth.hwnd_to_wid.get(h)
        if wid is None:
            continue
        for ln in lines:
            r = lb.label(wid, ln)
            if r and r[1] == "content":
                spans[r[0]].append((r[2], r[3]))
    cov = {}
    for tid, sp in spans.items():
        sp.sort()
        tot, cur_s, cur_e = 0, None, None
        for s, e in sp:
            if cur_e is None or s > cur_e:
                if cur_e is not None:
                    tot += cur_e - cur_s
                cur_s, cur_e = s, e
            else:
                cur_e = max(cur_e, e)
        if cur_e is not None:
            tot += cur_e - cur_s
        cov[tid] = tot
    return cov


def drop_metrics(base_cov: dict[str, float], cand_cov: dict[str, float], truth: S.RunTruth) -> dict:
    res = {}
    for name, pred in (("all", lambda t: True), ("pages", lambda t: not t.startswith("term:")), ("term", lambda t: t.startswith("term:"))):
        cap = {t: c for t, c in base_cov.items() if pred(t) and c >= max(6, 0.3 * len(truth.line_by_id[t].folded))}
        dropped = [t for t, c in cap.items() if cand_cov.get(t, 0) < 0.5 * c]
        lost = sum(max(0.0, c - cand_cov.get(t, 0)) for t, c in cap.items())
        res[name] = {"captured": len(cap), "dropped": len(dropped), "rate": len(dropped) / len(cap) if cap else 0.0,
                     "char_rate": lost / max(sum(cap.values()), 1)}
    return res


def link_metrics(out, obs_at, truth: S.RunTruth, lb: S.Labeler, rng: random.Random) -> dict:
    lines_of: dict[tuple[int, int], set[str]] = {}
    exact = total = nolink = 0
    pending = []
    tl_ok = tl_n = 0
    tl_seen = set()
    for h, st in out.items():
        wid = truth.hwnd_to_wid.get(h)
        for kl in st.lines:
            if kl.kind not in INDEXED or not kl.text.strip():
                continue
            total += 1
            o = obs_at.get((h, kl.frame_id))
            if o is None:
                nolink += 1
                continue
            key = (h, kl.frame_id)
            if key not in lines_of:
                lines_of[key] = {norm(x) for x in o.text.splitlines()}
            t = norm(kl.text)
            if t in lines_of[key]:
                exact += 1
            else:
                pending.append((t, o))
            if kl.kind == "content" and wid:
                r = lb.label(wid, kl.text)
                if r and r[1] == "content" and not r[0].startswith("term:") and (r[0], kl.frame_id) not in tl_seen:
                    tl_seen.add((r[0], kl.frame_id))
                    tl_n += 1
                    # by text: cycled 'more' lines repeat a text under several ids
                    vis = {truth.line_by_id[i].folded for i in truth.visible_near(wid, o.ts_ms) if i in truth.line_by_id}
                    tl_ok += truth.line_by_id[r[0]].folded in vis
    sample = pending if len(pending) <= NEAR_SAMPLE else rng.sample(pending, NEAR_SAMPLE)
    near_ok = sum(S.lev_search(t, norm(o.text))[0] <= max(1, 0.1 * len(t)) for t, o in sample)
    near_est = near_ok / len(sample) * len(pending) if sample else 0.0
    return {"lines": total, "exact": exact / max(total, 1), "valid": (exact + near_est) / max(total, 1), "nolink": nolink,
            "near_sampled": len(sample), "truth_valid": tl_ok / max(tl_n, 1), "truth_n": tl_n}


def noise_metric(index: Index, lx: S.Lexicon) -> dict:
    tok = unk = garb = 0
    for d in index.docs.values():
        for ln in d["lines"]:
            for t in S.tokens(ln):
                tok += 1
                if lx.classify(t) == "unknown":
                    unk += 1
                    garb += lx.garbled(t)
    return {"tokens": tok, "unknown": unk, "share": unk / max(tok, 1), "garbled_share": garb / max(tok, 1)}


def crowding(index: Index, truth: S.RunTruth, wid_of: dict[int, str | None]) -> dict:
    from akashic.search.keyword import build_match

    def sighting(hwnd, ts):
        for i, (a, b) in enumerate(truth.shown_spans.get(wid_of.get(hwnd), [])):
            if a - 2000 <= ts <= b + 2000:
                return i
        return None

    def run(queries):
        found = slots = same_win = same_sight = 0
        for q in queries:
            m = build_match(q)
            hits = index.top5(m) if m else []
            if not hits:
                continue
            found += 1
            slots += len(hits)
            prev = []
            for hw, ts in hits:
                sg = sighting(hw, ts)
                if any(p[0] == hw for p in prev):
                    same_win += 1
                    if sg is not None and (hw, sg) in prev:
                        same_sight += 1
                prev.append((hw, sg))
        return {"queries": len(queries), "found": found, "dup_same_window": same_win / max(slots, 1), "dup_same_sighting": same_sight / max(slots, 1),
                "mean_hits": slots / max(found, 1)}

    rng = random.Random(7)
    words = sorted({t for tl in truth.line_by_id.values() if tl.kind == "content" for t in tl.text.split()[:-1] if len(t) >= 6 and t.isalpha()})
    return {"nonce": run(sorted(set(truth.nonces))), "word": run(rng.sample(words, min(200, len(words))))}


def eval_run(mod, key: str, reps: int, base_cov_cache: dict) -> dict:
    t0 = time.perf_counter()
    truth = S.RunTruth(S.RUNS[key])
    store = S.load_store(S.RUNS[key], truth)
    lx = S.Lexicon(truth.strings)
    lb = S.Labeler(truth)
    wins = [(w.history, w.ctx) for w in store.windows]
    obs_at = {(h, o.frame_id): o for h, o in store.order}
    out, timing = run_candidate(mod, wins, reps, store.span_ms)
    index = Index(out, obs_at)
    m33_found = sum(index.has(n) for n in truth.nonces)
    base_lines = defaultdict(set)
    for h, o in store.order:
        base_lines[h].update(S.text_lines(o.text))
    base_cov = coverage(base_lines, truth, lb)
    cand_lines = defaultdict(set)
    for h, st in out.items():
        for kl in st.lines:
            if kl.kind == "content":
                cand_lines[h].update(S.text_lines(kl.text))
    cand_cov = coverage(cand_lines, truth, lb)
    res = {
        "run": S.RUNS[key], "span_h": store.span_ms / 3.6e6, "rows_in": len(store.order),
        "m32": m32(out), "m33": m33_found / max(len(truth.nonces), 1), "m33_n": len(truth.nonces),
        "m31": m31(store, truth, out, {}),
        "drop": drop_metrics(base_cov, cand_cov, truth),
        "link": link_metrics(out, obs_at, truth, lb, random.Random(3)),
        "noise": noise_metric(index, lx),
        "fts_kb": index.fts_bytes / 1024, "text_kb": index.text_bytes / 1024, "docs": len(index.info), "lines": index.n_lines,
        "crowding": crowding(index, truth, {w.hwnd: w.wid for w in store.windows}),
        "timing": timing,
    }
    res["runtime_s"] = time.perf_counter() - t0
    store.conn.close()
    return res


# --- synthetic -------------------------------------------------------------------------------------------


def eval_synth(mod, reps: int) -> dict:
    wins_raw = [json.loads(x) for x in (HERE / "synthetic" / "windows.jsonl").read_text(encoding="utf-8").splitlines()]
    wins, recs_at, obs_at = [], {}, {}
    for w in wins_raw:
        hist = []
        for f in w["frames"]:
            o = S.Obs(f["ts"], f["fid"], f["wrow"], w["source"], f["text"], f["title"], f["url"], w["monitor"], True)
            hist.append(o)
            obs_at[w["hwnd"], f["fid"]] = o
            lines = f["text"].split("\n") if f["text"] else []
            recs_at[w["hwnd"], f["fid"]] = list(zip(lines, f["recs"], strict=True))
        wins.append((hist, S.Ctx("synthetic", w["hwnd"], w["process"], w["app"], hist[0].title, hist[0].url, w["monitor"])))
    span = max(o.ts_ms for h, _ in wins for o in h) - min(o.ts_ms for h, _ in wins for o in h)
    out, timing = run_candidate(mod, wins, reps, span)
    S.init_corpus()
    res = {"timing": timing, "m32": m32(out)}
    # truth units per window
    units: dict[int, dict[str, dict]] = defaultdict(dict)
    by_clean: dict[int, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    vocab: dict[int, set[str]] = defaultdict(set)
    for w in wins_raw:
        h = w["hwnd"]
        for f in w["frames"]:
            for rec in f["recs"]:
                iid, kind, clean = rec
                if iid is None:
                    continue
                u = units[h].get(iid)
                if u is None:
                    u = units[h][iid] = {"kind": kind, "clean": clean, "ts": []}
                    by_clean[h][S.fold(clean)].append(iid)
                u["ts"].append(f["ts"])
                vocab[h].update(S.fts_tokens(clean))

    def map_line(h: int, kl: S.KeptLine) -> tuple[str | None, list[str]]:
        """(clean text the line stands for, unit ids it keeps) via its link frame, else fuzzy."""
        recs = recs_at.get((h, kl.frame_id), [])
        t = norm(kl.text)
        clean = next((rec[2] for txt, rec in recs if norm(txt) == t and rec[0]), None)
        if clean is None:
            f = S.fold(kl.text)
            best = None
            pool = {rec[2] for _, rec in recs if rec[0]} or {u["clean"] for u in units[h].values()}
            for c in pool:
                fc = S.fold(c)
                if fc and abs(len(fc) - len(f)) <= 0.3 * len(fc):
                    d = S.lev(f, fc) / len(fc)
                    if d <= 0.3 and (best is None or d < best[0]):
                        best = (d, c)
            clean = best[1] if best else None
        if clean is None:
            return None, []
        ids = [i for i in by_clean[h].get(S.fold(clean), []) if any(kl.first_ts <= ts <= kl.last_ts for ts in units[h][i]["ts"])]
        return clean, ids

    kept_units: dict[int, set[str]] = defaultdict(set)
    chrome_chars = link_ok = link_n = 0
    tok = noise = 0
    mapped_lines = Counter()
    for h, st in out.items():
        for kl in st.lines:
            if not kl.text.strip():
                continue
            clean, ids = map_line(h, kl)
            if kl.kind in INDEXED:
                for t in S.fts_tokens(kl.text):
                    tok += 1
                    noise += t not in vocab[h]
                if clean is not None:
                    link_n += 1
                    link_ok += any(rec[2] == clean for _, rec in recs_at.get((h, kl.frame_id), []))
                    kinds = {units[h][i]["kind"] for i in ids} or {next((u["kind"] for u in units[h].values() if u["clean"] == clean), "?")}
                    if kinds & {"chrome", "vchrome"}:
                        chrome_chars += len(kl.text)
            if kl.kind == "content":
                kept_units[h].update(ids)
                for i in ids:
                    mapped_lines[h, i] += 1
    for kind in ("content", "vcontent"):
        tot = [(h, i) for h, us in units.items() for i, u in us.items() if u["kind"] == kind]
        lost = [x for x in tot if x[1] not in kept_units[x[0]]]
        res[f"drop_{kind}"] = len(lost) / max(len(tot), 1)
        res[f"n_{kind}"] = len(tot)
        res[f"drop_{kind}_by_scenario"] = dict(Counter(next(w["scenario"] for w in wins_raw if w["hwnd"] == h) for h, _ in lost))
    content_ids = {(h, i) for h, us in units.items() for i, u in us.items() if u["kind"] == "content"}
    res["dup_per_unit"] = sum(v for k, v in mapped_lines.items() if k in content_ids) / max(sum(1 for k in mapped_lines if k in content_ids), 1)
    res["link_valid"] = link_ok / max(link_n, 1)
    res["chrome_chars"] = chrome_chars
    res["noise_share"] = noise / max(tok, 1)
    res["tokens"] = tok
    res["kept_lines"] = sum(len(st.lines) for st in out.values())
    return res


# --- main -------------------------------------------------------------------------------------------------


def metric_line(r: dict, base: dict | None) -> str:
    def g(run, *path):
        v = r.get(run)
        for p in path:
            if v is None:
                return None
            v = v.get(p) if isinstance(v, dict) else None
        return v

    parts = []

    def put(name, val, fmt="{:.4f}"):
        parts.append(f"{name}={'na' if val is None else fmt.format(val)}")

    runs = [k for k in ("smoke", "standard", "day") if k in r]
    for k in runs:
        put(f"m32_{S.SHORT[k]}", g(k, "m32"), "{:.3f}")
    for k in runs:
        put(f"m33_{S.SHORT[k]}", g(k, "m33"), "{:.3f}")
    for k in runs:
        put(f"cer_{S.SHORT[k]}", g(k, "m31", "all", "cer"), "{:.4f}")
    for k in runs:
        put(f"drop_{S.SHORT[k]}", g(k, "drop", "all", "rate"), "{:.4f}")
    for k in runs:
        put(f"cdrop_{S.SHORT[k]}", g(k, "drop", "all", "char_rate"), "{:.4f}")
    for k in runs:
        put(f"link_{S.SHORT[k]}", g(k, "link", "valid"), "{:.4f}")
    for k in runs:
        put(f"tlink_{S.SHORT[k]}", g(k, "link", "truth_valid"), "{:.3f}")
    for k in runs:
        put(f"noise_{S.SHORT[k]}", g(k, "noise", "share"), "{:.4f}")
        if base and base.get(k):
            b = base[k]["noise"]["share"]
            put(f"noise_x_{S.SHORT[k]}", g(k, "noise", "share") / b if b else None, "{:.2f}")
    for k in runs:
        put(f"fts_kb_{S.SHORT[k]}", g(k, "fts_kb"), "{:.0f}")
        if base and base.get(k):
            put(f"fts_x_{S.SHORT[k]}", g(k, "fts_kb") / base[k]["fts_kb"], "{:.3f}")
    for k in runs:
        put(f"text_kb_{S.SHORT[k]}", g(k, "text_kb"), "{:.0f}")
    for k in runs:
        put(f"crowd_{S.SHORT[k]}", g(k, "crowding", "nonce", "dup_same_window"), "{:.3f}")
    for k in runs:
        put(f"crowdw_{S.SHORT[k]}", g(k, "crowding", "word", "dup_same_window"), "{:.3f}")
    wk = "standard" if "standard" in r else runs[0] if runs else None
    if wk:
        put("write_us_p50", g(wk, "timing", "write_us_p50"), "{:.1f}")
        put("write_us_p99", g(wk, "timing", "write_us_p99"), "{:.1f}")
    for k in runs:
        put(f"defer_pct_{S.SHORT[k]}", g(k, "timing", "defer_pct"), "{:.3f}")
    if "synthetic" in r:
        sy = r["synthetic"]
        put("syn_drop", sy["drop_content"], "{:.4f}")
        put("syn_vdrop", sy["drop_vcontent"], "{:.4f}")
        put("syn_m32", sy["m32"], "{:.3f}")
        put("syn_noise", sy["noise_share"], "{:.4f}")
        put("syn_link", sy["link_valid"], "{:.4f}")
        put("syn_dup", sy["dup_per_unit"], "{:.2f}")
        put("syn_chrome_kc", sy["chrome_chars"] / 1000, "{:.1f}")
    put("runtime_s", r.get("runtime_s"), "{:.0f}")
    return " ".join(parts)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("cand")
    ap.add_argument("out")
    ap.add_argument("--runs", default="smoke,standard,day")
    ap.add_argument("--reps", type=int, default=9)
    ap.add_argument("--no-synth", action="store_true")
    a = ap.parse_args()
    S.init_corpus()
    mod = load_module(a.cand)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    t0 = time.perf_counter()
    r: dict = {"candidate": str(Path(a.cand).resolve())}
    for k in [x for x in a.runs.split(",") if x]:
        r[k] = eval_run(mod, k, a.reps, {})
        print(f"{k}: done in {r[k]['runtime_s']:.0f}s", flush=True)
    if not a.no_synth:
        r["synthetic"] = eval_synth(mod, a.reps)
    r["runtime_s"] = time.perf_counter() - t0
    base_file = S.STUDY / "baseline" / "details.json"
    base = json.loads(base_file.read_text(encoding="utf-8")) if base_file.is_file() else None
    if base is None and out.resolve() == base_file.parent.resolve():
        base = r
    line = metric_line(r, base)
    (out / "details.json").write_text(json.dumps(r, indent=1), encoding="utf-8")
    (out / "metrics.txt").write_text(line + "\n", encoding="utf-8")
    from plot import side

    side(r, base or r, out / "side.png", Path(a.out).name)
    print(line)


if __name__ == "__main__":
    main()
