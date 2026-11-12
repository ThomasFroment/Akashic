"""C3: midpoint cell table, closed form.

1. M1 voice sweep (today's 5 min `<=` rule, gap from the running max voice end), freezing
   (vs_k, ve_k) per cluster on voice segments only.
2. Cell boundaries S_k = ve_k + vs_{k+1} (strictly increasing, since vs_{k+1} > ve_k + 300 s).
   A non-voice mic segment m with sigma = m.start + m.end goes to k = bisect_left(S, sigma)
   (sigma == S_k -> k, the earlier cluster): when neither distance is clamped to 0,
   m.start - ve_k < vs_{k+1} - m.end  <=>  sigma < S_k. Then one overlap check against
   [vs_k - 30 s, ve_k + 30 s). No scan loop, no division, integers only.
   Exact for mic segments <= 300 s; a longer segment spanning a whole gap (d = 0 to two
   clusters) can be sent to the later cluster instead of the earlier one.
3. Members via an owner slot per row and one ordered pass (rows arrive sorted by (start, id)).
4. Finality: max(ve, attached mic ends) + 300 s <= horizon; growing_start = min member start.
Stdlib only.
"""

from __future__ import annotations

import json
from bisect import bisect_left
from typing import Final

CLOSE_GAP_MS: Final = 5 * 60_000
MIC_MARGIN_MS: Final = 30_000

VOICE_APPS: Final = frozenset(
    {
        "discord.exe", "discordptb.exe", "discordcanary.exe", "teams.exe", "ms-teams.exe",
        "zoom.exe", "skype.exe", "telegram.exe", "whatsapp.exe", "teamspeak.exe",
        "ts3client_win64.exe", "mumble.exe", "guilded.exe", "slack.exe", "signal.exe",
    }
)  # fmt: skip


def _classify(sources_json, games):
    """True (voice app), False (never voice), or the game intervals that would make it voice."""
    sources = tuple(str(x) for x in json.loads(sources_json))
    if VOICE_APPS.intersection(sources):
        return True
    rel = tuple((start, end) for process, start, end in games if process in sources)
    return rel or False


def clusters(rows, games, horizon):
    if not rows:
        return [], None
    gap = CLOSE_GAP_MS
    memo: dict[str, object] = {}
    n = len(rows)
    owner = [-1] * n
    vs: list[int] = []
    ve: list[int] = []
    mics: list[int] = []
    k = -1
    cur_end = 0
    # 1. voice sweep
    for i, row in enumerate(rows):
        src = row[5]
        info = memo.get(src)
        if info is None:
            info = memo[src] = _classify(src, games)
        start = row[2]
        if info is False:
            if row[1] == "mic":
                mics.append(i)
            continue
        end = row[3]
        if info is not True and not any(a < end and b > start for a, b in info):
            if row[1] == "mic":
                mics.append(i)
            continue
        if k >= 0 and start - cur_end <= gap:
            if end > cur_end:
                cur_end = end
        else:
            if k >= 0:
                ve.append(cur_end)
            k += 1
            vs.append(start)
            cur_end = end
        owner[i] = k
    if k < 0:
        return [], None
    ve.append(cur_end)
    n_clusters = k + 1

    # 2. midpoint cells + one overlap check
    cells = [ve[j] + vs[j + 1] for j in range(n_clusters - 1)]
    end_all = ve[:]
    first = vs[:]
    margin = MIC_MARGIN_MS
    for i in mics:
        row = rows[i]
        start = row[2]
        end = row[3]
        j = bisect_left(cells, start + end)
        if start < ve[j] + margin and end > vs[j] - margin:
            owner[i] = j
            if end > end_all[j]:
                end_all[j] = end
            if start < first[j]:
                first[j] = start

    # 3. members in (start, id) order
    members: list[list[int]] = [[] for _ in range(n_clusters)]
    for row, o in zip(rows, owner):
        if o >= 0:
            members[o].append(row[0])

    # 4. finality
    final = []
    growing = None
    for j in range(n_clusters):
        if end_all[j] + gap <= horizon:
            final.append(members[j])
        elif growing is None or first[j] < growing:
            growing = first[j]
    return final, growing
