"""C3 variant: build(poll=True): switch rows as a 0.1 s poll would write them (latency 0.1-0.2 s)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from c3 import build  # noqa: E402

bands = build(poll=True)
