"""C3 fix variant: the sparse shape test skips the taskbar band (bottom 48 CSS px = 60 device px at
125 %, 48 at 100 %: the area outside the work area on a default Windows 11 taskbar), so the
taskbar keeps today's count-only behaviour. Everything else is c3.py unchanged."""

import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location("c3_base_for_taskbar", Path(__file__).with_name("c3.py"))
_m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_m)
_m.TASKBAR_PX = {"4k": 60, "1440p": 48}

reduce, make_state, decide = _m.reduce, _m.make_state, _m.decide
