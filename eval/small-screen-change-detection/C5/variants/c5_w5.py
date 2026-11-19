"""C5 variant (W2 = 5 at 4K (underscore vs x-height word trade)): c5.py with W2 4k = 5."""
import importlib.util
import pathlib

_spec = importlib.util.spec_from_file_location("c5_core_w5", pathlib.Path(__file__).resolve().parent.parent / "c5.py")
m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m)
m.W2 = {"4k": 5, "1440p": 4}
make_state, decide = m.make_state, m.decide
