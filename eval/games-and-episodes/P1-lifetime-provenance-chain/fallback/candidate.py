"""P1 variant: C2 fallback rows (no process handle) (everything else as ../candidate.py)."""
import importlib.util
import os
from pathlib import Path

os.environ["P1_ROWS"] = "fallback"
_spec = importlib.util.spec_from_file_location("p1_main_fallback", Path(__file__).resolve().parents[1] / "candidate.py")
_m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_m)
globals().update({k: v for k, v in vars(_m).items() if not k.startswith("__")})
NAME = "P1 variant: C2 fallback without a process handle (today rule + C1 + reopen same instance within 300 s)"
