"""Re-run the harness's own run_timing (unchanged) several times for C3 vs baseline, to separate
the candidate's cost from contention by other harness runs on the same PC.

    uv run python eval/small-screen-change-detection/C3/timing_quiet.py [reps]
"""

import os
import sys
from pathlib import Path

HARNESS = Path(__file__).resolve().parents[1] / "harness"
sys.path.insert(0, str(HARNESS))

from hcommon import Candidate, baseline  # noqa: E402
from run import run_timing  # noqa: E402


def busy() -> int:
    """Other python processes running the harness (best effort, Windows)."""
    try:
        import subprocess

        out = subprocess.run(["powershell", "-NoProfile", "-Command",
                              "(Get-CimInstance Win32_Process -Filter \"Name='python.exe'\").CommandLine"],
                             capture_output=True, text=True, timeout=20).stdout
        return sum("harness/run.py" in line or "harness\\run.py" in line for line in out.splitlines())
    except Exception:  # noqa: BLE001
        return -1


def main() -> None:
    reps = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    cand, base = Candidate(Path(__file__).with_name("c3.py")), baseline()
    rows = []
    for r in range(reps):
        b = busy()
        t = run_timing(cand, base)
        sig = t["t_sig_ms"] - t["t_sig_base_ms"]
        cmp_worst = max(t[f"t_{k}_ms"] - t[f"t_{k}_base_ms"] for k in ("identical", "word", "caret", "large"))
        rows.append((t["extra_ms_worst"], sig, cmp_worst))
        print(f"rep {r} other_harness_runs={b} pid={os.getpid()} sig {t['t_sig_base_ms']:.3f}->{t['t_sig_ms']:.3f} "
              f"(+{sig:.3f}) cmp_worst {cmp_worst:+.3f} extra_ms_worst {t['extra_ms_worst']:+.3f} "
              + " ".join(f"{k}={t[f't_{k}_base_ms']:.3f}->{t[f't_{k}_ms']:.3f}" for k in ("identical", "word", "caret", "large")),
              flush=True)
    rows.sort()
    print("extra_ms_worst min / median / max:", f"{rows[0][0]:+.3f} / {rows[len(rows) // 2][0]:+.3f} / {rows[-1][0]:+.3f}")


if __name__ == "__main__":
    main()
