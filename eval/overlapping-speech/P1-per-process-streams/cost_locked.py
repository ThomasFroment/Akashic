"""Cost runs under the shared GPU lock: candidate --only-cost into this folder, then the baseline
--only-cost into a temporary copy, so gpu_x compares like with like (numbers only).

    uv run python eval/overlapping-speech/P1-per-process-streams/cost_locked.py
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
STUDY = HERE.parent
LOCK = STUDY / "gpu.lock"


def main() -> int:
    while True:
        try:
            with open(LOCK, "x", encoding="utf-8") as f:
                f.write(f"P1-per-process-streams {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
            break
        except FileExistsError:
            print("gpu.lock held, waiting 60 s", flush=True)
            time.sleep(60)
    try:
        run = str(STUDY / "harness" / "run.py")
        subprocess.run([sys.executable, run, str(HERE / "candidate.py"), str(HERE), "--only-cost"], check=True)
        tmp = HERE / "tmp-baseline-cost"
        tmp.mkdir(exist_ok=True)
        shutil.copy(STUDY / "baseline" / "detail.json", tmp / "detail.json")
        subprocess.run([sys.executable, run, str(STUDY / "baseline" / "baseline.py"), str(tmp), "--only-cost"], check=True)
    finally:
        LOCK.unlink(missing_ok=True)
    c = json.loads((HERE / "detail.json").read_text(encoding="utf-8"))["cost"]
    b = json.loads((HERE / "tmp-baseline-cost" / "detail.json").read_text(encoding="utf-8"))["cost"]
    out = {"candidate": c, "baseline_same_session": b, "gpu_x_same_session": c["gpu_s_per_h"] / b["gpu_s_per_h"],
           "vram_delta_gb": (c["vram_peak_mib"] - b["vram_peak_mib"]) / 1024}
    (HERE / "cost-same-session.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(json.dumps({k: v for k, v in out.items() if k.startswith(("gpu_x", "vram"))}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
