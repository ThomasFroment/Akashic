"""Back-to-back timing of the seam wrapper vs its base on the continuation pairs.

    ..\\.venv\\Scripts\\python.exe candidates\\R2seam_timing.py candidates\\C2_calib.py

Configs, each with its own enrolled store, fed the same 120 pairs (N then N+1) in the fixture's
seeded order; per segment the configs run back to back in a rotating order on one SharedModels:
  base       the base module as is (the fixture's lead_ms: C2 drops the lead)
  ctl_t0     wrapper, SEAM_RULE=0, SEAM_TRIMMED=0 (lead counted as body, no seam rule)
  seam_t0    wrapper, rule on, trimmed unknown (lead as body)
  seam_t1    wrapper, rule on, trimmed = 1
Writes out/R2seam_timing_<base>/timing.json and prints ratios (sum of ms over segments).
"""

import importlib.util
import json
import os
import sys
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
os.environ.setdefault("C2_CALIB_FILE", "calib_iter2.json")

import harness as H  # noqa: E402
from common import FIXTURE, OUT, SR, PrevSegment, Segment, SharedModels  # noqa: E402


def load(path: Path, modname: str, env: dict):
    os.environ.update(env)
    spec = importlib.util.spec_from_file_location(modname, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    spec.loader.exec_module(mod)
    return mod


def main() -> None:
    base_path = Path(sys.argv[1]).resolve()
    stem = base_path.stem
    cfgs = {
        "base": load(base_path, f"t_base_{stem}", {}),
        "ctl_t0": load(HERE / "R2seam_wrap.py", "t_ctl", {"SEAM_BASE": str(base_path), "SEAM_TRIMMED": "0", "SEAM_RULE": "0"}),
        "seam_t0": load(HERE / "R2seam_wrap.py", "t_s0", {"SEAM_BASE": str(base_path), "SEAM_TRIMMED": "0", "SEAM_RULE": "1"}),
        "seam_t1": load(HERE / "R2seam_wrap.py", "t_s1", {"SEAM_BASE": str(base_path), "SEAM_TRIMMED": "1", "SEAM_RULE": "1"}),
    }  # fmt: skip
    index = json.loads((FIXTURE / "index.json").read_text(encoding="utf-8"))
    blob = (FIXTURE / "segments.bin").read_bytes()
    models = SharedModels()
    warm = np.zeros(SR * 3, np.float32)
    warm[: SR * 2] = np.random.default_rng(0).standard_normal(SR * 2).astype(np.float32) * 0.05
    models.turns(warm)
    models.embed(warm)
    stores = {}
    for k, m in cfgs.items():
        stores[k] = m.make_store()
        H.enrol_all(m, stores[k], models, index, blob)
    firsts = [e for e in index["segments"] if e["kind"] == "cont_first"]
    order = np.random.default_rng(index["seed"]).permutation(len(firsts))
    second = {e["continues"]: e for e in index["segments"] if e["kind"] == "cont_second"}
    names = list(cfgs)
    ms = {k: {"first": [], "second": []} for k in names}
    fw = {k: {"seg_n": 0, "emb_n": 0} for k in names}
    step = 0
    for i in order:
        n, n1 = firsts[i], second[firsts[i]["id"]]
        xs = H.decode(blob, n)
        x1 = H.decode(blob, n1)
        rot = names[step % 4 :] + names[: step % 4]
        step += 1
        prev_auth = {}
        for k in rot:  # N
            models.reset_counter()
            t = time.perf_counter()
            a = cfgs[k].attribute(stores[k], models, Segment(n["id"], xs, SR, 0, None))
            ms[k]["first"].append((time.perf_counter() - t) * 1000)
            fw[k]["seg_n"] += models.counter["seg_n"]
            fw[k]["emb_n"] += models.counter["emb_n"]
            prev_auth[k] = a.author
        for k in rot[::-1]:  # N+1
            prev = PrevSegment(n["id"], prev_auth[k], xs)
            models.reset_counter()
            t = time.perf_counter()
            cfgs[k].attribute(stores[k], models, Segment(n1["id"], x1, SR, n1["lead_ms"], prev))
            ms[k]["second"].append((time.perf_counter() - t) * 1000)
            fw[k]["seg_n"] += models.counter["seg_n"]
            fw[k]["emb_n"] += models.counter["emb_n"]
    out = {"base": stem, "pairs": len(order)}
    for k in names:
        f, s = np.array(ms[k]["first"]), np.array(ms[k]["second"])
        out[k] = {"first_sum": float(f.sum()), "second_sum": float(s.sum()),
                  "pair_sum": float(f.sum() + s.sum()), "second_p50": float(np.median(s)),
                  "seg_forwards": int(fw[k]["seg_n"]), "emb_forwards": int(fw[k]["emb_n"])}  # fmt: skip
    for k in names[1:]:
        out[k]["ratio_vs_base_pair"] = out[k]["pair_sum"] / out["base"]["pair_sum"]
        out[k]["ratio_vs_base_second"] = out[k]["second_sum"] / out["base"]["second_sum"]
        out[k]["ratio_vs_ctl_pair"] = out[k]["pair_sum"] / out["ctl_t0"]["pair_sum"]
    d = OUT / f"R2seam_timing_{stem}"
    d.mkdir(parents=True, exist_ok=True)
    (d / "timing.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
