"""Baseline = today: every stored frame keeps its window's whole text as one FTS row, linked to its
own frame (capture/text.py assemble_text -> storage/frames.py -> frame_windows_fts trigger)."""

from __future__ import annotations

from shared import KeptLine, StoredText

WRITE_PATH = False  # nothing beyond today's insert; process() only re-expresses the stored rows


def process(history, ctx):
    lines, meta = [], {}
    for o in history:
        doc = ("row", o.window_id)
        meta[doc] = (o.title, o.url)
        lines += [KeptLine(ln, o.frame_id, o.ts_ms, o.ts_ms, doc) for ln in o.text.split("\n")]
    return StoredText(lines, meta)
