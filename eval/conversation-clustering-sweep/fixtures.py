"""Seeded inputs for the conversation clustering harness.

Every generator returns (rows, games, horizon):
  rows    list of (id, stream, start_ms, end_ms, speech_ms, sources_json, transcript), ordered by
          (start_ms, id) exactly as the SQL in episodes._conversation_clusters returns them;
          sources_json is written with json.dumps(list), like storage/audio.py does.
  games   list of (process, start_ms, end_ms).
  horizon int.
"""

from __future__ import annotations

import json
import random

S = 1_000
M = 60_000
T0 = 1_762_000_000_000  # an epoch ms in Nov 2025
GAP = 300_000
MARGIN = 30_000
GAME = "eldenring.exe"

SRC_POOL = [
    [],
    ["discord.exe"],
    ["discord.exe", "chrome.exe"],
    ["chrome.exe"],
    ["zen.exe"],
    [GAME],
    ["teams.exe"],
]
WORDS = "on lance la partie ok attends je reviens what game tonight yes non peut-être".split()


def _row(id_, stream, start, end, sources, text="..."):
    return (id_, stream, start, end, max(0, int((end - start) * 0.7)), json.dumps(sources), text)


def _sorted(rows):
    return sorted(rows, key=lambda r: (r[2], r[0]))


class _B:
    """Tiny builder: auto ids in insertion order."""

    def __init__(self):
        self.rows = []
        self.next = 1

    def add(self, stream, start, end, sources=None, id_=None):
        if sources is None:
            sources = ["discord.exe"] if stream == "system" else []
        if id_ is None:
            id_, self.next = self.next, self.next + 1
        self.rows.append(_row(id_, stream, start, end, sources))
        return id_

    def call(self, start, minutes=10, every=10 * S, length=5 * S, sources=None):
        """Voice segments every `every` ms; returns (vs, ve)."""
        t = start
        end = start + minutes * M
        last = start
        while t < end:
            self.add("system", t, t + length, sources)
            last = t + length
            t += every
        return start, last

    def done(self, games=(), horizon=None):
        rows = _sorted(self.rows)
        if horizon is None:
            horizon = max(r[3] for r in rows) + 10 * 3600 * S if rows else T0
        return rows, list(games), horizon


# --- realistic 8-hour call -------------------------------------------------------------------


def realistic(seed=0, n_voice=10_000, n_mic=10_000):
    """An ~8 h Discord evening: 10k system (voice) segments <= 30 s and 10k mic segments,
    with a few breaks over 5 minutes (so a few earlier clusters are final), a 2 h game session
    with in-game chat, and the horizon 1 minute after the last speech (the call is growing)."""
    rng = random.Random(seed)
    segs = []  # (stream, start, end, sources)
    t = T0
    breaks = set(rng.sample(range(500, n_voice - 500), 3))
    pauses = []  # the call is off: mic speech there is the user alone (never voice)
    for i in range(n_voice):
        dur = int(min(30 * S, max(400, rng.lognormvariate(7.6, 0.6))))  # median ~2.0 s
        segs.append(("system", t, t + dur, None))
        t += dur + int(rng.expovariate(1 / 200))
        if i in breaks:
            pause = rng.randint(6 * M, 25 * M)
            pauses.append((t, t + pause))
            t += pause
    span_end = t
    game_start = T0 + (span_end - T0) // 3
    games = [(GAME, game_start, game_start + 2 * 3600 * S)]

    sys_src = [
        (["discord.exe"], 0.80),
        (["discord.exe", "chrome.exe"], 0.10),
        (["chrome.exe"], 0.04),
        ([], 0.03),
        ([GAME], 0.03),
    ]
    mic_src = [([], 0.85), (["discord.exe"], 0.05), (["chrome.exe"], 0.05), (["zen.exe"], 0.05)]
    alone_src = [([], 0.9), (["zen.exe"], 0.1)]

    def pick(pool):
        x = rng.random()
        for src, w in pool:
            x -= w
            if x <= 0:
                return src
        return pool[0][0]

    durs = []
    for _ in range(n_mic):
        if rng.random() < 0.001:
            durs.append(rng.randint(240 * S, 700 * S))
        else:
            durs.append(int(min(30 * S, max(300, rng.lognormvariate(7.3, 0.7)))))
    mean_gap = max(200, ((span_end - T0) - sum(durs)) // n_mic)
    t = T0 + rng.randint(0, 5 * S)
    for d in durs:
        segs.append(("mic", t, t + d, None))
        t += d + int(rng.expovariate(1 / mean_gap))

    # ids autoincrement in transcription order (by end time)
    order = sorted(range(len(segs)), key=lambda k: (segs[k][2], k))
    rows = []
    for id_, k in enumerate(order, start=1):
        stream, start, end, _ = segs[k]
        if stream == "mic" and any(a - 60 * S <= start < b for a, b in pauses):
            src = pick(alone_src)
        else:
            src = pick(sys_src if stream == "system" else mic_src)
        text = " ".join(rng.choice(WORDS) for _ in range(rng.randint(2, 12)))
        rows.append(_row(id_, stream, start, end, src, text))
    rows = _sorted(rows)
    horizon = max(r[3] for r in rows) + 1 * M
    return rows, games, horizon


# --- edge sets -------------------------------------------------------------------------------


def _mic_chain_after(horizon_after_ve=None):
    b = _B()
    _vs, ve = b.call(T0)
    t = ve + 5 * S  # ve+5 s and ve+25 s are inside the 30 s margin, the rest are not
    while t < ve + 10 * M:
        b.add("mic", t, t + 5 * S)
        t += 20 * S
    return b.done(horizon=None if horizon_after_ve is None else ve + horizon_after_ve)


def e_mic_chain_after_call():
    """Mic speech every 20 s for 10 min after the call: today chains it all."""
    return _mic_chain_after()


def e_mic_chain_after_call_finality():
    """Same, horizon ve+400 s: contract says final (end ve+30 s), today says growing."""
    return _mic_chain_after(400 * S)


def e_mic_chain_before_call():
    """Mic chain in the 10 min before the call: only the last ones are within 30 s (control)."""
    b = _B()
    vs = T0 + 10 * M
    t = T0
    while t < vs:
        b.add("mic", t, t + 5 * S)
        t += 20 * S
    b.call(vs)
    return b.done()


def _two_calls():
    b = _B()
    _vs_a, ve_a = b.call(T0, minutes=2)
    vs_b = ve_a + GAP + 1
    b.call(vs_b, minutes=2)
    return b, ve_a, vs_b


def e_mic_equal_distance_two_clusters():
    """Long mic d=10 s from both clusters: goes to the earlier one; today attaches to both."""
    b, ve_a, vs_b = _two_calls()
    b.add("mic", ve_a + 10 * S, vs_b - 10 * S)
    return b.done()


def e_mic_nearer_second_cluster():
    """Long mic 20 s after A, 5 s before B: goes to B; today attaches to both."""
    b, ve_a, vs_b = _two_calls()
    b.add("mic", ve_a + 20 * S, vs_b - 5 * S)
    return b.done()


def e_mic_spans_both_clusters():
    """Mic overlapping both calls (d=0 for both): earlier cluster; today both."""
    b, ve_a, vs_b = _two_calls()
    b.add("mic", ve_a - 5 * S, vs_b + 5 * S)
    return b.done()


def e_mic_spans_three_clusters():
    b = _B()
    _, ve_a = b.call(T0, minutes=1)
    vs_b, ve_b = b.call(ve_a + GAP + 1, minutes=1)
    vs_c, _ = b.call(ve_b + GAP + 1, minutes=1)
    b.add("mic", ve_a - 1 * S, vs_c + 1 * S)  # long (> 600 s): d=0 to all three
    return b.done()


def e_gap_exactly_300000():
    """Voice gap of exactly 300,000 ms: same cluster."""
    b = _B()
    b.add("system", T0, T0 + 5 * S)
    b.add("system", T0 + 5 * S + GAP, T0 + 10 * S + GAP)
    return b.done()


def e_gap_300001():
    """Voice gap of 300,001 ms: two clusters."""
    b = _B()
    b.add("system", T0, T0 + 5 * S)
    b.add("system", T0 + 5 * S + GAP + 1, T0 + 10 * S + GAP + 1)
    return b.done()


def e_gap_measured_from_max_end():
    """A long early segment holds the cluster end: the gap is from the max end, not the last."""
    b = _B()
    b.add("system", T0, T0 + 30 * S)
    b.add("system", T0 + 1 * S, T0 + 2 * S)
    b.add("system", T0 + 30 * S + GAP, T0 + 35 * S + GAP)  # joins (gap from max end = 300 s)
    b.add("system", T0 + 35 * S + 2 * GAP + 1, T0 + 40 * S + 2 * GAP + 1)  # splits
    return b.done()


def e_mic_start_at_ve_plus_30s():
    """m.start == ve + 30 s: outside the half-open window."""
    b = _B()
    _vs, ve = b.call(T0, minutes=1)
    b.add("mic", ve + MARGIN, ve + MARGIN + 5 * S)
    return b.done()


def e_mic_start_at_ve_plus_30s_minus_1():
    b = _B()
    _vs, ve = b.call(T0, minutes=1)
    b.add("mic", ve + MARGIN - 1, ve + MARGIN + 5 * S)
    return b.done()


def e_mic_end_at_vs_minus_30s():
    """m.end == vs - 30 s: outside."""
    b = _B()
    vs = T0 + 2 * M
    b.add("mic", vs - MARGIN - 5 * S, vs - MARGIN)
    b.call(vs, minutes=1)
    return b.done()


def e_mic_end_at_vs_minus_30s_plus_1():
    b = _B()
    vs = T0 + 2 * M
    b.add("mic", vs - MARGIN - 5 * S, vs - MARGIN + 1)
    b.call(vs, minutes=1)
    return b.done()


def e_voice_mic_in_cluster():
    """Mic segments with a discord.exe source are voice: they cluster and extend ve."""
    b = _B()
    _vs, ve = b.call(T0, minutes=2)
    b.add("mic", ve + 60 * S, ve + 65 * S, ["discord.exe"])  # voice, extends ve by 65 s
    b.add("mic", ve + 90 * S, ve + 95 * S)  # in only because of the voice mic above
    b.add("mic", ve + 130 * S, ve + 135 * S)  # 65 s past the new ve: out
    return b.done()


def e_chain_bridge_into_next_call():
    """Mic chain from call A into call B, B has voice-mic segments: today A swallows B's mic
    side (voice mic included) and B takes it again."""
    b = _B()
    _, ve_a = b.call(T0, minutes=3)
    vs_b = ve_a + 8 * M
    t = ve_a + 5 * S
    while t < vs_b + 2 * M:
        b.add("mic", t, t + 5 * S)
        t += 20 * S
    b.call(vs_b, minutes=3)
    b.add("mic", vs_b + 30 * S, vs_b + 36 * S, ["discord.exe"])
    b.add("mic", vs_b + 70 * S, vs_b + 76 * S, ["discord.exe", "chrome.exe"])
    return b.done()


def e_game_voice():
    """In-game chat counts as voice only while the game session overlaps the segment."""
    b = _B()
    g0 = T0 + 1 * M
    b.add("system", T0, T0 + 5 * S, [GAME])  # before the session: not voice
    b.add("system", g0 + 10 * S, g0 + 15 * S, [GAME])
    b.add("system", g0 + 40 * S, g0 + 50 * S, [GAME])
    b.add("mic", g0 + 55 * S, g0 + 58 * S)
    b.add("system", g0 + 20 * M, g0 + 20 * M + 5 * S, [GAME])  # after the session: not voice
    b.add("mic", g0 + 20 * M, g0 + 20 * M + 5 * S)
    return b.done(games=[(GAME, g0, g0 + 5 * M)])


def e_long_mic_segments():
    """Mic segments of 240-700 s around a call, then a short mic chained off the long one."""
    b = _B()
    vs = T0 + 15 * M
    b.add("mic", vs - MARGIN + 1 - 600 * S, vs - MARGIN + 1)  # ends 29,999 ms before vs: in
    b.add("mic", vs - 1000 * S, vs - MARGIN)  # ends exactly vs-30 s: out
    _, ve = b.call(vs, minutes=5)
    b.add("mic", vs + 60 * S, vs + 300 * S)  # 240 s inside the call
    b.add("mic", ve + MARGIN - 1, ve + MARGIN - 1 + 700 * S)  # in, ends ve+729 s
    b.add("mic", ve + 740 * S, ve + 745 * S)  # today chains it, contract: out
    b.add("mic", ve + 760 * S, ve + 1100 * S)  # today chains it, contract: out
    return b.done()


def e_finality_exact():
    """Cluster end + 300 s == horizon: final."""
    b = _B()
    _, ve = b.call(T0, minutes=1)
    return b.done(horizon=ve + GAP)


def e_finality_minus_1():
    """Horizon one ms short: growing, growing_start = vs."""
    b = _B()
    _, ve = b.call(T0, minutes=1)
    return b.done(horizon=ve + GAP - 1)


def e_finality_from_mic_end():
    """An attached mic ending after ve decides finality."""
    b = _B()
    _, ve = b.call(T0, minutes=1)
    b.add("mic", ve - 1 * S, ve + 20 * S)
    return b.done(horizon=ve + 20 * S + GAP - 1)


def e_growing_start_from_mic():
    """Growing cluster whose earliest member is a mic segment before vs."""
    b = _B()
    _, ve_a = b.call(T0, minutes=1)
    vs_b = ve_a + 20 * M
    b.add("mic", vs_b - 20 * S, vs_b - 15 * S)
    _, ve_b = b.call(vs_b, minutes=1)
    return b.done(horizon=ve_b + 1 * M)


def e_empty():
    return [], [], T0


def e_no_voice():
    b = _B()
    b.add("mic", T0, T0 + 5 * S)
    b.add("system", T0 + 10 * S, T0 + 15 * S, ["chrome.exe"])
    b.add("system", T0 + 20 * S, T0 + 25 * S, [])
    return b.done()


def e_equal_starts_id_order():
    """Equal start_ms, ids not in insertion order: members sorted by (start, id)."""
    b = _B()
    b.add("system", T0, T0 + 5 * S, id_=50)
    b.add("system", T0, T0 + 6 * S, id_=7)
    b.add("mic", T0, T0 + 4 * S, id_=30)
    b.add("mic", T0 + 2 * S, T0 + 3 * S, id_=2)
    b.add("system", T0 + 2 * S, T0 + 9 * S, id_=1)
    return b.done()


def e_growing_and_final_mixed():
    """Three calls: two final, last growing; mics near each."""
    b = _B()
    _, ve_a = b.call(T0, minutes=2)
    b.add("mic", ve_a + 10 * S, ve_a + 12 * S)
    vs_b, ve_b = b.call(ve_a + 10 * M, minutes=2)
    b.add("mic", vs_b - 10 * S, vs_b - 8 * S)
    vs_c, ve_c = b.call(ve_b + 10 * M, minutes=2)
    b.add("mic", ve_c + 5 * S, ve_c + 9 * S)
    return b.done(horizon=ve_c + 2 * M)


EDGES = {
    name.removeprefix("e_"): fn
    for name, fn in sorted(globals().items())
    if name.startswith("e_") and callable(fn)
}


def edge_sets():
    return {name: fn() for name, fn in EDGES.items()}


# --- random small cases ----------------------------------------------------------------------

BOUNDARY_DELTAS = [
    0, 1, -1, MARGIN, MARGIN - 1, MARGIN + 1, -MARGIN, -MARGIN + 1, -MARGIN - 1,
    GAP, GAP - 1, GAP + 1, GAP + MARGIN, GAP + MARGIN - 1, 2 * MARGIN, 2 * MARGIN - 1,
]  # fmt: skip


def random_case(seed, max_n=40, horizon_mode=None):
    """A small random case biased toward the contract boundaries."""
    rng = random.Random(seed)
    n = rng.randint(0, max_n)
    raw = []  # (stream, start, end)
    t = T0
    for _ in range(n):
        r = rng.random()
        if raw and r < 0.35:  # anchor on a previous end/start with a boundary delta
            stream, s0, e0 = rng.choice(raw)
            anchor = e0 if rng.random() < 0.7 else s0
            start = anchor + rng.choice(BOUNDARY_DELTAS)
        else:
            if r < 0.6:
                gap = rng.randint(-5 * S, 25 * S)
            elif r < 0.8:
                gap = rng.randint(25 * S, 90 * S)
            elif r < 0.93:
                gap = rng.randint(250 * S, 360 * S)
            else:
                gap = rng.randint(6 * M, 20 * M)
            start = t + gap
        q = rng.random()
        if q < 0.08:
            dur = rng.randint(240 * S, 700 * S)
        elif q < 0.12:
            dur = rng.choice([1, 2, MARGIN, MARGIN + 1, 2 * MARGIN])
        else:
            dur = rng.randint(300, 30 * S)
        stream = "mic" if rng.random() < 0.5 else "system"
        raw.append((stream, start, start + dur))
        t = max(t, start)
    ids = rng.sample(range(1, 3 * max(n, 1) + 1), n)
    if rng.random() < 0.2 and n > 1:  # duplicate starts
        k = rng.randrange(1, n)
        s, _, e = raw[k]
        st = raw[k - 1][1]
        raw[k] = (s, st, st + max(1, e - raw[k][1]))
    rows = []
    for id_, (stream, start, end) in zip(ids, raw, strict=True):
        if stream == "mic":
            src = rng.choice([[], [], [], ["zen.exe"], ["chrome.exe"], ["discord.exe"], [GAME]])
        else:
            src = rng.choice(SRC_POOL + [["discord.exe"]] * 3)
        rows.append(_row(id_, stream, start, end, src))
    rows = _sorted(rows)
    games = []
    if rows:
        lo, hi = rows[0][2], max(r[3] for r in rows)
        for _ in range(rng.randint(0, 2)):
            a = rng.randint(lo - 60 * S, hi)
            games.append((GAME, a, a + rng.randint(1, max(2, hi - lo))))
    if not rows:
        return rows, games, T0
    max_end = max(r[3] for r in rows)
    mode = horizon_mode or rng.choice(["boundary", "random", "far", "early"])
    if mode == "boundary":
        horizon = rng.choice([r[3] for r in rows]) + GAP + rng.choice([-1, 0, 1, MARGIN])
    elif mode == "random":
        horizon = rng.randint(rows[0][2], max_end + 2 * GAP)
    elif mode == "far":
        horizon = max_end + 10 * GAP
    else:
        horizon = rng.randint(rows[0][2], max_end)
    return rows, games, horizon


def replay_case(seed):
    """A random case for the multi-pass replay (its horizon is ignored)."""
    rows, games, _ = random_case(10_000_000 + seed, max_n=25)
    return rows, games
