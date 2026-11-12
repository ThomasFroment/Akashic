"""Lead envelope correlation of every continuation pair, and against a wrong (shuffled) prev."""

import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import FIXTURE, LEAD_MS, SR  # noqa: E402
from harness import decode  # noqa: E402

import C1_linked as c1  # noqa: E402

index = json.loads((FIXTURE / "index.json").read_text(encoding="utf-8"))
blob = (FIXTURE / "segments.bin").read_bytes()
by_id = {e["id"]: e for e in index["segments"]}
lead = LEAD_MS * SR // 1000
good, bad = [], []
seconds = [e for e in index["segments"] if e["kind"] == "cont_second"]
firsts = [by_id[e["continues"]] for e in seconds]
for k, e in enumerate(seconds):
    cur = decode(blob, e)[:lead]
    for prev, out in ((firsts[k], good), (firsts[(k + 7) % len(firsts)], bad)):
        p = decode(blob, prev)[-lead:]
        a, b = c1._envelope(cur), c1._envelope(p)  # noqa: SLF001
        out.append(float(np.corrcoef(a, b)[0, 1]))
for name, v in (("true prev", good), ("wrong prev", bad)):
    v = np.array(v)
    print(name, "min %.3f p5 %.3f p50 %.3f max %.3f" % (v.min(), *np.percentile(v, [5, 50]), v.max()),
          "pass@0.8", float((v >= c1.LEAD_ENV_CORR).mean()))
