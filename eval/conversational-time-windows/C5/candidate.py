"""C5 session-closure line window (retrieval only; the resolver is today's).

Lines a time window W = [start, end) lets through, as the union of:

(A) Line overlap: ``s.start < W.end AND s.end > W.start``, with the index-friendly lower bound
    ``s.start >= W.start - MAX_SEG_MS`` (a segment is at most 30,016 ms long, so this loses
    nothing and keeps the ``audio_segments_start`` range scan).

(B) Session closure: whole voice sessions that cross W, clipped to [W.start - CAP, W.end + CAP).
    A voice session is a run of *voice* segments (sources include a voice app, or the segment
    is linked to a ``conversation`` episode, which covers in-game chat once episodes exist),
    keyed by that voice app (or game), chained while the gap to the next segment with the same
    key is <= G. Music-only / video-only / other system audio never extends a run: it is not a
    voice segment, so a continuous Spotify stream cannot keep a session open and its lyrics are
    never added by (B). The microphone stream inside a run (+- MIC_MARGIN) belongs to it, as in
    the episode builder. A run is taken when its hull overlaps W and, if the question names
    people, when one of its segments passes the person filter. Runs are built from segments
    directly, so the builder's 5-minute split (two episodes for one call with a 6-min quiet
    stretch) and still-open calls (no episode yet) do not matter.

Both parts then pass today's person / kind clauses unchanged. The extension only runs when the
plan can return conversation lines (no kinds, or ``conversation`` among them).

Episodes keep today's overlap rule, plus the conversation episodes that overlap a taken run's
hull (the second half of a call split by the builder).

Constants, chosen from first principles (not tuned to the 14-question fixture):
- G = 15 min. Longer than the silences inside one sitting (AFK, a loading screen, a queue, a
  match where nobody talks: 5-10 min is common, which is why the builder's 5 min splits calls),
  shorter than a break that ends a sitting (dinner, a different group later in the evening,
  typically >= 30 min). Errs on joining: a merged neighbour costs a few outside lines that the
  person/kind filters and ranking still sort; a cut call loses answers.
- CAP = 3 h per side. A long evening call that started before the window or ends after it is
  kept whole up to 3 h beyond the window; the cap bounds the damage of a voice stream that
  never pauses (a Discord stage, a stream with voice left on) and the scan size
  (W + 6 h of segments at most).

Not modelled here (production notes): the vector paths (embeddings.py / passages.py) should
query the hull returned by ``line_hull`` and keep only ids in the selected set;
``transcripts.py`` browse should use rule (A) only.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("c5_baseline_today", HERE.parent / "baseline_today.py")
assert _spec is not None and _spec.loader is not None
_base = importlib.util.module_from_spec(_spec)
sys.modules["c5_baseline_today"] = _base
_spec.loader.exec_module(_base)

SRC = HERE.parents[2] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
from akashic.pipeline.episodes import VOICE_APPS  # noqa: E402

MAX_SEG_MS = 30_016  # longest segment the capture can write (max_segment_ms + one VAD window)
G_MS = 15 * 60_000
CAP_MS = 3 * 3_600_000
MIC_MARGIN_MS = 30_000  # the builder's: mic speech this close to a call belongs to it


def resolve_question(question, now, model_time_text):  # noqa: ANN001, ANN201
    """Today's resolver, unchanged: only retrieval differs in C5."""
    return _base.resolve_question(question, now, model_time_text)


# --- shared filters ------------------------------------------------------------------------
def _person_clause(people, params: dict) -> str:  # noqa: ANN001
    if not people:
        return ""
    marks = ",".join(f":p{i}" for i in range(len(people)))
    params.update({f"p{i}": p for i, p in enumerate(sorted(people))})
    return (
        f" AND (s.speaker_id IN ({marks}) OR EXISTS (SELECT 1 FROM speaker_turns t"
        f" WHERE t.segment_id = s.id AND t.speaker_id IN ({marks})))"
    )


def _kind_clause(plan_kinds, params: dict) -> str:  # noqa: ANN001
    if not plan_kinds:
        return ""
    params["kinds"] = json.dumps(list(plan_kinds))
    params["conversation"] = int("conversation" in plan_kinds)
    return (
        " AND (EXISTS (SELECT 1 FROM episode_items item JOIN episodes e"
        " ON e.id = item.episode_id WHERE item.item_kind IN ('segment', 'video_segment')"
        " AND item.item_id = s.id AND e.kind IN (SELECT value FROM json_each(:kinds)))"
        " OR (:conversation = 1 AND NOT EXISTS (SELECT 1 FROM episode_items item"
        " WHERE item.item_kind IN ('segment', 'video_segment') AND item.item_id = s.id)))"
    )


# --- (B) session runs ------------------------------------------------------------------------
_cache: dict[tuple, tuple[list[int], list[tuple[int, int]]]] = {}


def _sessions(conn, start_ms: int, end_ms: int, plan_kinds, people):  # noqa: ANN001, ANN202
    """(segment ids of the taken runs incl. their mic side, hulls of the taken runs)."""
    if plan_kinds and "conversation" not in plan_kinds:
        return [], []
    key = (id(conn), start_ms, end_ms, tuple(plan_kinds or ()), tuple(sorted(people or ())))
    if key in _cache:
        return _cache[key]
    lo, hi = start_ms - CAP_MS, end_ms + CAP_MS
    # One statement: the conversation episodes' segments in range are materialized once (CTE,
    # episodes_kind_time + the episode_items primary key); people are checked on the segment's
    # own speaker_id (a run needs one hit anywhere; the final line query applies the full
    # person clause, turns included).
    rows = conn.execute(
        "WITH conv AS (SELECT i.item_id FROM episodes e JOIN episode_items i"
        " ON i.episode_id = e.id AND i.item_kind = 'segment'"
        " WHERE e.kind = 'conversation' AND e.start_ms < :hi AND e.end_ms > :lo)"
        " SELECT s.id, s.stream, s.start_ms, s.end_ms, s.sources, s.id IN conv, s.speaker_id"
        " FROM audio_segments s WHERE s.start_ms >= :lo AND s.start_ms < :hi"
        " ORDER BY s.start_ms, s.id",
        {"lo": lo - MAX_SEG_MS, "hi": hi},
    ).fetchall()
    wanted = set(people or ())
    # Group voice segments into runs per key (voice app, or the conversation episode's sources
    # for in-game chat), chained while the gap is <= G.
    runs: dict[str, list[list]] = {}  # key -> list of [start, end, ids, person_hit]
    mic: list[tuple[int, int, int]] = []
    for sid, stream, s, e, sources, conv, speaker in rows:
        ok = not wanted or speaker in wanted
        if stream == "mic":
            mic.append((sid, s, e))
            continue
        apps = set(json.loads(sources or "[]"))
        keys = apps & VOICE_APPS
        if not keys and conv:
            keys = {f"episode-sources:{sorted(apps)}"}
        for k in keys:
            lst = runs.setdefault(k, [])
            if lst and s - lst[-1][1] <= G_MS:
                run = lst[-1]
                run[1] = max(run[1], e)
                run[2].append(sid)
                run[3] = run[3] or bool(ok)
            else:
                lst.append([s, e, [sid], bool(ok)])
    taken_ids: set[int] = set()
    hulls: list[tuple[int, int]] = []
    for lst in runs.values():
        for r_start, r_end, ids, hit in lst:
            if r_start < end_ms and r_end > start_ms and (hit or not people):
                hulls.append((max(r_start, lo), min(r_end, hi)))
                taken_ids.update(ids)
    for h0, h1 in hulls:
        taken_ids.update(
            sid for sid, s, e in mic if s < h1 + MIC_MARGIN_MS and e > h0 - MIC_MARGIN_MS
        )
    # The clip: a run longer than the cap keeps only its part within [lo, hi) (rows outside
    # were never read).
    out = (sorted(taken_ids), hulls)
    _cache[key] = out
    return out


def _bounds(start_ms: int, end_ms: int, hulls) -> tuple[int, int]:  # noqa: ANN001
    """[lo, hi) on s.start_ms that contains every selected line (rule A and the runs with their
    microphone margin)."""
    lo = min([start_ms] + [h[0] - MIC_MARGIN_MS for h in hulls]) - MAX_SEG_MS
    hi = max([end_ms] + [h[1] + MIC_MARGIN_MS for h in hulls])
    return lo, hi


def line_hull(conn, start_ms: int, end_ms: int, plan_kinds, people=()):  # noqa: ANN001, ANN201
    """Time range the vector paths must scan (then keep only the selected ids)."""
    _, hulls = _sessions(conn, start_ms, end_ms, plan_kinds, people)
    return _bounds(start_ms, end_ms, hulls)


def select_lines(conn, start_ms: int, end_ms: int, plan_kinds, people=()) -> list[int]:  # noqa: ANN001
    ext, hulls = _sessions(conn, start_ms, end_ms, plan_kinds, people)
    params: dict = {"start": start_ms, "end": end_ms, "seglo": start_ms - MAX_SEG_MS}
    rule_a = "(s.start_ms >= :seglo AND s.start_ms < :end AND s.end_ms > :start)"
    where = rule_a
    if ext:
        params["lo"], params["hi"] = _bounds(start_ms, end_ms, hulls)
        params["ext"] = json.dumps(ext)
        where = (
            "s.start_ms >= :lo AND s.start_ms < :hi AND (" + rule_a
            + " OR s.id IN (SELECT value FROM json_each(:ext)))"
        )  # fmt: skip
    sql = "SELECT s.id FROM audio_segments s WHERE " + where
    sql += _person_clause(people, params) + _kind_clause(plan_kinds, params)
    return [int(r[0]) for r in conn.execute(sql, params)]


def select_episodes(conn, start_ms: int, end_ms: int, plan_kinds, people=()) -> list[int]:  # noqa: ANN001
    _, hulls = _sessions(conn, start_ms, end_ms, plan_kinds, people)
    params: dict = {"start": start_ms, "end": end_ms}
    time_sql = "(e.start_ms < :end AND e.end_ms > :start)"
    for i, (h0, h1) in enumerate(hulls):
        params[f"h0_{i}"], params[f"h1_{i}"] = h0, h1
        time_sql += (
            f" OR (e.kind = 'conversation' AND e.start_ms < :h1_{i} AND e.end_ms > :h0_{i})"
        )
    sql = f"SELECT e.id FROM episodes e WHERE ({time_sql})"
    if plan_kinds:
        marks = ",".join(f":k{i}" for i in range(len(plan_kinds)))
        sql += f" AND e.kind IN ({marks})"
        params.update({f"k{i}": k for i, k in enumerate(plan_kinds)})
    if people:
        marks = ",".join(f":p{i}" for i in range(len(people)))
        sql += (
            " AND EXISTS (SELECT 1 FROM episode_people p WHERE p.episode_id = e.id"
            f" AND p.speaker_id IN ({marks}))"
        )
        params.update({f"p{i}": p for i, p in enumerate(sorted(people))})
    return [int(r[0]) for r in conn.execute(sql + " ORDER BY e.start_ms DESC, e.id DESC", params)]
