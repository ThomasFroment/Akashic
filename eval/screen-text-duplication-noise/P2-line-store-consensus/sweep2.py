"""Round-2 sweep: harness metrics + score_v2 (misread noise, per-line truth link) per P2 config.

    uv run python .../sweep2.py OUT.json [runs]
Configs run 3 at a time, each as subprocesses with P2_* overrides (1 rep, no synthetic). Numbers only.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPLAY = HERE.parent / "harness" / "replay.py"
OFF = {"SHORT_GARBAGE": False, "MISREAD": False, "USE_LM": False, "N6_ON": False, "OCR_CONSENSUS": False}
CONFIGS = {
    "none": OFF,
    "n6+short+misread": {"OCR_CONSENSUS": False},
    "+consensus (final)": {},
    "+consensus/split": {"MODE": "split"},
    "+consensus+lm": {"LM_MIN_TYPES": 300},
    "+consensus+lm/split": {"LM_MIN_TYPES": 300, "MODE": "split"},
    "+consensus/nopair": {"D6_LONG": 0, "MAX_SLOT_CHANGES": 0},
    "+consensus/d6long2": {"D6_LONG": 2},
    "none/nopair": {**OFF, "D6_LONG": 0, "MAX_SLOT_CHANGES": 0},
}


def run(name: str, cfg: dict, runs: str) -> tuple[str, dict]:
    env = dict(os.environ)
    env.update({f"P2_{k}": repr(v) for k, v in cfg.items()})
    with tempfile.TemporaryDirectory() as td:
        subprocess.run([sys.executable, str(REPLAY), str(HERE / "cand.py"), td, "--runs", runs, "--reps", "1", "--no-synth"],
                       env=env, check=True, capture_output=True)
        d = json.loads((Path(td) / "details.json").read_text(encoding="utf-8"))
        subprocess.run([sys.executable, str(HERE / "score_v2.py"), str(HERE / "cand.py"), str(Path(td) / "v2.json"), runs],
                       env=env, check=True, capture_output=True)
        v = json.loads((Path(td) / "v2.json").read_text(encoding="utf-8"))
    return name, {k: {"noise_old": d[k]["noise"]["share"], "drop": d[k]["drop"]["all"]["rate"], "cdrop": d[k]["drop"]["all"]["char_rate"],
                      "dropped": d[k]["drop"]["all"]["dropped"], "captured": d[k]["drop"]["all"]["captured"],
                      "m33": d[k]["m33"], "m32": d[k]["m32"], "link": d[k]["link"]["valid"],
                      "noise": v[k]["noise_occ"], "noise_doc": v[k]["noise_doc"], "noise_guard": v[k]["noise_guard"],
                      "noise_n": v[k]["noise_occ_n"], "tlink_line": v[k]["tlink_line"]} for k in runs.split(",")}


def main() -> None:
    out = Path(sys.argv[1])
    runs = sys.argv[2] if len(sys.argv) > 2 else "smoke,standard,day"
    res = json.loads(out.read_text()) if out.is_file() else {}
    todo = [(n, c) for n, c in CONFIGS.items() if n not in res]
    with ThreadPoolExecutor(3) as ex:
        for name, r in ex.map(lambda nc: run(nc[0], nc[1], runs), todo):
            res[name] = r
            out.write_text(json.dumps(res, indent=1))
            print(name, {k: (round(v["noise"], 4), round(v["drop"], 4), round(v["m33"], 3)) for k, v in r.items()}, flush=True)


if __name__ == "__main__":
    main()
