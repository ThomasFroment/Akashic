"""P3 cost under the shared GPU lock (../gpu.lock, created exclusively once nvidia-smi shows no model
process and < IDLE_MIB used; retried every 60 s; deleted when done): harness run.py --only-cost on P3
(into this folder's detail.json) and on the baseline (into tmp-baseline-cost/, removed afterwards),
then one counting pass of each on the same cost window (decode calls and decoded audio seconds, for
the two-parameter daily model). Separation outputs are cached across the timed reps (P2_SEP_CACHE=1:
CPU separation is not GPU time). Writes cost-same-session.json. Numbers only.

    uv run python eval/overlapping-speech/P3-stems-plus-separation/cost_p3.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
STUDY = HERE.parent
LOCK = STUDY / "gpu.lock"
RUN = STUDY / "harness" / "run.py"
BASE = STUDY / "baseline" / "baseline.py"
IDLE_MIB = 2500


def gpu_state() -> tuple[int, list[str]]:
    used = int(subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"], capture_output=True, text=True).stdout.split()[0])
    apps = subprocess.run(["nvidia-smi", "--query-compute-apps=pid,process_name", "--format=csv,noheader"], capture_output=True, text=True).stdout.splitlines()
    busy = [a for a in apps if any(k in a.lower() for k in ("python", "llama", "akashic")) and str(os.getpid()) not in a]
    return used, busy


def idle() -> bool:
    used, busy = gpu_state()
    if busy or used > IDLE_MIB:
        print(f"gpu busy ({used} MiB, {len(busy)} model processes), retry in 60 s", flush=True)
        return False
    return True


COUNT = r'''
import json, os, sys
sys.path.insert(0, sys.argv[1])
import core, cost
cand = core.load_candidate(__import__("pathlib").Path(sys.argv[2]))
models = core.load_models()
run = core.Run(core.RUNS[0])
a, b = cost.cost_window(run)
segs = [s for s in run.segments if a <= s.start_ms < b]
ctx = core.Ctx(run, *models)
out = cand.process(segs, ctx)
p3 = getattr(ctx, "p3", None)
calls = p3["backends"]["p3"]["n_decode"] if p3 else len(out)
extra = {"ls_detect_calls": p3.get("ls_detect_calls", 0), "ls_gpu_s": p3.get("ls_gpu_s", 0.0), "ls_split": p3.get("ls_split", 0), "gated_utts": p3["gated_utts"], "sep_used_utts": p3["sep_used_utts"], "pieces": p3["pieces"], "sep_audio_s": p3["sep"]["sep_audio_s"],
         "sep_wall_s": p3["sep"]["sep_wall_s"]} if p3 else {}
print(json.dumps({"calls": calls, "audio_s": ctx.audio_s_decoded, "rows": len(out), "gpu_s": ctx.gpu_s, **extra}))
'''


def last_json(cmd, env) -> dict:
    out = subprocess.run([sys.executable, *cmd], capture_output=True, text=True, check=True, env=env)
    return json.loads(out.stdout.strip().splitlines()[-1])


def main() -> int:
    while True:
        if not idle():
            time.sleep(60)
            continue
        try:
            fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, f"P3-stems-plus-separation pid {os.getpid()} {time.ctime()}\n".encode())
            os.close(fd)
        except FileExistsError:
            print("lock busy, retry in 60 s", flush=True)
            time.sleep(60)
            continue
        if idle():
            break
        LOCK.unlink(missing_ok=True)
        time.sleep(60)
    tmp = HERE / "tmp-baseline-cost"
    env = {k: v for k, v in os.environ.items() if k not in ("P3_SHADOW",)}
    env["P2_SEP_CACHE"] = "1"
    try:
        used, _ = gpu_state()
        print("lock taken", time.ctime(), "gpu used at start", used, "MiB", flush=True)
        subprocess.run([sys.executable, str(RUN), str(HERE / "candidate.py"), str(HERE), "--only-cost"], check=True, env=env)
        tmp.mkdir(exist_ok=True)
        shutil.copy(STUDY / "baseline" / "detail.json", tmp / "detail.json")
        subprocess.run([sys.executable, str(RUN), str(BASE), str(tmp), "--only-cost"], check=True, env=env)
        cnt_c = last_json(["-c", COUNT, str(STUDY / "harness"), str(HERE / "candidate.py")], env)
        cnt_b = last_json(["-c", COUNT, str(STUDY / "harness"), str(BASE)], env)
        c = json.loads((HERE / "detail.json").read_text(encoding="utf-8"))["cost"]
        b = json.loads((tmp / "detail.json").read_text(encoding="utf-8"))["cost"]
        out = {"candidate": c, "baseline": b, "counts": {"candidate": cnt_c, "baseline": cnt_b}, "gpu_used_mib_at_lock": used,
               "gpu_x_same_session": c["gpu_s_per_h"] / b["gpu_s_per_h"], "vram_delta_gb": (c["vram_peak_mib"] - b["vram_peak_mib"]) / 1024}
        (HERE / "cost-same-session.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
        print(json.dumps({k: v for k, v in out.items() if k.startswith(("gpu_x", "vram"))}), cnt_c, cnt_b, flush=True)
        subprocess.run([sys.executable, str(HERE / "ls_census.py"), "std,day"], check=True, env=env)  # language-split GPU on whole runs
    finally:
        LOCK.unlink(missing_ok=True)
        shutil.rmtree(tmp, ignore_errors=True)
        print("lock released", time.ctime(), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
