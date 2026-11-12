r"""Paired timing as C2_timing.py: reference, C2_calib and the round-2 candidates' attribute() on the
same 300 segments, interleaved with a rotating order so all see the same machine load.
Ratios to the reference per segment.  -> out\R2span_C1\timing_<tag>.json

    ..\.venv\Scripts\python.exe candidates\R2span_timing.py <tag> [N]
(set C2_CALIB_FILE=calib_iter2.json; OPENBLAS_NUM_THREADS is recorded as given)
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

from common import FIXTURE, OUT, SR, Segment, SharedModels  # noqa: E402
from harness import decode, enrol_all, load_candidate  # noqa: E402

NAMES = ("reference", "C2_calib", "R2span_C1", "R2span_C2")


def main() -> None:
    tag = sys.argv[1] if len(sys.argv) > 1 else "default"
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 300
    index = json.loads((FIXTURE / "index.json").read_text(encoding="utf-8"))
    blob = (FIXTURE / "segments.bin").read_bytes()
    models = SharedModels()
    warm = np.random.default_rng(0).standard_normal(SR * 3).astype(np.float32) * 0.05
    models.turns(warm)
    models.embed(warm)
    cands = {name: load_candidate(str(HERE / f"{name}.py")) for name in NAMES}
    stores = {}
    for name, c in cands.items():
        stores[name] = c.make_store()
        enrol_all(c, stores[name], models, index, blob)
    segs = [e for e in index["segments"] if e["kind"] not in ("drift", "cont_second")]
    order = np.random.default_rng(1).permutation(len(segs))[:n]
    # warm each candidate once (session creation of the derived model)
    e0 = segs[order[0]]
    for name in NAMES:
        cands[name].attribute(stores[name], models, Segment(-1, decode(blob, e0), SR, 0, None))
    times = {k: [] for k in NAMES}
    lengths = []
    for j, i in enumerate(order):
        e = segs[i]
        x = decode(blob, e)
        seg = Segment(e["id"], x, SR, 0, None)
        rot = j % len(NAMES)
        for name in NAMES[rot:] + NAMES[:rot]:
            t = time.perf_counter()
            cands[name].attribute(stores[name], models, seg)
            times[name].append((time.perf_counter() - t) * 1000)
        lengths.append(e["length_ms"])
    r = np.array(times["reference"])
    out = {"n": n, "openblas_threads": os.environ.get("OPENBLAS_NUM_THREADS", "default"),
           "ref_p50": float(np.percentile(r, 50)), "ref_p95": float(np.percentile(r, 95))}  # fmt: skip
    for name in NAMES[1:]:
        c = np.array(times[name])
        q = c / r
        out[name] = {
            "p50_ms": float(np.percentile(c, 50)), "p95_ms": float(np.percentile(c, 95)),
            "ratio_total": float(c.sum() / r.sum()),
            "ratio_p50": float(np.percentile(q, 50)), "ratio_p95": float(np.percentile(q, 95)),
            "ratio_max": float(q.max()),
            "worst": sorted(zip(q.tolist(), lengths, strict=True))[-5:],
        }  # fmt: skip
    (OUT / "R2span_C1" / f"timing_{tag}.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
