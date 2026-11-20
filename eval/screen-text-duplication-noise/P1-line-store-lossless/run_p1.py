"""Run P1 through the harness (replay.py functions, unchanged) plus the user's definitions that the
harness cannot see from KeptLines alone:
  m32P   M3.2 over what P1 physically stores (line rows per (process, day))
  cerR   M3.1 on frames rebuilt from the line store (one interval per streak, every kind)
  m33J   M3.3 when the junk column (garbage lines) is searchable too
  noiseA share of unknown tokens in the top-5 documents of the harness queries (+ Ask's chrome block)
  noiseB share of unknown tokens counting each distinct token once per window run / document
  lossless rebuild check, content-as-chrome / content-as-garbage by app and window (counts only)

    uv run python .../P1-line-store-lossless/run_p1.py OUT_DIR [--runs smoke,standard,day] [--reps 3] [--no-synth]
Prints numbers only.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
HARNESS = HERE.parent / "harness"
sys.path.insert(0, str(HARNESS))
import replay as R  # noqa: E402
import shared as S  # noqa: E402

MOD = R.load_module(str(HERE / "cand.py"))
BASE = R.load_module(str(HERE.parent / "baseline" / "cand.py"))


def top5_rids(index: R.Index, match: str) -> list[int]:
    rows = index.fts.execute("SELECT rowid, bm25(t, 1.0, 3.0, 2.0) FROM t WHERE t MATCH ?", (match,)).fetchall()
    by_win = defaultdict(list)
    for rid, score in rows:
        hwnd, ts, key = index.info[rid]
        by_win[hwnd].append((ts, rid, key, score))
    kept = []
    for lst in by_win.values():
        lst.sort()
        prev = None
        for ts, rid, key, score in lst:
            if prev is None or key != prev:
                kept.append((score, -ts, -rid))
            prev = key
    kept.sort()
    return [-k[2] for k in kept[:5]]


def doc_texts(index: R.Index) -> dict[int, tuple]:
    out = {}
    for i, (k, d) in enumerate(index.docs.items(), 1):
        out[i] = (k, d)
    return out


def noise_views(index: R.Index, truth: S.RunTruth, lx: S.Lexicon, chrome_block: dict | None) -> dict:
    """A: pooled tokens of the top-5 docs of the harness queries (+ the hit's app-day chrome block once
    per query when chrome_block maps hwnd -> chrome doc). B: distinct tokens per document."""
    from akashic.search.keyword import build_match

    docs = doc_texts(index)
    rng = random.Random(7)
    words = sorted({t for tl in truth.line_by_id.values() if tl.kind == "content" for t in tl.text.split()[:-1] if len(t) >= 6 and t.isalpha()})
    queries = sorted(set(truth.nonces)) + rng.sample(words, min(200, len(words)))
    tok = unk = 0
    tok_c = unk_c = 0
    cache: dict[int, tuple[int, int]] = {}

    def count(rid):
        got = cache.get(rid)
        if got is None:
            t = u = 0
            for ln in docs[rid][1]["lines"]:
                for w in S.tokens(ln):
                    t += 1
                    u += lx.classify(w) == "unknown"
            got = cache[rid] = (t, u)
        return got

    for q in queries:
        m = build_match(q)
        if not m:
            continue
        rids = top5_rids(index, m)
        extra = set()
        for rid in rids:
            t, u = count(rid)
            tok += t
            unk += u
            tok_c += t
            unk_c += u
            if chrome_block is not None:
                cb = chrome_block.get(docs[rid][1]["hwnd"])
                if cb is not None and cb not in rids:
                    extra.add(cb)
        for rid in extra:
            t, u = count(rid)
            tok_c += t
            unk_c += u
    dt = du = 0
    for _, d in docs.values():
        seen = {S.fold_token(w) for ln in d["lines"] for w in S.tokens(ln)}
        dt += len(seen)
        du += sum(lx.classify(w) == "unknown" for w in seen)
    return {"A": unk / max(tok, 1), "A_tokens": tok, "A_chrome": unk_c / max(tok_c, 1) if chrome_block is not None else None,
            "B": du / max(dt, 1), "B_tokens": dt}


def noise_evidence(index: R.Index, out: dict, obs_at: dict, ev: dict, truth: S.RunTruth, lx: S.Lexicon, chrome_block: dict) -> dict:
    """What Ask would read for the top-5 hits in P1: the frame rebuilt at the matched line's link
    moment (junk lines and junk tokens left out, chrome lines replaced by the app-day chrome block,
    added once per query). Pooled token noise share."""
    from akashic.search.keyword import build_match

    rng = random.Random(7)
    words = sorted({t for tl in truth.line_by_id.values() if tl.kind == "content" for t in tl.text.split()[:-1] if len(t) >= 6 and t.isalpha()})
    queries = sorted(set(truth.nonces)) + rng.sample(words, min(200, len(words)))
    doc_keys = list(index.docs)
    lines_of: dict = defaultdict(list)
    for h, st in out.items():
        for kl in st.lines:
            if kl.kind != "noise":
                lines_of[kl.doc].append((h, kl))
    tok = unk = 0

    def add(texts):
        nonlocal tok, unk
        for t in texts:
            for w in S.tokens(t):
                tok += 1
                unk += lx.classify(w) == "unknown"

    for q in queries:
        m = build_match(q)
        if not m:
            continue
        qf = S.fold_token(q)
        blocks = set()
        for rid in top5_rids(index, m):
            dk = doc_keys[rid - 1]
            hit = next(((h, kl) for h, kl in lines_of[dk] if any(t.startswith(qf) for t in S.fts_tokens(kl.text))), None)
            if hit is None:  # matched on the title or url only
                hit = lines_of[dk][0] if lines_of[dk] else None
            if hit is None:
                continue
            h, kl = hit
            if dk[0] == "chrome":
                blocks.add(dk)
                continue
            o = obs_at[(h, kl.frame_id)]
            od = ("doc", h, *MOD.doc_key(o))
            for ln in o.text.split("\n"):
                if not ln.strip():
                    continue
                e = ev[h].get((od, ln), [ln])
                if e == "chrome":
                    cb = chrome_block.get(h)
                    if cb is not None:
                        blocks.add(doc_keys[cb - 1])
                else:
                    add(e)
        for dk in blocks:
            add(kl.text for _, kl in lines_of[dk])
    return {"share": unk / max(tok, 1), "tokens": tok}


# --- round-2 definitions (user, 2026-11-20) --------------------------------------------------------


class Misread:
    """Tokens provably misread from harness-rendered text: in a stored line the Labeler matches to a
    truth line of its (truth-labelled) window, a folded token absent from that truth line. Tokens
    found in another truth line of the same window are not counted (a stored line may join a truth
    line with its neighbours). Unmatched lines and unlabelled windows count in the denominator only."""

    def __init__(self, truth: S.RunTruth, lb: S.Labeler) -> None:
        self.truth, self.lb = truth, lb
        self.cache: dict = {}
        self.vocab: dict = {}

    def bad(self, wid: str | None, raw: str) -> frozenset:
        if wid is None:
            return frozenset()
        key = (wid, raw)
        got = self.cache.get(key)
        if got is None:
            r = self.lb.label(wid, raw)
            if not r:
                got = frozenset()
            else:
                v = self.vocab.get(wid)
                if v is None:
                    v = self.vocab[wid] = {t for tl in self.truth.lines.get(wid, []) for t in S.fts_tokens(tl.text)}
                ok = set(S.fts_tokens(self.truth.line_by_id[r[0]].text)) | v
                got = frozenset(t for t in S.fts_tokens(raw) if t not in ok)
            self.cache[key] = got
        return got


class Tally:
    def __init__(self, mis: Misread) -> None:
        self.mis, self.tok, self.bad = mis, 0, 0

    def add(self, wid, text: str, raw: str) -> None:
        b = self.mis.bad(wid, raw)
        for t in S.fts_tokens(text):
            self.tok += 1
            self.bad += t in b

    @property
    def share(self) -> float:
        return self.bad / max(self.tok, 1)


def per_doc_share(items, mis: Misread) -> float:
    """items: (doc, wid, text, raw). Each distinct token once per document; misread when every
    occurrence of it in that document is a misread one."""
    per: dict = defaultdict(dict)
    for doc, wid, text, raw in items:
        b = mis.bad(wid, raw)
        d = per[doc]
        for t in S.fts_tokens(text):
            d[t] = d.get(t, True) and (t in b)
    tot = sum(len(d) for d in per.values())
    return sum(v for d in per.values() for v in d.values()) / max(tot, 1)


def queries_of(truth: S.RunTruth) -> list[str]:
    rng = random.Random(7)
    words = sorted({t for tl in truth.line_by_id.values() if tl.kind == "content" for t in tl.text.split()[:-1] if len(t) >= 6 and t.isalpha()})
    return sorted(set(truth.nonces)) + rng.sample(words, min(200, len(words)))


def truth_noise(store, truth, mis, out, raws, index, base_index, obs_at, ev, chrome_block) -> dict:
    from akashic.search.keyword import build_match

    wid_of = {w.hwnd: w.wid for w in store.windows}
    # occurrences over the index (harness reading)
    occ_c, occ_t = Tally(mis), Tally(mis)
    items_c = []
    lines_of: dict = defaultdict(list)
    for h, st in out.items():
        for kl, raw in zip(st.lines, raws[h], strict=True):
            if kl.kind in R.INDEXED:
                occ_c.add(wid_of[h], kl.text, raw)
                items_c.append((kl.doc, wid_of[h], kl.text, raw))
                lines_of[kl.doc].append((h, kl, raw))
    for h, o in store.order:
        for ln in o.text.split("\n"):
            occ_t.add(wid_of[h], ln, ln)
    # per document, each distinct token once
    items_t = []
    for w in store.windows:
        for o in w.history:
            doc = ("doc", w.hwnd, *MOD.doc_key(o))
            for ln in o.text.split("\n"):
                if ln.strip():
                    items_t.append((doc, w.wid, ln, ln))
    items_t = list(dict.fromkeys(items_t))
    # Ask evidence of the top-5 hits: today = the hit rows; P1 = the frame rebuilt at the matched line
    ask_c, ask_t = Tally(mis), Tally(mis)
    doc_keys = list(index.docs)
    base_docs = list(base_index.docs.values())
    for q in queries_of(truth):
        m = build_match(q)
        if not m:
            continue
        for rid in top5_rids(base_index, m):
            d = base_docs[rid - 1]
            for ln in d["lines"]:
                ask_t.add(wid_of[d["hwnd"]], ln, ln)
        qf = S.fold_token(q)
        blocks = set()
        for rid in top5_rids(index, m):
            dk = doc_keys[rid - 1]
            hit = next(((h, kl) for h, kl, _ in lines_of[dk] if any(t.startswith(qf) for t in S.fts_tokens(kl.text))), None)
            if hit is None:
                hit = (lines_of[dk][0][0], lines_of[dk][0][1]) if lines_of[dk] else None
            if hit is None:
                continue
            h, kl = hit
            if dk[0] == "chrome":
                blocks.add(dk)
                continue
            o = obs_at[(h, kl.frame_id)]
            od = ("doc", h, *MOD.doc_key(o))
            for ln in o.text.split("\n"):
                if not ln.strip():
                    continue
                e = ev[h].get((od, ln), [ln])
                if e == "chrome":
                    cb = chrome_block.get(h)
                    if cb is not None:
                        blocks.add(doc_keys[cb - 1])
                else:
                    for piece in e:
                        ask_c.add(wid_of[h], piece, ln)
        for dk in blocks:
            for h, kl, raw in lines_of[dk]:
                ask_c.add(wid_of[h], kl.text, raw)
    res = {"occ": [occ_c.share, occ_t.share], "doc": [per_doc_share(items_c, mis), per_doc_share(items_t, mis)],
           "ask": [ask_c.share, ask_t.share], "ask_tokens": [ask_c.tok, ask_t.tok], "occ_bad": [occ_c.bad, occ_t.bad]}
    for k in ("occ", "doc", "ask"):
        c, t = res[k]
        res[k + "_x"] = c / t if t else None
    return res


def link_per_line(store, truth, lb, out, raws, obs_at) -> dict:
    """Truth-visible link with each distinct (window, stored line) labelled as harness content (not
    terminal) counted once, for today and P1. Today: the share of the line's copies whose frame shows
    it (mean), and its first copy. P1: the frame its kept line links to."""
    copies: dict = defaultdict(list)
    for h, o in store.order:
        for ln in set(o.text.split("\n")):
            if ln.strip():
                copies[h, ln].append(o.ts_ms)
    p1_ts: dict = {}
    for h, st in out.items():
        for kl, raw in zip(st.lines, raws[h], strict=True):
            o = obs_at.get((h, kl.frame_id))
            cur = p1_ts.get((h, raw))
            if o is not None and (cur is None or (cur[1] != "content" and kl.kind == "content")):
                p1_ts[h, raw] = (o.ts_ms, kl.kind)
    wid_of = {w.hwnd: w.wid for w in store.windows}
    vis_cache: dict = {}

    def vis(wid, ts, folded):
        s = vis_cache.get((wid, ts))
        if s is None:
            s = vis_cache[wid, ts] = {truth.line_by_id[i].folded for i in truth.visible_near(wid, ts) if i in truth.line_by_id}
        return folded in s

    n = mean_t = first_t = p1 = missing = 0
    for (h, ln), tss in copies.items():
        wid = wid_of.get(h)
        if wid is None:
            continue
        r = lb.label(wid, ln)
        if not r or r[1] != "content" or r[0].startswith("term:"):
            continue
        folded = truth.line_by_id[r[0]].folded
        got = p1_ts.get((h, ln))
        if got is None:
            missing += 1  # chrome line kept by another window of the app-day
            continue
        n += 1
        tss = sorted(tss)
        oks = [vis(wid, t, folded) for t in tss]
        mean_t += sum(oks) / len(oks)
        first_t += oks[0]
        p1 += vis(wid, got[0], folded)
    return {"lines": n, "missing": missing, "today_mean": mean_t / max(n, 1), "today_first": first_t / max(n, 1), "p1": p1 / max(n, 1)}


def baseline_runs_out(store: S.Store) -> dict[int, S.StoredText]:
    """Today's rows grouped into P1's documents, one copy of each distinct line per document: the same
    basis as noiseB (each distinct stored token once per window run) for today's store."""
    out = {}
    for w in store.windows:
        lines, seen = [], set()
        for o in w.history:
            doc = ("doc", w.hwnd, *MOD.doc_key(o))
            for ln in o.text.split("\n"):
                if ln.strip() and (doc, ln) not in seen:
                    seen.add((doc, ln))
                    lines.append(S.KeptLine(ln, o.frame_id, o.ts_ms, o.ts_ms, doc))
        out[w.hwnd] = S.StoredText(lines, {})
    return out


def eval_run(key: str, reps: int) -> dict:
    t0 = time.perf_counter()
    truth = S.RunTruth(S.RUNS[key])
    store = S.load_store(S.RUNS[key], truth)
    lx = S.Lexicon(truth.strings)
    lb = S.Labeler(truth)
    wins = [(w.history, w.ctx) for w in store.windows]
    obs_at = {(h, o.frame_id): o for h, o in store.order}
    out, timing = R.run_candidate(MOD, wins, reps, store.span_ms)
    sh = MOD.LAST
    index = R.Index(out, obs_at)
    m33_found = sum(index.has(n) for n in truth.nonces)
    base_lines = defaultdict(set)
    for h, o in store.order:
        base_lines[h].update(S.text_lines(o.text))
    base_cov = R.coverage(base_lines, truth, lb)
    cand_lines = defaultdict(set)
    for h, st in out.items():
        for kl in st.lines:
            if kl.kind == "content":
                cand_lines[h].update(S.text_lines(kl.text))
    cand_cov = R.coverage(cand_lines, truth, lb)
    res = {
        "run": S.RUNS[key], "span_h": store.span_ms / 3.6e6, "rows_in": len(store.order),
        "m32": R.m32(out), "m33": m33_found / max(len(truth.nonces), 1), "m33_n": len(truth.nonces),
        "m31": R.m31(store, truth, out, {}),
        "drop": R.drop_metrics(base_cov, cand_cov, truth),
        "link": R.link_metrics(out, obs_at, truth, lb, random.Random(3)),
        "noise": R.noise_metric(index, lx),
        "fts_kb": index.fts_bytes / 1024, "text_kb": index.text_bytes / 1024, "docs": len(index.info), "lines": index.n_lines,
        "crowding": R.crowding(index, truth, {w.hwnd: w.wid for w in store.windows}),
        "timing": timing,
    }
    # --- P1 extras --------------------------------------------------------------------------------
    ex: dict = {}
    # lossless: every stored text rebuilds byte for byte from its ordered line ids
    bad = n = 0
    proc_of = {w.hwnd: w.ctx.process for w in store.windows}
    for h, o in store.order:
        st = sh.stores[proc_of[h], MOD.day_of(o.ts_ms)]
        rebuilt = "\n".join(st.text(st.ids[ln]) if ln.strip() else ln for ln in o.text.split("\n"))
        n += 1
        bad += rebuilt != o.text
    ex["rebuild_rows"], ex["rebuild_mismatch"] = n, bad
    # physical store: line rows (template text, digit slots, edit residuals) and interval rows
    stored = sum(s.stored_chars for s in sh.stores.values())
    raw_line_chars = sum(len(ln) for s in sh.stores.values() for ln in s.ids)
    today_chars = sum(len(o.text) for _, o in store.order)
    rows = Counter()
    for s in sh.stores.values():
        rows["new"] += s.n_new
        rows["digit"] += s.n_digit
        rows["edit"] += s.n_edit
    dup = tot = 0  # M3.2 over physically stored line rows (exact repeats of an already stored row)
    for s in sh.stores.values():
        seen = set()
        for ln in s.ids:
            for x in S.text_lines(ln):
                tot += len(x)
                dup += len(x) * (x in seen)
                seen.add(x)
    n_int = sum(len(v) for v in sh.debug["faithful"].values())
    ex["m32P"] = dup / max(tot, 1)
    ex["stored_chars"], ex["distinct_line_chars"], ex["today_chars"] = stored, raw_line_chars, today_chars
    ex["stored_x"] = stored / max(today_chars, 1)
    ex["intervals"] = n_int
    ex["store_kb_est"] = (stored + 16 * n_int + 12 * sum(rows.values())) / 1024  # + interval rows + line row keys
    ex["line_rows"] = dict(rows)
    # M3.1 on rebuilt frames: every kind, one KeptLine per interval
    faithful = {h: S.StoredText(v) for h, v in sh.debug["faithful"].items()}
    ex["m31R"] = R.m31(store, truth, faithful, {})
    # harness M3.2 by app type (FTS-document view)
    by_app = defaultdict(lambda: [0, 0])
    app_of = {w.hwnd: w.ctx.app for w in store.windows}
    for h, st in out.items():
        seen = set()
        for kl in st.lines:
            for ln in S.text_lines(kl.text):
                by_app[app_of[h]][1] += len(ln)
                if ln in seen:
                    by_app[app_of[h]][0] += len(ln)
                seen.add(ln)
    ex["m32_by_app"] = {a: {"m32": d / max(t, 1), "chars": t} for a, (d, t) in by_app.items()}
    # junk column searchable: M3.3 with garbage lines indexed too
    out_j = {h: S.StoredText([S.KeptLine(kl.text, kl.frame_id, kl.first_ts, kl.last_ts, kl.doc, "content" if kl.kind == "noise" else kl.kind)
                              for kl in st.lines], st.doc_meta) for h, st in out.items()}
    index_j = R.Index(out_j, obs_at)
    ex["m33J"] = sum(index_j.has(nn) for nn in truth.nonces) / max(len(truth.nonces), 1)
    noise_lines = [kl for st in out.values() for kl in st.lines if kl.kind == "noise"]
    ex["noise_lines"] = len(noise_lines)
    ex["noise_lines_findable"] = sum(1 for kl in noise_lines if S.tokens(kl.text))
    # content-as-chrome / content-as-garbage: captured truth lines whose candidate content coverage fell
    # below half, by the kind that still holds them
    cov_kind = {}
    for kind in ("chrome", "noise"):
        ls = defaultdict(set)
        for h, st in out.items():
            for kl in st.lines:
                if kl.kind == kind:
                    ls[h].update(S.text_lines(kl.text))
        cov_kind[kind] = R.coverage(ls, truth, lb)
    wid_app = {w.wid: w.ctx.app for w in store.windows if w.wid}
    tid_wid = {}
    for wid, tls in truth.lines.items():
        for tl in tls:
            tid_wid.setdefault(tl.tid, wid)
    causes = Counter()
    by_app_cause = Counter()
    by_win_cause = Counter()
    cap = {t: c for t, c in base_cov.items() if c >= max(6, 0.3 * len(truth.line_by_id[t].folded))}
    for t, c in cap.items():
        if cand_cov.get(t, 0) >= 0.5 * c:
            continue
        cause = "chrome" if cov_kind["chrome"].get(t, 0) >= 0.5 * c else "garbage" if cov_kind["noise"].get(t, 0) >= 0.5 * c else "other"
        causes[cause] += 1
        wid = tid_wid.get(t)
        by_app_cause[f"{wid_app.get(wid, '?')}|{cause}"] += 1
        by_win_cause[f"{S.keyed_hash(str(wid))}|{cause}"] += 1
    ex["content_down"] = {"captured": len(cap), **causes, "by_app": dict(by_app_cause), "by_window": dict(by_win_cause.most_common(8))}
    # noise views, candidate and today on the same basis
    chrome_doc_of = {}
    for h, st in out.items():
        for kl in st.lines:
            if kl.kind == "chrome":
                chrome_doc_of.setdefault((proc_of[h]), kl.doc)
    rid_of = {k: i for i, k in enumerate(index.docs, 1)}
    chrome_block = {h: rid_of.get(chrome_doc_of.get(proc_of[h])) for h in proc_of}
    chrome_block = {h: r for h, r in chrome_block.items() if r is not None and r in index.info}
    ex["noise_cand"] = noise_views(index, truth, lx, chrome_block)
    ex["noise_cand"]["A_frame"] = noise_evidence(index, out, obs_at, sh.debug["evidence"], truth, lx, chrome_block)["share"]
    base_out = R.run_candidate(BASE, wins, 1, store.span_ms)[0]
    base_index = R.Index(base_out, obs_at)
    ex["noise_today"] = noise_views(base_index, truth, lx, None)
    ex["noise_today"]["B"] = noise_views(R.Index(baseline_runs_out(store), obs_at), truth, lx, None)["B"]
    mis = Misread(truth, lb)
    raws = sh.debug["raw"]
    ex["tnoise"] = truth_noise(store, truth, mis, out, raws, index, base_index, obs_at, sh.debug["evidence"], chrome_block)
    ex["tlink_line"] = link_per_line(store, truth, lb, out, raws, obs_at)
    ex["kinds"] = dict(sum((Counter(v) for v in sh.debug["kinds"].values()), Counter()))
    # unknown tokens left in the harness index: by kind and by harness 'garbled'
    uk = Counter()
    comp = Counter()
    wid_of = {w.hwnd: w.wid for w in store.windows}
    tok_all = garb_all = 0
    for h, st in out.items():
        for kl in st.lines:
            if kl.kind == "noise":
                continue
            lab = None
            for w in S.tokens(kl.text):
                tok_all += 1
                if lx.classify(w) == "unknown":
                    g = lx.garbled(w)
                    garb_all += g
                    uk[f"{kl.kind}|garbled={g}|app={app_of[h]}"] += 1
                    if lab is None:
                        r_ = lb.label(wid_of[h], kl.text) if wid_of[h] else None
                        lab = r_[1] if r_ else "none"
                    comp[f"harness_window={wid_of[h] is not None}|line={lab}|garbled={g}"] += 1
    ex["unknown_left"] = dict(uk)
    ex["unknown_composition"] = dict(comp)
    # ceiling: the harness noise share if every remaining 'garbled' unknown token were removed too
    # (an oracle garbage detector using the metric's own classifier: an upper bound, not a method)
    unk_all = sum(uk.values())
    ex["noise_oracle_garbled_removed"] = (unk_all - garb_all) / max(tok_all - garb_all, 1)
    res["p1"] = ex
    res["runtime_s"] = time.perf_counter() - t0
    store.conn.close()
    return res


def extra_line(r: dict) -> str:
    parts = []
    for k in ("smoke", "standard", "day"):
        if k not in r:
            continue
        s, ex = S.SHORT[k], r[k]["p1"]
        nt, nc = ex["noise_today"], ex["noise_cand"]
        parts += [f"m32P_{s}={ex['m32P']:.3f}", f"cerR_{s}={ex['m31R']['all']['cer']:.4f}", f"m33J_{s}={ex['m33J']:.3f}",
                  f"noiseA_x_{s}={nc['A'] / nt['A']:.2f}", f"noiseAc_x_{s}={nc['A_chrome'] / nt['A']:.2f}", f"noiseAf_x_{s}={nc['A_frame'] / nt['A']:.2f}",
                  f"noiseB_x_{s}={nc['B'] / nt['B']:.2f}",
                  f"stored_x_{s}={ex['stored_x']:.3f}", f"rebuild_bad_{s}={ex['rebuild_mismatch']}"]
        tn, tl = ex["tnoise"], ex["tlink_line"]
        parts += [f"mis_occ_{s}={tn['occ'][0]:.4f}", f"mis_occ_x_{s}={tn['occ_x']:.2f}", f"mis_doc_{s}={tn['doc'][0]:.4f}",
                  f"mis_doc_x_{s}={tn['doc_x']:.2f}", f"mis_ask_{s}={tn['ask'][0]:.4f}", f"mis_ask_x_{s}={tn['ask_x']:.2f}",
                  f"tlinkL_{s}={tl['p1']:.3f}", f"tlinkL_today_{s}={tl['today_mean']:.3f}", f"tlinkL_today1_{s}={tl['today_first']:.3f}"]
    return " ".join(parts)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--runs", default="smoke,standard,day")
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--no-synth", action="store_true")
    ap.add_argument("--set", action="append", default=[], help="tunable=value (cand.P)")
    a = ap.parse_args()
    for kv in a.set:
        k, v = kv.split("=")
        MOD.P[k] = type(MOD.P[k])(eval(v))  # noqa: S307 (local tuning only)
    S.init_corpus()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    t0 = time.perf_counter()
    r: dict = {"candidate": str(HERE / "cand.py"), "params": dict(MOD.P)}
    for k in [x for x in a.runs.split(",") if x]:
        r[k] = eval_run(k, a.reps)
        print(f"{k}: done in {r[k]['runtime_s']:.0f}s", flush=True)
    if not a.no_synth:
        r["synthetic"] = R.eval_synth(MOD, a.reps)
    r["runtime_s"] = time.perf_counter() - t0
    base = json.loads((S.STUDY / "baseline" / "details.json").read_text(encoding="utf-8"))
    line = R.metric_line(r, base) + " " + extra_line(r)
    (out / "details.json").write_text(json.dumps(r, indent=1, default=str), encoding="utf-8")
    (out / "metrics.txt").write_text(line + "\n", encoding="utf-8")
    from plot import side

    side(r, base, out / "side-harness.png", out.name + " (harness definitions)")
    # side.png on the round-2 definitions: noise = misread tokens in Ask evidence (today: hit rows),
    # truth link = each distinct line once (today: mean over its copies)
    import copy

    r2, b2 = copy.deepcopy(r), copy.deepcopy(base)
    for k in ("smoke", "standard", "day"):
        if k in r2 and "p1" in r2[k]:
            tn, tl = r2[k]["p1"]["tnoise"], r2[k]["p1"]["tlink_line"]
            r2[k]["noise"]["share"], b2[k]["noise"]["share"] = tn["ask"]
            r2[k]["link"]["truth_valid"], b2[k]["link"]["truth_valid"] = tl["p1"], tl["today_mean"]
    side(r2, b2, out / "side.png", out.name)
    from PIL import Image, ImageDraw
    import plot as PL

    img = Image.open(out / "side.png")
    d = ImageDraw.Draw(img)
    pw, ph = (PL.W - 16 * 5) / 4, (PL.H - 60 - 16 * 3) / 2
    for (i, j), title in (((3, 0), "misread tokens % of Ask evidence (must <= half)"),
                          ((1, 1), "link valid (frame text) / truth-visible per line")):
        x0, y0 = 16 + i * (pw + 16), 50 + j * (ph + 16)
        d.rectangle((x0 + 4, y0 + 4, x0 + pw - 4, y0 + 30), fill=PL.SURFACE)
        d.text((x0 + 12, y0 + 8), title, font=PL.f(15, True), fill=PL.INK)
    img.save(out / "side.png")
    print(line)


if __name__ == "__main__":
    main()
