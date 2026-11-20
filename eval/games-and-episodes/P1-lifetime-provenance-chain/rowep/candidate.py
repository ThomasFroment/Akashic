"""P1 variant: one game episode per row (everything else as ../candidate.py)."""
import importlib.util
import os
from pathlib import Path

os.environ["P1_GAME_EPISODE_PER_ROW"] = "1"
_spec = importlib.util.spec_from_file_location("p1_main_rowep", Path(__file__).resolve().parents[1] / "candidate.py")
_m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_m)
globals().update({k: v for k, v in vars(_m).items() if not k.startswith("__")})
NAME = "P1 variant: one game episode per row (no chaining across a Pause)"
