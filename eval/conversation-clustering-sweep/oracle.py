"""Literal statement of the CONTRACT, deliberately slow (O(N*K)) and obvious.

1. Voice clustering: today's loop unchanged (5 min gap measured from the max end of the
   cluster's voice segments so far).
2. Bounds frozen on voice only: vs = min voice start, ve = max voice end.
3. Every non-voice mic segment m checks every cluster window [vs - 30 s, ve + 30 s):
   attachable iff m.start < ve + 30 s and m.end > vs - 30 s. Among attachable clusters it goes
   to the one with the smallest d = max(0, vs - m.end, m.start - ve), ties to the lowest index
   (earliest cluster). At most one owner.
4. Finality: max(ve, ends of attached mic) + 300 s <= horizon. growing_start = min start over
   the members (voice + attached mic) of growing clusters.
Members are listed sorted by (start_ms, id); final clusters in voice-cluster order.
"""

from __future__ import annotations

from ref_today import (
    CLOSE_GAP_MS,
    MIC_MARGIN_MS,
    _Cluster,
    _is_voice,
    _segment,
)


def clusters(rows, games, horizon):
    segments = [_segment(r) for r in rows]
    if not segments:
        return [], None
    voice_flags = [_is_voice(s, games) for s in segments]

    # 1. today's voice loop, voice segments only
    voice: list[_Cluster] = []
    for seg, is_voice in zip(segments, voice_flags, strict=True):
        if not is_voice:
            continue
        if voice and seg.start_ms - voice[-1].end_ms <= CLOSE_GAP_MS:
            voice[-1].segments.append(seg)
        else:
            voice.append(_Cluster([seg]))

    # 2. frozen voice-only bounds
    bounds = [
        (min(s.start_ms for s in c.segments), max(s.end_ms for s in c.segments)) for c in voice
    ]

    # 3. each non-voice mic segment: check every cluster, nearest wins, ties to earliest
    attached: list[list] = [[] for _ in voice]
    for seg, is_voice in zip(segments, voice_flags, strict=True):
        if is_voice or seg.stream != "mic":
            continue
        best_d, best_i = None, None
        for i, (vs, ve) in enumerate(bounds):
            if seg.start_ms < ve + MIC_MARGIN_MS and seg.end_ms > vs - MIC_MARGIN_MS:
                d = max(0, vs - seg.end_ms, seg.start_ms - ve)
                if best_d is None or d < best_d:
                    best_d, best_i = d, i
        if best_i is not None:
            attached[best_i].append(seg)

    # 4. finality
    final: list[list[int]] = []
    growing: list[int] = []
    for c, (_vs, ve), mics in zip(voice, bounds, attached, strict=True):
        members = sorted(c.segments + mics, key=lambda s: (s.start_ms, s.id))
        end = max([ve] + [m.end_ms for m in mics])
        if end + CLOSE_GAP_MS <= horizon:
            final.append([s.id for s in members])
        else:
            growing.append(min(s.start_ms for s in members))
    return final, min(growing, default=None)
