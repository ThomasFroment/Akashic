"""C5 variant (FIX4 taskbar band off): c5.py with TASKBAR_BAND = False."""
import importlib.util
import pathlib

_spec = importlib.util.spec_from_file_location("c5_core_noband", pathlib.Path(__file__).resolve().parent.parent / "c5.py")
m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m)
m.TASKBAR_BAND = False
make_state, decide = m.make_state, m.decide
