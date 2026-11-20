"""Why content lines are dropped by P2 (counts only).

For every dropped truth line: did a stored reading of it exist, and was that reading a minority
variant (voted away), part of a chrome track, or flagged as garbage? Also writes nothing but counts.
"""

from __future__ import annotations

import sys
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "harness"))
sys.path.insert(0, str(HERE))
import replay as R  # noqa: E402
import shared as S  # noqa: E402
import cand  # noqa: E402


def analyse(key: str) -> Counter:
    S.init_corpus()
    truth = S.RunTruth(S.RUNS[key])
    store = S.load_store(S.RUNS[key], truth)
    lb = S.Labeler(truth)
    cand.READ_LOG = []
    sh = cand.prepare(R.fresh_windows([(w.history, w.ctx) for w in store.windows]))
    out = sh["out"]
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
    cap = {t: c for t, c in base_cov.items() if c >= max(6, 0.3 * len(truth.line_by_id[t].folded))}
    dropped = {t for t, c in cap.items() if cand_cov.get(t, 0) < 0.5 * c}
    # readings that label to a dropped line
    why: dict[str, set] = defaultdict(set)
    for h, rd, canon, chrome, nread in cand.READ_LOG:
        wid = truth.hwnd_to_wid.get(h)
        if wid is None:
            continue
        r = lb.label(wid, rd)
        if not r or r[0] not in dropped:
            continue
        rc = lb.label(wid, canon)
        if chrome:
            why[r[0]].add("chrome")
        elif rd != canon:
            if rc and rc[0] != r[0]:
                same_text = truth.line_by_id[rc[0]].folded == truth.line_by_id[r[0]].folded
                why[r[0]].add("minority_other_line_" + ("same_truth_text" if same_text else "diff_truth_text"))
            else:
                why[r[0]].add("minority_same_line" if rc else "minority_unlabelled_canon")
        else:
            why[r[0]].add("canonical_garbage_cut")
    c: Counter = Counter()
    c["captured"] = len(cap)
    c["dropped"] = len(dropped)
    for t in dropped:
        k = "+".join(sorted(why.get(t, {"no_reading"})))
        c["why", k, "term" if t.startswith("term:") else "page"] += 1
    return c


if __name__ == "__main__":
    import json

    res = {}
    for run in (sys.argv[1] if len(sys.argv) > 1 else "smoke,standard,day").split(","):
        c = analyse(run)
        res[run] = {"|".join(map(str, k)) if isinstance(k, tuple) else k: v for k, v in c.items()}
        for k, v in sorted(c.items(), key=lambda kv: str(kv[0])):
            print(run, k, v)
    (HERE / "drop_causes.json").write_text(json.dumps(res, indent=1), encoding="utf-8")
