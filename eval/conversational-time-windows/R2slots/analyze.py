"""Dev inspection: model-path failures and regressions vs the rules module, per question.

    python R2slots/analyze.py <phrases_file> [--all]

Uses harness.score_case on cached model outputs only (questions without a cached output are
skipped and counted). Env R2_DISJOINT / R2_VETO select the merge variant.
"""

from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import harness  # noqa: E402

import candidate as cand  # noqa: E402

TZ = ZoneInfo("Europe/Paris")


def main() -> None:
    path = Path(sys.argv[1])
    if not path.exists():
        path = HERE.parent / sys.argv[1]
    show_all = "--all" in sys.argv
    recs = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]
    n = p_model = p_rules = 0
    skipped = 0
    hows: Counter = Counter()
    fails: dict = defaultdict(list)
    regress = gains = 0
    for rec in recs:
        q = rec["question"]
        raw = cand.CACHE.get(q)
        if raw is None:
            skipped += 1
            continue
        for lab in rec["labels"]:
            now = datetime.fromisoformat(lab["now"])
            gold = None if lab["gold"] is None else tuple(map(datetime.fromisoformat, lab["gold"]))
            win, how = cand.merge(q, now, raw)
            rw = cand.R.resolve_question(q, now, None)
            sm = harness.score_case(win, gold, now)
            sr = harness.score_case(rw, gold, now)
            n += 1
            p_model += sm["pass"]
            p_rules += sr["pass"]
            hows[how] += 1
            regress += sr["pass"] and not sm["pass"]
            gains += sm["pass"] and not sr["pass"]
            if sm["future"]:
                print("FUTURE", q, lab["now_label"])
            if not sm["pass"] or show_all:
                fails[(rec["id"], rec["category"], q)].append(
                    (lab["now_label"], how, "R" if sr["pass"] else "-",
                     None if win is None else f"{win[0]:%a %H:%M}-{win[1]:%a %H:%M}",
                     None if gold is None else f"{gold[0]:%a %H:%M}-{gold[1]:%a %H:%M}")
                )
    for (i, cat, q), v in fails.items():
        w = cand._parse_when(cand.CACHE[q])  # noqa: SLF001
        ws = {k: w[k] for k in ("span", "day", "part", "clock", "special", "n")
              if w and w[k] not in ("none", "", 0)} if w else None  # fmt: skip
        print(f"{i} {cat} | {q} | M={ws}")
        for row in v:
            print("     ", *row)
    print(f"cases={n} skipped_questions={skipped} model_pass={p_model / n:.4f} "
          f"rules_pass={p_rules / n:.4f} regressions={regress} gains={gains}")  # fmt: skip
    print(dict(hows))


if __name__ == "__main__":
    main()
