"""Throwaway: where the misread tokens P1 still indexes sit (counts only): lines with another reading
of the same place (edit-paired, so consensus could see them) vs single stable readings; by source."""

from __future__ import annotations

import sys
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "harness"))
sys.path.insert(0, str(HERE))
import replay as R  # noqa: E402
import run_p1 as P1  # noqa: E402
import shared as S  # noqa: E402


def main(key: str) -> None:
    truth = S.RunTruth(S.RUNS[key])
    store = S.load_store(S.RUNS[key], truth)
    lb = S.Labeler(truth)
    wins = [(w.history, w.ctx) for w in store.windows]
    out, _ = R.run_candidate(P1.MOD, wins, 1, store.span_ms)
    sh = P1.MOD.LAST
    mis = P1.Misread(truth, lb)
    src = {}
    for h, o in store.order:
        for ln in o.text.split("\n"):
            src.setdefault((h, ln), o.source)
    proc = {w.hwnd: w.ctx.process for w in store.windows}
    wid_of = {w.hwnd: w.wid for w in store.windows}
    # lines in a doc that are paired with another line of the same doc (either side)
    c = Counter()
    for h, st in out.items():
        docs = defaultdict(set)
        for kl, raw in zip(st.lines, sh.debug["raw"][h], strict=True):
            docs[kl.doc].add(raw)
        paired_raw = set()
        for doc, raws in docs.items():
            if doc[0] != "doc":
                continue
            store_ = sh.stores[(proc[h], doc[4])]
            for raw in raws:
                lid = store_.ids.get(raw)
                if lid is not None and store_.rows[lid][0] == "E":
                    base = store_.text(store_.rows[lid][1])
                    if base in raws:
                        paired_raw.update({(doc, raw), (doc, base)})
        for kl, raw in zip(st.lines, sh.debug["raw"][h], strict=True):
            if kl.kind not in R.INDEXED:
                continue
            b = mis.bad(wid_of[h], raw)
            n = sum(t in b for t in S.fts_tokens(kl.text))
            if not n:
                continue
            c["mis"] += n
            c[f"paired_in_doc={(kl.doc, raw) in paired_raw}"] += n
            c[f"src={src.get((h, raw))}"] += n
            c[f"kind={kl.kind}"] += n
    print(f"[{key}] misread token occurrences indexed: {c['mis']}")
    for k in sorted(x for x in c if x != "mis"):
        print(f"  {k}: {c[k] / c['mis']:.3f}")


if __name__ == "__main__":
    for k in sys.argv[1:]:
        main(k)
