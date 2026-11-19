"""C3: foreground switch rows as exact band edges (refuter-narrowed).

Switch rows come from the capture loop's existing 0.1 s GetForegroundWindow poll (process included,
also in game mode). Here they are SIMULATED by `ctx.sim_fg()` (the observer's foreground log, the
scorer's own truth source: real-run M5.1/M5.3 are an upper bound).

    edges  = switch rows  U  stored session starts whose process differs from the current edge's
             process while no switch row came in the last FALLBACK_TOL_MS (covers a dropped row)
    band k = [edge_k, edge_k+1)  minus  stored all/screen gaps, away spans (sim_idle stretches of at
             least T, back-dated to the last input: C1), game end (band of the game's process
             overlapping a stored game session stops at its end), end of recording
    identity: title (and URL) sub-edges at the starts of stored sessions of the band's process
             inside the edge band; the lead-in before the first frame takes that frame's title
    'excluded' row -> typed band (process None, title EXCLUDED), not an app; 'unknown' -> no band.

One sorted sweep; three small fetches (sim_fg, app_sessions, game_sessions) + ctx gaps/idle; no
frame join, no per-band SQL.
"""

from __future__ import annotations

import bisect
import zlib

from core import AppBand

EXCLUDED = "<excluded>"
FALLBACK_TOL_MS = 1_000


def _u(seed: int, ts: int, salt: str) -> float:
    return zlib.crc32(f"{seed}:{salt}:{ts}".encode()) / 2**32


def perturb(fg, drop: float, delay: bool, seed: int, poll: bool = False):
    """drop: lose that share of rows (seeded per row). delay: every row 0.1-0.2 s late, order kept
    (a poll cannot reorder switches). poll: a 0.1 s GetForegroundWindow poll with a seeded phase,
    a row stamped 0.1 s after the poll that saw the change (latency 0.1-0.2 s); a window in front
    for less than one poll period is never seen."""
    if poll:
        phase = int(_u(seed, 0, "phase") * 100)
        seen, last_key = [], None
        for k, (ts, hwnd, proc, state) in enumerate(fg):
            p = phase + -(-(ts - phase) // 100) * 100  # first poll at or after ts
            if k + 1 < len(fg) and fg[k + 1][0] <= p:
                continue  # replaced before any poll saw it
            if (hwnd, proc, state) == last_key:
                continue
            seen.append((p + 100, hwnd, proc, state))
            last_key = (hwnd, proc, state)
        fg = seen
    out, prev = [], None
    for ts, hwnd, proc, state in fg:
        if drop and _u(seed, ts, "drop") < drop:
            continue
        if delay:
            ts = ts + 100 + int(_u(seed, ts, "delay") * 101)  # poll latency 0.1-0.2 s
            if prev is not None and ts <= prev:
                ts = prev + 1
        out.append((ts, hwnd, proc, state))
        prev = ts
    return out


def _subtract(segs, cuts):
    """segs: time-ordered, non-overlapping (a, b, payload); cuts: merged sorted (a, b)."""
    out, j = [], 0
    for a, b, p in segs:
        while j < len(cuts) and cuts[j][1] <= a:
            j += 1
        k, cur = j, a
        while k < len(cuts) and cuts[k][0] < b:
            if cuts[k][0] > cur:
                out.append((cur, cuts[k][0], p))
            cur = max(cur, cuts[k][1])
            k += 1
        if cur < b:
            out.append((cur, b, p))
    return out


def _merge(spans):
    out = []
    for a, b in sorted(spans):
        if b <= a:
            continue
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


def build(T_ms: int = 180_000, subedges: bool = True, drop: float = 0.0, delay: bool = False, poll: bool = False,
          excluded_band: bool = True, fallback: bool = True, endcheck: bool = True, game_away: bool = True, seed: int = 1,
          stats: dict | None = None):
    def bands(conn, start_ms, end_ms, now_ms, ctx):
        stop = min(ctx.rec_end_ms, now_ms)
        fg = ctx.sim_fg()
        if drop or delay or poll:
            fg = perturb(fg, drop, delay, seed, poll)
        first = fg[0][0] if fg else start_ms
        sess = conn.execute(
            "SELECT start_ms, end_ms, process, window_title, url FROM app_sessions"
            " WHERE start_ms < ? AND end_ms >= ? ORDER BY start_ms, id", (end_ms, min(first, start_ms))).fetchall()

        # --- edges: switch rows, plus session starts where the rows missed a switch ------------
        edges = []  # (ts, process or None, state)
        i = j = 0
        cur, left, last_fg = None, None, None  # left: the process the last switch row left
        n_fb = 0
        while i < len(fg) or j < len(sess):
            if j >= len(sess) or (i < len(fg) and fg[i][0] <= sess[j][0]):
                ts, _h, proc, state = fg[i]
                edges.append((ts, proc, state))
                left, cur, last_fg = cur, (proc.lower() if proc else None), ts
                i += 1
                continue
            s0, proc = sess[j][0], sess[j][2]
            j += 1
            if not fallback or not proc or (cur == proc.lower()):
                continue
            if last_fg is not None and s0 - last_fg < FALLBACK_TOL_MS and proc.lower() == left:
                continue  # a frame of the app just left, stamped right after the switch
            if s0 < first:
                continue
            edges.append((s0, proc, "app"))
            cur = proc.lower()
            n_fb += 1

        # --- span check: a stored session of p proves p was in front at its frames. An edge inside
        # the session (or the edge in force at its last frame) that is not p and is not followed
        # by a p edge before the session ends means a lost switch back to p: the first focused
        # frame of p after that edge becomes an edge. Indexed lookups for these conflicts only.
        n_ec = n_q = 0
        if fallback and endcheck and edges:
            ets = [e[0] for e in edges]
            add = set()
            for s0, e0, proc, _t, _u2 in sess:
                if not proc or e0 < first:
                    continue
                pl = proc.lower()
                k_hi = bisect.bisect_right(ets, e0) - 1
                if k_hi < 0:
                    continue
                k_lo = min(bisect.bisect_right(ets, s0), k_hi)
                for k in range(k_lo, k_hi + 1):
                    t_k, p_k, st_k = edges[k]
                    if st_k == "app" and p_k and p_k.lower() == pl:
                        continue
                    nxt = ets[k + 1] if k + 1 < len(ets) else None
                    if nxt is not None and nxt <= e0 and edges[k + 1][2] == "app" and (edges[k + 1][1] or "").lower() == pl:
                        continue
                    hi = e0 if nxt is None else min(nxt, e0)
                    prev_p = (edges[k - 1][1] or "").lower() if k > 0 else ""
                    lo = t_k + (FALLBACK_TOL_MS if prev_p == pl else 0)  # the app just left: skip stale frames
                    if hi <= lo:
                        continue
                    n_q += 1
                    row = conn.execute(  # walk frames forward in time (frames_ts), not all of p's rows
                        "SELECT f.ts_ms FROM frames f INDEXED BY frames_ts"
                        " JOIN frame_windows w ON w.frame_id = f.id AND w.focused = 1"
                        " WHERE f.ts_ms > ? AND f.ts_ms <= ? AND +w.process = ? ORDER BY f.ts_ms LIMIT 1",
                        (lo, hi, proc)).fetchone()
                    if row and row[0] is not None:
                        add.add((row[0], proc, "app"))
            n_ec = len(add)
            if add:
                edges = sorted(edges + list(add), key=lambda e: e[0])
        if stats is not None:
            stats["fallback_edges"] = stats.get("fallback_edges", 0) + n_fb
            stats["endcheck_edges"] = stats.get("endcheck_edges", 0) + n_ec
            stats["lookups"] = stats.get("lookups", 0) + n_q

        # --- sessions per process, for identity -------------------------------------------------
        by_proc: dict[str, tuple[list, list]] = {}
        for s0, _e0, proc, title, url in sess:
            if proc:
                st, info = by_proc.setdefault(proc.lower(), ([], []))
                st.append(s0)
                info.append((title, url))

        games = conn.execute("SELECT lower(process), start_ms, end_ms FROM game_sessions"
                             " WHERE start_ms < ? AND (end_ms IS NULL OR end_ms > ?)", (end_ms, first)).fetchall()

        # --- edge bands -> identity segments ------------------------------------------------------
        segs = []  # (a, b, (process, title))
        for k, (ts, proc, state) in enumerate(edges):
            b = min(edges[k + 1][0] if k + 1 < len(edges) else stop, stop)
            if b <= ts:
                continue
            if state == "unknown":
                continue
            if state == "excluded":
                if excluded_band:
                    segs.append((ts, b, (None, EXCLUDED, False)))
                continue
            p = proc.lower()
            in_game = False
            for gp, g0, g1 in games:
                if gp == p and g1 is not None and ts < g1 and b > g0 and b > g1:
                    b = max(ts, g1)
                if gp == p and ts < (g1 if g1 is not None else stop) and b > g0:
                    in_game = True
            if b <= ts:
                continue
            st, info = by_proc.get(p, ([], []))
            lo = bisect.bisect_left(st, ts)
            hi = bisect.bisect_left(st, b)
            if not subedges or lo == hi:
                if lo < hi:
                    title = info[lo][0]
                elif lo > 0:
                    title = info[lo - 1][0]
                else:
                    title = None
                segs.append((ts, b, (proc, title, in_game)))
                continue
            a, ident = ts, info[lo]
            for m in range(lo + 1, hi):
                if info[m] != ident:
                    segs.append((a, st[m], (proc, ident[0], in_game)))
                    a, ident = st[m], info[m]
            segs.append((a, b, (proc, ident[0], in_game)))

        # --- cuts: gaps, away, end of recording ------------------------------------------------
        # (game_away=False: a stored game session keeps today's handling, no away cut inside it)
        cuts = [(g.start_ms, g.end_ms) for g in ctx.gaps(("all", "screen"))]
        cuts.append((stop, max(stop, now_ms) + 1))
        away = []
        for s, e in ctx.sim_idle():
            e = now_ms if e is None else e
            if e - s >= T_ms:
                away.append((s, e))
        if game_away:
            pieces = _subtract(segs, _merge(cuts + away))
        else:
            pieces = sorted(_subtract([x for x in segs if not x[2][2]], _merge(cuts + away))
                            + _subtract([x for x in segs if x[2][2]], _merge(cuts)))
        out = []
        for a, b, (proc, title, _g) in pieces:
            if b > start_ms and a < end_ms:
                out.append(AppBand(a, b, proc, title))
        return out

    return bands


bands = build()
