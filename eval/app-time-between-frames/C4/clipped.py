"""C4 extras (numbers only): clipped M5.3 per run, channel ablations on the synthetic timelines, and
the derivation cost on the day run with a dense simulated idle record (so V/S/G are read).

    uv run python eval/app-time-between-frames/C4/clipped.py

Clipped M5.3: per truth run (non-foreign), the time of same-app bands inside [t0 - 5 s, t1 + 5 s]
(same-app bands merged), |that - truth duration|, summed / total truth time. score.py untouched.
"""

from __future__ import annotations

import importlib.util
import json
import statistics
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "harness"))
import core  # noqa: E402
from core import Ctx, install_sim, merge, overlap_ms  # noqa: E402

import score  # noqa: E402

RUNS = ["20261118-smoke-s11-target-2220", "20261118-smoke-s11-target-2241", "20261119-standard-s21-target-0315",
        "20261119-standard-s21-target-1818", "20261119-day-s31-target-0547"]
PAD = 5_000
DAY_MS = 86_400_000


def load(name):
    spec = importlib.util.spec_from_file_location(f"x_{name}", HERE / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


c4 = load("c4")
base = core.load(HERE.parent / "baseline" / "baseline.py")
CANDS = {"baseline": base.bands, "c4": c4.bands, "c4_t300": c4.make(t_ms=300_000)}


def clipped_err(tr, bands):
    by_app: dict[str, list] = {}
    for x in bands:
        by_app.setdefault((x.process or "").lower(), []).append((x.start_ms, x.end_ms))
    by_app = {k: merge(v) for k, v in by_app.items()}
    err = tot = 0
    for r in tr.fg_runs():
        if r["kind"] == "foreign":
            continue
        d = r["t1"] - r["t0"]
        got = overlap_ms([(r["t0"] - PAD, r["t1"] + PAD)], by_app.get((r["exe"] or "").lower(), []))
        err += abs(got - d)
        tot += d
    return err / tot


def timed(fn, conn, a, b, now):
    fn(conn, a, b, now, Ctx(conn, a, b, now))
    ts = []
    for _ in range(9):
        t = time.perf_counter()
        fn(conn, a, b, now, Ctx(conn, a, b, now))
        ts.append((time.perf_counter() - t) * 1000)
    return statistics.median(ts)


def extra_scenarios(synthetic):
    """C4's own TEST cases the harness lacks, built with the harness's Syn (not edited)."""
    from synthetic import BROWSER, DISCORD, HWND, S, Syn, at
    HWND.setdefault(DISCORD, 104)
    out = []

    x = Syn("x_voice_call_present", 21)  # Discord in front, static window, the user talks, no input
    x.active(at(0), at(300), BROWSER)
    x.switch(at(300), DISCORD)
    x.truth.append((at(300), at(1500), "present", DISCORD))
    x.no_input.append((at(300), at(1500)))
    for t in range(305, 1490, 25):
        stream, src = ("mic", []) if (t // 25) % 2 else ("system", [DISCORD.lower()])
        x.conn.execute("INSERT INTO audio_segments (stream, start_ms, end_ms, speech_ms, sources, status) VALUES (?, ?, ?, 12000, ?, 'done')",
                       (stream, at(t), at(t + 15), json.dumps(src)))
    x._frame(at(1501), 1, DISCORD, "input")
    x.active(at(1500), at(1800), DISCORD, switch=False)
    out.append(x)

    x = Syn("x_podcast_static_away", 22)  # a spoken podcast in the focused browser, static picture, user away
    x.active(at(0), at(300), BROWSER)
    x.truth.append((at(300), at(1500), "away", None))
    x.no_input.append((at(300), at(1500)))
    for t in range(305, 1490, 30):
        x.conn.execute("INSERT INTO audio_segments (stream, start_ms, end_ms, speech_ms, sources, status) VALUES ('system', ?, ?, 15000, ?, 'done')",
                       (at(t), at(t + 20), json.dumps([BROWSER.lower()])))
    x._frame(at(1501), 1, BROWSER, "input")
    x.active(at(1500), at(1800), BROWSER, switch=False)
    out.append(x)

    x = Syn("x_autoplay_left_4h", 23)  # a focused video playlist left playing 4 h after the user left: expected over-count <= 3 h
    x.active(at(0), at(300), BROWSER)
    end = 300 + 4 * 3600
    x.truth.append((at(300), at(end), "away", None))
    x.no_input.append((at(300), at(end)))
    for t in range(305, end, 5):
        x._frame(at(t), 1, BROWSER, "idle")
    for t in range(310, end - 30, 30):
        x.conn.execute("INSERT INTO audio_segments (stream, start_ms, end_ms, speech_ms, sources, status) VALUES ('system', ?, ?, 15000, ?, 'done')",
                       (at(t), at(t + 20), json.dumps([BROWSER.lower()])))
    x.active(at(end), at(end + 300), BROWSER, switch=False)
    out.append(x)

    res = []
    for x in out:
        conn, a, b, n = x.finish()
        res.append({"name": x.name, "conn": conn, "start": a, "end": b, "now": n, "truth": x.truth})
    return res


def main():
    out = {"clipped_M5.3": {}, "cost_day_dense_idle": {}, "synthetic_ablation": {}}
    now = int(time.time() * 1000)
    for rid in RUNS:
        tr = score.Truth(score.RESULTS / rid)
        _, _, conn, _ = score.open_store(rid)
        install_sim(conn, [], [])
        res = {}
        for name, fn in CANDS.items():
            res[name] = round(clipped_err(tr, fn(conn, tr.t0, tr.t1, now, Ctx(conn, tr.t0, tr.t1, now))), 3)
        out["clipped_M5.3"][rid] = res
        print(rid[-14:], "clipped M5.3", res)
        if "day" in rid:
            span = tr.t1 - tr.t0
            for label, every_s, dur_s in (("none", None, None), ("240s_every_15min", 900, 240), ("200s_every_4min", 240, 200)):
                idle = [] if every_s is None else [(t, t + dur_s * 1000) for t in range(tr.t0, tr.t1, every_s * 1000)]
                install_sim(conn, [], idle)
                ms = timed(c4.bands, conn, tr.t0, tr.t1, now)
                # the same dense record forces V/S reads: count how much band time it removes (numbers only)
                bands = c4.bands(conn, tr.t0, tr.t1, now, Ctx(conn, tr.t0, tr.t1, now))
                kept = sum(x.end_ms - x.start_ms for x in bands) / 1000
                out["cost_day_dense_idle"][label] = {"idle_rows": len(idle), "ms": round(ms, 2), "ms_per_day": round(ms * DAY_MS / span, 1),
                                                     "band_s": round(kept)}
                print("  day cost", label, out["cost_day_dense_idle"][label])
        conn.close()

    import synthetic
    variants = {"c4": c4.bands, "no_V": c4.make(use_v=False), "no_S": c4.make(use_s=False), "no_G": c4.make(use_game=False),
                "S_unpaired": c4.make(s_paired=False), "K_only": c4.make(use_v=False, use_s=False, use_game=False)}
    for sc in synthetic.build_all() + extra_scenarios(synthetic):
        conn, a, b, n = sc["conn"], sc["start"], sc["end"], sc["now"]
        row = {}
        for name, fn in variants.items():
            bands = fn(conn, a, b, n, Ctx(conn, a, b, n))
            spans = [(x.start_ms, x.end_ms) for x in bands]
            away = overlap_ms(spans, [(t0, t1) for t0, t1, lab, _ in sc["truth"] if lab != "present"])
            missed = 0
            for t0, t1, lab, proc in sc["truth"]:
                if lab == "present":
                    missed += (t1 - t0) - overlap_ms([(t0, t1)], [s for s, x in zip(spans, bands) if (x.process or "").lower() == proc.lower()])
            row[name] = (round(away / 1000, 1), round(missed / 1000, 1))
        out["synthetic_ablation"][sc["name"]] = row
        print(f"  syn {sc['name']:<17}", "  ".join(f"{k}: away+{v[0]:g} missed {v[1]:g}" for k, v in row.items()))
        conn.close()
    (HERE / "extras.json").write_text(json.dumps(out, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
