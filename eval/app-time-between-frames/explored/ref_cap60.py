"""Reference (brief, Explored): hold each session to the next session's start, at most 60 s past its
last frame, never into a stored all/screen gap, never past the end of recording.

Port of `held` in eval/quality/spikes/session_hold_replay.py; the one change is the end of
recording, read from the store (`ctx.rec_end_ms`, the open stop gap) instead of the truth's t1.
"""

from dataclasses import replace

CAP_MS = 60_000


def bands(conn, start_ms, end_ms, now_ms, ctx):
    out = []
    stored = ctx.stored_bands()
    gaps = ctx.gaps(("all", "screen"))
    starts = sorted(b.start_ms for b in stored)
    stop = ctx.rec_end_ms
    for b in stored:
        end = min(b.end_ms + CAP_MS, stop)
        nxt = min((s for s in starts if s > b.end_ms), default=None)
        if nxt is not None:
            end = min(end, nxt)
        for g in gaps:
            if g.start_ms >= b.end_ms:
                end = min(end, g.start_ms)
        out.append(replace(b, end_ms=max(b.end_ms, end)))
    return out
