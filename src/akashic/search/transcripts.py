"""Keyword search over transcripts (FTS5) and browsing them by time.

Same rules as frame search (``keyword.py``): user input never reaches FTS5 syntax, the last word
is a prefix, accents and case are ignored, highlight markers are control characters.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from akashic.jsonish import as_list
from akashic.search.keyword import HIGHLIGHT_END, HIGHLIGHT_START, MAX_LIMIT, build_match

if TYPE_CHECKING:
    from sqlite3 import Connection


@dataclass(frozen=True, slots=True)
class Utterance:
    id: int
    stream: str
    start_ms: int
    end_ms: int
    language: str | None
    sources: tuple[str, ...]
    snippet: str  # text, with HIGHLIGHT_START / HIGHLIGHT_END around matches when searched
    speaker_id: int | None = None
    speaker: str | None = None  # the name, once the user has given one
    confidence: float | None = None  # Whisper's, 0-1; None when unknown


def _sources(raw: object) -> tuple[str, ...]:
    return tuple(str(n) for n in as_list(json.loads(str(raw))) or [])


def _utterance(row: tuple[object, ...]) -> Utterance:
    return Utterance(
        int(str(row[0])),
        str(row[1]),
        int(str(row[2])),
        int(str(row[3])),
        None if row[4] is None else str(row[4]),
        _sources(row[5]),
        str(row[6] or ""),
        None if row[7] is None else int(str(row[7])),
        None if row[8] is None else str(row[8]),
        None if row[9] is None else float(str(row[9])),
    )


def _clamp(limit: int) -> int:
    return max(1, min(limit, MAX_LIMIT))


def search(  # noqa: PLR0913 - a query, a time window and paging
    conn: Connection,
    query: str,
    start_ms: int = 0,
    end_ms: int = 2**62,
    *,
    limit: int = 30,
    offset: int = 0,
) -> list[Utterance]:
    """Transcripts matching ``query``, newest first."""
    match = build_match(query)
    if match is None:
        return []
    rows = conn.execute(
        "SELECT s.id, s.stream, s.start_ms, s.end_ms, s.language, s.sources,"  # noqa: S608
        f" snippet(audio_segments_fts, 0, '{HIGHLIGHT_START}', '{HIGHLIGHT_END}', '…', 24),"
        " s.speaker_id, k.name, s.confidence"
        " FROM audio_segments_fts JOIN audio_segments s ON s.id = audio_segments_fts.rowid"
        " LEFT JOIN speakers k ON k.id = s.speaker_id"
        " WHERE audio_segments_fts MATCH :match AND s.start_ms >= :start AND s.start_ms < :end"
        " ORDER BY s.start_ms DESC, s.id DESC LIMIT :limit OFFSET :offset",
        {
            "match": match,
            "start": start_ms,
            "end": end_ms,
            "limit": _clamp(limit),
            "offset": max(0, offset),
        },
    ).fetchall()
    return [_utterance(r) for r in rows]


def browse(  # noqa: PLR0913
    conn: Connection,
    start_ms: int = 0,
    end_ms: int = 2**62,
    *,
    limit: int = 60,
    offset: int = 0,
    newest_first: bool = True,
) -> list[Utterance]:
    """Transcribed segments in a time window, without a text query."""
    order = "DESC" if newest_first else "ASC"
    rows = conn.execute(
        "SELECT s.id, s.stream, s.start_ms, s.end_ms, s.language, s.sources, s.transcript,"  # noqa: S608
        " s.speaker_id, k.name, s.confidence"
        " FROM audio_segments s LEFT JOIN speakers k ON k.id = s.speaker_id"
        " WHERE s.status = 'done' AND s.start_ms >= :start AND s.start_ms < :end"
        f" ORDER BY s.start_ms {order}, s.id {order} LIMIT :limit OFFSET :offset",
        {"start": start_ms, "end": end_ms, "limit": _clamp(limit), "offset": max(0, offset)},
    ).fetchall()
    return [_utterance(r) for r in rows]
