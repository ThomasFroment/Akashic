"""Shared pieces of the screen-change-small-text harness: monitors, today's reduction, candidates.

Candidate contract (a plain Python module, loaded by path):

    make_state() -> object
        Fresh per-monitor state. The sweep calls it once per pair (a page with no history);
        the sequences and the live replay call it once per stream / monitor.
    decide(state, ref_gray, cur_gray, prev_gray, monitor[, now]) -> bool
        True = store the current frame. ``ref_gray`` is the last STORED frame (the harness
        replaces it with ``cur_gray`` only when decide returns True, as screen.py does),
        ``prev_gray`` the previous CHECK's frame, ``monitor`` "4k" (3840x2160 at 125 %) or
        "1440p" (2560x1440 at 100 %). If decide has a ``now`` parameter it gets the
        simulated wall-clock time in seconds (sequences: 5 s per check; sweep: 0.0).
        Inputs are read-only: a candidate that patches its reference keeps its own copy in
        ``state`` (the harness passes the same ``ref_gray`` object until the next store).
    reduce(img: PIL.Image RGB) -> uint8 2-D array        (optional)
        Overrides today's 1/4 gray reduction; it must be a block reduction with integer
        factors that divide 16 (the harness reduces aligned patches and checks this).
"""

from __future__ import annotations

import hashlib
import importlib.util
import inspect
import sys
from pathlib import Path

import numpy as np
from PIL import Image

HERE = Path(__file__).resolve().parent
STUDY = HERE.parent
FONT = "C:/Windows/Fonts/segoeui.ttf"
MONITORS = {"4k": (3840, 2160, 1.25), "1440p": (2560, 1440, 1.0)}
THEMES = {"dark": ((32, 33, 36), (220, 221, 222)), "light": ((255, 255, 255), (0, 0, 0))}

if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))


def today_reduce(img: Image.Image) -> np.ndarray:
    """Copy of akashic.capture.imaging.gray_small (factor 4); checked against it in run.py."""
    return np.asarray(img.reduce(4).convert("L"), dtype=np.uint8)


def digest(gray: np.ndarray) -> bytes:
    """Today's signature digest (imaging.signature)."""
    return hashlib.blake2b(gray.tobytes(), digest_size=16).digest()


class Candidate:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).resolve()
        self.name = self.path.stem
        spec = importlib.util.spec_from_file_location(f"cand_{self.name}_{abs(hash(self.path))}", self.path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        self.mod = mod
        self.overrides_reduce = callable(getattr(mod, "reduce", None))
        self.reduce = mod.reduce if self.overrides_reduce else today_reduce
        self.make_state = mod.make_state
        self._decide = mod.decide
        self._takes_now = "now" in inspect.signature(mod.decide).parameters

    def decide(self, state, ref, cur, prev, monitor: str, now: float = 0.0) -> bool:
        if self._takes_now:
            return bool(self._decide(state, ref, cur, prev, monitor, now=now))
        return bool(self._decide(state, ref, cur, prev, monitor))


def baseline() -> Candidate:
    return Candidate(HERE / "baseline.py")


def readonly(a: np.ndarray) -> np.ndarray:
    v = a.view()
    v.flags.writeable = False
    return v
