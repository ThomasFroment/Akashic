"""Ablation control: the same logistic fusion on primary-decode statistics only (no align passes)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location("C5_contrast_decode", Path(__file__).with_name("C5_contrast.py"))
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
_mod.MODE = "decode"
fit = _mod.fit
judge = _mod.judge
