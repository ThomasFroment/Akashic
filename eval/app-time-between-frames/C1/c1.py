"""C1 Three-layer priority partition (with the refuter's fixes): gap > away > app.

App layer: today's sessions (the rows `ctx.stored_bands()` returns, read here in the same query as
their last frame's monitor), each held past its last stored frame to the earliest of
  - the next session start (also the first one after the range),
  - the first later frame on its last frame's monitor, focused row or not (a focus move to an
    excluded, shell or null window stores a frame without a focused row, which starts no session),
  - the last "unchanged" confirmation of that monitor's shown run + one check (checked_ms + 5 s),
  - the start of the next stored gap that stops the screen (all, screen),
  - the start of the next away span,
  - the stored end of a game session of the same process (today's handling),
  - the end of recording (ctx.rec_end_ms).
The held band [start, hold) is then cut by the gap and away layers: any stored gap or away span
it covers (also inside its stored part: a session spanning the pause) is removed, so a band can
split in pieces.

Away layer: the input-idle record (ctx.sim_idle(): no keyboard/mouse input, start back-dated to the
last input). A stretch is away when it lasts at least T_AWAY_MS; the away span is the whole stretch
[last input, return). In production the record would be written by capture from GetLastInputInfo
on the 1 s gap tick with idle = max(0, signed int32(GetTickCount() - dwTime)) (a signed 32-bit
difference clamped at 0: SendInput can put the input tick ahead of GetTickCount, and the 49.7 day
wrap stays correct), the stretch start clipped to >= the end of the last all/stalled gap (the value
is stale after wake). The simulated record is already back-dated, so nothing to do here.

Cost: four small fetches (sessions + monitor, the next start, frames per monitor, monitor runs,
gaps, games, idle), then bisects over sorted arrays; no per-band SQL.
"""

from __future__ import annotations

import os
from bisect import bisect_left, bisect_right

from akashic.pipeline.sessions import AppBand
from akashic.storage.gaps import overlapping

T_AWAY_MS = int(os.environ.get("C1_T_AWAY_S", "180")) * 1000
CHECK_MS = 5_000
STOP_KINDS = ("all", "screen")
DEBUG: list | None = None  # diagnosis only (diagnose.py): (start, last frame, hold end, process, cause) per band


def _merge(spans):
    out: list[list[int]] = []
    for a, b in sorted(spans):
        if b <= a:
            continue
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


def bands(conn, start_ms, end_ms, now_ms, ctx):
    rows = conn.execute(
        "SELECT s.start_ms, s.end_ms, s.process, s.window_title, f.monitor_id FROM app_sessions s"
        " LEFT JOIN frames f ON f.id = s.last_frame_id"
        " WHERE ? < ? AND s.start_ms < ? AND s.end_ms >= ? ORDER BY s.start_ms, s.id",
        (start_ms, end_ms, end_ms, start_ms)).fetchall()  # = sessions.app_bands' rows (ctx.stored_bands())
    if not rows:
        return []
    starts = [r[0] for r in rows]
    nxt = conn.execute("SELECT MIN(start_ms) FROM app_sessions WHERE start_ms >= ?", (end_ms,)).fetchone()[0]
    if nxt is not None:
        starts.append(nxt)
    rec_end = ctx.rec_end_ms
    lo = min(r[1] for r in rows)
    hi = min(max(nxt or rec_end, end_ms), rec_end)  # no hold reaches past the next start or the end of recording

    # frames per monitor, sorted by time (one fetch for the range, one MIN per monitor for the tail)
    frames: dict[int, list[int]] = {}
    for mid, ts in conn.execute("SELECT monitor_id, ts_ms FROM frames WHERE ts_ms > ? AND ts_ms < ? ORDER BY ts_ms",
                                (lo, end_ms)):
        frames.setdefault(mid, []).append(ts)
    for mid in {r[4] for r in rows if r[4] is not None}:
        t = conn.execute("SELECT MIN(ts_ms) FROM frames WHERE monitor_id = ? AND ts_ms >= ?", (mid, end_ms)).fetchone()[0]
        if t is not None:
            frames.setdefault(mid, []).append(t)

    # shown runs per monitor: (start, checked), sorted by start
    shown: dict[int, tuple[list[int], list[int]]] = {}
    for mid, s0, chk in conn.execute("SELECT monitor_id, start_ms, checked_ms FROM monitor_states"
                                     " WHERE state = 'shown' AND start_ms <= ? ORDER BY monitor_id, start_ms", (hi,)):
        a, c = shown.setdefault(mid, ([], []))
        a.append(s0)
        c.append(chk)

    games: dict[str, list[tuple[int, int]]] = {}
    for proc, g0, g1 in conn.execute("SELECT lower(process), start_ms, end_ms FROM game_sessions"
                                     " WHERE start_ms <= ? AND end_ms + ? >= ?", (hi, CHECK_MS, lo)):
        games.setdefault(proc, []).append((g0, g1))

    # layers that override an app band: stored gaps (gap) and away spans (away)
    gap_spans = [(g.start_ms, g.end_ms) for g in overlapping(conn, min(starts), hi, now_ms, STOP_KINDS)]
    idle = list(ctx.sim_idle())
    idle += conn.execute("SELECT start_ms, end_ms FROM temp.sim_idle WHERE start_ms >= ? AND start_ms < ?",
                         (end_ms, hi)).fetchall()  # tail past the range (ctx only reads the range)
    away = [(s, e if e is not None else now_ms) for s, e in idle if (e if e is not None else now_ms) - s >= T_AWAY_MS]
    blocked = _merge(gap_spans + away)
    b_starts = [a for a, _ in blocked]
    b_ends = [b for _, b in blocked]

    out = []
    for i, (s, e, proc, title, mid) in enumerate(rows):
        cands = [(rec_end, "rec_end")]
        k = bisect_right(starts, e)
        if k < len(starts):
            cands.append((starts[k], "next_start"))
        if mid is not None:
            fts = frames.get(mid)
            if fts:
                k = bisect_right(fts, e)
                if k < len(fts):
                    cands.append((fts[k], "monitor_frame"))
            run = shown.get(mid)
            if run:
                k = bisect_right(run[0], e) - 1
                if k >= 0:
                    cands.append((max(e, run[1][k]) + CHECK_MS, "checked"))
        for g0, g1 in games.get((proc or "").lower(), ()):
            if g0 <= e <= g1 + CHECK_MS:
                cands.append((max(e, g1), "game_end"))
        k = bisect_left(b_starts, e)  # next gap or away start at or after the last frame
        if k < len(b_starts):
            cands.append((b_starts[k], "gap_or_away"))
        end, cause = min(cands)
        end = max(e, end)
        if DEBUG is not None:
            DEBUG.append((s, e, end, proc, cause))
        # cut the held band [s, end) by the blocked layer
        a = s
        k = bisect_right(b_ends, a)  # first blocked span ending after a
        while k < len(blocked) and b_starts[k] < end:
            if b_starts[k] > a:
                out.append(AppBand(a, b_starts[k], proc, title))
            a = max(a, b_ends[k])
            k += 1
        if end > a:
            out.append(AppBand(a, end, proc, title))
    return out
