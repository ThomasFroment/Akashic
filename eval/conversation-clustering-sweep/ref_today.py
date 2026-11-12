"""Today's conversation clustering, copied verbatim from src/akashic/pipeline/episodes.py
(pre-audit code, lines 39-40, 49-56, 81-102, 121-123, 262-312), stdlib only.

Only the SQL around it is replaced: ``clusters`` takes the rows that ``fetchall`` returned
(id, stream, start_ms, end_ms, speech_ms, sources_json, transcript), already ordered by
(start_ms, id), and the games list that ``_games`` would return. Everything after fetchall is
today's code unchanged. Known bugs reproduced on purpose: mic chaining (``cluster.end_ms`` is
re-read while the generator extends the cluster) and a mic segment attaching to two clusters.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Final

CLOSE_GAP_MS: Final = 5 * 60_000  # silence that ends a conversation / a browsing run
MIC_MARGIN_MS: Final = 30_000  # microphone speech this close to a call belongs to it

# Lower-case executable names whose sound means people are talking to each other.
VOICE_APPS: Final = frozenset(
    {
        "discord.exe", "discordptb.exe", "discordcanary.exe", "teams.exe", "ms-teams.exe",
        "zoom.exe", "skype.exe", "telegram.exe", "whatsapp.exe", "teamspeak.exe",
        "ts3client_win64.exe", "mumble.exe", "guilded.exe", "slack.exe", "signal.exe",
    }
)  # fmt: skip


@dataclass(frozen=True, slots=True)
class _Segment:
    id: int
    stream: str
    start_ms: int
    end_ms: int
    speech_ms: int
    sources: tuple[str, ...]
    transcript: str


@dataclass(slots=True)
class _Cluster:
    segments: list[_Segment] = field(default_factory=list[_Segment])

    @property
    def start_ms(self) -> int:
        return self.segments[0].start_ms

    @property
    def end_ms(self) -> int:
        return max(s.end_ms for s in self.segments)


def _segment(row: tuple[int, str, int, int, int, str, str]) -> _Segment:
    sources = tuple(str(x) for x in json.loads(row[5]))
    return _Segment(row[0], row[1], row[2], row[3], row[4], sources, row[6])


def _is_voice(seg: _Segment, games: list[tuple[str, int, int]]) -> bool:
    """Sound from a voice app, or from the game that was running at that moment (in-game chat)."""
    if VOICE_APPS.intersection(seg.sources):
        return True
    return any(
        process in seg.sources and start < seg.end_ms and end > seg.start_ms
        for process, start, end in games
    )


def _conversation_clusters_rows(
    rows: list[tuple[int, str, int, int, int, str, str]],
    games: list[tuple[str, int, int]],
    horizon: int,
) -> tuple[list[_Cluster], int | None]:
    """Body of ``_conversation_clusters`` after ``fetchall`` (games passed in, not queried)."""
    segments = [_segment(r) for r in rows]
    if not segments:
        return [], None
    clusters: list[_Cluster] = []
    for seg in segments:
        if not _is_voice(seg, games):
            continue
        if clusters and seg.start_ms - clusters[-1].end_ms <= CLOSE_GAP_MS:
            clusters[-1].segments.append(seg)
        else:
            clusters.append(_Cluster([seg]))
    for cluster in clusters:  # the microphone side of the same stretch
        taken = {s.id for s in cluster.segments}
        cluster.segments.extend(
            s
            for s in segments
            if s.id not in taken
            and s.stream == "mic"
            and s.start_ms < cluster.end_ms + MIC_MARGIN_MS
            and s.end_ms > cluster.start_ms - MIC_MARGIN_MS
        )
        cluster.segments.sort(key=lambda s: (s.start_ms, s.id))
    # A cluster is final once no later speech could still chain onto it.
    final = [c for c in clusters if c.end_ms + CLOSE_GAP_MS <= horizon]
    growing = [c.start_ms for c in clusters if c.end_ms + CLOSE_GAP_MS > horizon]
    return final, min(growing, default=None)


def clusters(rows, games, horizon):
    """Harness interface: (final member-id lists, growing_start)."""
    final, growing_start = _conversation_clusters_rows(rows, games, horizon)
    return [[s.id for s in c.segments] for c in final], growing_start
