"""Harness self-test, not a candidate: line_dedupe that discards every second distinct line. The
drop metrics must report a large drop (standard: drop 0.44, M3.3 0.48)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import line_dedupe  # noqa: E402
from shared import StoredText  # noqa: E402


def process(history, ctx):
    st = line_dedupe.process(history, ctx)
    return StoredText([kl for i, kl in enumerate(st.lines) if i % 2 == 0])
