"""Seeded inputs for build_app_runs(rows, games, horizon, add_episode).

rows  = app_sessions rows (id, process, window_title, url, start_ms, end_ms), sorted by
        (start_ms, id) as the SQL returns them; ids unique.
games = game_sessions (start_ms, end_ms) pairs.

app_sessions/game_sessions are non-STRICT tables with INTEGER affinity on start/end: a REAL
value that is exactly integral is stored as INTEGER, any other REAL (fractions, +-inf, values
beyond int64) stays REAL. NaN cannot be stored (becomes NULL, rejected by NOT NULL), but
inf - inf durations are NaN inside today's code. Category "pyonly" goes beyond what SQLite can
return (integral floats, -0.0) and is reported separately.

All generators use random.Random(<int or str>) which does not depend on PYTHONHASHSEED.
"""

from __future__ import annotations

import math
import random

GAP = 5 * 60_000
T0 = 1_700_000_000_000
DAY = 86_400_000
HOUR = 3_600_000

BROWSERS = ["zen.exe", "chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe"]
WORK = ["code.exe", "windowsterminal.exe", "explorer.exe", "notepad.exe", "discord.exe",
        "obsidian.exe", "excel.exe", "slack.exe"]  # fmt: skip


def sql_game_filter(rows, games):
    """The SQL prefilter today applies: start_ms < rows[-1].end AND end_ms > rows[0].start."""
    if not rows:
        return list(games)
    hi, lo = rows[-1][5], rows[0][4]
    return [g for g in games if g[0] < hi and g[1] > lo]


def _sort(rows):
    rows.sort(key=lambda r: (r[4], r[0]))
    return rows


def _ids(rng, n):
    ids = list(range(1, n + 1))
    # mostly increasing with start (insertion order), some out of order
    for _ in range(n // 20):
        a, b = rng.randrange(n), rng.randrange(n)
        ids[a], ids[b] = ids[b], ids[a]
    return ids


# --- games ---------------------------------------------------------------------------------


def make_games(rng, rows, n, coverage=0.10):
    """n game sessions over the rows' span covering about ``coverage`` of it, with inverted,
    zero-length, touching (exactly at a visit boundary), overlapping and nested sessions."""
    if not rows or n <= 0:
        return []
    lo = min(r[4] for r in rows if math.isfinite(r[4]))
    hi = max(r[5] for r in rows if math.isfinite(r[5]))
    span = max(hi - lo, 1)
    mean = max(int(coverage * span / n), 1)
    games: list[tuple] = []
    for _ in range(n):
        m = rng.random()
        s = rng.randrange(lo, lo + span)
        d = int(mean * rng.uniform(0.2, 1.8))
        if m < 0.05:  # inverted
            games.append((s, s - rng.randrange(0, mean + 1)))
        elif m < 0.10:  # zero length
            games.append((s, s))
        elif m < 0.20:  # touching a visit boundary exactly
            r = rows[rng.randrange(len(rows))]
            if rng.random() < 0.5:
                games.append((r[5], r[5] + d))  # starts where the visit ends
            else:
                games.append((r[4] - d, r[4]))  # ends where the visit starts
        elif m < 0.27 and games:  # overlapping an earlier game
            gs, ge = games[rng.randrange(len(games))]
            a = min(gs, ge)
            games.append((a + (abs(ge - gs) // 2), a + abs(ge - gs) // 2 + d))
        elif m < 0.34 and games:  # nested inside an earlier game
            gs, ge = games[rng.randrange(len(games))]
            a, b = min(gs, ge), max(gs, ge)
            x = rng.randint(a, b)
            games.append((x, rng.randint(x, b)))
        else:
            games.append((s, s + d))
    rng.shuffle(games)
    return games


# --- realistic day shape -------------------------------------------------------------------


def realistic(seed, V=50_000, G=500, coverage=0.10):
    """Days of 16 active hours made of long same-kind blocks (~200 visits), sticky processes,
    short gaps, rare idle breaks: a few hundred runs per 50k visits."""
    rng = random.Random(f"realistic-{seed}-{V}-{G}")
    rows = []
    t = T0 + 8 * HOUR
    day_end = T0 + DAY
    ids = _ids(rng, V)
    i = 0
    while i < V:
        kind_browser = rng.random() < 0.45
        pool = BROWSERS[:3] if kind_browser else WORK
        weights = [6, 2, 1] if kind_browser else [6, 3, 3, 1, 2, 1, 1, 1]
        block = max(1, int(rng.lognormvariate(math.log(180), 0.7)))
        titles = [f"{'page' if kind_browser else 'doc'} {rng.randrange(10_000)}" for _ in range(25)]
        urls = [f"https://site{rng.randrange(300)}.example/{rng.randrange(10_000)}" for _ in range(25)]
        proc = rng.choices(pool, weights)[0]
        for _ in range(min(block, V - i)):
            if rng.random() > 0.7:
                proc = rng.choices(pool, weights)[0]
            m = rng.random()
            d = 0 if m < 0.10 else int(min(rng.lognormvariate(math.log(20_000), 1.3), 1_800_000))
            tr = rng.random()
            title = None if tr < 0.05 else "" if tr < 0.07 else rng.choice(titles)
            if proc in BROWSERS:
                ur = rng.random()
                url = None if ur < 0.10 else "" if ur < 0.15 else rng.choice(urls)
            else:
                url = None
            rows.append((ids[i], proc, title, url, t, t + d))
            i += 1
            g = rng.random()
            if g < 0.70:
                step = d
            elif g < 0.95:
                step = d + rng.randrange(0, 10_000)
            elif g < 0.97:
                step = max(d - rng.randrange(0, 5_000), 0)  # overlapping sessions
            elif g < 0.997:
                step = d + rng.randrange(10_000, 240_000)
            else:
                step = d + rng.randrange(GAP + 1, 20 * 60_000)  # idle break inside the block
            t += step
            if t >= day_end:
                t = day_end + 8 * HOUR
                day_end += DAY
        if rng.random() < 0.5:
            t += rng.randrange(6 * 60_000, HOUR)  # idle between blocks
    _sort(rows)
    games = sql_game_filter(rows, make_games(rng, rows, G, coverage))
    horizon = max(r[5] for r in rows) + 10 * GAP
    return rows, games, horizon


# --- synthetic fragmented shape ------------------------------------------------------------


def fragmented(seed, V=50_000, G=500, coverage=0.10):
    """Random process per visit and random gaps: 10-22k runs per 50k visits."""
    rng = random.Random(f"fragmented-{seed}-{V}-{G}")
    procs = ["zen.exe"] * 2 + ["chrome.exe"] + ["code.exe"] * 3 + [
        "discord.exe", "explorer.exe", "windowsterminal.exe", "notepad.exe",
        "obsidian.exe", "excel.exe", "slack.exe", "spotify.exe"]  # fmt: skip
    ids = _ids(rng, V)
    rows = []
    t = T0
    for i in range(V):
        t += rng.choice([0, 1000, 5000, 20_000, 60_000, rng.randrange(0, 900_000)])
        d = rng.choice([0, 0, 2000, 15_000, 60_000, rng.randrange(0, 600_000)])
        p = rng.choice(procs)
        title = rng.choice([None, "", f"title {rng.randrange(5000)}", f"common {rng.randrange(20)}"])
        url = (
            f"https://ex.com/{rng.randrange(10000)}"
            if p in BROWSERS and rng.random() < 0.8
            else rng.choice([None, ""])
        )
        rows.append((ids[i], p, title, url, t, t + d))
    _sort(rows)
    games = sql_game_filter(rows, make_games(rng, rows, G, coverage))
    horizon = max(r[5] for r in rows) + 10 * GAP
    return rows, games, horizon


SHAPES = {"realistic": realistic, "fragmented": fragmented}


# --- tie-heavy small fuzz cases ------------------------------------------------------------

_PROCS = BROWSERS[:4] + WORK[:6]
_TITLES = [None, "", "a", "b", "c", "A", "a b", "é"]
_URLS = [None, "", "u1", "u2", "u3"]
_STEPS = [0, 0, 1000, 60_000, 120_000, GAP - 1, GAP, GAP + 1, 400_000]
_DURS = [-1000, 0, 1000, 60_000]
_FRACS = [0.5, 0.25, 0.1, 0.7, 0.3, 1e-3]
_INF = math.inf
_HUGE = [1e12, 1e12 + 0.5, 2.0**53, 3e15, 1e16]
_TINY = [1e-4, 1.1e-4, 2e-4, 0.1, 0.2, 0.3, 1.0001 - 1, 2.00011 - 2, 0.5]


def _cancel_rows(rng):
    """Runs whose per-process durations are +X, -X pairs plus tiny fractions: a plain running
    float sum loses the fractions, builtin sum() (compensated since Python 3.12) keeps them,
    and the dominant process flips between the two."""
    browser = rng.random() < 0.3
    procs = rng.sample(BROWSERS[:4] if browser else WORK[:6], rng.randrange(2, 4))
    if rng.random() < 0.15:
        procs.append(rng.choice(WORK if browser else BROWSERS))  # a kind switch
    durs = []
    for p in procs:
        for _ in range(rng.randrange(0, 3)):
            x = rng.choice(_HUGE)
            durs += [(p, x), (p, -x)]
        durs += [(p, rng.choice(_TINY)) for _ in range(rng.randrange(1, 4))]
    rng.shuffle(durs)
    ids = rng.sample(range(1, 10 * len(durs) + 2), len(durs))
    t = rng.choice([0, 1000, -3_000_000])
    rows = []
    for k, (p, d) in enumerate(durs):
        t += rng.choice([0, 1000, 60_000, GAP])
        rows.append((ids[k], p, rng.choice(_TITLES), rng.choice(_URLS), t, t + d))
    # an anchor pair (+400 s then -400 s, exact ints, net 0) so the run passes the minimum
    # length even without huge durations, without changing any process's sum
    n = 10 * len(durs) + 5
    rows.append((n, procs[0], "anchor", None, t, t + 400_000))
    rows.append((n + 1, procs[0], None, None, t + 400_000, t))
    return _sort(rows)


def _small_rows(rng, cat):
    V = rng.randrange(0, 60)
    procs = rng.sample(_PROCS, rng.randrange(1, 8))
    titles = rng.sample(_TITLES, rng.randrange(1, len(_TITLES)))
    urls = rng.sample(_URLS, rng.randrange(1, len(_URLS)))
    ids = rng.sample(range(1, 10 * V + 2), V)
    t = rng.choice([0, T0, -5_000_000])
    rows = []
    for k in range(V):
        t += rng.choice(_STEPS)
        d = rng.choice(_DURS) if rng.random() < 0.85 else rng.choice([200_000, 400_000, 2_000_000])
        s, e = t, t + d
        if cat == "real":
            m = rng.random()
            if m < 0.35:
                s = s + rng.choice(_FRACS)
            if rng.random() < 0.35:
                e = e + rng.choice(_FRACS)
            if m > 0.97:
                s = rng.choice([_INF, -_INF])
            if rng.random() < 0.03:
                e = rng.choice([_INF, -_INF])
            if rng.random() < 0.01:
                s, e = s + 1e19, e + 1e19  # beyond int64: stays REAL
        elif cat == "pyonly":
            if rng.random() < 0.4:
                s = float(s)
            if rng.random() < 0.4:
                e = float(e)
            if rng.random() < 0.05:
                s = -0.0 if s == 0 else s
        rows.append((ids[k], rng.choice(procs), rng.choice(titles), rng.choice(urls), s, e))
    return _sort(rows)


def _small_games(rng, rows, cat):
    games = []
    if not rows:
        return [(0, 1)] if rng.random() < 0.5 else []
    finite = [r for r in rows if math.isfinite(r[4]) and math.isfinite(r[5])] or [(0, "", "", "", 0, 0)]
    for _ in range(rng.randrange(0, 7)):
        r = rng.choice(finite)
        m = rng.random()
        a = r[4]
        d = rng.choice([1, 1000, 60_000, GAP, 900_000])
        if m < 0.15:
            g = (a + d, a)  # inverted
        elif m < 0.30:
            g = (a, a)  # zero length
        elif m < 0.45:
            g = (r[5], r[5] + d)  # touching end
        elif m < 0.60:
            g = (a - d, a)  # touching start
        elif m < 0.70 and games:
            gs, ge = games[-1]
            g = (gs, ge) if rng.random() < 0.3 else ((gs + ge) / 2 if cat != "int" else (gs + ge) // 2, ge + d)
        elif m < 0.80 and games:
            gs, ge = games[-1]
            g = (gs + 1, max(gs + 1, ge - 1))  # nested
        else:
            g = (a - rng.randrange(0, 600_000), a + rng.randrange(0, 600_000))
        if cat == "real" and rng.random() < 0.15:
            g = rng.choice([(-_INF, g[1]), (g[0], _INF), (g[0] + 0.5, g[1] - 0.5), (_INF, -_INF), (-_INF, _INF)])
        games.append(g)
    if rng.random() < 0.6:
        games = sql_game_filter(rows, games)
    return games


def _small_horizon(rng, rows, cat):
    if not rows:
        return rng.choice([0, T0])
    ends = [r[5] for r in rows if math.isfinite(r[5])] or [0]
    starts = [r[4] for r in rows if math.isfinite(r[4])] or [0]
    e = rng.choice(ends)
    hi = max(ends)
    options = [
        e + GAP,  # exactly at a (candidate) run end + GAP: closed
        e + GAP - 1,
        e + GAP + 1,
        hi + GAP,  # last run closed exactly
        (min(starts) + hi) // 2 if isinstance(hi, int) and isinstance(min(starts), int) else (min(starts) + hi) / 2,
        10**15,  # far future
        min(starts) - 1,  # nothing closed
    ]
    if cat == "real":
        options += [_INF, e + GAP + 0.5, e + GAP - 0.5]
    return rng.choice(options)


CATEGORIES = ("int", "real", "pyonly", "medium")


def fuzz_case(i):
    """Case i (deterministic): (category, rows, games, horizon)."""
    rng = random.Random(10_007 * i + 3)
    m = i % 20
    if m < 13:
        cat = "int"
    elif m < 18:
        cat = "real"
    elif m < 19:
        cat = "pyonly"
    else:
        cat = "medium"
    if cat == "medium":
        V = rng.randrange(200, 2500)
        G = rng.randrange(0, 40)
        shape = realistic if (i // 20) % 2 else fragmented
        rows, games, _ = shape(i, V, G, coverage=rng.choice([0.0, 0.05, 0.3]))
        ends = [r[5] for r in rows]
        horizon = rng.choice([max(ends) + GAP, rng.choice(ends) + GAP, rng.choice(ends) + GAP - 1, 10**15])
        return cat, rows, games, horizon
    rows = _cancel_rows(rng) if cat == "real" and rng.random() < 0.5 else _small_rows(rng, cat)
    games = _small_games(rng, rows, cat)
    horizon = _small_horizon(rng, rows, cat)
    return cat, rows, games, horizon


def run_count(rows, games, horizon=10**18):
    """Number of runs (closed or not) today's rules make: for describing fixtures."""
    import ref_today as R  # noqa: PLC0415

    visits = [R._Visit(r[0], r[1], r[2] or "", r[3], r[4], r[5]) for r in rows
              if not any(g[0] < r[5] and g[1] > r[4] for g in games)]  # fmt: skip
    return len(R._runs(visits)), len(visits)
