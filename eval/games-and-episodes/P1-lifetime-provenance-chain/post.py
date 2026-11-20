"""Run the harness on a P1 candidate, then add the fields the harness line lacks and draw figures.

    uv run python eval/games-and-episodes/P1-lifetime-provenance-chain/post.py <candidate.py> [--figs]

Adds to metrics.txt / metrics.json: active time (front ledger vs the truth's in-front time),
gaming_now (game mode from last_front_ms) against today's game mode, video-chain counts, the
derivation cost of the parts this candidate changes (media chains, game episodes, app runs), and
two readings with the harness artifacts removed (rows assigned to one truth session each; Outer
Wilds' truth end clamped to the capture stop). Numbers, exe names and harness video titles only.
"""

import bisect
import importlib.util
import json
import statistics
import sys
from pathlib import Path

HARNESS = Path(__file__).resolve().parents[1] / "harness"
sys.path.insert(0, str(HARNESS))
import ge  # noqa: E402

_spec = importlib.util.spec_from_file_location("harness_main", HARNESS / "harness.py")
harness = importlib.util.module_from_spec(_spec)
sys.modules["harness_main"] = harness
_spec.loader.exec_module(harness)

LOADED: dict = {}
MODS: dict = {}
_load, _load_module = ge.load, harness.load_module


def load(rid):
    if rid not in LOADED:
        LOADED[rid] = _load(rid)
    return LOADED[rid]


def load_module(path):
    MODS[str(path)] = _load_module(path)
    return MODS[str(path)]


ge.load = load
harness.load_module = load_module
REPS = harness.REPS + 1  # warm-up + timed calls per run

BG, FG, DIM, GRID = (18, 18, 22), (225, 225, 230), (120, 120, 130), (40, 40, 48)
COL = {"qualitygame.exe": (90, 170, 250), "outerwilds.exe": (250, 170, 70), "front": (120, 210, 120), "mode": (200, 120, 230),
       "today": (150, 150, 160), "gap": (230, 80, 80), "row_a": (120, 200, 120), "row_b": (70, 140, 70), "chain": (200, 120, 230),
       "base": (150, 150, 160), "truth": (90, 170, 250)}


def in_gaps(gaps, t):
    return any(a <= t < b for a, b in gaps)


def truth_front_ms(run, proc, s, e) -> int:
    """In-front, restored time of ``proc`` in [s, e) from the observer's foreground log, on a 100 ms grid,
    outside stored gaps (independent of the candidate's ledger code)."""
    fg_t = [x[0] for x in run.fg]
    gaps = [(a, b) for a, b, _r in run.ctx.gaps]
    modes = run.ctx.modes.get(proc, [])
    n = 0
    for t in range(s, e, ge.CHECK_MS):
        i = bisect.bisect_right(fg_t, t) - 1
        if i >= 0 and run.fg[i][1] == proc and ge.shown(modes, t) and not in_gaps(gaps, t) and run.ctx.first_frame <= t < run.ctx.t1:
            n += ge.CHECK_MS
    return n


def extras(cand: Path) -> dict:
    mod = MODS[str(cand)]
    out = {"runs": {}}
    run_ids = list(LOADED)
    for k, rid in enumerate(run_ids):
        run = LOADED[rid]
        rows = mod.LAST[rid]
        hours = run.hours
        r = {}
        # active time per truth session (rows of that process instance)
        errs = []
        per = []
        for t in run.games:
            mine = [x for x in rows if x.inst.exe == t.process and x.inst.created == t.start]
            act = sum(x.active_ms for x in mine)
            tru = truth_front_ms(run, t.process, t.m6_start, t.end)
            errs.append(abs(act - tru) / 1000)
            per.append({"process": t.process, "active_s": act / 1000, "truth_front_s": tru / 1000,
                        "span_s": (t.end - t.m6_start) / 1000})
        r["active"] = per
        r["active_err_s"] = errs
        # gaming_now from last_front_ms (on while the row is open and the game was in front within 5 s)
        # vs today's GameMode (replayed rows + 5 s grace), as interval unions
        today = ge.replay(run.ctx)
        p1 = _union([(max(a, x.start), min(b + 5000, x.end)) for x in rows for a, b in x.inst.front if a < x.end and b > x.start])
        td = _union([(s, e + 5000) for _p, _w, s, e in today])
        both = _length(_intersect(p1, td))
        r["gaming_now_s"], r["today_mode_s"] = _length(p1) / 1000, _length(td) / 1000
        r["mode_differ_s"] = (_length(p1) + _length(td) - 2 * both) / 1000
        # video chains
        media = [mod._Media(*row, None, None, None) for row in run.shadow.execute(
            "SELECT id, app, title, artist, url, start_ms, end_ms, played_ms FROM media_sessions WHERE closed = 1 ORDER BY start_ms")]
        chains = mod.media_chains(media, mod._gaps(run.shadow), mod.CHAIN_P_MS, mod._pauses(run.shadow) if mod.PAUSE_SPLITS else ())
        kept = [c for c in chains if sum(x.played for x in c) >= mod.MIN_PLAYED_MS]
        r["media_rows"], r["chains"], r["chains_kept"] = len(media), len(chains), len(kept)
        r["rows_per_chain_max"] = max((len(c) for c in kept), default=0)
        r["chains_per_truth_video"] = [sum(1 for c in kept if c[0].title == v.title and ge.overlaps(c[0].start, max(x.end for x in c), v.start, v.end))
                                       for v in run.videos]
        # cost of the changed parts, per day
        sl = slice(k * REPS + 1, (k + 1) * REPS)
        med = {p: statistics.median(mod.TIMES[p][sl]) for p in mod.TIMES}
        r["parts_ms"] = med
        r["changed_ms_per_day"] = (med["media"] + med["games"] + med["app_runs"]) / hours * 24
        r["parts_ms_per_day"] = {p: v / hours * 24 for p, v in med.items()}
        # harness-artifact-free readings
        assigned = [0] * len(run.games)
        for x in rows:
            best = max(range(len(run.games)), key=lambda i: _ov(run.games[i], x) if run.games[i].process == x.process else -1)
            if run.games[best].process == x.process and _ov(run.games[best], x) > 0:
                assigned[best] += 1
        r["rows_per_truth_assigned"] = assigned
        rec_end = run.ctx.t1
        r["m6_end_err_clamped_s"] = [min((abs(x.end - min(t.end, rec_end)) / 1000 for x in rows if x.process == t.process), default=None)
                                     for t in run.games]
        r["truth_end_after_stop_s"] = [max(0, t.end - rec_end) / 1000 for t in run.games]
        out["runs"][rid] = r
    allr = out["runs"].values()
    asg = [x for r in allr for x in r["rows_per_truth_assigned"]]
    ce = [x for r in allr for x in r["m6_end_err_clamped_s"] if x is not None]
    ae = [x for r in allr for x in r["active_err_s"]]
    cpt = [x for r in allr for x in r["chains_per_truth_video"]]
    out["aggregate"] = {
        "chain_p_s": mod.CHAIN_P_MS / 1000,
        "active_err_med_s": statistics.median(ae), "active_err_max_s": max(ae),
        "gaming_now_h": sum(r["gaming_now_s"] for r in allr) / 3600, "today_mode_h": sum(r["today_mode_s"] for r in allr) / 3600,
        "mode_differ_s": sum(r["mode_differ_s"] for r in allr),
        "media_rows": sum(r["media_rows"] for r in allr), "chains_kept": sum(r["chains_kept"] for r in allr),
        "rows_per_chain_max": max(r["rows_per_chain_max"] for r in allr),
        "chains_per_truth_video_max": max(cpt) if cpt else None, "chains_per_truth_video_min": min(cpt) if cpt else None,
        "changed_ms_per_day_max": max(r["changed_ms_per_day"] for r in allr),
        "media_ms_per_day_max": max(r["parts_ms_per_day"]["media"] for r in allr),
        "games_ms_per_day_max": max(r["parts_ms_per_day"]["games"] for r in allr),
        "app_runs_ms_per_day_max": max(r["parts_ms_per_day"]["app_runs"] for r in allr),
        "conversations_ms_per_day_max": max(r["parts_ms_per_day"]["conversations"] for r in allr),
        "rows_per_truth_assigned_mean": statistics.mean(asg), "rows_per_truth_assigned_max": max(asg),
        "m6_end_clamped_max_s": max(ce) if ce else None,
    }
    return out


def _pieces(a: int, b: int, lo: int, hi: int, pauses, min_ms: int) -> list[tuple[int, int]]:
    """[a, b) clipped to the recorded span [lo, hi) and split at stored Pause gaps; short pieces dropped."""
    a, b = max(a, lo), min(b, hi)
    out = []
    for x, y in sorted(pauses):
        if x < b and y > a:
            out.append((a, x))
            a = y
    out.append((a, b))
    return [(s, e) for s, e in out if e - s >= min_ms]


def clipped_scoring(mod) -> dict:
    """The musts with truth clipped to the recorded span and split at stored Pause gaps (the user's
    rule: a Pause splits the row). Same IoU 0.5 matching and title rule as score.m11; conversations
    and desk activities keep score.m11's truth; M6.1 compares each piece with rows of its process."""
    import score

    runs = {}
    for rid, run in LOADED.items():
        tr, ctx = run.tr, run.ctx
        lo, hi = ctx.first_frame, ctx.t1
        pauses = [(a, b) for a, b, r in ctx.gaps if r == "paused"]
        games = [(g.process, s, e, g.name) for g in run.games for s, e in _pieces(g.start, g.end, lo, hi, pauses, 120_000)]
        vids = [(v.title.lower(), s, e) for v in run.videos for s, e in _pieces(v.start, v.end, lo, hi, pauses, 60_000)]
        rows = mod.LAST[rid]
        per = [sum(1 for x in rows if x.process == p and ge.overlaps(x.start, x.end, s, e)) for p, s, e, _n in games]
        st = [min((abs(x.start - s) for x in rows if x.process == p), default=None) for p, s, e, _n in games]
        en = [min((abs(x.end - e) for x in rows if x.process == p), default=None) for p, s, e, _n in games]
        conn = harness.episode_conn(run, [(x.process, x.title, x.start, x.end) for x in rows])
        eps = mod.episodes(conn, ctx)
        truth = [("video", s, e, t) for t, s, e in vids] + [("game", s, e, n) for _p, s, e, n in games]
        truth += [("conversation", a, b, "discord") for a, b in tr.call_spans().values()]
        truth += score.desk_activities(tr)

        def iou(a0, a1, b0, b1):
            inter = max(0, min(a1, b1) - max(a0, b0))
            return inter / max(1, (a1 - a0) + (b1 - b0) - inter)

        tp, nt, title_ok = {}, {}, 0
        for kind, a, b, name in truth:
            cand = [e for e in eps if e[1] == kind and iou(e[2], e[3], a, b) >= score.IOU]
            nt[kind] = nt.get(kind, 0) + 1
            tp[kind] = tp.get(kind, 0) + bool(cand)
            title_ok += name is not None and any(name in (e[4] or "").lower() for e in cand)
        ne = {}
        for e in eps:
            ne[e[1]] = ne.get(e[1], 0) + 1

        def f1(k):
            p, r = (tp.get(k, 0) / ne[k] if ne.get(k) else 0), (tp.get(k, 0) / nt[k] if nt.get(k) else 0)
            return 2 * p * r / (p + r) if p + r else 0.0

        n_titled = sum(1 for t in truth if t[3])
        runs[rid] = {"n_game_pieces": len(games), "n_video_pieces": len(vids), "rows_per_truth": per,
                     "m6_start_err_s": [x / 1000 for x in st if x is not None], "m6_end_err_s": [x / 1000 for x in en if x is not None],
                     "f1_video": f1("video"), "f1_game": f1("game"), "title_acc": title_ok / n_titled,
                     "episodes": {k: ne.get(k, 0) for k in ("video", "game")}, "matched": {k: tp.get(k, 0) for k in ("video", "game")}}
    allr = list(runs.values())
    per = [x for r in allr for x in r["rows_per_truth"]]
    return {"runs": runs, "aggregate": {
        "rows_per_truth_mean": statistics.mean(per), "rows_per_truth_max": max(per),
        "m6_start_max_s": max(x for r in allr for x in r["m6_start_err_s"]), "m6_end_max_s": max(x for r in allr for x in r["m6_end_err_s"]),
        "f1_video_min": min(r["f1_video"] for r in allr), "f1_game_min": min(r["f1_game"] for r in allr),
        "title_acc_min": min(r["title_acc"] for r in allr)}}


def _union(spans):
    out = []
    for a, b in sorted(x for x in spans if x[1] > x[0]):
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


def _intersect(xs, ys):
    return [(max(a, c), min(b, d)) for a, b in xs for c, d in ys if max(a, c) < min(b, d)]


def _length(spans) -> int:
    return sum(b - a for a, b in spans)


def _ov(t, x) -> int:
    return max(0, min(t.end, x.end) - max(t.m6_start, x.start))


def _last_front_before(row, t) -> int:
    """last_front_ms as the capture side would hold it at time t (the latest front tick <= t)."""
    best = 0
    for a, b in row.inst.front:
        if a <= t:
            best = min(b, t)
        else:
            break
    return best


# --- figures ------------------------------------------------------------------------------------


def _font():
    from PIL import ImageFont

    try:
        return ImageFont.truetype("consola.ttf", 13), ImageFont.truetype("consola.ttf", 16)
    except OSError:
        f = ImageFont.load_default()
        return f, f


def fig_games(path: Path, mod, title: str) -> None:
    from PIL import Image, ImageDraw

    font, big = _font()
    W, X0, X1 = 1600, 170, 1580
    lanes = ("truth lifetime", "P1 rows", "in front (ledger)", "gaming_now (P1)", "today rows", "no-recording gaps")
    panel_h = 24 + len(lanes) * 20 + 14
    im = Image.new("RGB", (W, 44 + panel_h * len(LOADED) + 40), BG)
    d = ImageDraw.Draw(im)
    d.text((10, 10), title, fill=FG, font=big)
    y = 44
    for rid, run in LOADED.items():
        rows = mod.LAST[rid]
        t0, t1 = run.ctx.first_frame - 300_000, max([run.tr.t1] + [g.end for g in run.games]) + 60_000
        sx = lambda t: X0 + (X1 - X0) * (min(max(t, t0), t1) - t0) / (t1 - t0)  # noqa: E731
        d.text((10, y), f"{rid}   ({(t1 - t0) / 3.6e6:.1f} h shown; blue Quality Game, amber Outer Wilds; capture stop = white tick)", fill=FG, font=font)
        yy = y + 22
        d.line((sx(run.ctx.t1), yy, sx(run.ctx.t1), yy + len(lanes) * 20), fill=FG)
        today = ge.replay(run.ctx)
        for li, lane in enumerate(lanes):
            ly = yy + li * 20
            d.text((10, ly), lane, fill=DIM, font=font)
            d.line((X0, ly + 8, X1, ly + 8), fill=GRID)
            spans = []
            if lane == "truth lifetime":
                spans = [(g.m6_start, g.end, COL[g.process], g.process) for g in run.games]
            elif lane == "P1 rows":
                spans = [(x.start, x.end, COL.get(x.process, COL["gap"]), x.process) for x in rows]
            elif lane == "in front (ledger)":
                spans = [(a, b, COL[x.inst.exe], x.inst.exe) for x in rows for a, b in x.inst.front if a < x.end and b > x.start]
            elif lane == "gaming_now (P1)":
                spans = [(a, b + 5000, COL["mode"], x.inst.exe) for x in rows for a, b in x.inst.front if a < x.end and b > x.start]
            elif lane == "today rows":
                spans = [(s, e, COL.get(p, COL["today"]), p) for p, _w, s, e in today]
            else:
                spans = [(a, b, COL["gap"], "") for a, b, _r in run.ctx.gaps]
            for k, (a, b, col, p) in enumerate(spans):
                top, bot = (ly + 1, ly + 8) if p == "qualitygame.exe" else ((ly + 9, ly + 15) if p == "outerwilds.exe" else (ly + 2, ly + 14))
                xa, xb = sx(a), max(sx(b), sx(a) + 1)
                d.rectangle((xa, top, xb, bot), fill=col, outline=BG if lane in ("P1 rows", "today rows") else None)
        y += panel_h
    d.text((10, y), "P1 rows span process lifetimes (split only by the Pause); game mode follows the front ledger, like today's rows.", fill=DIM, font=font)
    im.save(path)


def fig_videos(path: Path, mod, title: str) -> None:
    from PIL import Image, ImageDraw

    font, big = _font()
    W, X0, X1 = 1600, 170, 1580
    lanes = ("truth plays", "media rows", "P1 chains", "today episodes", "no-recording gaps")
    panel_h = 24 + len(lanes) * 20 + 14
    im = Image.new("RGB", (W, 44 + panel_h * len(LOADED) + 40), BG)
    d = ImageDraw.Draw(im)
    d.text((10, 10), title, fill=FG, font=big)
    y = 44
    from akashic.pipeline.episodes import MIN_PLAYED_MS

    for rid, run in LOADED.items():
        media = [mod._Media(*row, None, None, None) for row in run.shadow.execute(
            "SELECT id, app, title, artist, url, start_ms, end_ms, played_ms FROM media_sessions WHERE closed = 1 ORDER BY start_ms")]
        chains = [c for c in mod.media_chains(media, mod._gaps(run.shadow), mod.CHAIN_P_MS, mod._pauses(run.shadow) if mod.PAUSE_SPLITS else ()) if sum(x.played for x in c) >= MIN_PLAYED_MS]
        ts = [v.start for v in run.videos] + [m.start for m in media]
        te = [v.end for v in run.videos] + [m.end for m in media]
        t0, t1 = min(ts) - 120_000, max(te) + 120_000
        sx = lambda t: X0 + (X1 - X0) * (min(max(t, t0), t1) - t0) / (t1 - t0)  # noqa: E731
        d.text((10, y), f"{rid}   media rows {len(media)} -> chains {len(chains)}   ({(t1 - t0) / 3.6e6:.1f} h shown)", fill=FG, font=font)
        yy = y + 22
        for li, lane in enumerate(lanes):
            ly = yy + li * 20
            d.text((10, ly), lane, fill=DIM, font=font)
            d.line((X0, ly + 8, X1, ly + 8), fill=GRID)
            if lane == "truth plays":
                spans = [(a, b, COL["truth"]) for v in run.videos for a, b in v.stretches]
            elif lane == "media rows":
                spans = [(m.start, m.end, COL["row_a"] if i % 2 == 0 else COL["row_b"]) for i, m in enumerate(media)]
            elif lane == "P1 chains":
                spans = [(c[0].start, max(x.end for x in c), COL["chain"]) for c in chains]
            elif lane == "today episodes":
                spans = [(m.start, m.end, COL["base"]) for m in media if m.played >= MIN_PLAYED_MS]
            else:
                spans = [(a, b, COL["gap"]) for a, b, _r in run.ctx.gaps]
            for k, (a, b, col) in enumerate(spans):
                d.rectangle((sx(a), ly + 2, max(sx(b), sx(a) + 1), ly + 14), fill=col, outline=BG if k % 2 else None)
        y += panel_h
    d.text((10, y), "Each chain is one video episode; today builds one episode per media row that played >= 60 s.", fill=DIM, font=font)
    im.save(path)


def main() -> None:
    cand = Path(sys.argv[1]).resolve()
    run_ids = [a for a in sys.argv[2:] if not a.startswith("--")]
    sys.argv = [str(HARNESS / "harness.py"), str(cand), *run_ids]
    harness.main()
    mod = MODS[str(cand)]
    ex = extras(cand)
    out = cand.parent
    mj = json.loads((out / "metrics.json").read_text(encoding="utf-8"))
    mj["extras"] = ex
    ge.write_json(out / "metrics.json", mj)
    a = ex["aggregate"]
    line = (out / "metrics.txt").read_text(encoding="utf-8").strip()
    line += (f" | active err med={a['active_err_med_s']:.1f}s max={a['active_err_max_s']:.1f}s"
             f" gaming_now={a['gaming_now_h']:.2f}h today_mode={a['today_mode_h']:.2f}h differ={a['mode_differ_s']:.0f}s"
             f" | chains P={a['chain_p_s']:.0f}s media_rows={a['media_rows']} chains={a['chains_kept']}"
             f" rows/chain max={a['rows_per_chain_max']} chains/truth video min={a['chains_per_truth_video_min']} max={a['chains_per_truth_video_max']}"
             f" | changed parts max={a['changed_ms_per_day_max']:.1f} ms/day (media {a['media_ms_per_day_max']:.1f} games {a['games_ms_per_day_max']:.2f}"
             f" app_runs {a['app_runs_ms_per_day_max']:.1f}; conversations unchanged {a['conversations_ms_per_day_max']:.1f})"
             f" | artifact-free: rows/truth assigned mean={a['rows_per_truth_assigned_mean']:.2f} max={a['rows_per_truth_assigned_max']}"
             f" M6.1 end max (truth end clamped to capture stop)={a['m6_end_clamped_max_s']:.1f}s")
    cs = clipped_scoring(mod)
    mj["clipped_split"] = cs
    ge.write_json(out / "metrics.json", mj)
    c = cs["aggregate"]
    line += (f" || CLIPPED+SPLIT truth (recorded span, split at stored Pause; pieces under 60 s video / 120 s game dropped):"
             f" rows/truth mean={c['rows_per_truth_mean']:.2f} max={c['rows_per_truth_max']}"
             f" M6.1 start max={c['m6_start_max_s']:.1f}s end max={c['m6_end_max_s']:.1f}s"
             f" M11.1 F1 video min={c['f1_video_min']:.2f} game min={c['f1_game_min']:.2f}"
             f" ({' '.join(f'{r[-4:]}:{v["f1_video"]:.2f}/{v["f1_game"]:.2f}' for r, v in cs['runs'].items())})"
             f" M11.2 min={c['title_acc_min']:.2f}")
    (out / "metrics.txt").write_text(line + "\n", encoding="utf-8")
    print(line)
    if FIGS:
        fig_games(out / "fig-game-timeline.png", mod, f"{mod.NAME}: game rows vs truth lifetimes, front ledger and game mode")
        fig_videos(out / "fig-video-chains.png", mod, f"{mod.NAME}: media rows, viewing chains (P = {a['chain_p_s']:.0f} s) and truth plays")


FIGS = "--figs" in sys.argv
if __name__ == "__main__":
    main()
