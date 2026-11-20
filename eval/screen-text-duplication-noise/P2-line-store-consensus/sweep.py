"""Garbage-rule sweep: noise share vs content drop for P2 configs (numbers only).

    uv run python .../sweep.py OUT.json [runs]
Each config runs harness/replay.py in a subprocess with P2_* overrides (1 rep, no synthetic).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPLAY = HERE.parent / "harness" / "replay.py"

CONFIGS = {
    "none": {"SHORT_GARBAGE": False, "MISREAD": False, "USE_LM": False, "N6_ON": False},
    "n6": {"SHORT_GARBAGE": False, "MISREAD": False, "USE_LM": False},
    "n6+short": {"MISREAD": False, "USE_LM": False},
    "n6+short+misread": {"USE_LM": False},
    "n6+short+misread+lm": {"LM_MIN_TYPES": 300},
    "n6+short+misread/split": {"USE_LM": False, "MODE": "split"},
    "n6+short+misread/edge": {"USE_LM": False, "MODE": "edge"},
    "n6+short+misread+lm/split": {"LM_MIN_TYPES": 300, "MODE": "split"},
    "n6+short+misread/d6long2": {"USE_LM": False, "D6_LONG": 2},
    "n6+short+misread/noslots": {"USE_LM": False, "MAX_SLOT_CHANGES": 0},
    "n6+short+misread/nopair": {"USE_LM": False, "D6_LONG": 0, "MAX_SLOT_CHANGES": 0},
}


def main() -> None:
    out = Path(sys.argv[1])
    runs = sys.argv[2] if len(sys.argv) > 2 else "smoke,standard,day"
    res = json.loads(out.read_text()) if out.is_file() else {}
    for name, cfg in CONFIGS.items():
        if name in res:
            continue
        env = dict(os.environ)
        env.update({f"P2_{k}": repr(v) for k, v in cfg.items()})
        with tempfile.TemporaryDirectory() as td:
            subprocess.run([sys.executable, str(REPLAY), str(HERE / "cand.py"), td, "--runs", runs, "--reps", "1", "--no-synth"],
                           env=env, check=True, capture_output=True)
            d = json.loads((Path(td) / "details.json").read_text(encoding="utf-8"))
        res[name] = {k: {"noise": d[k]["noise"]["share"], "drop": d[k]["drop"]["all"]["rate"], "cdrop": d[k]["drop"]["all"]["char_rate"],
                         "m33": d[k]["m33"], "cer": d[k]["m31"]["all"]["cer"], "m32": d[k]["m32"],
                         "tlink": d[k]["link"]["truth_valid"], "link": d[k]["link"]["valid"], "crowd": d[k]["crowding"]["nonce"]["dup_same_window"]}
                     for k in runs.split(",")}
        out.write_text(json.dumps(res, indent=1))
        print(name, {k: (round(v["noise"], 4), round(v["drop"], 4), round(v["m33"], 3)) for k, v in res[name].items()}, flush=True)


if __name__ == "__main__":
    main()
