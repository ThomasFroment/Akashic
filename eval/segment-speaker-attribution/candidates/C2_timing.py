"""Paired timing: reference vs C2_calib attribute() on the same segments, interleaved, so both see
the same machine load (other jobs share this CPU). -> out\\C2_calib\\timing.json

    ..\\.venv\\Scripts\\python.exe candidates\\C2_timing.py [N]
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

from common import FIXTURE, OUT, SR, Segment, SharedModels  # noqa: E402
from harness import decode, enrol_all, load_candidate  # noqa: E402


def main() -> None:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 300
    index = json.loads((FIXTURE / "index.json").read_text(encoding="utf-8"))
    blob = (FIXTURE / "segments.bin").read_bytes()
    models = SharedModels()
    warm = np.random.default_rng(0).standard_normal(SR * 3).astype(np.float32) * 0.05
    models.turns(warm)
    models.embed(warm)
    cands = {name: load_candidate(str(HERE / f"{name}.py")) for name in ("reference", "C2_calib")}
    stores = {}
    for name, c in cands.items():
        stores[name] = c.make_store()
        enrol_all(c, stores[name], models, index, blob)
    segs = [e for e in index["segments"] if e["kind"] not in ("drift", "cont_second")]
    order = np.random.default_rng(1).permutation(len(segs))[:n]
    times = {k: [] for k in cands}
    lengths = []
    for j, i in enumerate(order):
        e = segs[i]
        x = decode(blob, e)
        seg = Segment(e["id"], x, SR, 0, None)
        names = list(cands) if j % 2 == 0 else list(cands)[::-1]
        for name in names:
            t = time.perf_counter()
            cands[name].attribute(stores[name], models, seg)
            times[name].append((time.perf_counter() - t) * 1000)
        lengths.append(e["length_ms"])
    r, c = np.array(times["reference"]), np.array(times["C2_calib"])
    out = {
        "n": n,
        "ref_p50": float(np.percentile(r, 50)), "ref_p95": float(np.percentile(r, 95)),
        "c2_p50": float(np.percentile(c, 50)), "c2_p95": float(np.percentile(c, 95)),
        "ratio_total": float(c.sum() / r.sum()), "ratio_median_per_seg": float(np.median(c / r)),
        "ratio_p95_per_seg": float(np.percentile(c / r, 95)),
        "worst": sorted(zip((c / r).tolist(), lengths, strict=True))[-5:],
    }  # fmt: skip
    (OUT / "C2_calib" / "timing.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
