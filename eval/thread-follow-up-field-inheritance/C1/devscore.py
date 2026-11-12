"""Dev-only scorer: per-field accuracy and error rows on the dev split (held-out filtered out)."""

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import harness as h  # noqa: E402

_, fn, _ = h.load_candidate(HERE)
pairs = [p for p in h.load_pairs() if p["split"] == "dev"]
res = h.evaluate(fn, h.prepare(pairs))
rows = res["rows"]
print("dev", h.accuracy(rows)["all"], "exceptions", res["exceptions"])
for lang, a in h.accuracy(rows, "lang").items():
    print(" ", lang, round(a["min"], 3))
for cat, a in h.accuracy(rows, "category").items():
    print(" ", cat, {f: round(a[f], 2) for f in h.FIELDS}, a["n"])
for r in rows:
    bad = {f: v for f, v in r["fields"].items() if not v["ok"]}
    if bad:
        print(f"[{r['id']}] {r['prev_question']!r} -> {r['question']!r} {r['error'] or ''}")
        for f, v in bad.items():
            print(f"    {f}: gold={v['gold']} got={v['got']} C={v['candidate']} S={v['standalone']} E={v['earlier']}")
