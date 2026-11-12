"""C1 Frozen-bounds two-pointer sweep.

One voice sweep with the running max end as a local freezes each cluster's voice-only bounds
(vs_k, ve_k). Non-voice mic segments are assigned to at most one cluster with a monotone
pointer over the start-sorted mic list: k only moves forward because ve_k strictly increases
(each new cluster starts > 300 s after the previous ve). Members are rebuilt in one ordered pass
over the already (start_ms, id)-sorted segments. ``_segment`` and ``_is_voice`` are today's,
unchanged. Stdlib only.
"""

from __future__ import annotations

from ref_today import CLOSE_GAP_MS, MIC_MARGIN_MS, _is_voice, _segment


def clusters(rows, games, horizon):
    segments = [_segment(r) for r in rows]
    if not segments:
        return [], None

    # owner[i] = cluster index of segment i, or -1.
    n = len(segments)
    owner = [-1] * n
    vs_list: list[int] = []
    ve_list: list[int] = []
    mic_idx: list[int] = []  # non-voice mic segments, already in start order

    # 1. voice sweep: running max end of the current cluster's voice segments (== today's end_ms
    #    because during today's voice loop clusters hold only voice segments).
    k = -1
    ve = 0
    for i, seg in enumerate(segments):
        if _is_voice(seg, games):
            if k >= 0 and seg.start_ms - ve <= CLOSE_GAP_MS:
                if seg.end_ms > ve:
                    ve = seg.end_ms
            else:
                if k >= 0:
                    ve_list.append(ve)
                k += 1
                vs_list.append(seg.start_ms)  # first voice member = min voice start (sorted)
                ve = seg.end_ms
            owner[i] = k
        elif seg.stream == "mic":
            mic_idx.append(i)
    if k < 0:
        return [], None
    ve_list.append(ve)
    n_clusters = k + 1

    # 2. mic attachment, two pointers over frozen bounds.
    end_all = list(ve_list)
    p = 0
    for i in mic_idx:
        m = segments[i]
        ms, me = m.start_ms, m.end_ms
        while p < n_clusters and ve_list[p] + MIC_MARGIN_MS <= ms:
            p += 1
        best_d = -1
        best_j = -1
        j = p
        while j < n_clusters and vs_list[j] - MIC_MARGIN_MS < me:
            d = vs_list[j] - me
            d2 = ms - ve_list[j]
            if d2 > d:
                d = d2
            if d < 0:
                d = 0
            if best_j < 0 or d < best_d:
                best_d, best_j = d, j
            j += 1
        if best_j >= 0:
            owner[i] = best_j
            if me > end_all[best_j]:
                end_all[best_j] = me

    # 3. members in one ordered pass (segments are sorted by (start_ms, id)).
    members: list[list[int]] = [[] for _ in range(n_clusters)]
    start_all: list[int | None] = [None] * n_clusters
    for i, c in enumerate(owner):
        if c >= 0:
            seg = segments[i]
            if start_all[c] is None:
                start_all[c] = seg.start_ms
            members[c].append(seg.id)

    # 4. finality.
    final: list[list[int]] = []
    growing_start = None
    for c in range(n_clusters):
        if end_all[c] + CLOSE_GAP_MS <= horizon:
            final.append(members[c])
        else:
            s = start_all[c]
            if growing_start is None or s < growing_start:
                growing_start = s
    return final, growing_start
