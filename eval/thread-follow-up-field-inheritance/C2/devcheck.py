"""Print dev-split pairs only (S, E, gold). Held-out rows are never printed."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import harness as h
pairs = [p for p in h.load_pairs() if p["split"] == "dev"]
h.check(h.prepare(pairs))
