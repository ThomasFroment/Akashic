"""Classify C1's lost authorship from the turns it returned (out/C1_linked/details.json)."""

import json
from collections import Counter
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "out"
d = json.loads((OUT / "C1_linked" / "details.json").read_text(encoding="utf-8"))
why = Counter()
for s in d["segments"]:
    if s["expected"] is None or s["authored"]:
        continue
    ts = s["turns"]
    ids = {t[2] for t in ts}
    if not ts:
        k = "no speaker"
    elif len(ts) == 1 and ts[0][2] is None:
        k = "one speaker, unidentified/inconsistent"
    elif len(ts) == 1:
        k = "one speaker identified, lead veto" if s["kind"] == "cont_second" else "one speaker, wrong profile id?"
    elif None in ids and len(ids - {None}) <= 1:
        k = f"{len(ts)} speakers, one unidentified"
    elif len(ids) == 1:
        k = f"{len(ts)} speakers same id ?"
    else:
        k = f"{len(ts)} speakers, different ids"
    why[(s["kind"], k)] += 1
for (kind, k), n in sorted(why.items(), key=lambda x: -x[1]):
    print(f"{n:4d} {kind:12s} {k}")
