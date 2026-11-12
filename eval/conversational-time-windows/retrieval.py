"""Retrieval fixture: which transcript lines and episodes a time window lets through.

A synthetic, in-memory database (plain ``sqlite3``; no captured content) built with the repo's
own migrations (``akashic.storage.db.migrate``; all 18 apply unchanged on plain SQLite 3.50:
the FTS5 ``unicode61 remove_diacritics 2`` tokenizer is built in, and no migration uses a
SQLCipher pragma) and episodes built by the repo's real builder
(``akashic.pipeline.episodes.build_episodes``).

Timeline (Europe/Paris; reference Tuesday 2026-11-10), ground-truth session per segment:

    A  Sun 11-08 20:30-22:45  Discord call with Sacha
    E  Mon 11-09 12:20-13:10  Discord call with Sacha (lunch)
    B  Mon 11-09 17:00-19:30  Discord call with Léa
    C  Mon 11-09 22:00 - Tue 01:30  Discord call with Minlo, no speech 23:37-23:43 (6 min)
    M  Mon 23:00 - Tue 03:00  Spotify music stream (lyrics on the system stream)
    V  Tue 00:30-00:45        YouTube video in Zen (media session + soundtrack)
    F  Tue 08:52-09:00        fresh call speech with Sacha (exists only at now 09:00)

The system stream is one loopback: one segment at a time, owned by whichever source spoke, with
``sources`` = the owner's app plus apps audibly playing underneath (Spotify under the call,
Zen during the video). Segments every 10-30 s on mic and system. Segments ending in the last
10 minutes before "now" have no speaker turns yet and a pending ``speakers`` job (fresh,
unassigned speech, which also holds back the episode horizon as in production).

The fixture is built twice, truncated at now = Tue 09:00 and Tue 00:40 (only segments that had
ended, media sessions still playing are open).

For each retrieval question the harness gets the window (gold label, baseline resolver, or the
candidate's resolver), the plan kinds/people from the real ``heuristic_plan``, and calls
``select_lines(conn, start_ms, end_ms, plan_kinds, people=())`` -> list of segment ids
(``people`` are speaker ids). Reported per case:

- recall: share of the intended session's *eligible* segments returned. Eligible = segments of
  the intended session(s) that pass today's non-time filters (person, kind) with an unbounded
  window, so the number isolates the time-window effect;
- recall_win: the same, restricted to eligible segments overlapping the gold span (for
  "vers 22h" the moment, not the whole call);
- outside: returned segments that belong to no intended session;
- episodes: optional ``select_episodes`` (same signature) -> episode ids; ep_recall over the
  plan-kind episodes built from the intended sessions, ep_outside = other episodes returned.

Run alone: python retrieval.py   (baseline window + baseline filters)
"""

from __future__ import annotations

import json
import random
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
SRC = HERE.parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
sys.path.insert(0, str(HERE))

from akashic.pipeline import episodes as episode_builder  # noqa: E402
from akashic.search.planner import heuristic_plan  # noqa: E402
from akashic.storage import db  # noqa: E402
from make_phrases import CLK, RECENT, D, gold  # noqa: E402

TZ = ZoneInfo("Europe/Paris")
FAR = 2**62
FRESH_MS = 10 * 60_000
NAMES = ("Minlo", "Sacha", "Léa")
SPEAKER_IDS = {"self": 1, "Minlo": 2, "Sacha": 3, "Léa": 4}
NOW_0900 = datetime(2026, 11, 10, 9, 0, tzinfo=TZ)
NOW_0040 = datetime(2026, 11, 10, 0, 40, tzinfo=TZ)

CASES = [  # (id, now, question, meaning, intended sessions)
    ("R1", NOW_0900, "Qu'est-ce que Minlo a dit hier soir ?", D("yesterday", "evening"), {"C"}),
    ("R2", NOW_0900, "What did Minlo say last night?", D("yesterday", "night_en"), {"C"}),
    ("R3", NOW_0900, "De quoi on parlait vers 22h ?", CLK(22), {"C"}),
    ("R4", NOW_0900, "Qu'est-ce que Sacha a dit ce midi ?", D("today", "noon"), {"E"}),
    ("R5", NOW_0900, "De quoi on a parlé hier soir ?", D("yesterday", "evening"), {"B", "C"}),
    ("R6", NOW_0040, "Qu'est-ce que Sacha a dit hier soir ?", D("yesterday", "evening"), {"A"}),
    ("R7", NOW_0040, "What did Sacha say last night?", D("yesterday", "night_en"), {"A"}),
    ("R8", NOW_0040, "De quoi on parlait vers 22h ?", CLK(22), {"C"}),
    ("R9", NOW_0040, "Qu'est-ce que Sacha a dit ce midi ?", D("today", "noon"), {"E"}),
    ("R10", NOW_0040, "De quoi on a parlé hier soir ?", D("yesterday", "evening"), {"A"}),
    ("R11", NOW_0040, "Qu'est-ce que Minlo a dit ce soir ?", D("today", "evening"), {"C"}),
    # A window that ends inside the call: session-level recall vs the moment itself.
    ("R12", NOW_0900, "Qu'est-ce que Minlo a dit hier soir vers 23h ?", CLK(23, 0, "yesterday"), {"C"}),
    # Call still in progress, music stream still open (its lyrics are not in any episode yet).
    ("R13", NOW_0040, "De quoi on parlait vers 23h ?", CLK(23), {"C"}),
    # Fresh speech of the last minutes: no speaker turns, no episode yet.
    ("R14", NOW_0900, "De quoi on parlait tout à l'heure ?", RECENT, {"F"}),
]


def ms(x: datetime) -> int:
    return int(x.timestamp() * 1000)


def at(d: date, h: int, m: int = 0) -> datetime:
    extra, h = divmod(h, 24)
    return datetime.combine(d + timedelta(days=extra), time(h, m), tzinfo=TZ)


SUN, MON, TUE = date(2026, 11, 8), date(2026, 11, 9), date(2026, 11, 10)
SESSIONS = {
    "A": (at(SUN, 20, 30), at(SUN, 22, 45), "Sacha"),
    "E": (at(MON, 12, 20), at(MON, 13, 10), "Sacha"),
    "B": (at(MON, 17), at(MON, 19, 30), "Léa"),
    "C": (at(MON, 22), at(TUE, 1, 30), "Minlo"),
    "F": (at(TUE, 8, 52), at(TUE, 9), "Sacha"),
}
QUIET_C = (at(MON, 23, 37), at(MON, 23, 43))
MUSIC = (at(MON, 23), at(TUE, 3))
VIDEO = (at(TUE, 0, 30), at(TUE, 0, 45))


@dataclass
class Seg:
    stream: str
    start: datetime
    end: datetime
    owner: str
    sources: list[str]
    speaker: str | None
    id: int = 0


@dataclass
class Fixture:
    conn: sqlite3.Connection
    now: datetime
    owner: dict[int, str] = field(default_factory=dict)  # segment id -> session key


def _inside(t: datetime, span: tuple[datetime, datetime]) -> bool:
    return span[0] <= t < span[1]


def timeline(seed: int = 20261110) -> list[Seg]:
    rng = random.Random(seed)
    segs: list[Seg] = []

    def stream(stream_name: str, t0: datetime, t1: datetime, pick) -> None:  # noqa: ANN001
        t = t0 + timedelta(seconds=rng.uniform(0, 10))
        while t < t1:
            step = rng.uniform(10, 30)
            dur = rng.uniform(3, min(step - 1, 15))
            choice = pick(t)
            if choice is not None:
                owner, sources, speaker = choice
                segs.append(Seg(stream_name, t, t + timedelta(seconds=dur), owner, sources, speaker))
            t += timedelta(seconds=step)

    for key in ("A", "E", "B", "F"):
        s, e, person = SESSIONS[key]
        stream("system", s, e, lambda _t, k=key, p=person: (k, ["discord.exe"], p))
        stream("mic", s, e, lambda _t, k=key: (k, [], "self"))

    c0, c1, minlo = SESSIONS["C"]

    def system_night(t: datetime):  # noqa: ANN202
        call = _inside(t, (c0, c1)) and not _inside(t, QUIET_C)
        video = _inside(t, VIDEO)
        music = _inside(t, MUSIC) and not video  # music paused while the video plays
        r = rng.random()
        if call and (not video or r < 0.5) and (not music or r < 0.8):
            src = ["discord.exe"] + (["spotify.exe"] if music else []) + (["zen.exe"] if video else [])
            return "C", src, minlo
        if video:
            return "V", ["zen.exe"], None
        if music and r < 0.9:
            return "M", ["spotify.exe"], None
        return None

    stream("system", c0, MUSIC[1], system_night)
    stream("mic", c0, c1, lambda t: None if _inside(t, QUIET_C) else ("C", [], "self"))
    segs.sort(key=lambda s: (s.start, s.stream))
    return segs


def build(now: datetime) -> Fixture:
    conn = sqlite3.connect(":memory:", isolation_level=None)
    db.migrate(conn)
    fx = Fixture(conn, now)
    now_ms = ms(now)
    conn.execute("BEGIN")
    for name, sid in SPEAKER_IDS.items():
        conn.execute(
            "INSERT INTO speakers (id, name, is_self, created_ms) VALUES (?, ?, ?, 0)",
            (sid, None if name == "self" else name, int(name == "self")),
        )
    n = 0
    for seg in timeline():
        if seg.end > now:
            continue  # not closed yet: not written
        n += 1
        fresh = ms(seg.end) > now_ms - FRESH_MS
        speaker = None if fresh or seg.speaker is None else SPEAKER_IDS[seg.speaker]
        cur = conn.execute(
            "INSERT INTO audio_segments (stream, start_ms, end_ms, speech_ms, sources, status,"
            " language, transcript, speaker_id, confidence) VALUES (?, ?, ?, ?, ?, 'done', 'fr',"
            " ?, ?, 0.9)",
            (seg.stream, ms(seg.start), ms(seg.end), ms(seg.end) - ms(seg.start),
             json.dumps(seg.sources), f"synthetic {seg.owner} line {n}", speaker),
        )  # fmt: skip
        seg_id = int(cur.lastrowid)
        fx.owner[seg_id] = seg.owner
        dur = ms(seg.end) - ms(seg.start)
        if fresh:
            conn.execute(
                "INSERT INTO jobs (kind, key, payload, data_ms, created_ms) VALUES"
                " ('speakers', ?, ?, ?, ?)",
                (f"speakers:{seg_id}", json.dumps({"segment_id": seg_id}), ms(seg.start), now_ms),
            )
        else:
            conn.execute(
                "INSERT INTO speaker_turns (segment_id, speaker_id, start_ms, end_ms, speech_ms)"
                " VALUES (?, ?, 0, ?, ?)",
                (seg_id, speaker, dur, dur),
            )
    for app, title, artist, url, (s, e) in (
        ("Spotify", "Lofi Radio", "Chillhop", None, MUSIC),
        ("Zen", "Speedrun explained", "Some Channel", "https://www.youtube.com/watch?v=abc", VIDEO),
    ):
        if s >= now:
            continue
        end = min(e, now)
        conn.execute(
            "INSERT INTO media_sessions (app_id, app, title, artist, url, start_ms, end_ms,"
            " played_ms, closed) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (app, app, title, artist, url, ms(s), ms(end), ms(end) - ms(s), int(e <= now)),
        )
    conn.execute("COMMIT")
    episode_builder.build_episodes(conn, now_ms)
    return fx


# --- today's filters (hybrid.py) -------------------------------------------------------------
def _person_kind_filter(plan_kinds, people, params: dict) -> str:  # noqa: ANN001
    """The non-time filters of hybrid._ranked_lines (hybrid.py:327-355), verbatim."""
    sql = ""
    if people:
        marks = ",".join(f":p{i}" for i in range(len(people)))
        sql = (
            f" AND (s.speaker_id IN ({marks}) OR EXISTS (SELECT 1 FROM speaker_turns t"
            f" WHERE t.segment_id = s.id AND t.speaker_id IN ({marks})))"
        )
        params.update({f"p{i}": p for i, p in enumerate(sorted(people))})
    if plan_kinds:
        sql += (
            " AND (EXISTS (SELECT 1 FROM episode_items item JOIN episodes e"
            " ON e.id = item.episode_id WHERE item.item_kind IN ('segment', 'video_segment')"
            " AND item.item_id = s.id AND e.kind IN (SELECT value FROM json_each(:kinds)))"
            " OR (:conversation = 1 AND NOT EXISTS (SELECT 1 FROM episode_items item"
            " WHERE item.item_kind IN ('segment', 'video_segment') AND item.item_id = s.id)))"
        )
        params["kinds"] = json.dumps(list(plan_kinds))
        params["conversation"] = int("conversation" in plan_kinds)
    return sql


def baseline_select_lines(conn, start_ms: int, end_ms: int, plan_kinds, people=()) -> list[int]:  # noqa: ANN001
    """hybrid.py:356-373: a line is in the window when its START is in [start, end)."""
    params: dict = {"start": start_ms, "end": end_ms}
    extra = _person_kind_filter(plan_kinds, people, params)
    sql = "SELECT s.id FROM audio_segments s WHERE s.start_ms >= :start AND s.start_ms < :end"
    return [int(r[0]) for r in conn.execute(sql + extra, params)]


def baseline_select_episodes(conn, start_ms: int, end_ms: int, plan_kinds, people=()) -> list[int]:  # noqa: ANN001
    """hybrid._candidates (hybrid.py:197-221): episodes overlapping the window."""
    sql = "SELECT e.id FROM episodes e WHERE e.start_ms < :end AND e.end_ms > :start"
    params: dict = {"start": start_ms, "end": end_ms}
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


# --- evaluation ------------------------------------------------------------------------------
def _intended_episodes(fx: Fixture, sessions: set[str], plan_kinds) -> set[int]:  # noqa: ANN001
    rows = fx.conn.execute(
        "SELECT e.id, e.kind, i.item_id FROM episodes e JOIN episode_items i ON i.episode_id = e.id"
        " WHERE i.item_kind IN ('segment', 'video_segment')"
    ).fetchall()
    total: dict[int, int] = {}
    hit: dict[int, int] = {}
    kind: dict[int, str] = {}
    for eid, k, item in rows:
        kind[eid] = k
        total[eid] = total.get(eid, 0) + 1
        hit[eid] = hit.get(eid, 0) + (fx.owner.get(item) in sessions)
    return {
        e for e in total
        if hit[e] * 2 > total[e] and (not plan_kinds or kind[e] in plan_kinds)
    }  # fmt: skip


def _window(source: str, module, case, fx: Fixture) -> tuple[int, int] | None:  # noqa: ANN001
    _, now, question, meaning, _ = case
    if source == "gold":
        g = gold(meaning, now)
        return (ms(g[0]), ms(g[1])) if g else None
    try:
        out = module.resolve_question(question, now, None)
    except Exception:  # noqa: BLE001 - a crashing resolver gives no window
        return None
    if out is None:
        return None
    s, e = (x if x.tzinfo else x.replace(tzinfo=TZ) for x in out)
    return ms(s), ms(e)


def evaluate(module, baseline) -> dict:  # noqa: ANN001
    fixtures = {NOW_0900: build(NOW_0900), NOW_0040: build(NOW_0040)}
    lines_c = getattr(module, "select_lines", None) or baseline_select_lines
    eps_c = getattr(module, "select_episodes", None) or baseline_select_episodes
    combos = {
        "goldwin+base": ("gold", baseline, baseline_select_lines, baseline_select_episodes),
        "basewin+base": ("resolver", baseline, baseline_select_lines, baseline_select_episodes),
        "goldwin+cand": ("gold", module, lines_c, eps_c),
        "candwin+cand": ("resolver", module, lines_c, eps_c),
    }
    rows = []
    for case in CASES:
        cid, now, question, meaning, sessions = case
        fx = fixtures[now]
        plan = heuristic_plan(question, ms(now), NAMES, TZ)
        people = tuple(SPEAKER_IDS[p] for p in plan.people)
        kinds = plan.kinds
        members = {i for i, o in fx.owner.items() if o in sessions}
        eligible = members & set(baseline_select_lines(fx.conn, 0, FAR, kinds, people))
        g = gold(meaning, now)
        seg_span = {
            int(r[0]): (int(r[1]), int(r[2]))
            for r in fx.conn.execute("SELECT id, start_ms, end_ms FROM audio_segments")
        }
        eligible_w = {i for i in eligible if seg_span[i][0] < ms(g[1]) and seg_span[i][1] > ms(g[0])}
        want_eps = _intended_episodes(fx, sessions, kinds)
        for combo, (src, resolver, sel_lines, sel_eps) in combos.items():
            win = _window(src, resolver, case, fx)
            if win is None:
                got, got_eps = [], []
            else:
                got = sel_lines(fx.conn, win[0], win[1], kinds, people)
                got_eps = sel_eps(fx.conn, win[0], win[1], kinds, people)
            got_s, got_e = set(got), set(got_eps)
            rows.append(
                {
                    "case": cid, "now": now.strftime("%H:%M"), "question": question,
                    "sessions": sorted(sessions), "combo": combo,
                    "window": None if win is None else [
                        datetime.fromtimestamp(w / 1000, TZ).strftime("%a %H:%M") for w in win],
                    "kinds": list(kinds), "people": list(plan.people),
                    "eligible": len(eligible), "returned": len(got_s),
                    "recall": round(len(got_s & eligible) / len(eligible), 3) if eligible else None,
                    "recall_win": round(len(got_s & eligible_w) / len(eligible_w), 3)
                    if eligible_w else None,
                    "outside": len(got_s - members),
                    "ep_intended": len(want_eps),
                    "ep_recall": round(len(got_e & want_eps) / len(want_eps), 3) if want_eps else None,
                    "ep_outside": len(got_e - want_eps),
                }
            )  # fmt: skip
    summary = {}
    for combo in combos:
        rs = [r for r in rows if r["combo"] == combo]
        rec = [r["recall"] for r in rs if r["recall"] is not None]
        recw = [r["recall_win"] for r in rs if r["recall_win"] is not None]
        epr = [r["ep_recall"] for r in rs if r["ep_recall"] is not None]
        tag = combo.replace("+", "_")
        summary[f"{tag}_recall"] = round(sum(rec) / len(rec), 3)
        summary[f"{tag}_recallwin"] = round(sum(recw) / len(recw), 3)
        summary[f"{tag}_outside"] = sum(r["outside"] for r in rs)
        summary[f"{tag}_eprecall"] = round(sum(epr) / len(epr), 3)
        summary[f"{tag}_epoutside"] = sum(r["ep_outside"] for r in rs)
    fixture_info = {
        now.strftime("%H:%M"): {
            "segments": fx.conn.execute("SELECT count(*) FROM audio_segments").fetchone()[0],
            "episodes": [
                [r[0], r[1], datetime.fromtimestamp(r[2] / 1000, TZ).strftime("%a %H:%M"),
                 datetime.fromtimestamp(r[3] / 1000, TZ).strftime("%a %H:%M"), r[4]]
                for r in fx.conn.execute(
                    "SELECT id, kind, start_ms, end_ms, (SELECT count(*) FROM episode_items i"
                    " WHERE i.episode_id = e.id) FROM episodes e ORDER BY start_ms")
            ],
            "unassigned_segments": fx.conn.execute(
                "SELECT count(*) FROM audio_segments s WHERE NOT EXISTS (SELECT 1 FROM"
                " episode_items i WHERE i.item_kind IN ('segment','video_segment')"
                " AND i.item_id = s.id)").fetchone()[0],
        }
        for now, fx in fixtures.items()
    }  # fmt: skip
    return {"summary": summary, "rows": rows, "fixture": fixture_info}


def print_table(result: dict) -> None:
    for now, info in result["fixture"].items():
        print(f"fixture now={now}: {info['segments']} segments, "
              f"{info['unassigned_segments']} not in any episode")  # fmt: skip
        for ep in info["episodes"]:
            print("   episode", ep)
    print(f"{'case':4} {'now':5} {'combo':13} {'window':27} {'elig':>5} {'ret':>5} {'recall':>6}"
          f" {'rec_w':>6} {'outside':>7} {'ep_rec':>6} {'ep_out':>6}  question")  # fmt: skip
    for r in result["rows"]:
        win = " - ".join(r["window"]) if r["window"] else "None"
        print(f"{r['case']:4} {r['now']:5} {r['combo']:13} {win:27} {r['eligible']:>5} "
              f"{r['returned']:>5} {r['recall']!s:>6} {r['recall_win']!s:>6} {r['outside']:>7} "
              f"{r['ep_recall']!s:>6} {r['ep_outside']:>6}  {r['question']}")  # fmt: skip
    print(result["summary"])


if __name__ == "__main__":
    import importlib.util

    spec = importlib.util.spec_from_file_location("baseline_today", HERE / "baseline_today.py")
    base = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(base)
    res = evaluate(base, base)
    print_table(res)
