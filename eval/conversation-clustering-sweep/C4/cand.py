"""C4: two-pointer sweep with frozen voice bounds, plus a per-pass memo of parsed `sources`.

Interface: clusters(rows, games, horizon) -> (final member-id lists, growing_start).
rows are (id, stream, start_ms, end_ms, speech_ms, sources_json, transcript) ordered by
(start_ms, id), exactly as fetchall returns them. Stdlib only.

1. One pass: classify each row as voice through a dict keyed by the raw sources JSON string
   (parse + VOICE_APPS test once per distinct string; the game-overlap test stays per row),
   and run today's voice clustering with a running max end (5 min gap, `<=`).
2. Frozen bounds (vs_k, ve_k). Since vs_{k+1} > ve_k + 300 s, ve_k is strictly increasing,
   so the clusters whose window [vs - 30 s, ve + 30 s) can hold mic m form a contiguous run
   starting at lo(m.start) = first k with ve_k + 30 s > m.start, which is monotone in m.start
   (rows are start-sorted): one pointer. Only lo and lo+1 can be nearest: lo+1 starts after
   m.start, so it has d = max(0, vs - m.end) and every later cluster is farther still.
3. Members by one ordered pass over an owner array (rows are already in (start, id) order).
4. Finality: max(ve, attached mic ends) + 300 s <= horizon; growing_start = min member start.
"""

from __future__ import annotations

import json
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


def clusters(rows, games, horizon):
    if not rows:
        return [], None
    gap = CLOSE_GAP_MS
    margin = MIC_MARGIN_MS

    # games by process; a row's game term only looks at processes listed in its sources
    by_proc: dict[str, list[tuple[int, int]]] = {}
    for proc, g_start, g_end in games:
        by_proc.setdefault(proc, []).append((g_start, g_end))
    voice_apps = VOICE_APPS
    memo: dict[str, tuple[tuple[str, ...], bool, tuple[str, ...]]] = {}

    n = len(rows)
    owner = [-1] * n
    vs_l: list[int] = []
    ve_l: list[int] = []
    mic_idx: list[int] = []
    k = -1
    cur_end = 0
    for i, row in enumerate(rows):
        raw = row[5]
        m = memo.get(raw)
        if m is None:
            src = tuple(str(x) for x in json.loads(raw))
            m = (
                src,
                bool(voice_apps.intersection(src)),
                tuple(p for p in by_proc if p in src),
            )
            memo[raw] = m
        start = row[2]
        end = row[3]
        if m[1]:
            voice = True
        elif m[2]:
            voice = False
            for proc in m[2]:
                for g_start, g_end in by_proc[proc]:
                    if g_start < end and g_end > start:
                        voice = True
                        break
                if voice:
                    break
        else:
            voice = False
        if voice:
            if k >= 0 and start - cur_end <= gap:
                if end > cur_end:
                    cur_end = end
                    ve_l[k] = end
            else:
                k += 1
                vs_l.append(start)
                ve_l.append(end)
                cur_end = end
            owner[i] = k
        elif row[1] == "mic":
            mic_idx.append(i)

    n_cl = k + 1
    if n_cl == 0:
        return [], None
    end_all = ve_l[:]
    first_start = vs_l[:]

    # mic attachment: monotone pointer over start-sorted non-voice mic rows
    lo = 0
    for i in mic_idx:
        row = rows[i]
        m_start = row[2]
        m_end = row[3]
        while lo < n_cl and ve_l[lo] + margin <= m_start:
            lo += 1
        if lo == n_cl or vs_l[lo] - margin >= m_end:
            continue
        best = lo
        nxt = lo + 1
        if nxt < n_cl and vs_l[nxt] - margin < m_end:
            d0 = vs_l[lo] - m_end
            d1 = m_start - ve_l[lo]
            if d1 > d0:
                d0 = d1
            if d0 > 0:  # lo is not touching m; lo+1 may be nearer (ties stay with lo)
                d_n = vs_l[nxt] - m_end
                if d_n < 0:
                    d_n = 0
                if d_n < d0:
                    best = nxt
        owner[i] = best
        if m_end > end_all[best]:
            end_all[best] = m_end
        if m_start < first_start[best]:
            first_start[best] = m_start

    members: list[list[int]] = [[] for _ in range(n_cl)]
    for i, c in enumerate(owner):
        if c >= 0:
            members[c].append(rows[i][0])

    final = []
    growing = None
    limit = horizon - gap
    for c in range(n_cl):
        if end_all[c] <= limit:
            final.append(members[c])
        elif growing is None or first_start[c] < growing:
            growing = first_start[c]
    return final, growing
