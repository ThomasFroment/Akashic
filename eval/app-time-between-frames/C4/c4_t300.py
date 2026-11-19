"""C4 variant: T = 300 s. Loads c4.py next to this file."""

import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location("c4_base", Path(__file__).with_name("c4.py"))
c4 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(c4)

bands = c4.make(t_ms=300_000)
