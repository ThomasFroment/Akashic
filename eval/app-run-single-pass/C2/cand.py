"""C2: fused single pass with threshold-triggered accumulation.

One loop over the visits does the game-overlap filter (games sorted by end + suffix-min of
starts, a forward pointer), the run split (running max end), the horizon stop and the per-run
accumulators. A run gets accumulators only once its span first reaches its kind's minimum
(spans only grow, so qualification is monotone); then its earlier visits are folded once and
later visits as they arrive. The episode is emitted when the run closes.

Equivalence notes (same interpreter):
- running max with strict ``>`` keeps the first of equal ends, like ``max()``;
- titles: same dict insertion order and values, same stable ``sorted(reverse=True)[:8]``;
- process sums: per-process duration lists summed with builtin ``sum()`` (compensated for
  floats since 3.12), dominant process = ``max`` over a set built by inserting the processes in
  first-seen order (same table history as today's comprehension; duplicates are no-ops);
- URL: strict ``>`` scan = first max of a stable reverse sort when durations are totally
  ordered; if a process sum is NaN (a NaN duration may exist) the URL is recomputed exactly as
  today, because ``sorted`` with NaN keys is not a max scan;
- rows out of start order only cost a bisect to reposition the game pointer;
- rows are unpacked as the 6 columns the SQL selects;
- items are built in C (zip/repeat/map): the same list of ("app_session", id) tuples.
"""

from __future__ import annotations

import math
from bisect import bisect_right
from itertools import repeat
from operator import itemgetter
from typing import Final

CLOSE_GAP_MS: Final = 5 * 60_000
MIN_BROWSING_MS: Final = 3 * 60_000
MIN_WORK_MS: Final = 5 * 60_000
MAX_TITLES: Final = 8
BROWSER_PROCESSES: Final = frozenset(
    {"chrome.exe", "brave.exe", "msedge.exe", "firefox.exe", "zen.exe", "opera.exe", "vivaldi.exe"}
)

_ID = itemgetter(0)
_END = itemgetter(1)
_INF = math.inf


def app_label(process: str | None) -> str:
    stem = (process or "").removesuffix(".exe")
    return stem[:1].upper() + stem[1:]


def _emit(add_episode, browsing, seg, start_ms, end_ms, spent, sums, best, labels):
    kind = "browsing" if browsing else "work"
    titles = sorted(spent.items(), key=_END, reverse=True)[:MAX_TITLES]
    url = best
    if len(sums) == 1:  # max over a one-element set is that element
        for process, ds in sums.items():
            x = sum(ds)
            if x != x:
                url = _url_today(seg)
    else:
        tot = {p: sum(ds) for p, ds in sums.items()}
        process = max({p for p in sums}, key=tot.__getitem__)
        for x in tot.values():
            if x != x:
                url = _url_today(seg)
                break
    label = labels.get(process)
    if label is None:
        label = labels[process] = app_label(process)
    lead = titles[0][0] if titles else label
    body = "\n".join(map(_ID, titles))
    if not browsing:
        body = f"{label}\n{body}" if body else label
    return add_episode(
        kind=kind,
        ref=f"{kind}:{seg[0][0]}",
        start_ms=start_ms,
        end_ms=end_ms,
        title=lead,
        app=process,
        url=url,
        body=body,
        items=list(zip(repeat("app_session"), map(_ID, seg))),
    )


def _url_today(seg):
    """A NaN process sum means a NaN duration may exist; sorted() with NaN keys is not a max
    scan, so the URL is recomputed exactly as today."""
    return next(
        (r[3] for r in sorted(seg, key=lambda r: r[5] - r[4], reverse=True) if r[3]), None
    )


def _fold(seg, spent, sums):
    """Backfill a run that just qualified; returns (best url, its duration)."""
    best = bestd = None
    for _, p, t, u, s, e in seg:
        d = e - s
        if t:
            spent[t] = spent.get(t, 0) + (1_000 if 1_000 > d else d)  # max(d, 1_000)
        ds = sums.get(p)
        if ds is None:
            sums[p] = [d]
        else:
            ds.append(d)
        if u and (best is None or d > bestd):
            best = u
            bestd = d
    return best, bestd


def build_app_runs(rows, games, horizon, add_episode) -> int:
    if not rows:
        return 0
    gap = CLOSE_GAP_MS
    browsers = BROWSER_PROCESSES
    # games sorted by end; suf[k] = min start over games[k:]; a visit (s, e) overlaps a game
    # iff some game with end > s has start < e, i.e. suf[#ends <= s] < e. The pointer j keeps
    # prv = ends[j-1] <= s < nxt = ends[j] (-inf / +inf past the edges), lim = suf[j].
    gs = sorted(games, key=_END)
    ends = [g[1] for g in gs]
    G = len(ends)
    suf = [_INF] * (G + 1)
    m = _INF
    for k in range(G - 1, -1, -1):
        x = gs[k][0]
        if x < m:
            m = x
        suf[k] = m
    ends.append(_INF)  # sentinel: nxt past the last game
    j = 0
    prv = -_INF
    nxt = ends[0]
    lim = suf[0]

    labels = {}  # app_label per process, for this call
    built = 0
    open_ = False
    active = False
    seg = []
    rb = False
    rstart = rend = rmin = 0
    spent = sums = best = bestd = None

    for r in rows:
        _, p, t, u, s, e = r
        if s >= nxt or s < prv:
            if s < prv:  # rows out of start order: reposition
                j = bisect_right(ends, s, 0, G)
            else:  # also s = +inf at the sentinel: j stays at G
                while j < G and ends[j] <= s:
                    j += 1
            prv = ends[j - 1] if j else -_INF
            nxt = ends[j]
            lim = suf[j]
        if lim < e:
            continue
        b = p in browsers
        if open_ and b == rb and s - rend <= gap:
            seg.append(r)
            if e > rend:
                rend = e
            if active:
                d = e - s
                if t:
                    spent[t] = spent.get(t, 0) + (1_000 if 1_000 > d else d)  # max(d, 1_000)
                ds = sums.get(p)
                if ds is None:
                    sums[p] = [d]
                else:
                    ds.append(d)
                if u and (best is None or d > bestd):
                    best = u
                    bestd = d
            elif not (rend - rstart < rmin):
                active = True
                spent = {}
                sums = {}
                best, bestd = _fold(seg, spent, sums)
        else:
            if open_:
                if rend + gap > horizon:
                    return built
                if active:
                    built += _emit(add_episode, rb, seg, rstart, rend, spent, sums, best, labels)
            seg = [r]
            rend = e
            rstart = s
            rb = b
            rmin = MIN_BROWSING_MS if b else MIN_WORK_MS
            open_ = True
            if e - s < rmin:
                active = False
            else:
                active = True
                spent = {}
                sums = {}
                best, bestd = _fold(seg, spent, sums)
    if open_ and active and not (rend + gap > horizon):
        built += _emit(add_episode, rb, seg, rstart, rend, spent, sums, best, labels)
    return built
