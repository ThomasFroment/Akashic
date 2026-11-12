"""Memory search: filter by the plan first, then rank by words (FTS5) and meaning (vectors).

1. Candidate episodes are the ones inside the time range, narrowed to the planned kinds and
   people. Explicit constraints are retained even when they leave no evidence.
2. Candidates are ranked by reciprocal rank fusion of the full-text ranking (any of the planned
   keywords) and the vector ranking (the question against each episode). Candidates neither
   ranking knows, which still satisfy the filters, follow in time order: "what video did I watch
   yesterday afternoon" has no words to match and the filters are the whole answer.
3. Evidence: transcript lines inside the range (and by the planned people), ranked the same way,
   and screen text ranked by words and semantic passages. Each hit links to its original moment.

Everything returned is a row id plus enough text to show; the UI opens the moment from the id.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Final

from akashic.pipeline.transcribe import UNCLEAR_BELOW
from akashic.search.keyword import HIGHLIGHT_END, HIGHLIGHT_START, Hit, build_match
from akashic.search.timeparse import normalize
from akashic.storage import embeddings, passages

if TYPE_CHECKING:
    from collections.abc import Collection, Iterable
    from sqlite3 import Connection

    import numpy as np
    from numpy.typing import NDArray

    from akashic.search.planner import Plan

    type Vector = NDArray[np.float32]

RRF_K: Final = 60
FTS_DEPTH: Final = 200
VECTOR_DEPTH: Final = 100
LINE_CHARS: Final = 400
CONTEXT_BEFORE: Final = 3
CONTEXT_AFTER: Final = 3
CONTEXT_WINDOW_MS: Final = 45_000
_FAR_FUTURE: Final = 2**62


@dataclass(frozen=True, slots=True)
class EpisodeHit:
    id: int
    kind: str
    start_ms: int
    end_ms: int
    title: str
    app: str | None
    url: str | None
    people: tuple[str, ...]  # names given by the user; unnamed voices are counted in `unnamed`
    unnamed: int
    snippet: str
    score: float


@dataclass(frozen=True, slots=True)
class ContextLine:
    """One utterance around a matched line, both streams, so the answer reads the exchange."""

    segment_id: int
    start_ms: int
    who: str  # "You", the speaker's name, or "someone"
    text: str
    unclear: bool  # Whisper was unsure (confidence below UNCLEAR_BELOW)
    is_match: bool  # the line the search found


@dataclass(frozen=True, slots=True)
class LineHit:
    """One transcribed utterance: where to jump to, and what was said."""

    segment_id: int
    stream: str
    start_ms: int
    end_ms: int
    speaker_id: int | None
    speaker: str | None
    text: str
    episode_id: int | None
    score: float
    confidence: float | None = None
    context: tuple[ContextLine, ...] = ()  # the lines around it (search_memory fills it)


@dataclass(frozen=True, slots=True)
class Results:
    episodes: list[EpisodeHit] = field(default_factory=list[EpisodeHit])
    lines: list[LineHit] = field(default_factory=list[LineHit])
    frames: list[Hit] = field(default_factory=list[Hit])
    semantic: bool = False  # whether meaning (vectors) took part, or only words and filters
    overview_total: int | None = None  # eligible episode count for an untargeted time summary


def match_any(words: Iterable[str]) -> str | None:
    """FTS5 expression matching any of ``words`` (quoted, so no query syntax gets through)."""
    terms = [f'"{w}"' for w in dict.fromkeys(words) if re.fullmatch(r"\w+", w)]
    return " OR ".join(terms) if terms else None


def _matched_excerpts(
    conn: Connection, keywords: Iterable[str], ids: list[int], *, episodes: bool = False
) -> dict[int, str]:
    """Read query-centred text only for final hits, using the index's own token matching."""
    match = match_any(keywords)
    if not ids or match is None:
        return {}
    # Only these literal statements select a table/column; every value is bound.
    sql = (
        "SELECT rowid, highlight(episodes_fts, 1, :open, :close) FROM episodes_fts"
        " WHERE episodes_fts MATCH :match AND rowid IN (SELECT value FROM json_each(:ids))"
        if episodes
        else "SELECT rowid, highlight(audio_segments_fts, 0, :open, :close)"
        " FROM audio_segments_fts WHERE audio_segments_fts MATCH :match"
        " AND rowid IN (SELECT value FROM json_each(:ids))"
    )
    rows = conn.execute(
        sql,
        {"match": match, "ids": json.dumps(ids), "open": HIGHLIGHT_START, "close": HIGHLIGHT_END},
    ).fetchall()
    return {int(r[0]): _excerpt(str(r[1] or "")) for r in rows}


def _excerpt(marked: str) -> str:
    """Keep a bounded contiguous window near the first keyword; ellipses signal cuts."""
    marked = " ".join(marked.split())
    at = marked.find(HIGHLIGHT_START)
    text = marked.replace(HIGHLIGHT_START, "").replace(HIGHLIGHT_END, "")
    if at < 0:
        return text[:LINE_CHARS]
    if len(text) <= LINE_CHARS:
        return text
    # Reserve space for both ellipses; leave more room after the keyword than before it.
    width = LINE_CHARS - 2
    start = max(0, min(at - width // 3, len(text) - width))
    end = min(len(text), start + width)
    return ("…" if start else "") + text[start:end] + ("…" if end < len(text) else "")


def fuse(*rankings: Collection[int]) -> dict[int, float]:
    """Reciprocal rank fusion: an id scores 1/(k + rank) in every ranking that holds it."""
    scores: dict[int, float] = {}
    for ranking in rankings:
        for rank, item in enumerate(ranking, start=1):
            scores[item] = scores.get(item, 0.0) + 1.0 / (RRF_K + rank)
    return scores


def _semantic_coverage(order: list[int], meaning: list[int], *, latest: bool) -> list[int]:
    """Keep the fused winner and three semantic leaders for explicit revision questions.

    Repeated lexical mentions otherwise crowd bilingual revisions out of the five episode
    sources. This changes evidence coverage, not truth: timestamps and text must establish
    whether a later statement actually revises the same arrangement. No recency-only candidates
    are introduced, and ordinary/historical queries keep their existing order.
    """
    if not latest or not order:
        return order
    return list(dict.fromkeys([order[0], *meaning[:3], *order]))


def speaker_ids(conn: Connection, names: Iterable[str]) -> set[int]:
    """Ids of the speakers whose name matches one of ``names`` (accents and case ignored)."""
    wanted = {normalize(n).strip() for n in names}
    rows = conn.execute("SELECT id, name FROM speakers WHERE name IS NOT NULL").fetchall()
    return {int(i) for i, n in rows if normalize(str(n)).strip() in wanted}


def known_names(conn: Connection) -> list[str]:
    return [str(r[0]) for r in conn.execute("SELECT name FROM speakers WHERE name IS NOT NULL")]


# --- the question, narrowed ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Scope:
    """A plan with its time range and speakers resolved, plus the question's vector."""

    plan: Plan
    qvec: Vector | None
    model: str
    start_ms: int
    end_ms: int
    people: frozenset[int]


# --- episodes ------------------------------------------------------------------------------


def _candidates(conn: Connection, scope: _Scope) -> list[int]:
    def fetch(kinds: tuple[str, ...], *, with_people: bool) -> list[int]:
        sql = "SELECT e.id FROM episodes e WHERE e.start_ms < :end AND e.end_ms > :start"
        params: dict[str, object] = {"start": scope.start_ms, "end": scope.end_ms}
        if kinds:
            marks = ",".join(f":k{i}" for i in range(len(kinds)))
            sql += f" AND e.kind IN ({marks})"
            params.update({f"k{i}": k for i, k in enumerate(kinds)})
        if scope.plan.apps:
            sql += (
                " AND lower(replace(e.app, '.exe', '')) IN"
                " (SELECT lower(replace(value, '.exe', '')) FROM json_each(:apps))"
            )
            params["apps"] = json.dumps(scope.plan.apps)
        if with_people:
            marks = ",".join(f":p{i}" for i in range(len(scope.people)))
            sql += (
                " AND EXISTS (SELECT 1 FROM episode_people p WHERE p.episode_id = e.id"  # noqa: S608
                f"            AND p.speaker_id IN ({marks}))"
            )
            params.update({f"p{i}": p for i, p in enumerate(sorted(scope.people))})
        sql += " ORDER BY e.start_ms DESC, e.id DESC"
        return [int(r[0]) for r in conn.execute(sql, params).fetchall()]

    return fetch(scope.plan.kinds, with_people=bool(scope.people))


def _fts_episode_ranking(conn: Connection, scope: _Scope, candidates: list[int]) -> list[int]:
    match = match_any(scope.plan.keywords)
    if match is None:
        return []
    params: dict[str, object] = {
        "match": match,
        "start": scope.start_ms,
        "end": scope.end_ms,
        "depth": FTS_DEPTH,
        "candidates": json.dumps(candidates),
    }
    rows = conn.execute(
        "SELECT e.id FROM episodes_fts JOIN episodes e ON e.id = episodes_fts.rowid"
        " WHERE episodes_fts MATCH :match AND e.start_ms < :end AND e.end_ms > :start"
        " AND e.id IN (SELECT value FROM json_each(:candidates))"
        " ORDER BY bm25(episodes_fts, 3.0, 1.0) LIMIT :depth",
        params,
    ).fetchall()
    return [int(r[0]) for r in rows]


def _episode_rows(conn: Connection, ids: list[int]) -> dict[int, tuple[object, ...]]:
    marks = ",".join("?" * len(ids))
    rows = conn.execute(
        "SELECT id, kind, start_ms, end_ms, title, app, url, body"  # noqa: S608
        f" FROM episodes WHERE id IN ({marks})",
        ids,
    ).fetchall()
    return {int(r[0]): r for r in rows}


def _people_of(conn: Connection, ids: list[int]) -> dict[int, tuple[list[str], int]]:
    marks = ",".join("?" * len(ids))
    rows = conn.execute(
        "SELECT p.episode_id, s.name, s.is_self FROM episode_people p"  # noqa: S608
        f" JOIN speakers s ON s.id = p.speaker_id WHERE p.episode_id IN ({marks})"
        " ORDER BY p.speech_ms DESC",
        ids,
    ).fetchall()
    found: dict[int, tuple[list[str], int]] = {i: ([], 0) for i in ids}
    for episode_id, name, is_self in rows:
        names, unnamed = found[int(episode_id)]
        if is_self:
            names.append("You")
        elif name:
            names.append(str(name))
        else:
            found[int(episode_id)] = (names, unnamed + 1)
    return found


def _ranked_episodes(conn: Connection, scope: _Scope) -> list[tuple[int, float]]:
    candidates = _candidates(conn, scope)
    if not candidates:
        return []
    if scope.plan.overview and scope.plan.time and not scope.plan.keywords:
        return [(i, 0.0) for i in reversed(candidates)]
    allowed = set(candidates)
    by_words = _fts_episode_ranking(conn, scope, candidates)
    by_meaning: list[int] = []
    if scope.qvec is not None:
        scored = embeddings.top(
            conn, "episode", scope.model, scope.qvec, only=allowed, limit=VECTOR_DEPTH
        )
        by_meaning = [s.owner_id for s in scored]
    fused = fuse(by_words, by_meaning)
    recency = {i: n for n, i in enumerate(candidates)}  # candidates are newest first
    # Ranked first by score (ties: newest first); the rest follow oldest first, like a timeline.
    order = sorted(
        candidates,
        key=lambda i: (-fused.get(i, 0.0), recency[i] if i in fused else -recency[i]),
    )
    order = _semantic_coverage(order, by_meaning, latest=scope.plan.latest)
    return [(i, fused.get(i, 0.0)) for i in order]


# --- transcript lines -----------------------------------------------------------------------


def _line_rows(conn: Connection, ids: list[int]) -> dict[int, tuple[object, ...]]:
    marks = ",".join("?" * len(ids))
    rows = conn.execute(
        "SELECT s.id, s.stream, s.start_ms, s.end_ms, s.speaker_id, k.name, k.is_self,"  # noqa: S608
        " s.transcript, (SELECT episode_id FROM episode_items i WHERE i.item_kind = 'segment'"
        "                AND i.item_id = s.id LIMIT 1), s.confidence"
        " FROM audio_segments s LEFT JOIN speakers k ON k.id = s.speaker_id"
        f" WHERE s.id IN ({marks})",
        ids,
    ).fetchall()
    return {int(r[0]): r for r in rows}


def _ranked_lines(
    conn: Connection, scope: _Scope
) -> tuple[list[tuple[int, float]], dict[int, passages.Match]]:
    match = match_any(scope.plan.keywords)
    by_words: list[int] = []
    params: dict[str, object] = {
        "start": scope.start_ms,
        "end": scope.end_ms,
        "depth": FTS_DEPTH,
        "match": match,
    }
    person_filter = ""
    allowed: set[int] | None = None
    if scope.people:
        marks = ",".join(f":p{i}" for i in range(len(scope.people)))
        # Participation finds recordings; it does not assign their words to that participant.
        person_filter = (
            f" AND (s.speaker_id IN ({marks}) OR EXISTS (SELECT 1 FROM speaker_turns t"  # noqa: S608
            f" WHERE t.segment_id = s.id AND t.speaker_id IN ({marks})))"
        )
        params.update({f"p{i}": p for i, p in enumerate(sorted(scope.people))})
    if scope.plan.apps:
        person_filter += (
            " AND EXISTS (SELECT 1 FROM json_each(s.sources) source"
            " JOIN json_each(:apps) app ON lower(replace(source.value, '.exe', ''))"
            " = lower(replace(app.value, '.exe', '')))"
        )
        params["apps"] = json.dumps(scope.plan.apps)
    if scope.plan.kinds:
        person_filter += (
            " AND (EXISTS (SELECT 1 FROM episode_items item JOIN episodes e"
            " ON e.id = item.episode_id WHERE item.item_kind IN ('segment', 'video_segment')"
            " AND item.item_id = s.id AND e.kind IN (SELECT value FROM json_each(:kinds)))"
            " OR (:conversation = 1 AND NOT EXISTS (SELECT 1 FROM episode_items item"
            " WHERE item.item_kind IN ('segment', 'video_segment') AND item.item_id = s.id)))"
        )
        params["kinds"] = json.dumps(scope.plan.kinds)
        # Fresh speech remains searchable before episode construction. Once associated,
        # its episode kind is authoritative; unrelated videos cannot answer a call query.
        params["conversation"] = int("conversation" in scope.plan.kinds)
    if person_filter and scope.qvec is not None:
        allowed = {
            int(r[0])
            for r in conn.execute(
                "SELECT s.id FROM audio_segments s"  # noqa: S608 - generated placeholders only
                " WHERE s.start_ms >= :start AND s.start_ms < :end" + person_filter,
                params,
            )
        }
    if match is not None:
        rows = conn.execute(
            "SELECT s.id FROM audio_segments_fts JOIN audio_segments s"  # noqa: S608
            "  ON s.id = audio_segments_fts.rowid"
            " WHERE audio_segments_fts MATCH :match AND s.start_ms >= :start AND s.start_ms < :end"
            + person_filter
            + " ORDER BY bm25(audio_segments_fts) LIMIT :depth",
            params,
        ).fetchall()
        by_words = [int(r[0]) for r in rows]
    by_meaning: list[int] = []
    matched_passages: dict[int, passages.Match] = {}
    if scope.qvec is not None:
        passage_hits = passages.top(
            conn,
            scope.model,
            scope.qvec,
            start_ms=scope.start_ms,
            end_ms=scope.end_ms,
            only=allowed,
            limit=VECTOR_DEPTH,
        )
        matched_passages = {hit.owner_id: hit for hit in passage_hits}
        covered = {
            int(r[0])
            for r in conn.execute(
                "SELECT DISTINCT segment_id FROM segment_passages WHERE model = ? AND dim = ?",
                (scope.model, len(scope.qvec)),
            )
        }
        scored = embeddings.top(
            conn,
            "segment",
            scope.model,
            scope.qvec,
            start_ms=scope.start_ms,
            end_ms=scope.end_ms,
            only=allowed,
            exclude=covered,
            limit=VECTOR_DEPTH,
        )
        combined = [(s.owner_id, s.score) for s in scored]
        combined.extend((s.owner_id, s.score) for s in passage_hits)
        by_meaning = [
            i for i, _ in sorted(combined, key=lambda item: (-item[1], item[0]))[:VECTOR_DEPTH]
        ]
    fused = fuse(by_words, by_meaning)
    order = [i for i, _ in sorted(fused.items(), key=lambda kv: kv[1], reverse=True)]
    order = _semantic_coverage(order, by_meaning, latest=scope.plan.latest)
    return [(i, fused[i]) for i in order], matched_passages


def _episode_hit(r: tuple[object, ...], who: tuple[list[str], int], score: float) -> EpisodeHit:
    names, unnamed = who
    return EpisodeHit(
        int(str(r[0])), str(r[1]), int(str(r[2])), int(str(r[3])), str(r[4]),
        None if r[5] is None else str(r[5]), None if r[6] is None else str(r[6]),
        tuple(names), unnamed, str(r[7])[:LINE_CHARS], score,
    )  # fmt: skip


def _line_hit(r: tuple[object, ...], score: float) -> LineHit:
    speaker = "You" if r[6] else (None if r[5] is None else str(r[5]))
    return LineHit(
        int(str(r[0])), str(r[1]), int(str(r[2])), int(str(r[3])),
        None if r[4] is None else int(str(r[4])), speaker,
        str(r[7] or "")[:LINE_CHARS], None if r[8] is None else int(str(r[8])), score,
        None if r[9] is None else float(str(r[9])),
    )  # fmt: skip


def is_unclear(confidence: float | None) -> bool:
    return confidence is not None and confidence < UNCLEAR_BELOW


def line_context(
    conn: Connection, line: LineHit, *, before: int = CONTEXT_BEFORE, after: int = CONTEXT_AFTER
) -> tuple[ContextLine, ...]:
    """What was said around ``line`` on both streams: up to ``before`` utterances before it and
    ``after`` after it, within ``CONTEXT_WINDOW_MS``, in time order, the line itself included.
    A transcript can be wrong; the exchange around it is often what makes it readable."""
    rows = conn.execute(
        "SELECT s.id, s.stream, s.start_ms, s.transcript, s.confidence, k.name, k.is_self"
        " FROM audio_segments s LEFT JOIN speakers k ON k.id = s.speaker_id"
        " WHERE s.status = 'done' AND s.transcript IS NOT NULL AND s.transcript != ''"
        " AND s.start_ms >= ? AND s.start_ms <= ? ORDER BY s.start_ms, s.id",
        (line.start_ms - CONTEXT_WINDOW_MS, line.end_ms + CONTEXT_WINDOW_MS),
    ).fetchall()
    found = [
        ContextLine(
            int(r[0]),
            int(r[2]),
            "You" if r[1] == "mic" or r[6] else (str(r[5]) if r[5] else "someone"),
            line.text if int(r[0]) == line.segment_id else " ".join(str(r[3]).split())[:LINE_CHARS],
            is_unclear(None if r[4] is None else float(r[4])),
            int(r[0]) == line.segment_id,
        )
        for r in rows
    ]
    at = next((i for i, c in enumerate(found) if c.is_match), None)
    if at is None:  # the line itself is not a finished transcript: nothing around it
        return ()
    return tuple(found[max(at - before, 0) : at + after + 1])


def episode_hits(conn: Connection, ids: list[int]) -> list[EpisodeHit]:
    """The episodes with these ids, in the same order (unknown ids are skipped)."""
    if not ids:
        return []
    rows, who = _episode_rows(conn, ids), _people_of(conn, ids)
    return [_episode_hit(rows[i], who[i], 0.0) for i in ids if i in rows]


def episode_lines(conn: Connection, episode_id: int, limit: int) -> list[LineHit]:
    """What was said in an episode (its calls' utterances, or the video's soundtrack), in time
    order."""
    ids = [
        int(r[0])
        for r in conn.execute(
            "SELECT item_id FROM episode_items WHERE episode_id = ?"
            " AND item_kind IN ('segment', 'video_segment')",
            (episode_id,),
        )
    ]
    if not ids:
        return []
    rows = _line_rows(conn, ids)
    ordered = sorted(rows.values(), key=lambda r: (int(str(r[2])), int(str(r[0]))))
    return [_line_hit(r, 0.0) for r in ordered[:limit] if r[7]]


def _ranked_frames(conn: Connection, scope: _Scope, limit: int) -> list[Hit]:
    # Screen evidence cannot establish a person's authorship or an episode kind.
    if scope.plan.people or scope.plan.kinds:
        return []
    params: dict[str, object] = {
        "start": scope.start_ms,
        "end": scope.end_ms,
        "depth": FTS_DEPTH,
        "apps": json.dumps(scope.plan.apps),
    }
    filters = " AND f.ts_ms >= :start AND f.ts_ms < :end"
    if scope.plan.apps:
        filters += (
            " AND replace(lower(f.process), '.exe', '')"
            " IN (SELECT replace(lower(value), '.exe', '') FROM json_each(:apps))"
        )
    words: dict[int, str] = {}
    match = build_match(" ".join(scope.plan.keywords))
    if match:
        params["match"] = match
        rows = conn.execute(
            "SELECT f.id, snippet(frames_fts, -1, :open, :close, '…', 24)"  # noqa: S608
            " FROM frames_fts JOIN frames f ON f.id = frames_fts.rowid"
            " WHERE frames_fts MATCH :match"
            + filters
            + " ORDER BY bm25(frames_fts, 1.0, 3.0, 2.0), f.ts_ms DESC, f.id DESC LIMIT :depth",
            {**params, "open": HIGHLIGHT_START, "close": HIGHLIGHT_END},
        ).fetchall()
        words = {int(r[0]): str(r[1] or "") for r in rows}
    meaning: dict[int, passages.Match] = {}
    if scope.qvec is not None:
        meaning = {
            hit.owner_id: hit
            for hit in passages.top(
                conn,
                scope.model,
                scope.qvec,
                start_ms=scope.start_ms,
                end_ms=scope.end_ms,
                kind="frame",
                apps=scope.plan.apps,
                limit=VECTOR_DEPTH,
            )
        }
    fused = fuse(list(words), list(meaning))
    order = sorted(fused, key=lambda i: -fused[i])
    ids = _semantic_coverage(order, list(meaning), latest=scope.plan.latest)[:limit]
    if not ids:
        return []
    rows = conn.execute(
        "SELECT id, ts_ms, monitor, process, window_title, url, image_path IS NOT NULL,"
        " coalesce(text, '') FROM frames"
        " WHERE id IN (SELECT value FROM json_each(?))",
        (json.dumps(ids),),
    ).fetchall()
    found: dict[int, Hit] = {}
    for row in rows:
        frame_id = int(row[0])
        passage = meaning.get(frame_id)
        snippet = (
            str(row[7])[passage.start_char : passage.end_char]
            if passage is not None
            else words[frame_id]
        )
        found[frame_id] = Hit(
            frame_id,
            row[1],
            row[2],
            row[3],
            row[4],
            row[5],
            bool(row[6]),
            snippet,
        )
    return [found[i] for i in ids if i in found]


def search_memory(
    conn: Connection, plan: Plan, qvec: Vector | None, model: str, *, limit: int = 10
) -> Results:
    start_ms, end_ms = (plan.time.start_ms, plan.time.end_ms) if plan.time else (0, _FAR_FUTURE)
    people = frozenset(speaker_ids(conn, plan.people)) if plan.people else frozenset[int]()
    scope = _Scope(plan, qvec, model, start_ms, end_ms, people)
    if plan.people and not people:
        return Results(semantic=qvec is not None)

    episodes: list[EpisodeHit] = []
    all_ranked = _ranked_episodes(conn, scope)
    overview_total = len(all_ranked) if plan.overview and plan.time and not plan.keywords else None
    if overview_total is not None and len(all_ranked) > limit > 1:
        # Sample positions across chronological eligible episodes, including both ends.
        # Counts passed to the answer prompt keep a sampled day from looking exhaustive.
        ranked = [all_ranked[i * (len(all_ranked) - 1) // (limit - 1)] for i in range(limit)]
    else:
        ranked = all_ranked[:limit]
    if ranked:
        ids = [i for i, _ in ranked]
        rows, who = _episode_rows(conn, ids), _people_of(conn, ids)
        episodes = [_episode_hit(rows[i], who[i], score) for i, score in ranked]
        excerpts = _matched_excerpts(conn, plan.keywords, ids, episodes=True)
        episodes = [replace(e, snippet=excerpts.get(e.id, e.snippet)) for e in episodes]

    lines: list[LineHit] = []
    all_lines, passage_matches = _ranked_lines(conn, scope)
    ranked_lines = all_lines[:limit]
    if ranked_lines:
        line_rows = _line_rows(conn, [i for i, _ in ranked_lines])
        lines = [_line_hit(line_rows[i], score) for i, score in ranked_lines if i in line_rows]
        excerpts = _matched_excerpts(conn, plan.keywords, [line.segment_id for line in lines])
        for line in lines:
            hit = passage_matches.get(line.segment_id)
            if hit is not None:
                text = str(line_rows[line.segment_id][7] or "")
                # Offsets are text offsets, not word-aligned audio times. Link to the recording.
                excerpts[line.segment_id] = text[hit.start_char : hit.end_char]
        lines = [replace(line, text=excerpts.get(line.segment_id, line.text)) for line in lines]
        # A neighbouring hit may later be deduplicated by build_sources. Show its matched
        # excerpt here too, so that deduplication cannot replace it with an unrelated prefix.
        lines = [
            replace(
                line,
                context=tuple(
                    replace(c, text=excerpts.get(c.segment_id, c.text))
                    for c in line_context(conn, line)
                ),
            )
            for line in lines
        ]

    frames = _ranked_frames(conn, scope, min(limit, 5))
    return Results(
        episodes, lines, frames, semantic=qvec is not None, overview_total=overview_total
    )
