"""C5 measurements beyond harness.py: SQL statements and time per Ask, rule-(A)-only and
G/CAP sensitivity (not used to pick the constants), and two stress fixtures.

Run from the harness folder: ..\\.venv\\Scripts\\python.exe C5\\bench.py
"""

from __future__ import annotations

import importlib.util
import statistics
import sys
import time
from datetime import timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import retrieval as R  # noqa: E402
from akashic.search.planner import heuristic_plan  # noqa: E402


def load(name: str, path: Path):  # noqa: ANN201
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


base = load("bt", HERE.parent / "baseline_today.py")
c5 = load("c5cand", HERE / "candidate.py")


def per_ask_cost(fixtures) -> None:  # noqa: ANN001
    """Statements and wall time of select_lines + select_episodes for one Ask (cache cleared)."""
    for label, lines, eps in (
        ("today", R.baseline_select_lines, R.baseline_select_episodes),
        ("C5", c5.select_lines, c5.select_episodes),
    ):
        counts, times = [], []
        for case in R.CASES:
            _, now, q, meaning, _ = case
            fx = fixtures[now]
            plan = heuristic_plan(q, R.ms(now), R.NAMES, R.TZ)
            people = tuple(R.SPEAKER_IDS[p] for p in plan.people)
            for src in ("gold", "resolver"):
                win = R._window(src, base, case, fx)
                if win is None:
                    continue
                n = [0]
                fx.conn.set_trace_callback(lambda _s: n.__setitem__(0, n[0] + 1))
                c5._cache.clear()
                lines(fx.conn, *win, plan.kinds, people)
                eps(fx.conn, *win, plan.kinds, people)
                fx.conn.set_trace_callback(None)
                counts.append(n[0])
                best = float("inf")
                for _ in range(5):
                    c5._cache.clear()
                    t0 = time.perf_counter()
                    lines(fx.conn, *win, plan.kinds, people)
                    eps(fx.conn, *win, plan.kinds, people)
                    best = min(best, time.perf_counter() - t0)
                times.append(best * 1000)
        print(f"[cost] {label:5} asks={len(times)} statements/ask min={min(counts)} max={max(counts)}"
              f" | ms/ask p50={statistics.median(times):.2f} max={max(times):.2f}")  # fmt: skip


def summary_line(tag: str, module) -> dict:  # noqa: ANN001
    res = R.evaluate(module, base)
    s = res["summary"]
    keep = {k: v for k, v in s.items() if "_cand_" in k}
    print(f"[{tag}]", keep)
    return res


class RuleAOnly:
    resolve_question = staticmethod(base.resolve_question)

    @staticmethod
    def select_lines(conn, start_ms, end_ms, plan_kinds, people=()):  # noqa: ANN001, ANN205
        params = {"start": start_ms, "end": end_ms, "seglo": start_ms - c5.MAX_SEG_MS}
        sql = ("SELECT s.id FROM audio_segments s WHERE s.start_ms >= :seglo"
               " AND s.start_ms < :end AND s.end_ms > :start")  # fmt: skip
        sql += c5._person_clause(people, params) + c5._kind_clause(plan_kinds, params)
        return [int(r[0]) for r in conn.execute(sql, params)]

    select_episodes = staticmethod(R.baseline_select_episodes)


def with_constants(g_min: float, cap_h: float):  # noqa: ANN201
    c5.G_MS, c5.CAP_MS = int(g_min * 60_000), int(cap_h * 3_600_000)
    c5._cache.clear()
    return c5


def stress() -> None:
    """(1) Two different Discord calls 10 min apart (Léa 21:00-21:50, Minlo 22:00-01:30) asked
    without a person; (2) a Discord stream with voice that never pauses (18:00-06:00) under the
    call. Intended session: C (Minlo)."""
    saved = dict(R.SESSIONS)
    orig_timeline = R.timeline
    try:
        # (1) merged neighbours
        R.SESSIONS["B"] = (R.at(R.MON, 21), R.at(R.MON, 21, 50), "Léa")
        fx = R.build(R.NOW_0900)
        for q, meaning in (("De quoi on parlait vers 23h ?", R.CLK(23, 0, "yesterday")),
                           ("Qu'est-ce que Minlo a dit vers 23h ?", R.CLK(23, 0, "yesterday"))):
            _report(fx, q, meaning, {"C"})
        R.SESSIONS.clear()
        R.SESSIONS.update(saved)

        # (2) continuous voice-app audio (Discord stream) Mon 18:00 - Tue 06:00, no people
        def timeline_stream(seed: int = 20261110):  # noqa: ANN202
            segs = orig_timeline(seed)
            t = R.at(R.MON, 18)
            k = 0
            while t < R.at(R.TUE, 6):
                if not any(R._inside(t, (s, e)) for s, e, _ in (saved["C"],)):
                    segs.append(R.Seg("system", t, t + timedelta(seconds=12), "S",
                                      ["discord.exe"], None))  # fmt: skip
                t += timedelta(seconds=40 + (k % 7) * 20)  # gaps of 40-160 s, never > G
                k += 1
            segs.sort(key=lambda s: (s.start, s.stream))
            return segs

        R.timeline = timeline_stream
        fx = R.build(R.NOW_0900)
        print("   stress2 episodes:", fx.conn.execute(
            "SELECT kind, count(*) FROM episodes GROUP BY kind").fetchall())  # fmt: skip
        _report(fx, "De quoi on parlait vers 23h ?", R.CLK(23, 0, "yesterday"), {"C"})
        _report(fx, "De quoi on a parlé hier soir ?", R.D("yesterday", "evening"), {"C"})
    finally:
        R.SESSIONS.clear()
        R.SESSIONS.update(saved)
        R.timeline = orig_timeline


def _report(fx, q: str, meaning, sessions) -> None:  # noqa: ANN001
    plan = heuristic_plan(q, R.ms(fx.now), R.NAMES, R.TZ)
    people = tuple(R.SPEAKER_IDS[p] for p in plan.people)
    g = R.gold(meaning, fx.now)
    win = (R.ms(g[0]), R.ms(g[1]))
    members = {i for i, o in fx.owner.items() if o in sessions}
    elig = members & set(R.baseline_select_lines(fx.conn, 0, R.FAR, plan.kinds, people))
    for label, fn in (("today", R.baseline_select_lines), ("C5", c5.select_lines)):
        c5._cache.clear()
        got = set(fn(fx.conn, *win, plan.kinds, people))
        print(f"   {label:5} {q!r:42} recall={len(got & elig) / len(elig):.3f}"
              f" outside={len(got - members)} (intended lines {len(elig)})")


if __name__ == "__main__":
    fixtures = {R.NOW_0900: R.build(R.NOW_0900), R.NOW_0040: R.build(R.NOW_0040)}
    per_ask_cost(fixtures)
    summary_line("rule A only", RuleAOnly)
    for g, cap in ((15, 3), (5, 3), (10, 3), (30, 3), (15, 1), (15, 6)):
        summary_line(f"G={g}min CAP={cap}h", with_constants(g, cap))
    with_constants(15, 3)
    print("[stress]")
    stress()
