"""The harness's own timing() (12 pages x 9 repeats, interleaved with baseline) for c3 under the current env. Numbers only."""
import json, sys
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "harness"))
import run
from shared import ROOT, Candidate
import corpus as corpus_mod
pages = corpus_mod.build()["pages"]
tim = run.timing(Candidate(HERE / "c3.py"), Candidate(ROOT / "baseline.py"), pages, lambda s: None)
c, b = tim["cand"], tim["base"]
print(sys.argv[1] if len(sys.argv) > 1 else "", "dms_p95", round(c["live_ms_p95"] - b["live_ms_p95"], 1), "dms_def_p95", round(c["def_ms_p95"] - b["def_ms_p95"], 1),
      "cpu_ratio", round(c["live_cpu_mean"] / b["live_cpu_mean"], 2), "cpu_def_ratio", round(c["def_cpu_mean"] / b["def_cpu_mean"], 2))
