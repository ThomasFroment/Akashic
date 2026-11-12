"""C5 calibrated fusion: logistic regression over the contrast cells and decode statistics."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location("C5_contrast_fusion", Path(__file__).with_name("C5_contrast.py"))
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
_mod.MODE = "fusion"
fit = _mod.fit
judge = _mod.judge
