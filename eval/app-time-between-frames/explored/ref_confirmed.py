"""Reference (brief, Explored): hold each session to the next session's start, bounded by the last
"unchanged" confirmation of its last frame's monitor (monitor_states.checked_ms of the shown run
covering the last frame) + 5 s, a game's stored end, stored all/screen gaps and the end of recording.

Port of `confirmed` in eval/quality/spikes/session_hold_replay.py; the one change is the end of
recording, read from the store (`ctx.rec_end_ms`) instead of the truth's t1.
"""

from dataclasses import replace

CHECK_MS = 5_000


def bands(conn, start_ms, end_ms, now_ms, ctx):
    out = []
    stored = ctx.stored_bands()
    gaps = ctx.gaps(("all", "screen"))
    starts = sorted(b.start_ms for b in stored)
    stop = ctx.rec_end_ms
    games = conn.execute("SELECT lower(process), start_ms, end_ms FROM game_sessions").fetchall()
    for b in stored:
        row = conn.execute(
            "SELECT f.monitor_id FROM app_sessions s JOIN frames f ON f.id = s.last_frame_id"
            " WHERE s.start_ms = ? AND s.end_ms = ? AND s.process IS ?", (b.start_ms, b.end_ms, b.process)).fetchone()
        end = stop
        if row is not None:
            seen = conn.execute(
                "SELECT checked_ms FROM monitor_states WHERE monitor_id = ? AND state = 'shown'"
                " AND start_ms <= ? ORDER BY start_ms DESC LIMIT 1", (row[0], b.end_ms)).fetchone()
            end = min(end, max(b.end_ms, seen[0] if seen else b.end_ms) + CHECK_MS)
        nxt = min((x for x in starts if x > b.end_ms), default=None)
        if nxt is not None:
            end = min(end, nxt)
        for proc, g0, g1 in games:
            if proc == (b.process or "").lower() and g0 <= b.end_ms <= g1 + CHECK_MS:
                end = min(end, max(b.end_ms, g1))
        for g in gaps:
            if g.start_ms >= b.end_ms:
                end = min(end, g.start_ms)
        out.append(replace(b, end_ms=max(b.end_ms, end)))
    return out
