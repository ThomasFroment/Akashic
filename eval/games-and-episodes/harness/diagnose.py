"""Diagnose today's game rows and video/game episodes against the harness truth.

    uv run python eval/games-and-episodes/harness/diagnose.py [run-id ...]

Writes ../diagnosis.json and prints a summary. Numbers, exe names and harness-generated video
titles only.
"""

from __future__ import annotations

import bisect
import statistics
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ge  # noqa: E402

from akashic.pipeline.episodes import MIN_GAME_MS, MIN_PLAYED_MS  # noqa: E402

GRACE_MS = 5000
FLICKER_MS = 2000
OUT = ge.HERE.parent / "diagnosis.json"


def fg_at(run: ge.Run, t: int) -> tuple[int, str, str, int] | None:
    i = bisect.bisect_right([x[0] for x in run.fg], t) - 1
    return run.fg[i] if i >= 0 else None


def fg_after(run: ge.Run, t: int) -> tuple[int, str, str, int] | None:
    i = bisect.bisect_right([x[0] for x in run.fg], t)
    return run.fg[i] if i < len(run.fg) else None


def focus_fraction(run: ge.Run, proc: str, a: int, b: int) -> float:
    inside = 0
    fgs = run.fg + [(run.tr.t1, "", "", 0)]
    for (t, exe, _k, _h), (u, *_r) in zip(fgs, fgs[1:], strict=False):
        if exe == proc:
            inside += max(0, min(u, b) - max(t, a))
    return inside / max(1, b - a)


def end_cause(run: ge.Run, row, rows_all) -> str:
    proc, _w, _a, end = row
    for g0, _g1, reason in run.ctx.gaps:
        if end - 1000 <= g0 <= end + GRACE_MS + 2000:
            return f"capture gap ({reason})"
    for a, b in run.ctx.alive.get(proc, []):
        if end - 2000 <= b <= end + GRACE_MS + 1000:
            return "game process exit"
    nxt = [r for r in rows_all if end <= r[2] <= end + 1000 and r[0] != proc]
    if nxt:
        return f"other game in front ({nxt[0][0]})"
    after = fg_after(run, end - 50)
    if after is None:
        return "end of recording"
    if after[1] == proc:
        # the next foreground change is to the game itself: it was already in front, minimized
        nxt2 = fg_after(run, after[0])
        cat = ge.category(nxt2[1], nxt2[2]) if nxt2 else "none"
        return f"game minimized in front, then {cat}"
    cat = ge.category(after[1], after[2])
    return f"focus to {cat}" + (f" ({after[1]})" if cat == "other" else "")


def start_cause(run: ge.Run, row, prev_end: int | None) -> str:
    proc, _w, a, b = row
    before = fg_at(run, a - 200)
    what = ge.category(before[1], before[2]) if before else "none"
    what = f"{what} ({before[1]})" if what == "other" else what
    if b - a < FLICKER_MS:
        return f"flicker (<2 s) from {what}"
    return f"refocus from {what}"


def games(run: ge.Run) -> dict:
    rows_all = run.ctx.stored_games
    out = {"truth": [], "end_causes": Counter(), "start_causes": Counter(), "away_s": []}
    for tg in run.games:
        a, b = tg.m6_start, tg.end
        inside = [r for r in rows_all if ge.overlaps(r[2], r[3], a, b)]
        same = [r for r in inside if r[0] == tg.process]
        spans_two = sum(1 for r in same if sum(1 for t in run.games if t.process == r[0] and ge.overlaps(r[2], r[3], t.m6_start, t.end)) > 1)
        rec = {"process": tg.process, "start_s": ge.rel_s(run, a), "dur_s": round((b - a) / 1000),
               "focus_fraction": round(focus_fraction(run, tg.process, a, b), 3),
               "rows_same_process": len(same), "rows_other_game": len(inside) - len(same),
               "rows_spanning_two_truth": spans_two,
               "flicker_rows": sum(1 for r in same if r[3] - r[2] < FLICKER_MS),
               "short_rows_lt_min_game": sum(1 for r in same if r[3] - r[2] < MIN_GAME_MS),
               "first_row_start_err_s": round((same[0][2] - a) / 1000, 1) if same else None,
               "last_row_end_err_s": round((same[-1][3] - b) / 1000, 1) if same else None,
               "splits": []}
        for x, y in zip(same, same[1:], strict=False):
            ec, sc = end_cause(run, x, rows_all), start_cause(run, y, x[3])
            away = round((y[2] - x[3]) / 1000, 1)
            rec["splits"].append({"end": ec, "next_start": sc, "away_s": away})
            out["end_causes"][ec] += 1
            out["start_causes"][sc] += 1
            out["away_s"].append(away)
        if same:  # how the last row ended, for the end error
            rec["last_end"] = end_cause(run, same[-1], rows_all)
        out["truth"].append(rec)
    # rows outside every truth session and non-game processes
    out["rows_total"] = len(rows_all)
    out["rows_by_process"] = Counter(r[0] for r in rows_all)
    out["rows_outside_truth"] = sum(1 for r in rows_all if not any(
        t.process == r[0] and ge.overlaps(r[2], r[3], t.m6_start - 300_000, t.end) for t in run.games))
    out["rows_non_game_process"] = sum(1 for r in rows_all if r[0] not in ge.GAME_EXES)
    return out


def exclusions(run: ge.Run) -> dict:
    """Could the excluded process (qualityvault.exe) reach a game row in this run?"""
    fgs = [x for x in run.fg if x[1] == "qualityvault.exe"]
    covering = [c for c in run.ctx.checks if c.fg_exe == "qualityvault.exe" and c.verdict]
    rects = run.shadow.execute(
        "SELECT x1 - x0, y1 - y0 FROM frame_windows WHERE process = 'qualityvault.exe'").fetchall()
    return {"vault_foreground_events": len(fgs), "vault_checks_judged_game": len(covering),
            "vault_frame_window_rows": len(rects),
            "vault_max_rect": [max((w for w, _ in rects), default=0), max((h for _, h in rects), default=0)]}


def videos(run: ge.Run) -> dict:
    c = run.shadow
    eps = c.execute("SELECT id, kind, start_ms, end_ms, title FROM episodes WHERE kind IN ('video', 'music')").fetchall()
    out = {"truth": [], "close_causes": Counter(), "miss_causes": Counter()}
    titles = {v.title for v in run.videos}
    for v in run.videos:
        rows = c.execute("SELECT id, start_ms, end_ms, played_ms FROM media_sessions WHERE title = ? ORDER BY start_ms",
                         (v.title,)).fetchall()
        rec = {"vid": v.vid, "title": v.title, "start_s": ge.rel_s(run, v.start), "dur_s": round((v.end - v.start) / 1000),
               "rows": len(rows), "rows_lt_min_played": sum(1 for r in rows if r[3] < MIN_PLAYED_MS), "pieces": []}
        for x, y in zip(rows, rows[1:], strict=False):
            gap = y[1] - x[2]
            if any(x[2] - 2000 <= g0 <= y[1] for g0, _g1, _r in run.ctx.gaps):
                cause = "capture gap (pause) closed the row"
            elif gap <= 10_000:
                cause = "closed and reopened within 10 s, same title (#189 url_unknown hold)"
            else:
                cause = f"other ({round(gap / 1000)} s apart)"
            out["close_causes"][cause] += 1
            rec["pieces"].append({"cause": cause, "gap_s": round(gap / 1000, 1)})
        best = 0.0
        for e in eps:
            if e[4] == v.title:
                inter = max(0, min(e[3], v.end) - max(e[2], v.start))
                best = max(best, inter / max(1, (e[3] - e[2]) + (v.end - v.start) - inter))
        rec["video_episodes"] = sum(1 for e in eps if e[4] == v.title)
        rec["best_iou"] = round(best, 3)
        if best >= 0.5:
            rec["miss"] = None
        elif not rows:
            rec["miss"] = "no media row"
        elif len(rows) > 1:
            rec["miss"] = "split into rows, no piece reaches IoU 0.5"
        else:
            rec["miss"] = "single row, boundaries off"
        if rec["miss"]:
            out["miss_causes"][rec["miss"]] += 1
        out["truth"].append(rec)
    out["episodes_video"] = sum(1 for e in eps if e[1] == "video")
    out["episodes_other_title"] = sum(1 for e in eps if e[4] not in titles)
    out["episodes_duplicate_pieces"] = sum(1 for e in eps if e[4] in titles) - sum(1 for t in out["truth"] if t["best_iou"] >= 0.5)
    out["rows_dropped_lt_min_played"] = sum(t["rows_lt_min_played"] for t in out["truth"])
    return out


def game_episodes(run: ge.Run) -> dict:
    eps = run.shadow.execute("SELECT kind, start_ms, end_ms, title, app FROM episodes WHERE kind = 'game'").fetchall()
    out = []
    for tg in run.games:
        cand = [e for e in eps if e[4] == tg.process and ge.overlaps(e[1], e[2], tg.start, tg.end)]
        best = 0.0
        for e in cand:
            inter = max(0, min(e[2], tg.end) - max(e[1], tg.start))
            best = max(best, inter / max(1, (e[2] - e[1]) + (tg.end - tg.start) - inter))
        out.append({"process": tg.process, "episodes": len(cand), "best_iou": round(best, 3),
                    "title_has_name": any(tg.name in (e[3] or "").lower() for e in cand)})
    return {"truth": out, "episodes_game": len(eps)}


def main() -> None:
    runs = sys.argv[1:] or list(ge.RUNS)
    result, end_tot, start_tot, close_tot, miss_tot, away = {}, Counter(), Counter(), Counter(), Counter(), []
    for rid in runs:
        run = ge.load(rid)
        g, v = games(run), videos(run)
        result[rid] = {"hours": round(run.hours, 2), "games": g, "videos": v, "game_episodes": game_episodes(run),
                       "exclusions": exclusions(run)}
        end_tot.update(g["end_causes"])
        start_tot.update(g["start_causes"])
        close_tot.update(v["close_causes"])
        miss_tot.update(v["miss_causes"])
        away += g["away_s"]
        print(f"== {rid} ({run.hours:.2f} h): stored game rows {g['rows_total']} {dict(g['rows_by_process'])},"
              f" outside truth {g['rows_outside_truth']}, non-game {g['rows_non_game_process']}")
        for t in g["truth"]:
            print(f"  truth {t['process']} at {t['start_s']} s for {t['dur_s']} s: focus {t['focus_fraction']},"
                  f" rows {t['rows_same_process']} (+{t['rows_other_game']} other game), flicker {t['flicker_rows']},"
                  f" < 2 min {t['short_rows_lt_min_game']}, spanning two {t['rows_spanning_two_truth']},"
                  f" start err {t['first_row_start_err_s']} s, end err {t['last_row_end_err_s']} s ({t.get('last_end')})")
        for t in v["truth"]:
            print(f"  video {t['vid']} '{t['title']}' {t['dur_s']} s: rows {t['rows']} (<60 s played {t['rows_lt_min_played']}),"
                  f" episodes {t['video_episodes']}, best IoU {t['best_iou']}, miss: {t['miss']}")
        print(f"  video episodes {v['episodes_video']}, duplicate pieces {v['episodes_duplicate_pieces']}, other title {v['episodes_other_title']}")
        for t in result[rid]["game_episodes"]["truth"]:
            print(f"  game episode {t['process']}: episodes {t['episodes']}, best IoU {t['best_iou']}, title ok {t['title_has_name']}")
        print(f"  exclusions: {result[rid]['exclusions']}")
    print("== split end causes (all runs):")
    for k, n in end_tot.most_common():
        print(f"  {n:4d}  {k}")
    print("== next-row start causes:")
    for k, n in start_tot.most_common():
        print(f"  {n:4d}  {k}")
    if away:
        print(f"== away between split rows: n {len(away)}, median {statistics.median(away)} s, max {max(away)} s,"
              f" <= 300 s {sum(1 for x in away if x <= 300)}")
    print("== video row close causes:", dict(close_tot))
    print("== video episode miss causes:", dict(miss_tot))
    result["totals"] = {"end_causes": end_tot, "start_causes": start_tot, "video_close": close_tot, "video_miss": miss_tot,
                        "away_s": away}
    ge.write_json(OUT, result)


if __name__ == "__main__":
    main()
