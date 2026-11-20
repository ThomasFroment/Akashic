"""Scoring wrapper for the round-2 musts (user answers 2026-11-20, brief.md). Harness code is reused,
not edited. Numbers only.

  noise (misread): over the indexed lines (kinds content + chrome = what search and Ask see), the
    share of tokens that are provably misread harness text: the line labels (harness Labeler) to a
    truth line of its truth-labelled window, and the token is not a token of that truth line (a cut
    word at either end of the line counts only if it is not a prefix/suffix of a truth token).
    Denominator = every indexed token. Two countings:
      occ  : every token of every indexed line (harness noise_metric counting)
      doc  : each distinct token once per FTS document (user answer 1: once per window run; today's
             document is one frame row)
    Guard 'tok' (fragment-proof, independent of line labelling): a token of an indexed line in a
    truth-labelled window that is in none of that window's truth tokens but within 25 % edits of one.
  tlink per line: truth-visible link validity with each distinct line weighted once: per (hwnd,
    truth line id) the mean validity over its (line, link frame) pairs, then the mean over lines.
    Frame-text validity is the harness link metric (unchanged).

    uv run python .../score_v2.py CAND.py OUT.json [runs]
"""

from __future__ import annotations

import json
import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "harness"))
import replay as R  # noqa: E402
import shared as S  # noqa: E402

INDEXED = ("content", "chrome")


def truth_tokens(truth: S.RunTruth) -> dict[str, frozenset]:
    return {tid: frozenset(S.fts_tokens(tl.text)) for tid, tl in truth.line_by_id.items()}


def misread_tokens(toks: list[str], ttoks: frozenset) -> list[bool]:
    out = []
    n = len(toks)
    for i, t in enumerate(toks):
        if t in ttoks:
            out.append(False)
            continue
        edge = i == 0 or i == n - 1
        if edge and any(x.startswith(t) or x.endswith(t) for x in ttoks):
            out.append(False)
            continue
        out.append(True)
    return out


class WinVocab:
    """Truth tokens of one window, for the fragment-proof guard."""

    def __init__(self, truth: S.RunTruth, wid: str) -> None:
        self.vocab = {t for tl in truth.lines.get(wid, []) for t in S.fts_tokens(tl.text)}
        self.by_len: dict[int, list[str]] = defaultdict(list)
        for t in self.vocab:
            self.by_len[len(t)].append(t)
        self.cache: dict[str, bool] = {}

    def near_miss(self, t: str) -> bool:
        if t in self.vocab or len(t) < 3:
            return False
        r = self.cache.get(t)
        if r is None:
            b = max(1, int(0.25 * len(t)))
            r = any(S.lev(t, x) <= b for n in range(len(t) - b, len(t) + b + 1) for x in self.by_len.get(n, ()))
            self.cache[t] = r
        return r


def evaluate(mod, key: str) -> dict:
    t0 = time.perf_counter()
    truth = S.RunTruth(S.RUNS[key])
    store = S.load_store(S.RUNS[key], truth)
    lb = S.Labeler(truth)
    tt = truth_tokens(truth)
    wins = [(w.history, w.ctx) for w in store.windows]
    obs_at = {(h, o.frame_id): o for h, o in store.order}
    out, _ = R.run_candidate(mod, wins, 1, store.span_ms)
    vocabs: dict[str, WinVocab] = {}
    occ_tok = occ_mis = 0
    doc_seen: dict = defaultdict(set)
    doc_tok = doc_mis = 0
    guard_mis = 0
    pairs: dict[tuple, list] = defaultdict(list)
    seen_pair = set()
    for h, st in out.items():
        wid = truth.hwnd_to_wid.get(h)
        for kl in st.lines:
            if kl.kind not in INDEXED:
                continue
            toks = S.fts_tokens(kl.text)
            if not toks:
                continue
            lab = lb.label(wid, kl.text) if wid else None
            mis = misread_tokens(toks, tt[lab[0]]) if lab else [False] * len(toks)
            if wid:
                v = vocabs.get(wid) or vocabs.setdefault(wid, WinVocab(truth, wid))
                guard_mis += sum(v.near_miss(t) for t in toks)
            occ_tok += len(toks)
            occ_mis += sum(mis)
            ds = doc_seen[kl.doc]
            for t, m in zip(toks, mis, strict=True):
                if t not in ds:
                    ds.add(t)
                    doc_tok += 1
                    doc_mis += m
            # per-line truth link
            if kl.kind == "content" and lab and lab[1] == "content" and not lab[0].startswith("term:"):
                if (lab[0], kl.frame_id) in seen_pair:
                    continue
                seen_pair.add((lab[0], kl.frame_id))
                o = obs_at.get((h, kl.frame_id))
                if o is None:
                    continue
                vis = {truth.line_by_id[i].folded for i in truth.visible_near(wid, o.ts_ms) if i in truth.line_by_id}
                pairs[h, lab[0]].append(truth.line_by_id[lab[0]].folded in vis)
    per_line = [statistics.fmean(v) for v in pairs.values()]
    res = {
        "noise_occ": occ_mis / max(occ_tok, 1), "noise_occ_n": occ_mis, "tokens_occ": occ_tok,
        "noise_doc": doc_mis / max(doc_tok, 1), "noise_doc_n": doc_mis, "tokens_doc": doc_tok,
        "noise_guard": guard_mis / max(occ_tok, 1), "noise_guard_n": guard_mis,
        "tlink_line": statistics.fmean(per_line) if per_line else 0.0, "tlink_lines": len(per_line),
        "tlink_lines_invalid": sum(x == 0 for x in per_line),
        "runtime_s": time.perf_counter() - t0,
    }
    store.conn.close()
    return res


def main() -> None:
    cand, outp = sys.argv[1], Path(sys.argv[2])
    runs = (sys.argv[3] if len(sys.argv) > 3 else "smoke,standard,day").split(",")
    S.init_corpus()
    mod = R.load_module(cand)
    res = {"candidate": str(Path(cand).resolve())}
    for k in runs:
        res[k] = evaluate(mod, k)
        print(k, {a: (round(b, 4) if isinstance(b, float) else b) for a, b in res[k].items()}, flush=True)
    outp.write_text(json.dumps(res, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
