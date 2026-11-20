"""Store simulation over the live runs (run_live.py output), scored against the page's event log.

    .venv\\Scripts\\python.exe live\\analyze.py live\\edge live\\chrome live\\term  [--out live\\results.json]

Per run: replays the reduced frames in check order through baseline (today), C2, C3 (taskbar
variant, own 4x2 reduction) and C5, with ref = last stored frame and prev = previous check (as
harness/live_replay.py), monitor "1440p". Scores:
  * recall per event (reply by size and theme, badge_on, badge_off): k0 = first check whose frame
    arrived > 30 ms after the page logged the event; hit@1 = a store at k0, hit@2 = by k0+1.
    An event counts only when checks k0-1..k0+1 are clean (no phase/theme/reset/focus change,
    and, in the events phase, the window focused at all three).
  * useless stores per hour per nuisance phase (caret, ecaret, clock, spinner, idle, bar, block):
    every store at a clean check is useless (nothing else changes in those phases); and in the
    quiet/events phases, stores not attributable to an event (the first store at or after k0).
  * decide time per call on these real frames.
  * WGC REPORT_ONLY dirty rects per check, numbers only.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import zlib
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
STUDY = HERE.parent
sys.path.insert(0, str(STUDY / "harness"))
from hcommon import Candidate, baseline, readonly  # noqa: E402

CANDS = {"baseline": None, "C2": STUDY / "C2" / "c2.py", "C3": STUDY / "C3" / "c3_taskbar.py", "C5": STUDY / "C5" / "c5.py",
         "C5fix": HERE / "c5_livefix.py"}
TRANSITIONS = {"phase", "theme", "reset", "win_blur", "win_focus", "load", "done"}
FOCUS_PHASES = {"caret", "bar", "block"}  # the events phase is scored with and without focus (caret present or not)
NUISANCE = ("idle", "caret", "ecaret", "clock", "spinner", "bar", "block")
EVENT_KINDS = ("reply", "badge_on", "badge_off")
ANIMATED = {"caret", "ecaret", "clock", "spinner", "events", "bar", "block"}
RENDER_S = 0.03
ATTRIB_MAX = 6  # checks after k0 within which the first store is the event's (late) store
_OFF = HERE / "monitor_off.json"
OFF_INTERVALS = json.loads(_OFF.read_text(encoding="utf-8"))["intervals"] if _OFF.exists() else []


def load(run: Path):
    info = json.loads((run / "info.json").read_text(encoding="utf-8"))
    checks = [json.loads(x) for x in (run / "checks.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
    events = [json.loads(x) for x in (run / "events.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
    for e in events:
        e["ts"] = e["t"] / 1000.0
    events.sort(key=lambda e: e["ts"])
    raw = (run / "frames.bin").read_bytes()
    frames = []
    for c in checks:
        if c.get("frame") is None:
            continue
        pos, n0, n1 = c["blob"]
        (s0, s1) = c["shape"]
        if pos + n0 + n1 > len(raw):  # a copy taken while frames.bin was still being written
            break
        g = np.frombuffer(zlib.decompress(raw[pos:pos + n0]), dtype=np.uint8).reshape(s0)
        g3 = np.frombuffer(zlib.decompress(raw[pos + n0:pos + n0 + n1]), dtype=np.uint8).reshape(s1)
        frames.append((c, readonly(g), readonly(g3)))
    return info, frames, events


def simulate(frames, cands: dict[str, Candidate]):
    out = {}
    for name, c in cands.items():
        state = c.make_state()
        ref = prev = None
        stores = np.zeros(len(frames), dtype=bool)
        ns = []
        for k, (_c, g, g3) in enumerate(frames):
            cur = g3 if c.overrides_reduce else g
            if ref is None:
                ref = prev = cur
                stores[k] = True
                continue
            t = time.perf_counter_ns()
            fire = c.decide(state, ref, cur, prev, "1440p", now=k * 5.0)
            ns.append(time.perf_counter_ns() - t)
            if fire:
                ref = cur
                stores[k] = True
            prev = cur
        out[name] = (stores, np.array(ns, dtype=np.int64))
    return out


def annotate(info, frames, events):
    """Per check: phase, theme, transition (excluded), focused (page truth / fg check)."""
    n = len(frames)
    at = np.array([c["frame"]["at"] for c, _g, _g3 in frames])
    fg = np.array([c["fg_ok"] for c, _g, _g3 in frames])
    phase = [None] * n
    light = np.zeros(n, dtype=bool)
    focused = fg.copy()
    trans = np.zeros(n, dtype=bool)
    # page focus timeline (browsers): state at each frame time
    fevents = [(e["ts"], e["kind"] == "win_focus") for e in events if e["kind"] in ("win_focus", "win_blur")]
    pevents = [(e["ts"], e["name"]) for e in events if e["kind"] == "phase"]
    tevents = [(e["ts"], e.get("light", False)) for e in events if e["kind"] in ("theme", "phase")]
    for k in range(n):
        p = [nm for ts, nm in pevents if ts <= at[k]]
        phase[k] = p[-1] if p else None
        th = [lt if True else None for ts, lt in tevents if ts <= at[k]]
        light[k] = bool(th[-1]) if th else False
        if fevents:
            fs = [f for ts, f in fevents if ts <= at[k]]
            if fs:
                focused[k] = fs[-1] and fg[k]
        lo = at[k - 1] if k else -np.inf
        trans[k] = any(lo < e["ts"] <= at[k] for e in events if e["kind"] in TRANSITIONS)
        if k and fg[k] != fg[k - 1]:
            trans[k] = True
    excl = trans.copy()
    excl[1:] |= trans[:-1]  # the check after a transition too
    if n:
        excl[0] = True
    clean = ~excl
    for k in range(n):
        if phase[k] in FOCUS_PHASES and not (focused[k] and (k == 0 or focused[k - 1])):
            clean[k] = False
        if phase[k] is None or phase[k] == "done":
            clean[k] = False
    # stale frames: the window stopped presenting (seen in Chrome for minutes, probably occlusion
    # throttling while other agents moved windows): an animated phase with no frame for > 2.5 s, or
    # any phase where the page logged a change > 1.2 s before the check but the frame predates it
    tchk = np.array([c["t"] for c, _g, _g3 in frames])
    ets = np.array([e["ts"] for e in events])
    for k in range(n):
        age = tchk[k] - at[k]
        missed = bool(((ets > at[k]) & (ets < tchk[k] - 1.2)).any())
        if (phase[k] in ANIMATED and age > 2.5) or missed:
            clean[k] = False
            if k + 1 < n:
                clean[k + 1] = False
    # display off (monitor_off.json, from the System log): Chromium stops presenting and Windows
    # Terminal gives blank frames, so a check whose frame-to-check span touches an off interval
    # (+ 2 s settle) leaves the analysis, and so does the check after it. Browsers only: Windows
    # Terminal kept presenting fresh frames with the display off (term3, captured wholly with the
    # display off: frame age <= 1 s, 2-state blink, no blank frames); its blank frames in term and
    # term2 came from run_live.py's minimize/restore bug, so those runs are invalid anyway.
    off = np.zeros(n, dtype=bool)
    for s, e in (OFF_INTERVALS if info.get("app") != "term" else []):
        e = np.inf if e is None else e + 2.0
        off |= (at <= e) & (tchk >= s)
    off[1:] |= off[:-1].copy()
    clean &= ~off
    annotate.off_checks = int(off.sum())
    ends = [e["ts"] for e in events if e["kind"] == "done"]
    if ends:
        clean &= at < ends[0]
    return at, np.array(phase, dtype=object), light, focused, clean


def score(info, frames, events, sims):
    at, phase, light, focused, clean = annotate(info, frames, events)
    n = len(frames)
    res = {"checks": n, "clean_checks": int(clean.sum()), "fg_ok_checks": int(sum(c["fg_ok"] for c, _g, _g3 in frames)),
           "refocus_count": int(sum(c.get("refocused", False) for c, _g, _g3 in frames)),
           "monitor_off_checks": annotate.off_checks}
    # changes between consecutive checks (diagnostic: is the caret really blinking?)
    moved16 = np.zeros(n, dtype=np.int64)
    moved6 = np.zeros(n, dtype=np.int64)
    for k in range(1, n):
        a, b = frames[k - 1][1], frames[k][1]
        d = np.maximum(a, b) - np.minimum(a, b)
        moved16[k] = int(np.count_nonzero(d > 16))
        moved6[k] = int(np.count_nonzero(d > 6))
    # capture glitches: in a nuisance-only phase nothing large should change; Windows Terminal gave
    # WGC whole-window blank or tab-only frames now and then (2026-11-20). Such a check and the
    # one after it (the recovery) leave the nuisance rates and are counted on their own.
    glitch = np.zeros(n, dtype=bool)
    for k in range(1, n):
        if phase[k] in NUISANCE and moved16[k] > 2000:
            glitch[k] = True
    res["glitch_checks"] = {ph: int((glitch & clean & (phase == ph)).sum()) for ph in NUISANCE if (phase == ph).any()}
    clean_n = clean & ~glitch
    clean_n[1:] &= ~glitch[:-1]
    # events -> k0: the first check whose frame shows the mark (the mark's box changed against the
    # previous check), among the first two checks whose frame arrived after the page logged it
    load_ev = next((e for e in events if e["kind"] == "load" and "w" in e), None)
    evs = []
    unseen = 0
    for e in events:
        if e["kind"] not in EVENT_KINDS:
            continue
        later = np.flatnonzero(at > e["ts"])
        if later.size == 0 or later[0] == 0:
            continue
        k0 = int(later[0])
        box = _mark_box(frames[k0][0], e.get("rect"), load_ev)
        seen = False
        if box is not None:
            for k in (k0, k0 + 1):
                if k < n and _box_changed(frames[k - 1][1], frames[k][1], box):
                    k0, seen = k, True
                    break
            if not seen:
                unseen += 1
                k0 = min(k0 + 1, n - 1)
        amb = False
        ph = phase[k0]
        ok = 1 <= k0 < n - 1 and all(clean[k] for k in (k0 - 1, k0, k0 + 1)) and ph in ("quiet", "events")
        evs.append({"kind": e["kind"], "px": e.get("px"), "word": e.get("word"), "slot": e.get("slot"),
                    "row": e.get("row"), "col": e.get("col"), "rect": e.get("rect"), "ts": e["ts"], "k0": k0,
                    "amb": bool(amb), "phase": ph, "light": bool(light[k0]), "valid": bool(ok),
                    "focused": bool(focused[k0 - 1] and focused[k0]), "seen": seen})
    k0s = [ev["k0"] for ev in evs]
    prev_reply = None
    for ev in evs:
        ev["shared"] = k0s.count(ev["k0"]) > 1
        if ev["kind"] == "reply":
            if prev_reply is not None and prev_reply["phase"] == ev["phase"]:
                ev["gap_checks"] = ev["k0"] - prev_reply["k0"]
                ev["adjacent"] = ev["col"] == prev_reply["col"] and ev["row"] == prev_reply["row"] + 1
            else:
                ev["gap_checks"], ev["adjacent"] = None, False
            prev_reply = ev
    per_cand = {}
    for name, (stores, ns) in sims.items():
        cand = {}
        hits = []
        attributed = np.zeros(n, dtype=bool)
        for ev in evs:
            k0 = ev["k0"]
            h1 = bool(stores[k0] or (ev["amb"] and stores[k0 - 1]))
            h2 = h1 or bool(k0 + 1 < n and stores[k0 + 1])
            nxt = np.flatnonzero(stores[k0:min(n, k0 + ATTRIB_MAX + 1)])
            delay = int(nxt[0]) if nxt.size else None
            if delay is not None:
                attributed[k0 + delay] = True
            if ev["amb"] and stores[k0 - 1]:
                attributed[k0 - 1] = True
                delay = 0
            hits.append({"h1": h1, "h2": h2, "delay": delay})
        # recall tables
        def rec(pred):
            sel = [(ev, h) for ev, h in zip(evs, hits) if ev["valid"] and not ev["shared"] and pred(ev)]
            m1 = sum(not h["h1"] for _e, h in sel)
            m2 = sum(not h["h2"] for _e, h in sel)
            return {"n": len(sel), "miss1": m1, "miss2": m2,
                    "recall1": round(1 - m1 / len(sel), 4) if sel else None,
                    "recall2": round(1 - m2 / len(sel), 4) if sel else None}

        recall = {}
        for ph in ("quiet", "events", "events_caret", None):
            key = ph or "all"
            inph = (lambda ev, ph=ph: ph is None or ev["phase"] == ph or (ph == "events_caret" and ev["phase"] == "events" and ev["focused"]))
            r = {"reply": rec(lambda ev, f=inph: f(ev) and ev["kind"] == "reply"),
                 "badge_on": rec(lambda ev, f=inph: f(ev) and ev["kind"] == "badge_on"),
                 "badge_off": rec(lambda ev, f=inph: f(ev) and ev["kind"] == "badge_off")}
            for px in (13, 14, 16):
                r[f"reply_{px}px"] = rec(lambda ev, f=inph, px=px: f(ev) and ev["kind"] == "reply" and ev["px"] == px)
            for th in (False, True):
                r["reply_" + ("light" if th else "dark")] = rec(lambda ev, f=inph, th=th: f(ev) and ev["kind"] == "reply" and ev["light"] == th)
            r["reply_adjacent_recent"] = rec(lambda ev, f=inph: f(ev) and ev["kind"] == "reply" and ev.get("adjacent")
                                             and ev.get("gap_checks") is not None and ev["gap_checks"] <= 2)
            recall[key] = r
        cand["recall"] = recall
        cand["misses1"] = [{"kind": ev["kind"], "px": ev["px"], "word": ev["word"], "phase": ev["phase"], "light": ev["light"],
                            "adjacent": ev.get("adjacent"), "gap_checks": ev.get("gap_checks"), "delay": h["delay"]}
                           for ev, h in zip(evs, hits) if ev["valid"] and not ev["shared"] and not h["h1"]]
        # useless stores per hour
        useless = {}
        for ph in NUISANCE + ("quiet", "events", "events_caret"):
            base_sel = clean_n if ph in NUISANCE else clean
            sel = base_sel & (phase == ph) if ph != "events_caret" else clean & (phase == "events") & focused
            nv = int(sel.sum())
            if not nv:
                continue
            if ph in NUISANCE:
                bad = stores & sel
            else:
                bad = stores & sel & ~attributed
            row = {"clean_checks": nv, "hours": round(nv * 5 / 3600, 4), "stores": int(bad.sum()),
                   "per_hour": round(int(bad.sum()) / (nv * 5 / 3600), 1)}
            for th in (False, True):
                s2 = sel & (light == th)
                if s2.sum():
                    row["per_hour_" + ("light" if th else "dark")] = round(int((bad & s2).sum()) / (int(s2.sum()) * 5 / 3600), 1)
            useless[ph] = row
        cand["useless"] = useless
        nontriv = np.array([not np.array_equal(frames[k][1], frames[k - 1][1]) for k in range(1, n)]) if n > 1 else np.array([])
        cand["decide_ms"] = {"mean": round(float(ns.mean()) / 1e6, 4), "p50": round(float(np.median(ns)) / 1e6, 4),
                             "p95": round(float(np.percentile(ns, 95)) / 1e6, 4), "max": round(float(ns.max()) / 1e6, 4),
                             "mean_changed": round(float(ns[nontriv].mean()) / 1e6, 4) if nontriv.any() else None,
                             "calls": int(ns.size)}
        cand["stores_total"] = int(stores.sum())
        per_cand[name] = cand
    res["candidates"] = per_cand
    res["events"] = {"total": len(evs), "valid": sum(ev["valid"] for ev in evs),
                     "valid_alone": sum(ev["valid"] and not ev["shared"] for ev in evs),
                     "by_kind_valid": {k: sum(ev["valid"] and not ev["shared"] and ev["kind"] == k for ev in evs) for k in EVENT_KINDS},
                     "unseen": unseen, "valid_focused": sum(ev["valid"] and ev["focused"] for ev in evs)}
    # nuisance diagnostics: changed checks and moved pixels per phase
    diag = {}
    for ph in NUISANCE + ("quiet", "events"):
        sel = (clean_n if ph in NUISANCE else clean) & (phase == ph)
        if not sel.any():
            continue
        m = moved16[sel]
        diag[ph] = {"clean_checks": int(sel.sum()), "changed_frac": round(float((moved6[sel] > 0).mean()), 3),
                    "moved16_median_when_changed": int(np.median(m[m > 0])) if (m > 0).any() else 0,
                    "moved16_max": int(m.max()), "moved6_max": int(moved6[sel].max())}
    res["nuisance_diag"] = diag
    res["sig_ms_p50"] = round(float(np.median([c["t_sig_ms"] for c, _g, _g3 in frames])), 3)
    res["dirty"] = dirty_numbers(frames, evs, events, phase, clean_n, at)
    return res


def _mark_box(check: dict, rect, load_ev):
    """The mark's box in reduced (1/4) canvas pixels, grown by 1, or None."""
    if not rect or not load_ev or check.get("frame") is None:
        return None
    fw, fh = check["frame"]["size"]
    px, py = check["frame"]["pos"]
    ox = (fw - load_ev["w"]) // 2
    oy = fh - load_ev["h"] - ox
    x, y, w, h = rect
    x0, y0 = int((px + ox + x) // 4) - 1, int((py + oy + y) // 4) - 1
    x1, y1 = int((px + ox + x + w) // 4) + 2, int((py + oy + y + h) // 4) + 2
    return max(0, y0), y1, max(0, x0), x1


def _box_changed(a, b, box) -> bool:
    y0, y1, x0, x1 = box
    pa, pb = a[y0:y1, x0:x1], b[y0:y1, x0:x1]
    return bool(np.count_nonzero((np.maximum(pa, pb) - np.minimum(pa, pb)) > 6))


def _inside(r, u, pad=2):
    return u is not None and u[0] - pad <= r[0] and u[1] - pad <= r[1] and r[0] + r[2] <= u[0] + u[2] + pad and r[1] + r[3] <= u[1] + u[3] + pad


def dirty_numbers(frames, evs, events, phase, clean, at):
    """Union of WGC dirty rects per check (window px) for caret-only, clock, spinner, ecaret, bar,
    block checks and for quiet-phase reply/badge checks (k0)."""
    load = next((e for e in events if e["kind"] == "load" and "w" in e), None)
    out = {}
    for ph in NUISANCE:
        sel = [k for k in range(len(frames)) if clean[k] and phase[k] == ph]
        if not sel:
            continue
        ds = [frames[k][0]["dirty"] for k in sel]
        areas = np.array([d["area"] for d in ds])
        uareas = np.array([0 if d["union"] is None else d["union"][2] * d["union"][3] for d in ds])
        unions = [tuple(d["union"]) for d in ds if d["union"] is not None]
        common = max(set(unions), key=unions.count) if unions else None
        out[ph] = {"checks": len(sel), "frames_per_check_p50": float(np.median([d["frames"] for d in ds])),
                   "union_area_p50": int(np.median(uareas)), "union_area_max": int(uareas.max()),
                   "rect_area_sum_p50": int(np.median(areas)), "most_common_union": common,
                   "checks_with_no_dirty": int(sum(d["union"] is None for d in ds))}
    rows = []
    for ev in evs:
        if not ev["valid"] or ev["shared"] or ev["phase"] != "quiet":
            continue
        k0 = ev["k0"]
        c = frames[k0][0]
        d = c["dirty"]
        u = d["union"]
        rect = None
        if ev.get("rect") and load:
            fw, fh = c["frame"]["size"]
            ox = (fw - load["w"]) // 2
            oy = fh - load["h"] - ox
            x, y, w, h = ev["rect"]
            rect = [x + ox, y + oy, w, h]
        rows.append({"kind": ev["kind"], "px": ev["px"], "union": u, "area": d["area"], "rects": d["rects"],
                     "contains_mark": _inside(rect, u) if rect else None,
                     "union_over_mark": round(u[2] * u[3] / (rect[2] * rect[3]), 1) if (u and rect) else None})
    if rows:
        ua = np.array([r["union"][2] * r["union"][3] if r["union"] else 0 for r in rows])
        out["quiet_marks"] = {"n": len(rows), "union_area_p50": int(np.median(ua)), "union_area_max": int(ua.max()),
                              "contains_mark": sum(bool(r["contains_mark"]) for r in rows),
                              "union_over_mark_p50": float(np.median([r["union_over_mark"] for r in rows if r["union_over_mark"]] or [0])),
                              "no_dirty": int(sum(r["union"] is None for r in rows)),
                              "by_kind": {k: int(sum(r["kind"] == k for r in rows)) for k in EVENT_KINDS},
                              "examples": rows[:6]}
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("runs", nargs="+", type=Path)
    ap.add_argument("--out", type=Path)
    a = ap.parse_args()
    cands = {name: (baseline() if p is None else Candidate(p)) for name, p in CANDS.items()}
    allres = {}
    for run in a.runs:
        info, frames, events = load(run)
        sims = simulate(frames, cands)
        r = score(info, frames, events, sims)
        r["info"] = {k: info[k] for k in ("app", "plan", "seed", "window_req", "frame_interval_ms", "interval_s", "focus_on_launch", "window_closed") if k in info}
        allres[run.name] = r
        print(f"== {run.name}: checks={r['checks']} clean={r['clean_checks']} fg_ok={r['fg_ok_checks']} events={r['events']}")
        for name, c in r["candidates"].items():
            ra = c["recall"]["all"]
            print(f"  {name:<8} reply r1={ra['reply']['recall1']} ({ra['reply']['miss1']}/{ra['reply']['n']}) r2={ra['reply']['recall2']} "
                  f"badge_on r1={ra['badge_on']['recall1']} ({ra['badge_on']['miss1']}/{ra['badge_on']['n']}) "
                  f"badge_off r1={ra['badge_off']['recall1']} | useless/h " +
                  " ".join(f"{k}={v['per_hour']}" for k, v in c["useless"].items()) + f" | decide p50={c['decide_ms']['p50']} ms")
    if a.out:
        a.out.write_text(json.dumps(allres, indent=1, default=lambda o: o.item() if hasattr(o, "item") else str(o)), encoding="utf-8")


if __name__ == "__main__":
    main()
