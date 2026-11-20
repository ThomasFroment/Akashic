"""P1 variant: C3 chain gap P = 600 s (everything else as ../candidate.py)."""
import importlib.util
import os
from pathlib import Path

os.environ["P1_CHAIN_P_S"] = "600"
_spec = importlib.util.spec_from_file_location("p1_main_p600", Path(__file__).resolve().parents[1] / "candidate.py")
_m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_m)
globals().update({k: v for k, v in vars(_m).items() if not k.startswith("__")})
NAME = "P1 variant: P = 600 s"
