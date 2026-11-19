"""C3 variant: build(T_ms=300_000)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from c3 import build  # noqa: E402

bands = build(T_ms=300_000)
