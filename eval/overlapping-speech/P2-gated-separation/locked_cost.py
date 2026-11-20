"""Cost runs under the shared GPU lock (../gpu.lock, created exclusively, retried every 60 s, deleted
when done): the harness cost.py on the baseline and on the candidate (same lock, like with like),
then the separation bench. Writes cost-baseline.json, cost-candidate.json, bench.json here.

    uv run python eval/overlapping-speech/P2-gated-separation/locked_cost.py [--skip-bench]
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOCK = HERE.parent / "gpu.lock"
COST = HERE.parent / "harness" / "cost.py"


def last_json(cmd, env=None) -> dict:
    out = subprocess.run([sys.executable, *cmd], capture_output=True, text=True, check=True, env=env)
    return json.loads(out.stdout.strip().splitlines()[-1])


IDLE_MIB = 2500  # desktop apps only; one harness process holds ~4.5 GB


def gpu_state() -> tuple[int, list[str]]:
    """Device used MiB and the compute/graphics apps nvidia-smi lists that could hold model memory."""
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


def main() -> int:
    while True:
        if not idle():
            time.sleep(60)
            continue
        try:
            fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, f"P2-gated-separation pid {os.getpid()} {time.ctime()}\n".encode())
            os.close(fd)
        except FileExistsError:
            print("lock busy, retry in 60 s", flush=True)
            time.sleep(60)
            continue
        if idle():
            break
        LOCK.unlink(missing_ok=True)
        time.sleep(60)
    try:
        used, _ = gpu_state()
        print("lock taken", time.ctime(), "gpu used at start", used, "MiB", flush=True)
        (HERE / "cost-idle.json").write_text(json.dumps({"gpu_used_mib_at_lock": used, "time": time.ctime()}), encoding="utf-8")
        env = dict(os.environ, P2_SEP="cpu", P2_SEP_CACHE="1")
        b = last_json([str(COST), str(HERE.parent / "baseline" / "baseline.py"), "9"])
        (HERE / "cost-baseline.json").write_text(json.dumps(b), encoding="utf-8")
        print("baseline", b["gpu_s_per_h"], b["vram_peak_mib"], flush=True)
        c = last_json([str(COST), str(HERE / "candidate.py"), "9"], env)
        (HERE / "cost-candidate.json").write_text(json.dumps(c), encoding="utf-8")
        print("candidate", c["gpu_s_per_h"], c["vram_peak_mib"], flush=True)
        g = last_json([str(COST), str(HERE / "candidate.py"), "9"], dict(os.environ, P2_SEP="cuda"))
        (HERE / "cost-candidate-gpu.json").write_text(json.dumps(g), encoding="utf-8")
        print("candidate gpu-sep", g["gpu_s_per_h"], g["vram_peak_mib"], flush=True)
        if "--skip-bench" not in sys.argv:
            r = last_json([str(HERE / "bench_sep.py"), "both"])
            (HERE / "bench.json").write_text(json.dumps(r), encoding="utf-8")
            print("bench", r, flush=True)
    finally:
        LOCK.unlink(missing_ok=True)
        print("lock released", time.ctime(), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
