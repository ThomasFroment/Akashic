"""Foreground-change and process-change rates in the observer truth. Numbers only."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "eval" / "quality"))
import score
for rid in sys.argv[1:]:
    tr = score.Truth(score.RESULTS / rid)
    h = (tr.t1 - tr.t0) / 3_600_000
    fg = [e for e in tr.os if e["t"] == "fg"]
    proc = sum(1 for a, b in zip(fg, fg[1:]) if (a.get("exe") or "").lower() != (b.get("exe") or "").lower())
    print(rid, "hours", round(h, 2), "fg_per_h", round(len(fg) / h, 1), "process_changes_per_h", round(proc / h, 1))
