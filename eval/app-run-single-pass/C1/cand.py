"""C1: two-phase index-range runs + suffix-min game pointer.

Scan 1 walks the raw row tuples once: a game-overlap test in O(1) amortised (games sorted by
end, suffix minimum of their starts, a pointer advanced with the row starts), and runs grouped
with a running max end. It stops at the first run that may still grow (end + gap > horizon).
Scan 2 only visits runs long enough to become episodes and derives titles, the dominant process
and the URL with the same tie-breaking as today's code (see the comments).
"""

from __future__ import annotations

from bisect import bisect_right
from math import inf
from operator import itemgetter
from typing import Final

CLOSE_GAP_MS: Final = 5 * 60_000
MIN_BROWSING_MS: Final = 3 * 60_000
MIN_WORK_MS: Final = 5 * 60_000
MAX_TITLES: Final = 8
BROWSER_PROCESSES: Final = frozenset(
    {"chrome.exe", "brave.exe", "msedge.exe", "firefox.exe", "zen.exe", "opera.exe", "vivaldi.exe"}
)
_FIRST = itemgetter(0)
_SECOND = itemgetter(1)


def app_label(process: str | None) -> str:
    stem = (process or "").removesuffix(".exe")
    return stem[:1].upper() + stem[1:]


def _closed_runs(rows, games, horizon):
    """(kept rows, [(i0, i1, start, end, is_browsing)]): the runs closed before ``horizon``
    that are long enough to become episodes, as index ranges into the kept rows."""
    gap = CLOSE_GAP_MS
    browsers = BROWSER_PROCESSES
    min_b = MIN_BROWSING_MS
    min_w = MIN_WORK_MS
    runs = []
    add_run = runs.append
    rkind = None  # no open run
    rstart = rend = None
    i0 = 0
    if games:
        kept = []
        keep = kept.append
        gs = sorted(games, key=_SECOND)
        n_games = len(gs)
        ends = [g[1] for g in gs]
        ends.append(inf)  # sentinel; the j < n_games guard handles s == inf
        # suf[k] = min start of games k.. in end order; suf[n_games] = +inf
        suf = [inf] * (n_games + 1)
        m = inf
        for k in range(n_games - 1, -1, -1):
            s0 = gs[k][0]
            if s0 < m:
                m = s0
            suf[k] = m
        j = 0
        last = -inf
        for r in rows:
            s = r[4]
            e = r[5]
            # j = number of games with end <= s; some game overlaps iff end > s and start < e
            if s < last:  # rows out of start order (not what the SQL returns): re-seat
                j = bisect_right(ends, s, 0, n_games)
            else:
                while ends[j] <= s and j < n_games:
                    j += 1
            last = s
            if suf[j] < e:
                continue
            kind = r[1] in browsers
            if kind is rkind and s - rend <= gap:
                keep(r)
                if e > rend:  # same rule as builtin max(): first of equals kept
                    rend = e
                continue
            n = len(kept)
            if rkind is not None:
                if rend + gap > horizon:
                    return kept, runs  # may still grow; later runs are newer still
                if not rend - rstart < (min_b if rkind else min_w):
                    add_run((i0, n, rstart, rend, rkind))
            i0 = n
            keep(r)
            rstart = s
            rend = e
            rkind = kind
        n = len(kept)
    else:
        kept = rows
        n = -1
        for n, r in enumerate(rows):  # noqa: B007 - n is the last index after the loop
            s = r[4]
            kind = r[1] in browsers
            if kind is rkind and s - rend <= gap:
                e = r[5]
                if e > rend:
                    rend = e
                continue
            if rkind is not None:
                if rend + gap > horizon:
                    return kept, runs
                if not rend - rstart < (min_b if rkind else min_w):
                    add_run((i0, n, rstart, rend, rkind))
            i0 = n
            rstart = s
            rend = r[5]
            rkind = kind
        n += 1
    if rkind is not None and not rend + gap > horizon:
        if not rend - rstart < (min_b if rkind else min_w):
            add_run((i0, n, rstart, rend, rkind))
    return kept, runs


def _float_details(seg):
    """Dominant-process sums and URL when some duration is not an int: builtin sum() over the
    same values in the same order as today's generator sum (compensated since 3.12), and the
    literal stable sort when a NaN duration (inf - inf) makes the order partial."""
    per_proc: dict = {}
    nan = False
    url = None
    best = None
    for r in seg:
        d = r[5] - r[4]
        if d != d:
            nan = True
        lst = per_proc.get(r[1])
        if lst is None:
            per_proc[r[1]] = [d]
        else:
            lst.append(d)
        u = r[3]
        if u and (url is None or d > best):
            url = u
            best = d
    if nan:
        url = next((r[3] for r in sorted(seg, key=lambda r: r[5] - r[4], reverse=True) if r[3]), None)
    return {p: sum(lst) for p, lst in per_proc.items()}, url


def build_app_runs(rows, games, horizon, add_episode) -> int:
    if not rows:
        return 0
    kept, runs = _closed_runs(rows, games, horizon)
    built = 0
    labels: dict = {}  # app_label() memo for this pass
    for i0, i1, start_ms, end_ms, browsing in runs:
        first = kept[i0]
        seg = kept[i0:i1]
        spent: dict = {}
        sums: dict = {}
        url = None
        best = None
        tot = 0
        for _, p, t, u, s, e in seg:
            d = e - s
            tot += d  # stays an int iff every duration is an int
            if t:
                spent[t] = spent.get(t, 0) + (1_000 if d < 1_000 else d)  # == max(d, 1_000)
            sums[p] = sums.get(p, 0) + d  # exact for ints; floats recomputed below
            # first visit (in run order) with the longest duration among truthy URLs: what a
            # stable descending sort followed by "first truthy URL" picks under a total order
            if u and (url is None or d > best):
                url = u
                best = d
        if type(tot) is not int:
            sums, url = _float_details(seg)
        if len(spent) > 1:
            titles = sorted(spent.items(), key=_SECOND, reverse=True)[:MAX_TITLES]
        else:
            titles = list(spent.items())
        if len(sums) == 1:
            process = first[1]
        else:
            # Today's set comprehension inserts every visit's process; repeats are no-ops, so
            # inserting the distinct processes in first-seen order (the dict's key order) one
            # by one builds the same table and max() keeps the same process on ties.
            process = max({p for p in sums}, key=sums.__getitem__)
        if browsing:
            kind = "browsing"
            if titles:
                lead = titles[0][0]
                body = "\n".join(map(_FIRST, titles))
            else:
                lead = labels.get(process) or labels.setdefault(process, app_label(process))
                body = ""
        else:
            kind = "work"
            label = labels.get(process) or labels.setdefault(process, app_label(process))
            if titles:
                lead = titles[0][0]
                body = label + "\n" + "\n".join(map(_FIRST, titles))
            else:
                lead = body = label
        built += add_episode(
            kind=kind,
            ref=f"{kind}:{first[0]}",
            start_ms=start_ms,
            end_ms=end_ms,
            title=lead,
            app=process,
            url=url,
            body=body,
            items=[("app_session", r[0]) for r in seg],
        )
    return built
