"""Score one candidate for game rows, game detection and video/game episodes.

    uv run python eval/games-and-episodes/harness/harness.py <candidate.py> [run-id ...]

The candidate module may define any of (missing ones fall back to today's behaviour):

- ``game_rows(conn, ctx) -> list[(process, window_title | None, start_ms, end_ms)]``: game sessions
  re-derived from the stored rows (``conn``: plain in-memory copy of the run's store, no captured
  text) and/or ``ctx`` (ge.Ctx: simulated per-check foreground records ``ctx.checks``, the game
  processes' running spans ``ctx.alive`` and window modes ``ctx.modes``, stored gaps, monitors).
  ``ge.replay(ctx, is_game=..., end_grace_s=...)`` replays today's GameMode on the checks.
  Default: the stored game_sessions rows.
- ``episodes(conn, ctx) -> list[(id, kind, start_ms, end_ms, title)]``: ``conn`` is a writable copy
  whose game_sessions hold the candidate's rows and whose episodes tables are empty. Default:
  production ``build_episodes(conn, ctx.now_ms)``.
- ``is_game(window: WindowInfo, ctx) -> bool``: detection, scored on the real-window table
  (realwin.json); ``ctx`` is a DetCtx (monitors, settings, excluded processes, the case's extra
  signals in ``ctx.facts``). Default: ``gaming.is_game_window``.
- ``writes_name(window, ctx) -> bool``: whether a game row would carry the process name (False:
  content-free row). Default True.
- ``SIGNALS``: per-check probes the candidate calls besides the foreground read (names from
  realwin.json timings: quns, cursor, exe_path, facts_lookup, running_appid, class_name).

Writes side.png, metrics.txt (one line) and metrics.json next to the candidate module.
"""

from __future__ import annotations

import importlib.util
import json
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ge  # noqa: E402

import score  # noqa: E402

from akashic.capture import gaming  # noqa: E402
from akashic.capture.ports import Rect, WindowInfo  # noqa: E402
from akashic.pipeline import episodes as production  # noqa: E402

REPS = 9
REALWIN = ge.HERE / "realwin.json"


@dataclass(slots=True)
class DetCtx:
    monitors: list[Rect]
    settings: gaming.GameSettings = field(default_factory=gaming.GameSettings)
    excluded: frozenset[str] = frozenset({"qualityvault.exe"})
    facts: dict = field(default_factory=dict)
    now_s: float = 0.0
    cache: dict = field(default_factory=dict)


def load_module(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --- defaults (today) ----------------------------------------------------------------------------


def default_game_rows(conn, ctx):
    return [tuple(r) for r in conn.execute("SELECT process, window_title, start_ms, end_ms FROM game_sessions ORDER BY start_ms")]


def default_episodes(conn, ctx):
    production.build_episodes(conn, ctx.now_ms)
    return conn.execute("SELECT id, kind, start_ms, end_ms, title FROM episodes").fetchall()


def default_is_game(window, ctx):
    return gaming.is_game_window(window, ctx.monitors, ctx.settings)


def default_writes_name(window, ctx):
    return True


# --- timing helpers ------------------------------------------------------------------------------


def timed(fn, make_args, reps=REPS):
    """Warm-up call, then the median of ``reps`` calls; each call gets fresh arguments (not timed)."""
    out = fn(*make_args())
    xs = []
    for _ in range(reps):
        args = make_args()
        t = time.perf_counter()
        out = fn(*args)
        xs.append((time.perf_counter() - t) * 1000)
    return out, statistics.median(xs)


def episode_conn(run: ge.Run, rows):
    conn = ge.fresh(run.shadow)
    conn.execute("DELETE FROM game_sessions")
    conn.executemany("INSERT INTO game_sessions (id, process, window_title, start_ms, end_ms, closed) VALUES (?, ?, ?, ?, ?, 1)",
                     [(i + 1, r[0], r[1], r[2], r[3]) for i, r in enumerate(rows)])
    for t in ("episode_people", "episode_items", "episodes"):
        conn.execute(f"DELETE FROM {t}")  # noqa: S608
    conn.execute("DELETE FROM state WHERE key = ?", (production.CONVERSATIONS_SINCE,))
    return conn


# --- per run ------------------------------------------------------------------------------------


def score_run(run: ge.Run, mod) -> dict:
    game_rows = getattr(mod, "game_rows", default_game_rows)
    build = getattr(mod, "episodes", default_episodes)
    rows, rows_ms = timed(game_rows, lambda: (ge.fresh(run.shadow), run.ctx))
    rows = [tuple(r) if len(r) == 4 else (r[0], ge.titles(run.ctx).get(r[0]), r[1], r[2]) for r in rows]
    holder = {}

    def make():
        holder["conn"] = episode_conn(run, rows)
        return holder["conn"], run.ctx

    eps, eps_ms = timed(build, make)
    conn = holder["conn"]
    conn.execute("DELETE FROM episodes")
    conn.executemany("INSERT INTO episodes (id, kind, ref, start_ms, end_ms, title) VALUES (?, ?, ?, ?, ?, ?)",
                     [(e[0], e[1], f"h:{i}", e[2], e[3], e[4]) for i, e in enumerate(eps)])
    # M6.1 through score.m6, recording the per-truth errors summarize() is given
    seen: list[list[float]] = []
    orig = score.summarize
    score.summarize = lambda xs: (seen.append(list(xs)), orig(xs))[1]
    try:
        m6 = score.m6(run.tr, conn)["M6.1"]
    finally:
        score.summarize = orig
    m11 = score.m11(run.tr, conn)
    # seen: media start, media end, game start, game end, OW start, OW end (those present)
    game_seen = seen[2:] if "start_error_s" in m6.get("media", {}) else seen
    starts = [x for i, xs in enumerate(game_seen) if i % 2 == 0 for x in xs]
    ends = [x for i, xs in enumerate(game_seen) if i % 2 == 1 for x in xs]
    per_truth = [sum(1 for r in rows if r[0] == t.process and ge.overlaps(r[2], r[3], t.m6_start, t.end)) for t in run.games]
    f1 = m11["M11.1"]
    return {
        "hours": run.hours, "rows": len(rows), "rows_per_truth": per_truth,
        "m6_start_err_s": starts, "m6_end_err_s": ends,
        "f1_video": f1.get("video", {}).get("f1", 0.0), "f1_game": f1.get("game", {}).get("f1", 0.0),
        "m11_1": f1, "title_acc": m11["M11.2"].get("value"), "title_n": m11["M11.2"].get("n"),
        "useless": m11["M11.4"].get("value"), "useless_n": m11["M11.4"].get("n"),
        "rows_ms": rows_ms, "episodes_ms": eps_ms, "derive_ms_per_day": (rows_ms + eps_ms) / run.hours * 24,
        "episodes_ms_per_day": eps_ms / run.hours * 24,
        "_rows": rows, "_eps": [e for e in eps if e[1] in ("video", "game", "music")],
    }


# --- detection ----------------------------------------------------------------------------------


def detection(mod) -> dict:
    data = json.loads(REALWIN.read_text(encoding="utf-8"))
    cases = [c for c in data["cases"] if "error" not in c]
    is_game = getattr(mod, "is_game", default_is_game)
    writes = getattr(mod, "writes_name", default_writes_name)
    mons = [Rect(0, 0, 3840, 2160), Rect(-2560, 0, 0, 1440), Rect(3840, 0, 6400, 1440)]
    facts_keys = ("quns", "class", "cursor_showing", "cursor_clipped", "under_launcher_game_dir", "in_gameconfigstore",
                  "under_steam_root", "exe_dir_kind", "steam_running_appid_set", "style", "zoomed", "source")
    items = []
    for c in cases:
        win = WindowInfo(0, c["process"], "", Rect(*c["rect"]), has_frame=c["has_frame"],
                         exclusive_fullscreen=c["exclusive_fullscreen"])
        items.append((c, win, DetCtx(mons, facts={k: c.get(k) for k in facts_keys})))
    verdicts = [(c, bool(is_game(w, ctx)), bool(writes(w, ctx))) for c, w, ctx in items]

    def once():
        for _c, w, ctx in items:
            is_game(w, ctx)

    once()
    xs = []
    for _ in range(max(REPS, 15)):
        t = time.perf_counter()
        once()
        xs.append((time.perf_counter() - t) * 1e6 / len(items))
    tim = [c["timings"] for c in data["cases"] if "timings" in c]
    probe = {k.removesuffix("_us"): statistics.median(t[k] for t in tim) for k in tim[0] if k.endswith("_us")} if tim else {}
    signals = getattr(mod, "SIGNALS", ())
    extra = sum(probe.get(s, 0.0) for s in signals)
    rows = [{"name": c["name"], "truth_game": c["truth_game"], "excluded": c["excluded"], "verdict": v, "writes_name": wn}
            for c, v, wn in verdicts]
    return {
        "cases": rows,
        "false_game": sum(1 for r in rows if not r["truth_game"] and not r["excluded"] and r["verdict"]),
        "missed_game": sum(1 for r in rows if r["truth_game"] and not r["verdict"]),
        "excluded_written": sum(1 for r in rows if r["excluded"] and r["verdict"] and r["writes_name"]),
        "decision_us": statistics.median(xs), "signals": list(signals), "signals_us": extra,
        "check_extra_us": statistics.median(xs) + extra,
    }


# --- picture ------------------------------------------------------------------------------------

BG, FG, DIM = (18, 18, 22), (225, 225, 230), (120, 120, 130)
COL = {"qualitygame.exe": (90, 170, 250), "outerwilds.exe": (250, 170, 70), "video": (120, 210, 120), "game": (200, 120, 230),
       "music": (150, 150, 150), "ok": (80, 190, 110), "bad": (230, 80, 80), "warn": (240, 180, 60)}


def side_png(path: Path, name: str, runs: dict, det: dict, line: str) -> None:
    from PIL import Image, ImageDraw, ImageFont

    try:
        font = ImageFont.truetype("consola.ttf", 13)
        big = ImageFont.truetype("consola.ttf", 16)
    except OSError:
        font = big = ImageFont.load_default()
    W, X0, X1 = 1600, 150, 1580
    lanes = ("truth game", "game rows", "game episodes", "truth video", "video episodes")
    lane_h, panel_h = 16, 22 + 5 * 20 + 10
    det_h = 26 + 18 * (len(det["cases"]) + 1)
    H = 40 + panel_h * len(runs) + det_h + 60
    im = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(im)
    d.text((10, 10), f"{name}", fill=FG, font=big)
    y = 40
    for rid, (run, res) in runs.items():
        t0, t1 = run.tr.t0 - 900_000, run.tr.t1
        sx = lambda t: X0 + (X1 - X0) * (min(max(t, t0), t1) - t0) / (t1 - t0)  # noqa: E731
        d.text((10, y), f"{rid}  rows {res['rows']}  per truth {res['rows_per_truth']}  F1 v {res['f1_video']:.2f} g {res['f1_game']:.2f}",
               fill=FG, font=font)
        yy = y + 20
        for li, lane in enumerate(lanes):
            ly = yy + li * 20
            d.text((10, ly), lane, fill=DIM, font=font)
            d.line((X0, ly + lane_h // 2, X1, ly + lane_h // 2), fill=(40, 40, 48))
            spans = []
            if lane == "truth game":
                spans = [(t.m6_start, t.end, COL[t.process]) for t in run.games]
            elif lane == "game rows":
                spans = [(r[2], r[3], COL.get(r[0], COL["bad"])) for r in res["_rows"]]
            elif lane == "game episodes":
                spans = [(e[2], e[3], COL["game"]) for e in res["_eps"] if e[1] == "game"]
            elif lane == "truth video":
                spans = [(v.start, v.end, COL["video"]) for v in run.videos]
            elif lane == "video episodes":
                spans = [(e[2], e[3], COL[e[1]]) for e in res["_eps"] if e[1] in ("video", "music")]
            for k, (a, b, col) in enumerate(spans):
                xa, xb = sx(a), max(sx(b), sx(a) + 1)
                if lane in ("truth game", "game rows"):  # Quality Game on the top half, Outer Wilds below
                    top, bot = (ly + 1, ly + 8) if col == COL["qualitygame.exe"] else (ly + 9, ly + lane_h - 1)
                else:
                    top, bot = ly + 2, ly + lane_h - 2
                d.rectangle((xa, top, xb, bot), fill=col, outline=BG if k % 2 else None)
        y += panel_h
    d.text((10, y), "detection (real windows): verdict  [green right, red wrong, amber excluded name written]", fill=FG, font=font)
    y += 22
    for i, c in enumerate(det["cases"]):
        ok = c["verdict"] == c["truth_game"] if not c["excluded"] else not (c["verdict"] and c["writes_name"])
        col = COL["ok"] if ok else (COL["warn"] if c["excluded"] else COL["bad"])
        cx = 10 + (i % 3) * 520
        cy = y + (i // 3) * 18
        d.rectangle((cx, cy + 3, cx + 10, cy + 13), fill=col)
        kind = "excluded" if c["excluded"] else ("game" if c["truth_game"] else "non-game")
        d.text((cx + 16, cy), f"{c['name']:<30} {kind:<9} -> {'game' if c['verdict'] else 'no'}", fill=FG, font=font)
    y += 18 * ((len(det["cases"]) + 2) // 3) + 14
    for k, chunk in enumerate([line[i:i + 190] for i in range(0, len(line), 190)]):
        d.text((10, y + 16 * k), chunk, fill=FG, font=font)
    im = im.crop((0, 0, W, y + 16 * (len(line) // 190 + 1) + 12))
    im.save(path)


# --- main ---------------------------------------------------------------------------------------


def fmt(x, nd=2):
    return "na" if x is None else f"{x:.{nd}f}"


def main() -> None:
    cand = Path(sys.argv[1]).resolve()
    run_ids = sys.argv[2:] or list(ge.RUNS)
    mod = load_module(cand)
    out_dir = cand.parent
    name = getattr(mod, "NAME", out_dir.name)
    runs, results = {}, {}
    for rid in run_ids:
        run = ge.load(rid)
        res = score_run(run, mod)
        runs[rid] = (run, res)
        results[rid] = {k: v for k, v in res.items() if not k.startswith("_")}
        print(f"{rid}: rows {res['rows']} per truth {res['rows_per_truth']} F1 video {res['f1_video']:.2f} game {res['f1_game']:.2f}"
              f" titles {fmt(res['title_acc'])} useless {fmt(res['useless'])} derive {res['derive_ms_per_day']:.1f} ms/day", flush=True)
    det = detection(mod)
    per = [x for r in results.values() for x in r["rows_per_truth"]]
    st = [x for r in results.values() for x in r["m6_start_err_s"]]
    en = [x for r in results.values() for x in r["m6_end_err_s"]]
    agg = {
        "rows_per_truth_mean": statistics.mean(per), "rows_per_truth_max": max(per),
        "m6_start_med_s": statistics.median(st) if st else None, "m6_start_max_s": max(st) if st else None,
        "m6_end_med_s": statistics.median(en) if en else None, "m6_end_max_s": max(en) if en else None,
        "false_game": det["false_game"], "missed_game": det["missed_game"], "excluded_written": det["excluded_written"],
        "f1_video_min": min(r["f1_video"] for r in results.values()), "f1_game_min": min(r["f1_game"] for r in results.values()),
        "title_acc_min": min((r["title_acc"] for r in results.values() if r["title_acc"] is not None), default=None),
        "useless_max": max((r["useless"] for r in results.values() if r["useless"] is not None), default=None),
        "derive_ms_per_day_max": max(r["derive_ms_per_day"] for r in results.values()),
        "episodes_ms_per_day_max": max(r["episodes_ms_per_day"] for r in results.values()),
        "check_extra_us": det["check_extra_us"],
    }
    line = (f"rows/truth mean={fmt(agg['rows_per_truth_mean'])} max={agg['rows_per_truth_max']}"
            f" | M6.1 start med={fmt(agg['m6_start_med_s'], 1)}s max={fmt(agg['m6_start_max_s'], 1)}s"
            f" end med={fmt(agg['m6_end_med_s'], 1)}s max={fmt(agg['m6_end_max_s'], 1)}s"
            f" | det false_game={agg['false_game']} missed={agg['missed_game']} excluded_written={agg['excluded_written']}"
            f" | M11.1 F1 video min={fmt(agg['f1_video_min'])} game min={fmt(agg['f1_game_min'])}"
            f" ({' '.join(f'{r[-4:]}:{results[r]["f1_video"]:.2f}/{results[r]["f1_game"]:.2f}' for r in results)})"
            f" | M11.2 min={fmt(agg['title_acc_min'])} | M11.4 max={fmt(agg['useless_max'])}"
            f" | derive max={agg['derive_ms_per_day_max']:.1f} ms/day (episodes {agg['episodes_ms_per_day_max']:.1f}) | check +{agg['check_extra_us']:.1f} us")
    print(line)
    (out_dir / "metrics.txt").write_text(line + "\n", encoding="utf-8")
    ge.write_json(out_dir / "metrics.json", {"candidate": name, "aggregate": agg, "runs": results, "detection": det})
    side_png(out_dir / "side.png", name, runs, det, line)


if __name__ == "__main__":
    main()
