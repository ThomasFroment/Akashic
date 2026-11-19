"""C4 Presence split by channel: C1's held band, cut where every presence channel is quiet.

Band (C1, with the refuter's fixes): each stored session is held from its last frame to the earliest
of the next session start, the first later frame on its last frame's monitor (focus left to an
excluded, shell or null window), the next stored all/screen gap, its game's stored end and the end
of recording; stored gaps it spans are cut out.

Presence (C4): the band is also cut by away spans. Away = every channel quiet for T, back-dated
to the latest activity of any channel. Channels, all from stored rows except K:
- K keyboard/mouse: the simulated input-idle record (ctx.sim_idle(): no-input stretches, back-dated).
- V the band's own process audible (a stored system audio segment lists it in `sources`) while its
  monitor stored a frame within the last V_FRAME_MS (the picture is changing). V alone holds
  presence at most V_CAP_MS past the last K or S activity.
- S the user's own speech (stored mic segments), counted for the band only when the band's process
  was audible within S_PAIR_MS (the call app is in front): a call in a background app while the
  user is away from a static browser does not hold the browser.
- G a stored game session of the band's process (brief: game sessions keep today's handling); it
  stands in for the untestable P (XInput pad) channel. Off in the c4_nogame variant.
Only idle stretches of K that reach T can produce an away span, so V/S/G rows are read only when
such a stretch overlaps a band (lazy: the real runs have no idle record and never read them).
"""

from __future__ import annotations

import bisect
import json
from collections import defaultdict

from core import AppBand

T_MS = 180_000
CHECK_MS = 5_000
V_FRAME_MS = 15_000
V_CAP_MS = 3 * 3_600_000
S_PAIR_MS = 60_000
MAX_SEG_MS = 120_000  # longest stored audio segment (window for a start-ordered bisect)


def _merge(spans):
    out = []
    for a, b in sorted((x[0], x[1]) for x in spans):
        if b <= a:
            continue
        if out and a <= out[-1][1]:
            if b > out[-1][1]:
                out[-1][1] = b
        else:
            out.append([a, b])
    return out


def _intersect(a, b):
    """Intersection of two merged, sorted span lists."""
    i = j = 0
    out = []
    while i < len(a) and j < len(b):
        lo, hi = max(a[i][0], b[j][0]), min(a[i][1], b[j][1])
        if hi > lo:
            out.append([lo, hi])
        if a[i][1] < b[j][1]:
            i += 1
        else:
            j += 1
    return out


def _subtract(span, cuts):
    """[a, b) minus merged sorted `cuts`."""
    a, b = span
    out = []
    for c0, c1 in cuts:
        if c1 <= a:
            continue
        if c0 >= b:
            break
        if c0 > a:
            out.append((a, c0))
        a = max(a, c1)
        if a >= b:
            break
    if a < b:
        out.append((a, b))
    return out


def make(t_ms=T_MS, use_game=True, use_v=True, use_s=True, s_paired=True):
    def bands(conn, start_ms, end_ms, now_ms, ctx):
        rec_end = ctx.rec_end_ms
        sel = ("SELECT s.start_ms, s.end_ms, s.process, s.window_title, f.monitor_id, f.ts_ms FROM app_sessions s"
               " JOIN frames f ON f.id = s.last_frame_id")
        rows = conn.execute(sel + " WHERE s.start_ms < ? AND s.end_ms >= ? ORDER BY s.start_ms, s.id",
                            (end_ms, start_ms)).fetchall()
        prev = conn.execute(sel + " WHERE s.start_ms < ? AND s.end_ms < ? ORDER BY s.start_ms DESC LIMIT 1",
                            (start_ms, start_ms)).fetchone()
        if prev is not None:  # may be held into the range
            rows.insert(0, prev)
        if not rows:
            return []
        nxt = conn.execute("SELECT MIN(start_ms) FROM app_sessions WHERE start_ms >= ?", (end_ms,)).fetchone()[0]
        starts = sorted([r[0] for r in rows] + ([nxt] if nxt is not None else []))
        hi = min(rec_end, nxt) if nxt is not None else rec_end
        lo = min(r[0] for r in rows) - V_FRAME_MS  # V needs the frames inside the bands, not only after them
        by_mon = defaultdict(list)
        for mon, ts in conn.execute("SELECT monitor_id, ts_ms FROM frames WHERE ts_ms > ? AND ts_ms <= ? ORDER BY ts_ms",
                                    (lo, max(hi, end_ms))):
            by_mon[mon].append(ts)
        gaps = _merge([(g.start_ms, g.end_ms) for g in ctx.gaps(("all", "screen"))])
        gap_starts = [g[0] for g in gaps]
        games = defaultdict(list)
        for proc, g0, g1 in conn.execute("SELECT lower(process), start_ms, end_ms FROM game_sessions WHERE start_ms <= ? AND end_ms >= ?",
                                         (hi, lo - CHECK_MS)):
            games[proc].append((g0, g1))
        idle = [(a, b if b is not None else now_ms) for a, b in ctx.sim_idle()]
        long_idle = [(a, b) for a, b in idle if b - a >= t_ms]
        idle_starts = [a for a, _ in long_idle]
        audio = {}  # lazily: per-process audible spans (system), mic spans with their sources

        def load_audio():
            if "sys" in audio:
                return
            a0 = min(a for a, _ in long_idle) - S_PAIR_MS - MAX_SEG_MS
            b0 = max(b for _, b in long_idle) + S_PAIR_MS
            sys_by = defaultdict(list)
            mic = []
            for stream, s0, s1, src in conn.execute(
                    "SELECT stream, start_ms, end_ms, sources FROM audio_segments WHERE start_ms < ? AND end_ms > ?", (b0, a0)):
                names = json.loads(src) if src and src != "[]" else []
                if stream == "system":
                    for n in names:
                        sys_by[n.lower()].append((s0, s1))
                else:
                    mic.append((s0, s1, {n.lower() for n in names}))
            audio["sys"] = {}
            for k, v in sys_by.items():
                m = _merge(v)
                audio["sys"][k] = (m, [x[0] for x in m], [x[1] for x in m])
            audio["mic"] = sorted(mic)
            audio["mic_starts"] = [x[0] for x in audio["mic"]]

        away_memo = {}

        def away_in(proc, mon, i0, i1):
            """Away spans inside one long K-idle stretch [i0, i1) for a band of `proc` on `mon`."""
            key = (proc, mon, i0, i1)
            if key in away_memo:
                return away_memo[key]
            acts = []
            audible, a_starts, a_ends = [], [], []
            if use_v or use_s:
                load_audio()
                audible, a_starts, a_ends = audio["sys"].get(proc, ([], [], []))

            def audible_near(x0, x1):
                """Some audible span of proc overlaps [x0, x1) (merged spans: ends are sorted too)."""
                k = bisect.bisect_right(a_ends, x0)
                return k < len(a_starts) and a_starts[k] < x1

            ks_points = [i0]
            if use_s:
                mic, ms = audio["mic"], audio["mic_starts"]
                for s0, s1, src in mic[bisect.bisect_left(ms, i0 - MAX_SEG_MS):bisect.bisect_left(ms, i1)]:
                    if s1 <= i0 or s0 >= i1:
                        continue
                    if not s_paired or proc in src or audible_near(s0 - S_PAIR_MS, s1 + S_PAIR_MS):
                        acts.append([max(s0, i0), min(s1, i1)])
                        ks_points.append(min(s1, i1))
            if use_v and audible:
                fr = by_mon.get(mon, [])
                k0 = bisect.bisect_left(fr, i0 - V_FRAME_MS)
                k1 = bisect.bisect_left(fr, i1)
                cover = _merge([(f, f + V_FRAME_MS) for f in fr[k0:k1]])
                part = audible[bisect.bisect_right(a_ends, i0):bisect.bisect_left(a_starts, i1)]
                v = _intersect(_intersect(part, [[i0, i1]]), cover)
                horizon = _merge([(x, x + V_CAP_MS) for x in ks_points])
                acts += _intersect(v, horizon)
            if use_game:
                for g0, g1 in games.get(proc, []):
                    if g1 > i0 and g0 < i1:
                        acts.append([max(g0, i0), min(g1, i1)])
            out = []
            cur = i0
            for a, b in _merge(acts):
                if a - cur >= t_ms:
                    out.append((cur, a))
                cur = max(cur, b)
            if i1 - cur >= t_ms:
                out.append((cur, i1))
            away_memo[key] = out
            return out

        res = []
        for s0, s1, proc, title, mon, last_ts in rows:
            end = rec_end
            k = bisect.bisect_right(starts, s1)
            if k < len(starts):
                end = min(end, starts[k])
            fr = by_mon.get(mon, [])
            k = bisect.bisect_right(fr, last_ts)
            if k < len(fr):
                end = min(end, fr[k])
            for g0, g1 in games.get((proc or "").lower(), []):
                if g0 <= s1 <= g1 + CHECK_MS:
                    end = min(end, max(s1, g1))
            k = bisect.bisect_left(gap_starts, s1)
            if k < len(gap_starts):
                end = min(end, gap_starts[k])
            end = max(end, s1)
            cuts = gaps
            if long_idle and end > s0:
                j = max(0, bisect.bisect_right(idle_starts, end) - 1)
                aw = []
                # every long idle stretch overlapping [s0, end): walk back from the last one that starts before end
                while j >= 0 and j < len(long_idle):
                    i0, i1 = long_idle[j]
                    if i1 <= s0:
                        break
                    if i0 < end:
                        aw += away_in((proc or "").lower(), mon, i0, i1)
                    j -= 1
                if aw:
                    cuts = _merge(gaps + aw)
            pieces = _subtract((s0, end), cuts) if end > s0 else []
            if not pieces and end == s0 and not any(c0 <= s0 < c1 for c0, c1 in cuts):
                pieces = [(s0, s0)]
            for a, b in pieces:
                if b >= start_ms and a < end_ms:
                    res.append(AppBand(a, b, proc, title))
        return res

    return bands


bands = make()
