"""Brute-force oracles for the episode builders and the exact vector searches.

Each oracle restates the documented semantics literally: quadratic loops, no running state, no
shortcut a faster version would take. They are ported from the 2026-11-10 speed studies
(``docs/solutions/*/report.md`` and their ``oracle.py`` harnesses) so that a later rewrite of
``pipeline/episodes.py``, ``storage/passages.py`` or ``storage/embeddings.py`` must return the
same results as today's code on the same rows.

Only constants and the one-line ``app_label`` formatter come from the application: they are the
contract (gap lengths, voice apps, browser list), not the logic under test.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import numpy as np

from akashic.pipeline.episodes import (
    BROWSER_PROCESSES,
    CLOSE_GAP_MS,
    MAX_TITLES,
    MIC_MARGIN_MS,
    MIN_BROWSING_MS,
    MIN_WORK_MS,
    VOICE_APPS,
    app_label,
)

if TYPE_CHECKING:
    from collections.abc import Collection, Iterable, Mapping, Sequence
    from sqlite3 import Connection

    from numpy.typing import NDArray

    type Vector = NDArray[np.float32]

type Game = tuple[str, int, int]  # process, start_ms, end_ms

# --- conversations -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Seg:
    """One transcribed audio segment, as ``_conversation_clusters`` reads it."""

    id: int
    stream: str
    start_ms: int
    end_ms: int
    sources: tuple[str, ...] = ()
    text: str = "on parle du jeu"

    def row(self) -> tuple[int, str, int, int, int, str, str, str]:
        """(id, stream, start, end, speech_ms, sources JSON, status, transcript)."""
        return (
            self.id,
            self.stream,
            self.start_ms,
            self.end_ms,
            max(0, self.end_ms - self.start_ms),
            json.dumps(list(self.sources)),
            "done",
            self.text,
        )


def insert_segments(conn: Connection, segs: Iterable[Seg]) -> None:
    conn.executemany(
        "INSERT INTO audio_segments (id, stream, start_ms, end_ms, speech_ms, sources, status,"
        " transcript) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [s.row() for s in segs],
    )


def is_voice(seg: Seg, games: Sequence[Game]) -> bool:
    """Sound from a voice app, or from a game whose session overlaps the segment."""
    if any(source in VOICE_APPS for source in seg.sources):
        return True
    return any(
        process in seg.sources and start < seg.end_ms and end > seg.start_ms
        for process, start, end in games
    )


def ordered(segs: Iterable[Seg]) -> list[Seg]:
    return sorted(segs, key=lambda s: (s.start_ms, s.id))


def voice_clusters(segs: Iterable[Seg], games: Sequence[Game]) -> list[list[Seg]]:
    """Voice segments in (start, id) order, chained while the gap from the cluster's latest end
    so far is at most ``CLOSE_GAP_MS``."""
    clusters: list[list[Seg]] = []
    for seg in ordered(segs):
        if not is_voice(seg, games):
            continue
        if clusters and seg.start_ms - max(s.end_ms for s in clusters[-1]) <= CLOSE_GAP_MS:
            clusters[-1].append(seg)
        else:
            clusters.append([seg])
    return clusters


@dataclass(frozen=True, slots=True)
class Conversations:
    final: list[list[int]]  # member ids in (start, id) order, clusters in time order
    growing_start: int | None


def conversations(segs: Sequence[Seg], games: Sequence[Game], horizon: int) -> Conversations:
    """The contract of the Conversation Clustering Sweep study.

    1. Voice clusters as above; their bounds are frozen on voice segments only.
    2. Every non-voice microphone segment checks every cluster window
       ``[start - 30 s, end + 30 s)``; it goes to the window at the smallest distance
       ``max(0, start - m.end, m.start - end)``, ties to the earlier cluster, at most one owner.
    3. A cluster is final when ``max(voice end, attached mic ends) + 300 s <= horizon``;
       ``growing_start`` is the earliest member start of the clusters that are not.
    """
    voice = voice_clusters(segs, games)
    bounds = [(min(s.start_ms for s in c), max(s.end_ms for s in c)) for c in voice]
    attached: list[list[Seg]] = [[] for _ in voice]
    for seg in ordered(segs):
        if seg.stream != "mic" or is_voice(seg, games):
            continue
        best: tuple[int, int] | None = None  # (distance, cluster index)
        for index, (start, end) in enumerate(bounds):
            if seg.start_ms < end + MIC_MARGIN_MS and seg.end_ms > start - MIC_MARGIN_MS:
                distance = max(0, start - seg.end_ms, seg.start_ms - end)
                if best is None or distance < best[0]:
                    best = (distance, index)
        if best is not None:
            attached[best[1]].append(seg)
    final: list[list[int]] = []
    growing: list[int] = []
    for cluster, (_, end), mics in zip(voice, bounds, attached, strict=True):
        members = ordered(cluster + mics)
        if max([end, *(m.end_ms for m in mics)]) + CLOSE_GAP_MS <= horizon:
            final.append([s.id for s in members])
        else:
            growing.append(min(s.start_ms for s in members))
    return Conversations(final, min(growing, default=None))


# --- browsing and work runs ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Visit:
    """One ``app_sessions`` row."""

    id: int
    process: str | None
    title: str | None
    url: str | None
    start_ms: int
    end_ms: int


def insert_visits(conn: Connection, visits: Iterable[Visit]) -> None:
    conn.executemany(
        "INSERT INTO app_sessions (id, process, window_title, url, start_ms, end_ms, frame_count,"
        " last_frame_id, closed) VALUES (?, ?, ?, ?, ?, ?, 1, 0, 1)",
        [(v.id, v.process, v.title, v.url, v.start_ms, v.end_ms) for v in visits],
    )


def insert_games(conn: Connection, games: Iterable[Game]) -> None:
    conn.executemany(
        "INSERT INTO game_sessions (process, window_title, start_ms, end_ms, closed)"
        " VALUES (?, 'A game', ?, ?, 1)",
        list(games),
    )


@dataclass(frozen=True, slots=True)
class AppRun:
    """One expected browsing/work episode.

    ``options`` maps every process that may be called dominant (the longest total time; a tie
    admits each tied process) to the (title, body) that choice implies.
    """

    kind: str
    ref: str
    start_ms: int
    end_ms: int
    url: str | None
    items: tuple[int, ...]
    options: Mapping[str, tuple[str, str]] = field(default_factory=dict[str, tuple[str, str]])


def run_kind(process: str) -> str:
    return "browsing" if process in BROWSER_PROCESSES else "work"


def app_runs(visits: Iterable[Visit], games: Sequence[Game], horizon: int) -> list[AppRun]:
    """Visits in (start, id) order, without a process or overlapping a game, chained while the
    kind (browser or not) stays the same and the gap from the run's latest end so far is at most
    ``CLOSE_GAP_MS``. Runs are emitted oldest first and the pass stops at the first run that
    could still grow (``end + 300 s > horizon``); shorter than 3 min (browsing) or 5 min (work)
    is skipped. Titles rank by time spent (each visit counts at least 1 s), first seen first on
    ties; the URL is the one of the longest visit that has one, first on ties."""
    kept = [
        v
        for v in sorted(visits, key=lambda v: (v.start_ms, v.id))
        if v.process is not None
        and not any(start < v.end_ms and end > v.start_ms for _, start, end in games)
    ]
    runs: list[list[Visit]] = []
    for visit in kept:
        assert visit.process is not None
        if (
            runs
            and run_kind(str(runs[-1][-1].process)) == run_kind(visit.process)
            and visit.start_ms - max(v.end_ms for v in runs[-1]) <= CLOSE_GAP_MS
        ):
            runs[-1].append(visit)
        else:
            runs.append([visit])
    out: list[AppRun] = []
    for run in runs:
        kind = run_kind(str(run[0].process))
        start, end = run[0].start_ms, max(v.end_ms for v in run)
        if end + CLOSE_GAP_MS > horizon:
            break
        if end - start >= (MIN_BROWSING_MS if kind == "browsing" else MIN_WORK_MS):
            out.append(_describe(run, kind, start, end))
    return out


def _describe(run: list[Visit], kind: str, start: int, end: int) -> AppRun:
    spent: dict[str, int] = {}
    totals: dict[str, int] = {}
    url: str | None = None
    best = -math.inf
    for v in run:
        if v.title:
            spent[v.title] = spent.get(v.title, 0) + max(v.end_ms - v.start_ms, 1_000)
        totals[str(v.process)] = totals.get(str(v.process), 0) + v.end_ms - v.start_ms
        if v.url and v.end_ms - v.start_ms > best:
            url, best = v.url, v.end_ms - v.start_ms
    titles = [t for t, _ in sorted(spent.items(), key=lambda kv: -kv[1])][:MAX_TITLES]
    longest = max(totals.values())
    options: dict[str, tuple[str, str]] = {}
    for process in (p for p, total in totals.items() if total == longest):
        body = "\n".join(titles)
        if kind == "work":
            body = f"{app_label(process)}\n{body}" if body else app_label(process)
        options[process] = (titles[0] if titles else app_label(process), body)
    return AppRun(kind, f"{kind}:{run[0].id}", start, end, url, tuple(v.id for v in run), options)


# --- vector search -------------------------------------------------------------------------


class HashEmbedder:
    """A deterministic stand-in for the embedding model: every lower-case word adds one to a
    dimension picked by its BLAKE2 digest, and the sum is scaled to unit length. Identical text
    gives an identical vector and shared words a positive cosine. No model, no network."""

    model = "hash-embed"
    dims = 256

    def __init__(self) -> None:
        self.documents: list[str] = []
        self.queries: list[str] = []

    def vector(self, text: str) -> Vector:
        out = np.zeros(self.dims, dtype=np.float32)
        for word in re.findall(r"\w+", text.lower()):
            digest = hashlib.blake2b(word.encode(), digest_size=4).digest()
            out[int.from_bytes(digest, "big") % self.dims] += 1.0
        norm = float(np.linalg.norm(out))
        return out if norm == 0 else (out / norm).astype(np.float32)

    def available(self) -> bool:
        return True

    def embed_documents(self, texts: Sequence[str]) -> list[Vector]:
        self.documents.extend(texts)
        return [self.vector(t) for t in texts]

    def embed_query(self, text: str) -> Vector:
        self.queries.append(text)
        return self.vector(text)

    def unload_if_idle(self, *, force: bool = False) -> None:
        del force


@dataclass(frozen=True, slots=True)
class Ranked:
    owner_id: int
    score: float
    start_char: int = 0
    end_char: int = 0


def f16_score(blob: bytes, query: Vector) -> float:
    """The stored float16 vector against the query, computed in float64."""
    return float(
        np.frombuffer(blob, dtype=np.float16).astype(np.float64) @ query.astype(np.float64)
    )


_PASSAGE_ROWS = {
    "segment": (
        "SELECT p.segment_id, p.model, p.start_char, p.end_char, p.vec, s.start_ms,"
        " s.transcript IS NOT NULL, NULL FROM segment_passages p"
        " JOIN audio_segments s ON s.id = p.segment_id ORDER BY p.rowid"
    ),
    "frame": (
        "SELECT p.frame_id, p.model, p.start_char, p.end_char, p.vec, f.ts_ms,"
        " f.text IS NOT NULL, f.process FROM frame_passages p"
        " JOIN frames f ON f.id = p.frame_id ORDER BY p.rowid"
    ),
}


def _app_key(name: str) -> str:
    return name.lower().replace(".exe", "")


def passage_ranking(  # noqa: PLR0913 - the same filters as passages.top
    conn: Connection,
    model: str,
    query: Vector,
    *,
    start_ms: int,
    end_ms: int,
    only: Collection[int] | None = None,
    kind: str = "segment",
    apps: tuple[str, ...] = (),
) -> list[Ranked]:
    """Every eligible owner with its best passage, best first; no limit applied.

    Eligible: the model, a source time in ``[start_ms, end_ms)``, source text present, a vector
    of the query's size, in ``only``, and for frames with ``apps`` a window-scoped capture of one
    of those apps. Rows whose score is not finite are dropped (the #100 contract allows it).
    Per owner the highest score wins, the earliest passage on ties; owners rank by score, then
    by id."""
    wanted_apps = {_app_key(a) for a in apps}
    best: dict[int, Ranked] = {}
    for owner, row_model, start_char, end_char, vec, ts, has_text, process in conn.execute(
        _PASSAGE_ROWS[kind]
    ).fetchall():
        blob = bytes(vec)
        if (
            row_model != model
            or not start_ms <= ts < end_ms
            or not has_text
            or len(blob) != 2 * len(query)
            or (only is not None and owner not in only)
            or (wanted_apps and _app_key(str(process)) not in wanted_apps)
        ):
            continue
        score = f16_score(blob, query)
        if not math.isfinite(score):
            continue
        hit = Ranked(int(owner), score, int(start_char), int(end_char))
        held = best.get(hit.owner_id)
        if (
            held is None
            or hit.score > held.score
            or (hit.score == held.score and hit.start_char < held.start_char)
        ):
            best[hit.owner_id] = hit
    return sorted(best.values(), key=lambda h: (-h.score, h.owner_id))


def embedding_ranking(  # noqa: PLR0913 - the same filters as embeddings.top
    conn: Connection,
    owner_kind: str,
    model: str,
    query: Vector,
    *,
    start_ms: int = 0,
    end_ms: int = 2**62,
    only: Collection[int] | None = None,
    exclude: Collection[int] = (),
) -> list[Ranked]:
    """Every eligible owner vector, best first (ties in no particular order); no limit."""
    found = [
        Ranked(int(owner), f16_score(bytes(vec), query))
        for kind, owner, row_model, vec, start in conn.execute(
            "SELECT owner_kind, owner_id, model, vec, start_ms FROM embeddings"
        ).fetchall()
        if kind == owner_kind
        and row_model == model
        and start_ms <= start < end_ms
        and len(bytes(vec)) == 2 * len(query)
        and (only is None or owner in only)
        and owner not in exclude
    ]
    finite = [r for r in found if math.isfinite(r.score)]
    return sorted(finite, key=lambda r: -r.score)


def ranking_problems(
    got: Sequence[tuple[int, float]],
    full: Sequence[Ranked],
    limit: int,
    *,
    tol: float = 1e-6,
    tie_order: bool = False,
) -> list[str]:
    """Differences between a returned ranking and the oracle's full ranking cut at ``limit``.

    Scores must agree within ``tol``. Owners whose scores are within ``tol`` of each other form
    a tie group: with ``tie_order`` the exact order is required, otherwise only the set, and a
    group cut by the limit only needs the returned owners to belong to it."""
    want = list(full[:limit])
    problems: list[str] = []
    if len(got) != len(want):
        problems.append(f"length {len(got)} != {len(want)}")
    for position, ((owner, score), expected) in enumerate(zip(got, want, strict=False)):
        if not math.isclose(score, expected.score, abs_tol=tol, rel_tol=0):
            problems.append(f"#{position} owner {owner} score {score} != {expected.score}")
    if tie_order:
        if [o for o, _ in got] != [r.owner_id for r in want]:
            problems.append(f"order {[o for o, _ in got]} != {[r.owner_id for r in want]}")
        return problems
    position = 0
    while position < len(want):
        group_end = position
        while (
            group_end + 1 < len(full)
            and abs(full[group_end + 1].score - full[position].score) <= tol
        ):
            group_end += 1
        tied = {r.owner_id for r in full[position : group_end + 1]}
        returned = {o for o, _ in got[position : min(group_end + 1, limit)]}
        if group_end < limit and returned != tied:
            problems.append(f"tie group at #{position}: {sorted(returned)} != {sorted(tied)}")
        elif not returned <= tied:
            problems.append(f"cut tie group at #{position}: {sorted(returned)} not in {tied}")
        position = group_end + 1
    return problems
