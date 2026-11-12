r"""Per-subset table (reference, C2_calib, round-2 candidates) from details.json, and failure classes
of each round-2 candidate (with reasons.jsonl from a R2SPAN_REASONS=1 harness run).

    ..\.venv\Scripts\python.exe candidates\R2span_compare.py R2span_C1 R2span_C2
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from common import FIXTURE, OUT  # noqa: E402

ROWS = [("ALL", None), ("solo", "auth"), ("solo_10k", "auth"), ("solo_10_0", "auth"), ("similar", "auth"),
        ("cont_first", "auth"), ("cont_second", "auth"), ("cont_same", "auth"), ("cont_switch", "auth"),
        ("cont_switch_early", "auth"), ("mixed", "wrong"), ("mixed_alt", "wrong"), ("mixed_backchannel", "wrong"),
        ("mixed_lastwin", "wrong"), ("mixed_reply", "wrong"), ("impostor", "wrong"), ("cont_first", "wrong"),
        ("similar", "wrong")]  # fmt: skip


def main() -> None:
    names = ["reference", "C2_calib"] + sys.argv[1:]
    D = {n: json.loads((OUT / n / "details.json").read_text(encoding="utf-8")) for n in names}
    ix = {e["id"]: e for e in json.loads((FIXTURE / "index.json").read_text(encoding="utf-8"))["segments"]}
    roles = json.loads((FIXTURE / "index.json").read_text(encoding="utf-8"))["roles"]

    def sub(d, s, metric):
        rows = [r for r in d["segments"] if s in r["subsets"]]
        if metric == "auth":
            rows = [r for r in rows if r["expected"] is not None]
            return sum(r["authored"] for r in rows), len(rows)
        return sum(r["wrong"] for r in rows), len(rows)

    print("| subset | metric | " + " | ".join(names) + " |")
    print("| :- | :- | " + " | ".join("-:" for _ in names) + " |")
    print("| ALL | authorship | " + " | ".join(f"{d['summary']['authorship'][0]:.3f}" for d in D.values()) + " |")
    print("| ALL | wrong | " + " | ".join(f"{d['summary']['wrong'][0]:.4f}" for d in D.values()) + " |")
    se = lambda d: [r for r in d["segments"] if ix[r["id"]].get("cont_mode") == "switch_early" and ix[r["id"]]["kind"] == "cont_first"]  # noqa: E731
    for s, m in ROWS[1:]:
        cells = []
        for d in D.values():
            a, n = sub(d, s, m)
            cells.append(f"{a / n:.3f} ({a}/{n})" if n else "na")
        print(f"| {s} | {'authorship' if m == 'auth' else 'wrong'} | " + " | ".join(cells) + " |")
    cells = []
    for d in D.values():
        rows = se(d)
        cells.append(f"{sum(r['wrong'] for r in rows) / len(rows):.3f} ({sum(r['wrong'] for r in rows)}/{len(rows)})")
    print("| switch_early first | wrong | " + " | ".join(cells) + " |")
    for n, d in D.items():
        t = d["summary"]["timing"]
        print(f"{n}: p50 {t['p50_ms']:.0f} p95 {t['p95_ms']:.0f} profiles_end {d['n_profiles_end']} "
              f"drift wrong_all {(d.get('drift') or {}).get('wrong_all')}")  # fmt: skip

    for n in sys.argv[1:]:
        d = D[n]
        rp = OUT / n / "reasons.jsonl"
        reasons = {}
        if rp.is_file():
            for line in rp.read_text(encoding="utf-8").splitlines():
                r = json.loads(line)
                reasons.setdefault(r["id"], r)  # first = evaluation stream (drift comes after)
        wrong, lost = Counter(), Counter()
        for r in d["segments"]:
            e = ix[r["id"]]
            if r["wrong"]:
                if e["single"] is not None:
                    cls = "impostor->enrolled" if roles.get(e["single"]) != "enrolled" else "solo->other profile"
                elif e["kind"] == "cont_first":
                    cls = "switch_early first (mixed lead-out)"
                else:
                    other = max(v for s, v in e["speech_ms_after_lead"].items() if s != r["author"]) if len(e["speech_ms_after_lead"]) > 1 else 0
                    tag = [s for s in ("mixed_backchannel", "mixed_reply", "mixed_alt", "similar") if s in e["subsets"]]
                    cls = f"mixed {tag[0] if tag else '?'}, other voice {'<1 s' if other < 1000 else '>=1 s'}"
                wrong[cls] += 1
            elif r["expected"] is not None and not r["authored"]:
                rs = reasons.get(r["id"], {}).get("reason") or ("other author" if r["author"] else "?")
                lost[(rs, e["kind"] if e["kind"] != "cont_second" else "cont_second/" + e.get("cont_mode", ""))] += 1
        print(f"\n{n} wrong-author classes ({sum(wrong.values())}):")
        for k, v in wrong.most_common():
            print(f"  {v:3d}  {k}")
        print(f"{n} lost authorship classes ({sum(lost.values())}):")
        for k, v in lost.most_common():
            print(f"  {v:3d}  {k}")


if __name__ == "__main__":
    main()
