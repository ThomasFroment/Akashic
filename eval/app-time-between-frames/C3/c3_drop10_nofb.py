"""C3 variant: build(drop=0.1, fallback=False)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from c3 import build  # noqa: E402

bands = build(drop=0.1, fallback=False)
