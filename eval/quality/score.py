"""Scorer: reads scenario.json, truth/*.jsonl and the run's store (read-only) and writes scores.json,
coverage.json and summary.md. Outputs hold numbers and harness-generated ids only.

Choices fixed where scope.md left them open: text folding is NFC + casefold + punctuation stripped +
whitespace collapsed (an accent-folded variant beside it); WER and CER are word and character
Levenshtein; a segment's reference is the truth utterances of its stream whose midpoint falls inside
it; truth speech spans use the speaker study's rule; episodes match at IoU >= 0.5; the focused row
matches within +-100 ms; fonts are bucketed in device px.
"""

from __future__ import annotations

import bisect
import io
import itertools
import json
import re
import statistics
import sys
import tempfile
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import numpy as np
from scipy.optimize import linear_sum_assignment  # a transitive dependency (librosa), already in the environment

import assets
import scenario as sc
import tags
from common import CANARY_RGB, CANARY_TEXT, LOCAL, RESULTS, dpapi_unwrap, load_toml, read_jsonl, sha256_file, write_json

FOCUS_TOL_MS = 100
IOU = 0.5
DWELL_MS = 5000
SHARED_PLAYERS = ("call", "own", "music", "elsewhere")  # the players scheduled from the drive start
COLLAR_MS = 250
CASES = ("calculator", "assistant", "steam", "elevated", "hung")
CASE_LABEL = {"calculator": "uwp", "assistant": "pwa", "steam": "launcher", "elevated": "elevated", "hung": "ghost"}
GAP_REASONS = {"lock": {"locked"}, "sleep": {"locked", "stopped", "stalled"}, "pause": {"paused"},
               "device_change": {"device_missing", "device_failed", "audio_unavailable"}}
MAX_TEXT_ROWS = 3000
MAX_CHANGES = 6000  # changes evaluated by M1.1; more are sampled evenly up front
LOSS_WINDOW_MS = 60_000
DEVICE_SETTLE_MS = 3000  # a default-output change is a non-recording period of this length from its notification
CANARY_PIXELS = 400
EPISODE_GAP_MS = 5 * 60_000  # what episodes.py chains over, and the shortest browsing and work runs it keeps
MIN_EPISODE_MS = {"browsing": 3 * 60_000, "work": 5 * 60_000}
FONT_BUCKETS = ((0, 11, "<=11"), (12, 15, "12-15"), (16, 99, ">=16"))


def nul(reason: str) -> dict:
    return {"value": None, "n": 0, "reason": reason}


def pctl(xs: list[float], q: float):
    return None if not xs else float(np.percentile(xs, q))


def summarize(xs: list[float]) -> dict:
    return {"n": len(xs), "p50": pctl(xs, 50), "p90": pctl(xs, 90), "max": max(xs) if xs else None} if xs else nul("no samples")


# --- text ---------------------------------------------------------------------------------


def fold(text: str, accents: bool = True) -> str:
    text = unicodedata.normalize("NFC", text).casefold()
    if not accents:
        text = "".join(c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn")
    text = "".join(c if not unicodedata.category(c).startswith("P") else " " for c in text)
    return " ".join(text.split())


def lev(a, b) -> int:
    prev = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        cur = [i]
        for j, y in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (x != y)))
        prev = cur
    return prev[-1]


def best_line_cer(ref: str, hyp_lines: list[str]) -> float:
    r = fold(ref)
    if not r:
        return 0.0
    return min((lev(r, h) / len(r) for h in hyp_lines), default=1.0)


# --- truth ---------------------------------------------------------------------------------


class Truth:
    def __init__(self, run: Path) -> None:
        self.scn = json.loads((run / "scenario.json").read_text(encoding="utf-8"))
        t = run / "truth"
        self.driver, self.os, self.pages = read_jsonl(t / "driver.jsonl"), read_jsonl(t / "os.jsonl"), read_jsonl(t / "pages.jsonl")
        self.guard, self.game = read_jsonl(t / "guard.jsonl"), read_jsonl(t / "game.jsonl")
        self.run_log = read_jsonl(t / "run.jsonl")
        self.machine = json.loads((run / "machine.json").read_text(encoding="utf-8")) if (run / "machine.json").is_file() else {"monitors": []}
        self.game_ref = read_jsonl(t / "game-ref.jsonl")
        self.players = {p.stem.removeprefix("player-"): read_jsonl(p) for p in t.glob("player-*.jsonl")}
        start = next((d for d in self.driver if d["action"] == "drive_start"), None)
        end = next((d for d in self.driver if d["action"] == "drive_end"), None)
        self.t0 = start["start_wall_ms"] if start else 0
        self.t1 = end["wall_ms"] if end else self.t0 + self.scn["duration_ms"]
        self.windows = {w["id"]: w for w in self.scn["windows"]}
        self.steps_by_id = {s["id"]: s for s in self.scn["steps"]}
        self.hwnd_to_window = {d["hwnd"]: d["window"] for d in self.driver if d.get("hwnd") and d.get("window")}
        self.manifest = json.loads((assets.ASSETS / "manifest.json").read_text(encoding="utf-8"))
        self.clips = {c["id"]: c for c in self.manifest["clips"]}
        self.step_start: dict[str, int] = {}
        for d in self.driver:
            # page_load is logged while the window is still being launched, before the step's own act
            if d.get("step") and d["action"] != "page_load":
                self.step_start.setdefault(d["step"], d["wall_ms"])
        focus_ids = [s["id"] for s in sorted((s for s in self.scn["steps"] if s["kind"] == "focus"), key=lambda s: s["t0_ms"])]
        self.next_focus_start = {a: self.step_start[b] for a, b in itertools.pairwise(focus_ids) if b in self.step_start}
        # how late each shared-schedule audio item started, by its planned time: a sleep or device stall delays what follows
        starts = sorted((e["planned_ms"], e["start_wall_ms"] - self.t0 - e["planned_ms"]) for label in SHARED_PLAYERS for e in self.players.get(label, [])
                        if e.get("type") == "start" and "planned_ms" in e)
        self.delay_from, self.delays = [p for p, _ in starts], [d for _, d in starts]

    def span(self, step: dict) -> tuple[int, int]:
        """Wall span of a step: from its first logged driver action, else its plan moved by the delay of
        the latest audio item started before it."""
        a = self.step_start.get(step["id"])
        if a is None:
            i = bisect.bisect_right(self.delay_from, step["t0_ms"]) - 1
            a = self.t0 + step["t0_ms"] + (self.delays[i] if i >= 0 else 0)
        return a, min(a + step["t1_ms"] - step["t0_ms"], self.next_focus_start.get(step["id"], float("inf")))

    def call_spans(self) -> dict[str, tuple[int, int]]:
        """Each call step is heard from its first to its last utterance."""
        heard = defaultdict(list)
        for u in self.utterances():
            if self.steps_by_id.get(u.get("step"), {}).get("kind") == "call":
                heard[u["step"]].append(u)
        return {sid: (min(u["t0"] for u in us), max(u["t1"] for u in us)) for sid, us in heard.items()}

    def fg_runs(self) -> list[dict]:
        """Foreground runs from the OS log (truth), each with the harness window id when known."""
        fgs = [e for e in self.os if e["t"] == "fg"]
        out = []
        for a, b in itertools.pairwise(fgs + [{"wall_ms": self.t1}]):
            if a["wall_ms"] >= self.t1 or b["wall_ms"] <= self.t0:
                continue
            wid = self.hwnd_to_window.get(a["hwnd"])
            app = self.windows[wid]["app"] if wid in self.windows else None
            out.append({"t0": max(a["wall_ms"], self.t0), "t1": min(b["wall_ms"], self.t1), "hwnd": a["hwnd"], "kind": a["kind"],
                        "exe": a.get("exe"), "window": wid, "app": app, "monitor": self.windows[wid]["monitor"] if wid in self.windows else None})
        return out

    def utterances(self) -> list[dict]:
        """Truth speech: every scheduled utterance with absolute spans, per stream."""
        items = {}
        for s in self.scn["steps"]:
            for u in s["params"].get("utterances", []):
                items[f"{s['id']}-{u['t_ms']}"] = {**u, "step": s["id"]}
        out = []
        for label, log in self.players.items():
            for e in log:
                u = items.get(e.get("id"))
                if e.get("type") != "start" or not u:
                    continue
                clip = self.clips[u["clip"]]
                start = e["start_wall_ms"]
                stream = {"step": u["step"], "stream": "mic" if u["stream"] == "own" else "system", "source": u["stream"], "lang": clip["lang"], "kind": clip.get("kind", "speech")}
                spans = [(start + round(a * 1000), start + round(b * 1000)) for a, b in clip["spans"]]
                if "parts" in clip:  # a mixed turn is two voices: each half is its own utterance
                    cut = start + round(clip["split_s"] * 1000)
                    for part, (lo, hi) in zip(clip["parts"], ((start, cut), (cut, start + round(clip["dur_s"] * 1000))), strict=True):
                        out.append({**stream, "speaker": part["speaker"], "text": part["text"], "t0": lo, "t1": hi,
                                    "spans": [(max(a, lo), min(b, hi)) for a, b in spans if max(a, lo) < min(b, hi)]})
                    continue
                out.append({**stream, "speaker": u["speaker"], "text": clip["text"], "t0": start, "t1": start + round(clip["dur_s"] * 1000), "spans": spans})
        out += self.video_utterances()
        return sorted(out, key=lambda u: u["t0"])

    def video_utterances(self) -> list[dict]:
        """Narrator speech of each video, for every stretch it was playing. A play ends at a pause or
        ended event; a page that is navigated away fires neither, so an open play ends at its last tick."""
        built = {e["video"]: e for e in self.pages if e.get("type") == "video_built"}
        out = []
        for vid, info in built.items():
            events = sorted((e for e in self.pages if e.get("type") == "media" and e["s"] is not None and e.get("ev") in ("play", "pause", "ended", "tick")), key=lambda e: e["wall_ms"])
            events = [e for e in events if self.pages_video(e) == vid]
            playing, last_tick = None, None
            stretches = []
            for e in events:
                if e["ev"] == "play":
                    playing = (e["wall_ms"], e["t"])
                elif e["ev"] == "tick":
                    last_tick = e
                elif playing:
                    stretches.append((*playing, e["wall_ms"]))
                    playing = None
            if playing and last_tick and last_tick["wall_ms"] > playing[0]:
                stretches.append((*playing, last_tick["wall_ms"]))
            narrators = {c["id"]: c["speaker"] for c in self.manifest["clips"]}
            for a_wall, a_media, b_wall in stretches:
                b_media = a_media + (b_wall - a_wall) / 1000
                for c in info.get("plan", []):
                    clip = self.clips[c["id"]]
                    c0, c1 = c["t_ms"] / 1000, c["t_ms"] / 1000 + clip["dur_s"]
                    if c0 < b_media and c1 > a_media:
                        off = a_wall - a_media * 1000
                        out.append({"stream": "system", "source": "video", "speaker": narrators.get(c["id"]), "lang": clip["lang"], "text": clip["text"],
                                    "t0": round(off + c0 * 1000), "t1": round(off + c1 * 1000),
                                    "spans": [(round(off + (c0 + x) * 1000), round(off + (c0 + y) * 1000)) for x, y in clip["spans"]]})
        return out

    def outer_wilds_span(self) -> tuple[int, int] | None:
        """From the Outer Wilds window appearing to its exit; the game runs through the whole drive."""
        ev = {e["state"]: e["wall_ms"] for e in self.run_log if e.get("type") == "outer_wilds"}
        return (ev["window"], ev.get("closed", self.t1)) if "window" in ev else None

    def outer_wilds_modes(self) -> list[dict]:
        """The observer's record of Outer Wilds' window: monitor and borderless or windowed, each time it changed."""
        return [{"t": e["wall_ms"], "monitor": e["monitor"], "mode": "borderless" if e["covers"] else "windowed"} for e in self.os if e["t"] == "ow_mode"]

    def pages_video(self, e: dict) -> str | None:
        step = self.scn_step(e["s"])
        return step["params"]["video"]["title_id"] if step and "video" in step["params"] else None

    def scn_step(self, sid):
        return self.steps_by_id.get(sid)


def merge(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    out: list[list[int]] = []
    for a, b in sorted(spans):
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def overlap_ms(a: list[tuple[int, int]], b: list[tuple[int, int]]) -> int:
    return sum(max(0, min(x1, y1) - max(x0, y0)) for x0, x1 in merge(a) for y0, y1 in merge(b))


# --- store ---------------------------------------------------------------------------------


def open_store(run_id: str):
    from akashic.config import Paths
    from akashic.storage import db
    from akashic.storage.crypto import unwrap_data_key
    from akashic.storage.media import MediaStore

    local = LOCAL / "runs" / run_id
    paths = Paths(local / "store")
    passphrase = dpapi_unwrap((local / "passphrase.dpapi").read_bytes()).decode()
    key = unwrap_data_key(paths.keyfile.read_bytes(), passphrase)
    conn = db.connect(paths.db_file, key.subkey("db"))
    return paths, key, conn, MediaStore(paths.media_dir, key.subkey("media"))


def monitor_names(conn) -> dict[int, str]:
    rows = conn.execute("SELECT lm.monitor_id, lm.x0, lm.is_main FROM layout_monitors lm WHERE lm.layout_id = (SELECT MAX(id) FROM monitor_layouts)").fetchall()
    main_x = next((r[1] for r in rows if r[2]), 0)
    return {r[0]: "primary" if r[2] else ("right" if r[1] > main_x else "left") for r in rows}


# --- metrics -------------------------------------------------------------------------------


class FrameScan:
    """Decodes each stored frame once, for the change tags (M1) and the canary pixel marker (M10)."""

    def __init__(self, conn, media) -> None:
        self.conn, self.media = conn, media
        self.cache: dict[int, tuple[frozenset, bool, bool]] = {}

    def info(self, fid: int, path: str) -> tuple[frozenset, bool, bool]:
        """(tag ids in the frame, canary marker anywhere, canary marker inside a window rectangle)."""
        if fid not in self.cache:
            from PIL import Image

            arr = np.asarray(Image.open(io.BytesIO(self.media.read(path))).convert("RGB"))
            # window rectangles are in virtual-desktop pixels; the frame is one monitor, perhaps resized
            ox, oy, mon_w = self.conn.execute(
                "SELECT lm.x0, lm.y0, lm.x1 - lm.x0 FROM frames f JOIN layout_monitors lm ON lm.monitor_id = f.monitor_id "
                "WHERE f.id = ? AND lm.layout_id = (SELECT id FROM monitor_layouts WHERE start_ms <= f.ts_ms ORDER BY start_ms DESC LIMIT 1)", (fid,)).fetchone()
            to_px = lambda v, origin: max(0, round((v - origin) * arr.shape[1] / mon_w))
            mask = np.all(np.abs(arr.astype(np.int16) - np.array(CANARY_RGB)) < 24, axis=2)
            anywhere = mask.sum() >= CANARY_PIXELS
            in_window = anywhere and any(
                mask[to_px(y0, oy):to_px(y1, oy), to_px(x0, ox):to_px(x1, ox)].sum() >= CANARY_PIXELS
                for x0, y0, x1, y1 in self.conn.execute("SELECT x0, y0, x1, y1 FROM frame_windows WHERE frame_id = ?", (fid,)))
            self.cache[fid] = (frozenset(tags.decode(arr)), bool(anywhere), bool(in_window))
        return self.cache[fid]


def logged_changes(tr: Truth) -> list[dict]:
    """Every visible change the pages, videos and game HUD made. Private windows are excluded: they must not be stored."""
    video_of = {e["video"]: e for e in tr.pages if e.get("type") == "video_built"}
    changes = []
    for e in tr.pages:
        if "#private" in e.get("w", ""):
            continue
        w = tr.windows.get(e.get("w", ""))
        if e.get("type") == "change" and w:
            changes.append({"t": e["wall_ms"], "tag": e["tag"], "mon": w["monitor"], "kind": "page", "surface": e["w"]})
        elif e.get("type") == "media" and e.get("ev") == "tick" and w:
            built = video_of.get(tr.pages_video(e))
            if built:
                changes.append({"t": e["wall_ms"], "tag": built["tag_base"] + int(e["t"]), "mon": w["monitor"], "kind": "video", "surface": e["w"] + "#video"})
    mode = None
    for e in tr.game:
        if e["type"] == "game_start":
            mode = e["mode"]
        elif e["type"] == "mode_change":
            mode = e["state"]
        elif e["type"] == "change":
            changes.append({"t": e["wall_ms"], "tag": e["tag"], "mon": "primary", "kind": "hud", "mode": mode, "surface": "hud"})
    changes = sorted((c for c in changes if tr.t0 <= c["t"] <= tr.t1), key=lambda c: c["t"])
    # A change lasts until the same surface changes again (or the drive ends): M1.1 counts only
    # changes shown for at least DWELL_MS, and finds them in any frame stored while they were shown.
    last: dict[str, dict] = {}
    for c in changes:
        if c["surface"] in last:
            last[c["surface"]]["end"] = c["t"]
        last[c["surface"]] = c
    for c in changes:
        c.setdefault("end", tr.t1)
    return changes


def m1(tr: Truth, conn, scan: FrameScan, mon_names) -> dict:
    frames = defaultdict(list)
    for fid, ts, mid, path in conn.execute("SELECT id, ts_ms, monitor_id, image_path FROM frames ORDER BY ts_ms"):
        frames[mon_names.get(mid, str(mid))].append((ts, fid, path))
    stamps = {mon: [f[0] for f in fl] for mon, fl in frames.items()}
    changes = logged_changes(tr)
    if not changes:
        return {"M1.1": nul("no changes logged"), "M1.2": nul("no frames"), "M1.3": nul("no captured changes")}
    fg = tr.fg_runs()
    run_starts = [r["t0"] for r in fg]
    focused_mon = lambda t: (fg[i]["monitor"] if (i := bisect.bisect_right(run_starts, t) - 1) >= 0 and t < fg[i]["t1"] else None)
    lasting = [c for c in changes if c["end"] - c["t"] >= DWELL_MS]
    if not lasting:
        return {"M1.1": nul("no change lasted the dwell"), "M1.2": nul("no frames"), "M1.3": nul("no captured changes")}
    sample = lasting if len(lasting) <= MAX_CHANGES else [lasting[i] for i in np.linspace(0, len(lasting) - 1, MAX_CHANGES).astype(int)]
    hits, per_split, delays = 0, defaultdict(lambda: [0, 0]), defaultdict(list)
    for ch in sample:
        ts_list = stamps.get(ch["mon"], [])
        lo, hi = bisect.bisect_left(ts_list, ch["t"]), bisect.bisect_left(ts_list, ch["end"])
        found = next((ts for ts, fid, path in frames[ch["mon"]][lo:hi] if path and ch["tag"] in scan.info(fid, path)[0]), None) if ts_list else None
        split = ("focused" if focused_mon(ch["t"]) == ch["mon"] else "unfocused") + ("/" + ch["mode"] if ch.get("mode") else "")
        per_split[split][1] += 1
        per_split[split][0] += found is not None
        hits += found is not None
        if found is not None:
            delays[ch["mon"]].append(found - ch["t"])
    change_times = defaultdict(list)
    for c in changes:
        change_times[c["mon"]].append(c["t"])
    hours = max((tr.t1 - tr.t0) / 3_600_000, 1e-9)
    m12 = {}
    for mon, fl in frames.items():
        ct = change_times[mon]
        useful = sum(1 for (ts, _, _), (prev, _, _) in zip(fl[1:], fl, strict=False) if bisect.bisect_right(ct, ts) > bisect.bisect_right(ct, prev))
        m12[mon] = {"frames_per_hour": len(fl) / hours, "useful": useful, "useless_ratio": 1 - useful / max(1, len(fl) - 1), "n": len(fl)}
    return {"M1.1": {"value": hits / len(sample), "n": len(sample), "changes_logged": len(changes), "changes_lasting_dwell": len(lasting), "splits": {k: {"value": v[0] / v[1], "n": v[1]} for k, v in per_split.items()}},
            "M1.2": m12, "M1.3": {mon: summarize([x / 1000 for x in d]) for mon, d in delays.items()} or nul("no change was captured")}


def m2(tr: Truth, conn) -> dict:
    exe_of = {}
    for r in tr.fg_runs():
        if r["exe"]:
            exe_of[r["hwnd"]] = r["exe"].lower()
    rows = conn.execute("SELECT hwnd, process FROM frame_windows WHERE role = 'app'").fetchall()
    known = [(h, (p or "").lower()) for h, p in rows if h in exe_of]
    acc = sum(1 for h, p in known if p == exe_of[h]) / len(known) if known else None
    frames = conn.execute("SELECT f.ts_ms, w.hwnd FROM frames f JOIN frame_windows w ON w.frame_id = f.id AND w.focused = 1").fetchall()
    fg = [e for e in tr.os if e["t"] == "fg"]
    times = [e["wall_ms"] for e in fg]
    ok = n = 0
    for ts, hwnd in frames:
        lo, hi = np.searchsorted(times, ts - FOCUS_TOL_MS), np.searchsorted(times, ts + FOCUS_TOL_MS, side="right")
        cand = {fg[i]["hwnd"] for i in range(max(0, lo - 1), min(len(fg), hi))}
        n += 1
        ok += hwnd in cand
    unknown = {}
    for step in tr.scn["steps"]:
        app = step["params"].get("app") if step["kind"] == "focus" else None
        if app in CASES:
            a, b = tr.span(step)
            res = conn.execute("SELECT w.process FROM frames f JOIN frame_windows w ON w.frame_id = f.id AND w.focused = 1 WHERE f.ts_ms BETWEEN ? AND ?", (a, b)).fetchall()
            u = unknown.setdefault(CASE_LABEL[app], [0, 0])
            u[1] += len(res)
            u[0] += sum(1 for (p,) in res if not p)
    return {"M2.1": {"label_accuracy": {"value": acc, "n": len(known)} if known else nul("no stored window matches a truth window"),
                     "focused_row": {"value": ok / n, "n": n} if n else nul("no frames")},
            "M2.2": {k: {"value": v[0] / v[1], "n": v[1]} if v[1] else nul("no frames in the case") for k, v in unknown.items()} or nul("no case steps")}


def best_line_wer(ref: str, hyp_lines: list[str]) -> float:
    r = fold(ref).split()
    if not r:
        return 0.0
    return min((lev(r, h.split()) / len(r) for h in hyp_lines), default=1.0)


def monitor_of(tr: Truth, wid: str | None) -> dict:
    """The machine record of the monitor a window sits on: resolution and scale."""
    role = tr.windows[wid]["monitor"] if wid in tr.windows else None
    return next((m for m in tr.machine["monitors"] if m["name"] == role), {})


def visible_reports(tr: Truth) -> dict[str, list[tuple[int, list[str]]]]:
    """Per window, the ids of the lines on screen at each report of its page, in time order."""
    out = defaultdict(list)
    for e in tr.pages:
        if e.get("type") == "visible" and "#private" not in e["w"]:
            out[e["w"]].append((e["wall_ms"], e["ids"]))
    return out


def shown_spans(tr: Truth) -> dict[str, list[tuple[int, int]]]:
    """Per window, when it was shown: each run of consecutive focus steps that list it."""
    out = defaultdict(list)
    opened: dict[str, int] = {}
    stop = tr.t0
    for step in sorted((s for s in tr.scn["steps"] if s["kind"] == "focus"), key=lambda s: s["t0_ms"]):
        for wid in [w for w in opened if w not in step["windows"]]:
            out[wid].append((opened.pop(wid), stop))
        start, stop = tr.span(step)
        for wid in step["windows"]:
            opened.setdefault(wid, start)
    for wid, start in opened.items():
        out[wid].append((start, stop))
    return out


def dwelled_lines(reports: dict[str, list[tuple[int, list[str]]]], shown: dict[str, list[tuple[int, int]]], end: int) -> set[str]:
    """Lines that stayed visible for at least DWELL_MS. A hidden page reports nothing, so a line's
    visibility ends where its window stops being shown."""
    out: set[str] = set()
    for wid, window in reports.items():
        since: dict[str, int] = {}
        for t, ids in [*window, (end, [])]:
            for i in ids:
                since.setdefault(i, t)
            for i in [k for k in since if k not in ids]:
                first = since.pop(i)
                if max((min(t, b) - max(first, a) for a, b in shown.get(wid, [])), default=0) >= DWELL_MS:
                    out.add(i)
    return out


def m3(tr: Truth, conn) -> dict:
    lines = {}
    for e in tr.pages:
        if e.get("type") == "lines" and "#private" not in e["w"]:
            for ln in e["lines"]:
                lines[ln["id"]] = {**ln, "w": e["w"], "s": e["s"]}
    visible = visible_reports(tr)
    report_times = {wid: [t for t, _ in reports] for wid, reports in visible.items()}
    # The look a page really loaded with is in its URL (side pages load a look of their own).
    focus_params = {s["id"]: s["params"] for s in tr.scn["steps"] if s["kind"] == "focus"}
    for d in tr.driver:
        if d["action"] == "page_load" and d.get("url"):
            q = {k: v[0] for k, v in parse_qs(urlsplit(d["url"]).query).items()}
            if "s" in q:
                focus_params[q["s"]] = {"lang": q.get("lang"), "theme": q.get("theme"), "font_px": int(q.get("px", 16))}
    rows = conn.execute("SELECT w.hwnd, f.ts_ms, w.text, w.text_source FROM frame_windows w JOIN frames f ON f.id = w.frame_id WHERE w.role = 'app' AND w.text IS NOT NULL ORDER BY f.ts_ms").fetchall()
    by_hwnd = defaultdict(list)
    for h, ts, text, src in rows:
        by_hwnd[h].append((ts, text, src))
    splits = defaultdict(lambda: [[], [], []])
    dup_chars = total_chars = 0
    sample = rows[:: max(1, len(rows) // MAX_TEXT_ROWS)] if rows else []
    for h, ts, text, src in sample:
        wid = tr.hwnd_to_window.get(h)
        i = bisect.bisect_right(report_times.get(wid, []), ts) - 1
        shown = [lines[x] for x in visible[wid][i][1] if x in lines] if i >= 0 else []
        if not shown:
            continue
        hyp = [fold(x) for x in text.splitlines() if x.strip()]
        params = focus_params.get(shown[-1]["s"], {})
        mon = monitor_of(tr, wid)
        cer = statistics.mean(best_line_cer(ln["text"], hyp) for ln in shown)
        cer_f = statistics.mean(best_line_cer(fold(ln["text"], False), [fold(x, False) for x in hyp]) for ln in shown)
        wer = statistics.mean(best_line_wer(ln["text"], hyp) for ln in shown)
        device_px = params.get("font_px", 16) * mon.get("scale", 1)
        bucket = next((b[2] for b in FONT_BUCKETS if b[0] <= device_px < b[1] + 1), "?")
        for key in (f"lang={params.get('lang', '?')}", f"font={bucket}", f"theme={params.get('theme', '?')}", f"source={src}",
                    f"resolution={mon.get('w', '?')}x{mon.get('h', '?')}", "all"):
            splits[key][0].append(cer)
            splits[key][1].append(cer_f)
            splits[key][2].append(wer)
    for h, hist in by_hwnd.items():
        seen: set[str] = set()
        for _, text, _ in hist:
            for ln in (x.strip() for x in text.splitlines() if x.strip()):
                total_chars += len(ln)
                if ln in seen:
                    dup_chars += len(ln)
                seen.add(ln)
    hours = max((tr.t1 - tr.t0) / 3_600_000, 1e-9)
    shown_long = {i: lines[i] for i in dwelled_lines(visible, shown_spans(tr), tr.t1) if i in lines}
    found = 0
    for ln in shown_long.values():
        nonce = ln["text"].split()[-1]
        found += conn.execute("SELECT 1 FROM frame_windows_fts WHERE frame_windows_fts MATCH ? LIMIT 1", (f'"{nonce}"',)).fetchone() is not None
    return {"M3.1": {k: {"cer_exact": statistics.mean(v[0]), "cer_accent_folded": statistics.mean(v[1]), "wer": statistics.mean(v[2]), "n": len(v[0])} for k, v in splits.items()} or nul("no stored text matched a shown line"),
            "M3.2": {"value": dup_chars / total_chars if total_chars else None, "n": total_chars, "per_hour_chars": total_chars / hours} if total_chars else nul("no stored text"),
            "M3.3": {"value": found / len(shown_long), "n": len(shown_long)} if shown_long else nul("no lines shown")}


def m4(tr: Truth, conn) -> dict:
    tally = defaultdict(lambda: Counter())
    titles = {e["s"]: e["title"] for e in tr.pages if e.get("type") == "lines" and e.get("title") and "#private" not in e["w"]}
    urls = {d["step"]: d["url"] for d in tr.driver if d["action"] == "page_load" and "#private" not in d["window"]}
    for step in tr.scn["steps"]:
        if step["kind"] != "focus" or step["params"]["app"] not in ("chrome", "edge", "zen", "assistant"):
            continue
        a, b = tr.span(step)
        rows = conn.execute("SELECT w.title, w.url FROM frames f JOIN frame_windows w ON w.frame_id = f.id AND w.focused = 1 WHERE f.ts_ms BETWEEN ? AND ?", (a, b)).fetchall()
        sid = step["id"]
        if sid in titles:
            title_nonce = titles[sid].split()[0]
            tally["tab_title"][("right" if any(title_nonce in (t or "") for t, _ in rows) else ("missing" if not rows else "wrong"))] += 1
        if sid in urls:
            want = bare_url(urls[sid])
            tally["url"][("right" if any(bare_url(u or "") == want for _, u in rows) else ("missing" if not rows else "wrong"))] += 1
    for e in tr.pages:
        if e.get("type") == "video_built":
            row = conn.execute("SELECT title, artist FROM media_sessions WHERE title = ?", (e["title"],)).fetchone()
            tally["video_title"]["right" if row else "missing"] += 1
            if e.get("episode"):
                tag = f"S{e['season']:02d}E{e['number']:02d}"
                tally["series_season_episode"]["right" if row and tag in row[0] else "missing"] += 1
    games = conn.execute("SELECT process, window_title FROM game_sessions").fetchall()
    for _ in [e for e in tr.game if e["type"] == "game_start"]:
        tally["game_name"]["right" if any("qualitygame" in (p or "").lower() or "Quality Game" in (t or "") for p, t in games) else "missing"] += 1
    if tr.outer_wilds_span():
        tally["game_name"]["right" if any("outerwilds" in (p or "").lower() for p, _ in games) else "missing"] += 1
    return {"M4.1": {k: {"value": c["right"] / sum(c.values()), "n": sum(c.values()), "wrong": c["wrong"], "missing": c["missing"]} for k, c in tally.items()} or nul("no identity items")}


def bare_url(url: str) -> str:
    """A URL as the browser shows it: no scheme, no trailing slash."""
    return re.sub(r"^https?://", "", url).rstrip("/")


def m5(tr: Truth, conn, now_ms: int) -> dict:
    from akashic.pipeline.sessions import app_bands
    from akashic.storage.gaps import overlapping

    a, b = tr.t0, tr.t1
    bands = [(x.start_ms, x.end_ms, x.process) for x in app_bands(conn, a, b, None)]
    gaps = [(g.start_ms, g.end_ms) for g in overlapping(conn, a, b, now_ms, ("all", "screen"))]
    nonrec = merge([(e0, e1) for e0, e1 in truth_gaps(tr)])
    runs = [r for r in tr.fg_runs() if r["kind"] != "foreign"]
    covered = merge([(x, y) for x, y, _ in bands])
    truth_secs = sum(r["t1"] - r["t0"] for r in runs)
    holes = sum(r["t1"] - r["t0"] for r in runs) - overlap_ms([(r["t0"], r["t1"]) for r in runs], covered + merge(gaps) + nonrec)
    double = 0
    for (a0, a1, _), (b0, b1, _) in itertools.combinations(sorted(bands), 2):
        double += max(0, min(a1, b1) - max(a0, b0))
    double += overlap_ms(covered, merge(gaps))
    err = tot = 0
    per_app = defaultdict(lambda: [0, 0])
    for r in runs:
        match = [(y - x, p) for x, y, p in bands if x < r["t1"] and y > r["t0"] and (p or "").lower() == (r["exe"] or "").lower()]
        stored = max((d for d, _ in match), default=0)
        err += abs(stored - (r["t1"] - r["t0"]))
        tot += r["t1"] - r["t0"]
        per_app[r["exe"]][0] += abs(stored - (r["t1"] - r["t0"]))
        per_app[r["exe"]][1] += r["t1"] - r["t0"]
    gap_typing = {}
    for kind, spans in truth_gap_kinds(tr).items():
        want = GAP_REASONS[kind]
        stored = merge([(g.start_ms, g.end_ms) for g in overlapping(conn, a, b, now_ms, ("all", "screen", "mic", "system")) if g.reason in want])
        total = sum(y - x for x, y in spans)
        gap_typing[kind] = {"value": overlap_ms(spans, stored) / total if total else None, "n": len(spans)}
    return {"M5.1": {"seconds": holes / 1000, "n": len(runs), "truth_seconds": truth_secs / 1000} if runs else nul("no truth runs"),
            "M5.2": {"seconds": double / 1000, "n": len(bands)} if bands else nul("no stored bands"), "M5.3": {"total_abs_error_ratio": err / tot, "n": len(runs), "per_app": {k or "?": v[0] / v[1] for k, v in per_app.items() if v[1]}} if tot else nul("no runs"),
            "M5.4": gap_typing or nul("no non-recording period")}


def truth_gap_kinds(tr: Truth) -> dict[str, list[tuple[int, int]]]:
    out = defaultdict(list)
    def pairs(open_t, close_t):
        start = None
        for e in tr.os:
            if e["t"] == open_t:
                start = e["wall_ms"]
            elif e["t"] == close_t and start is not None:
                yield start, e["wall_ms"]
                start = None
    out["lock"] = list(pairs("lock", "unlock"))
    out["sleep"] = list(pairs("suspend", "resume"))
    start = None
    for d in tr.driver:
        if d["action"] == "pause":
            start = d["wall_ms"]
        elif d["action"] == "resume" and start is not None:
            out["pause"].append((start, d["wall_ms"]))
            start = None
    out["device_change"] = [(e["wall_ms"], e["wall_ms"] + DEVICE_SETTLE_MS) for e in tr.os if e["t"] == "default_changed"]
    return {k: v for k, v in out.items() if v}


def truth_gaps(tr: Truth) -> list[tuple[int, int]]:
    return [s for spans in truth_gap_kinds(tr).values() for s in spans]


def m6(tr: Truth, conn) -> dict:
    out = {}
    plays = defaultdict(list)
    for e in tr.pages:
        if e.get("type") == "media" and e.get("ev") in ("play", "pause", "ended", "tick"):
            vid = tr.pages_video(e)
            if vid:
                plays[vid].append(e)
    errs_s, errs_e, splits, n = [], [], 0, 0
    built = {e["video"]: e["title"] for e in tr.pages if e.get("type") == "video_built"}
    for vid, evs in plays.items():
        truth = (min(e["wall_ms"] for e in evs if e["ev"] == "play"), max(e["wall_ms"] for e in evs))
        rows = conn.execute("SELECT start_ms, end_ms FROM media_sessions WHERE title = ? ORDER BY start_ms", (built.get(vid, ""),)).fetchall()
        n += 1
        splits += max(0, len(rows) - 1)
        if rows:
            errs_s.append(abs(rows[0][0] - truth[0]) / 1000)
            errs_e.append(abs(rows[-1][1] - truth[1]) / 1000)
    games = [e for e in tr.game if e["type"] in ("game_start", "game_end")]
    stored_games = conn.execute("SELECT start_ms, end_ms FROM game_sessions ORDER BY start_ms").fetchall()
    out["media"] = {"start_error_s": summarize(errs_s), "end_error_s": summarize(errs_e), "split_rows": splits, "n": n} if n else nul("no video session")
    truth_games = [(a["wall_ms"], b["wall_ms"]) for a, b in zip(games[::2], games[1::2], strict=False)]
    out["game"] = boundary_errors(truth_games, stored_games) if truth_games else nul("no game session")
    ow = tr.outer_wilds_span()
    if ow:
        first_frame = conn.execute("SELECT min(ts_ms) FROM frames").fetchone()[0]
        rows = conn.execute("SELECT start_ms, end_ms FROM game_sessions WHERE lower(process) LIKE 'outerwilds%' ORDER BY start_ms").fetchall()
        # the game was already running when capture started: its session can only begin there
        out["outer_wilds"] = {**boundary_errors([(max(ow[0], first_frame or ow[0]), ow[1])], rows),
                              "window_modes": [{"monitor": m["monitor"], "mode": m["mode"]} for m in tr.outer_wilds_modes()]}
    return {"M6.1": out}


def boundary_errors(truth: list[tuple[int, int]], rows: list[tuple[int, int]]) -> dict:
    if not rows:
        return nul("no stored game session")
    return {"truth_sessions": len(truth), "stored_rows": len(rows),
            "start_error_s": summarize([min(abs(a - t0) for a, _ in rows) / 1000 for t0, _ in truth]),
            "end_error_s": summarize([min(abs(b - t1) for _, b in rows) / 1000 for _, t1 in truth])}


def audible_spans(tr: Truth) -> dict[str, list[tuple[int, int]]]:
    on: dict[tuple, int] = {}
    out = defaultdict(list)
    for e in tr.os:
        if e["t"] != "audio":
            continue
        key = (e["exe"].lower(), e["endpoint"])
        if e["audible"]:
            on[key] = e["wall_ms"]
        elif key in on:
            out[key[0]].append((on.pop(key), e["wall_ms"]))
    for key, t in on.items():
        out[key[0]].append((t, tr.t1))
    return out


def m7(tr: Truth, conn) -> dict:
    # Akashic stores lower-case exe names; the observer logs the image name as Windows reports it.
    aud = defaultdict(list)
    for exe, spans in audible_spans(tr).items():
        aud[exe if exe == "foreign" else exe.lower()].extend(spans)
    segs = conn.execute("SELECT stream, start_ms, end_ms, sources FROM audio_segments WHERE stream = 'system'").fetchall()
    if not segs:
        return {"M7.1": nul("no system segments"), "M7.2": nul("no system segments")}
    # Akashic's own processes (their audio sessions are 'foreign' in the OS log): a loopback capture
    # shows up as an audible session on the endpoint it records, so Akashic can list itself.
    self_pids = {int(pid) for e in tr.guard if e.get("t") == "guard" for tree in ("capture", "worker")
                 for pid in e.get("trees", {}).get(tree, {})}
    self_names = {m["exe"].lower() for e in tr.guard if e.get("t") == "guard" for tree in ("capture", "worker")
                  for m in e.get("trees", {}).get(tree, {}).values()}
    self_audible = any(e["t"] == "audio" and e.get("pid") in self_pids and e["audible"] for e in tr.os)
    leak = tot = leak_other = 0
    exact = 0
    tagok = 0
    self_listed = 0
    for _, a, b, src in segs:
        sources = {x.lower() for x in json.loads(src)}
        truth = {exe for exe, spans in aud.items() if overlap_ms(spans, [(a, b)]) > (b - a) * 0.2 and exe != "foreign"}
        extra = sources - {exe for exe, spans in aud.items() if overlap_ms(spans, [(a, b)]) > 0}
        leak += (b - a) * len(extra)
        leak_other += (b - a) * len(extra - self_names)
        self_listed += bool(sources & self_names)
        tot += b - a
        exact += sources == truth
        tagok += tag_of(sources) == tag_of(truth)
    return {"M7.1": {"leak_seconds": leak / 1000, "leak_seconds_other_than_self": leak_other / 1000, "stored_seconds": tot / 1000,
                     "segments_listing_akashic_itself": self_listed, "akashic_session_audible": self_audible, "n": len(segs)},
            "M7.2": {"sources_exact": exact / len(segs), "tag_accuracy": tagok / len(segs), "n": len(segs)}}


def tag_of(sources: set[str]) -> str:
    if "discord.exe" in sources:
        return "voice chat"
    if "qualitygame.exe" in sources:
        return "game"
    return "media" if sources else ""


def speech_frames(utts: list[dict], stream: str) -> list[tuple[int, int]]:
    return merge([span for u in utts if u["stream"] == stream for span in u["spans"]])


def m8_m9(tr: Truth, conn) -> dict:
    utts = tr.utterances()
    segs = conn.execute("SELECT id, stream, start_ms, end_ms, speech_ms, transcript, speaker_id, language FROM audio_segments").fetchall()
    if not utts:
        return {k: nul("no truth speech") for k in ("M8.1", "M8.2", "M8.3", "M9.1", "M9.2", "M9.3")}
    aud = audible_spans(tr)
    games, music = merge(aud.get("qualitygame.exe", [])), merge(aud.get("spotify.exe", []))
    m81 = {}
    for stream in ("mic", "system"):
        mine = [u for u in utts if u["stream"] == stream]
        truth = speech_frames(utts, stream)
        stored = merge([(a, b) for _, st, a, b, *_ in segs if st == stream])
        total = sum(b - a for a, b in truth)
        if not total:
            m81[stream] = nul("no truth speech")
            continue
        cond: dict[str, list[int]] = {}  # condition -> [covered ms, truth ms]
        for u in mine:
            others = [(o["t0"], o["t1"]) for o in mine if o is not u]
            kind = "overlap" if overlap_ms([(u["t0"], u["t1"])], others) else ("game" if overlap_ms(u["spans"], games) else ("music" if overlap_ms(u["spans"], music) else "clean"))
            c = cond.setdefault(kind, [0, 0])
            c[0] += overlap_ms(u["spans"], stored)
            c[1] += sum(y - x for x, y in u["spans"])
        fa = sum(b - a for a, b in stored) - overlap_ms(stored, truth)
        m81[stream] = {"recall": overlap_ms(truth, stored) / total, "false_alarm_s_per_hour": fa / 1000 / max((tr.t1 - tr.t0) / 3_600_000, 1e-9), "truth_seconds": total / 1000, "n": len(truth),
                       "by_condition": {k: {"recall": v[0] / v[1] if v[1] else None, "truth_seconds": v[1] / 1000} for k, v in cond.items()}}
    # WER per overlap group: segments and truth utterances of one stream that overlap in time form a
    # group (an utterance cut across two segments, or two segments of one utterance, stay together).
    # A group's language is its utterances' one language, else "multi"; TTS clips (English voices,
    # some reading French text) are their own bucket; groups with overlapping speakers are split out.
    wer = defaultdict(lambda: [0, 0, 0, 0])
    halluc = 0
    for stream in ("mic", "system"):
        items = [("u", u["t0"], u["t1"], u) for u in utts if u["stream"] == stream]
        items += [("s", a, b, (text or "")) for _, st, a, b, _, text, *_ in segs if st == stream]
        items.sort(key=lambda x: x[1])
        groups, cur, cur_end = [], [], -1
        for it in items:
            if cur and it[1] >= cur_end:
                groups.append(cur)
                cur = []
            cur.append(it)
            cur_end = max(cur_end, it[2]) if len(cur) > 1 else it[2]
        if cur:
            groups.append(cur)
        for g in groups:
            us = [x[3] for x in g if x[0] == "u"]
            hyp = fold(" ".join(x[3] for x in sorted((x for x in g if x[0] == "s"), key=lambda x: x[1])))
            if not us:
                halluc += len(hyp.split())
                continue
            ref = fold(" ".join(u["text"] for u in us))
            langs = {u["lang"] for u in us if u.get("kind") != "tts"}
            lang = "tts" if not langs else (langs.pop() if len(langs) == 1 else "multi")
            overlapped = any(us[i]["t1"] > us[j]["t0"] for i in range(len(us)) for j in range(i + 1, len(us)))
            for k in (lang, "all", "overlap" if overlapped else "no_overlap"):
                w = wer[k]
                rw, hw = ref.split(), hyp.split()
                w[0] += lev(rw, hw)
                w[1] += len(rw)
                w[2] += lev(ref, hyp)
                w[3] += len(ref)
    m91 = {k: {"wer": v[0] / v[1] if v[1] else None, "cer": v[2] / v[3] if v[3] else None, "ref_words": v[1]} for k, v in wer.items()}
    terms = [c["text"] for c in tr.manifest["clips"] if c["kind"] == "tts"]
    present = [(t, u) for u in utts for t in set(terms) if u["text"] == t]
    joined = " ".join(fold(s[5] or "") for s in segs)
    m93 = {"value": sum(fold(t) in joined for t, _ in present) / len(present), "n": len(present)} if present else nul("no term was spoken")
    hours = max((tr.t1 - tr.t0) / 3_600_000, 1e-9)
    # diarization and authors on system segments
    der = diarization(tr, conn, utts, segs)
    return {"M8.1": m81, "M8.2": der["M8.2"], "M8.3": der["M8.3"], "M9.1": m91 or nul("no reference"),
            "M9.2": {"hallucinated_words_per_hour": halluc / hours, "n": len(segs)}, "M9.3": m93}


def diarization(tr: Truth, conn, utts: list[dict], segs) -> dict:
    turns = conn.execute("SELECT t.speaker_id, s.start_ms + t.start_ms, s.start_ms + t.end_ms FROM speaker_turns t JOIN audio_segments s ON s.id = t.segment_id WHERE s.stream = 'system'").fetchall()
    truth_turns = [(u["speaker"], u["t0"], u["t1"]) for u in utts if u["stream"] == "system" and u["speaker"]]
    if not turns or not truth_turns:
        return {"M8.2": nul("no turns or no truth speakers"), "M8.3": nul("no turns or no truth speakers")}
    lo, hi = min(t[1] for t in truth_turns) // 10, max(t[2] for t in truth_turns) // 10 + 1
    ref = np.full(hi - lo, -1)
    hyp = np.full(hi - lo, -1)
    names = sorted({t[0] for t in truth_turns})
    for s, a, b in truth_turns:
        ref[max(0, a // 10 - lo):max(0, b // 10 - lo)] = names.index(s)
    ids = sorted({t[0] for t in turns if t[0] is not None})
    for s, a, b in turns:
        if s is not None:
            hyp[max(0, a // 10 - lo):max(0, min(hi, b // 10) - lo)] = ids.index(s)
    collar = np.zeros(len(ref), bool)
    for s, a, b in truth_turns:
        for edge in (a, b):
            collar[max(0, (edge - COLLAR_MS) // 10 - lo):max(0, (edge + COLLAR_MS) // 10 - lo)] = True
    keep = ~collar
    conf = np.zeros((len(names), len(ids)))
    for i in range(len(names)):
        for j in range(len(ids)):
            conf[i, j] = np.sum((ref == i) & (hyp == j) & keep)
    mapping = best_assignment(conf)
    mapped = np.array([mapping.get(j, -2) if j >= 0 else -1 for j in hyp])
    speech = (ref >= 0) & keep
    miss = np.sum(speech & (hyp < 0))
    fa = np.sum((ref < 0) & (hyp >= 0) & keep)
    confusion = np.sum(speech & (hyp >= 0) & (mapped != ref))
    total = max(1, int(speech.sum()))
    purity = {j: conf[:, j].max() / max(1, conf[:, j].sum()) for j in range(len(ids))}
    frag = {names[i]: int(np.sum(conf[i] > 100)) for i in range(len(names))}
    segs_correct = wrong = unknown = 0
    for sid, stream, a, b, _, text, spk, lang in segs:
        if stream != "system":
            continue
        truth = Counter(u["speaker"] for u in utts if u["stream"] == "system" and u["speaker"] and a <= (u["t0"] + u["t1"]) / 2 < b)
        if not truth:
            continue
        if spk is None:
            unknown += 1
        elif spk in ids and ids.index(spk) in mapping and names[mapping[ids.index(spk)]] == truth.most_common(1)[0][0]:
            segs_correct += 1
        else:
            wrong += 1
    return {"M8.2": {"der": float((miss + fa + confusion) / total), "miss": float(miss / total), "false_alarm": float(fa / total), "confusion": float(confusion / total), "n": int(speech.sum()) * 10 // 1000},
            "M8.3": {"correct": segs_correct, "wrong": wrong, "unknown": unknown, "purity_mean": float(np.mean(list(purity.values()))) if purity else None, "fragmentation": frag}}


def best_assignment(conf: np.ndarray) -> dict[int, int]:
    """hyp speaker -> truth speaker, the one-to-one mapping that overlaps the most time."""
    rows, cols = linear_sum_assignment(-conf)
    return {int(j): int(i) for i, j in zip(rows, cols, strict=True)}


def m10(tr: Truth, run: Path, run_id: str, conn, scan: FrameScan, paths) -> dict:
    private_lines = [ln for e in tr.pages if e.get("type") == "lines" and "#private" in e["w"] for ln in e["lines"]]
    canaries = {CANARY_TEXT, *(ln["text"].split()[-1] for ln in private_lines)}
    pattern = re.compile("|".join(re.escape(c) for c in canaries), re.I)
    nonces = {ln["text"].split()[-1] for e in tr.pages if e.get("type") == "lines" for ln in e["lines"]}
    hits: list[dict] = []
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
    for t in tables:
        cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{t}")') if (r[2] or "").upper() in ("TEXT", "BLOB", "")]
        for c in cols:
            try:
                for (v,) in conn.execute(f'SELECT "{c}" FROM "{t}" WHERE "{c}" IS NOT NULL'):
                    text = v.decode("utf-8", "ignore") if isinstance(v, bytes) else str(v)
                    if pattern.search(text):
                        hits.append({"where": f"sql:{t}.{c}"})
                        break
            except Exception:  # noqa: BLE001 - virtual tables without plain columns
                continue
    frame_hits = window_hits = frames_scanned = 0
    for fid, path in conn.execute("SELECT id, image_path FROM frames WHERE image_path IS NOT NULL").fetchall():
        _, anywhere, in_window = scan.info(fid, path)
        frames_scanned += 1
        frame_hits += anywhere
        window_hits += in_window
    if frame_hits:
        hits.append({"where": "frames", "frames": frame_hits, "in_window_rect": window_hits})
    for p in sorted(paths.logs_dir.rglob("*")):
        if p.is_file() and any(n.encode() in p.read_bytes() for n in canaries | nonces):
            hits.append({"where": f"log:{p.relative_to(paths.root).as_posix()}"})
    temp = Path(tempfile.gettempdir())
    snapshot = LOCAL / "runs" / run_id / "temp-before.json"
    before = set(json.loads(snapshot.read_text())) if snapshot.exists() else None
    temp_unreadable = 0
    if before is not None:
        for p in temp.iterdir():
            if p.name in before or not p.is_file():
                continue
            try:
                data = p.read_bytes()
            except OSError:  # held open by another program: counted, not read
                temp_unreadable += 1
                continue
            if any(n.encode() in data for n in canaries | nonces):
                hits.append({"where": "temp:" + p.name})
    plain = []
    for p in sorted(paths.root.rglob("*")):
        rel = p.relative_to(paths.root)
        if not p.is_file() or rel.parts[0] in ("models", "runtime"):
            continue
        if p.suffix == ".tmp":
            plain.append({"where": f"leftover:{rel.as_posix()}"})
        if rel == Path("db/akashic.db") and p.read_bytes()[:16].startswith(b"SQLite format 3"):
            plain.append({"where": "plaintext-db-header"})
        if p.suffix == ".enc":
            try:
                scan.media.read(rel.relative_to("media").as_posix().removesuffix(".enc"))
            except Exception:  # noqa: BLE001
                plain.append({"where": f"undecryptable:{rel.as_posix()}"})
        data = p.read_bytes() if p.stat().st_size < 64 << 20 else b""
        if any(n.encode() in data for n in canaries | nonces):
            plain.append({"where": f"content:{rel.as_posix()}"})
    outbound = [{"proto": e["proto"], "exe": e["exe"], "rport": e["rport"]} for e in tr.guard if e.get("t") == "conn"]
    return {"M10.1": {"hits": len(hits), "locations": hits, "frames_scanned": frames_scanned, "canaries": len(canaries), "temp_files_unreadable": temp_unreadable,
                      "not_scanned": [{"location": "export", "reason": "app shell out of scope for this harness"}]},
            "M10.2": {"hits": len(plain), "locations": plain}, "M10.3": {"connections": len(outbound), "locations": outbound}}


def desk_activities(tr: Truth) -> list[tuple[str, int, int, str | None]]:
    """Browsing and work as episodes build them: foreground time outside games, chained while the gap stays
    short and the kind is the same, long enough."""
    chains: list[list] = []
    for r in tr.fg_runs():
        if not r["app"] or sc.APPS[r["app"]][2] == "game":
            continue
        kind = sc.APPS[r["app"]][2]
        kind = "browsing" if kind == "browser" else "work"
        if chains and chains[-1][0] == kind and r["t0"] - chains[-1][2] <= EPISODE_GAP_MS:
            chains[-1][2] = max(chains[-1][2], r["t1"])
        else:
            chains.append([kind, r["t0"], r["t1"]])
    return [(kind, a, b, None) for kind, a, b in chains if b - a >= MIN_EPISODE_MS[kind]]


def m11(tr: Truth, conn) -> dict:
    eps = conn.execute("SELECT id, kind, start_ms, end_ms, title FROM episodes").fetchall()
    video_titles = {e["video"]: e["title"].lower() for e in tr.pages if e.get("type") == "video_built"}
    truth = []
    # a video's truth span is what the page really played (first play to its last media event), not its step
    played = defaultdict(list)
    for e in tr.pages:
        if e.get("type") == "media" and e.get("ev") in ("play", "pause", "ended", "tick") and (vid := tr.pages_video(e)):
            played[vid].append(e)
    for vid, evs in played.items():
        starts = [e["wall_ms"] for e in evs if e["ev"] == "play"]
        if starts:
            truth.append(("video", min(starts), max(e["wall_ms"] for e in evs), video_titles.get(vid)))
    truth += [("conversation", a, b, "discord") for a, b in tr.call_spans().values()]
    for a, b in zip([e for e in tr.game if e["type"] == "game_start"], [e for e in tr.game if e["type"] == "game_end"], strict=False):
        truth.append(("game", a["wall_ms"], b["wall_ms"], "quality game"))
    if ow := tr.outer_wilds_span():
        truth.append(("game", ow[0], ow[1], "outer wilds"))
    truth += desk_activities(tr)
    if not truth:
        return {k: nul("no truth activity") for k in ("M11.1", "M11.2", "M11.3", "M11.4")}
    def iou(a0, a1, b0, b1):
        inter = max(0, min(a1, b1) - max(a0, b0))
        return inter / max(1, (a1 - a0) + (b1 - b0) - inter)
    per_kind = defaultdict(lambda: [0, 0, 0])
    matched_eps, dup = set(), 0
    title_ok = 0
    for kind, a, b, name in truth:
        cand = [e for e in eps if e[1] == kind and iou(e[2], e[3], a, b) >= IOU]
        per_kind[kind][1] += 1
        if cand:
            per_kind[kind][0] += 1
            dup += len(cand) - 1
            matched_eps.update(e[0] for e in cand)
            title_ok += name is not None and any(name in (e[4] or "").lower() for e in cand)
    for e in eps:
        per_kind[e[1]][2] += 1
    f1 = {}
    for k, (tp, n_truth, n_eps) in per_kind.items():
        p, r = tp / n_eps if n_eps else 0, tp / n_truth if n_truth else 0
        f1[k] = {"precision": p, "recall": r, "f1": 2 * p * r / (p + r) if p + r else 0, "n_truth": n_truth, "n_episodes": n_eps}
    n_titled = sum(1 for t in truth if t[3])
    people = conn.execute("SELECT count(DISTINCT speaker_id) FROM episode_people").fetchone()[0]
    return {"M11.1": f1, "M11.2": {"value": title_ok / n_titled, "n": n_titled} if n_titled else nul("no truth activity with a title"),
            "M11.3": {"distinct_people": people, "truth_speakers": sum(1 for c in tr.scn["cast"].values() if c["role"] == "call"), "n": len(truth)},
            "M11.4": {"value": (len(eps) - len(matched_eps) + dup) / len(eps), "n": len(eps)} if eps else nul("no episodes")}


def m12(tr: Truth, conn) -> dict:
    text_rows = conn.execute("SELECT count(*) FROM frame_windows WHERE role = 'app' AND text IS NOT NULL").fetchone()[0]
    fts_rows = conn.execute("SELECT count(*) FROM frame_windows_fts_docsize").fetchone()[0]
    done = conn.execute("SELECT count(*) FROM audio_segments WHERE status = 'done'").fetchone()[0]
    seg_fts = conn.execute("SELECT count(*) FROM audio_segments_fts_docsize").fetchone()[0]
    emb = conn.execute("SELECT owner_kind, count(*) FROM embeddings WHERE owner_kind != 'query' GROUP BY owner_kind").fetchall()
    delays = defaultdict(list)
    for kind, data_ms, finished in conn.execute("SELECT kind, data_ms, finished_ms FROM jobs WHERE state = 'done' AND finished_ms IS NOT NULL"):
        delays[kind].append((finished - data_ms) / 1000)
    return {"M12.1": {"text_rows": text_rows, "text_rows_in_fts": fts_rows, "segments_done": done, "segments_in_fts": seg_fts,
                      "embeddings": dict(emb), "delay_s_by_job": {k: summarize(v) for k, v in delays.items()}}}


def m13(tr: Truth, conn, now_ms: int) -> dict:
    from akashic.storage.gaps import overlapping

    events = {k: v for k, v in truth_gap_kinds(tr).items()}
    events["busy_disk"] = [(d["wall_ms"], d["wall_ms"] + int(d.get("seconds", 60) * 1000)) for d in tr.driver if d["action"] == "busy_disk"]
    fg = [r for r in tr.fg_runs() if r["kind"] != "foreign"]
    frame_ts = [r[0] for r in conn.execute("SELECT ts_ms FROM frames ORDER BY ts_ms")]
    seg = conn.execute("SELECT start_ms, end_ms FROM audio_segments").fetchall()
    out = {}
    for kind, spans in events.items():
        lost = total = 0
        for _, end in spans:
            window = (end, end + LOSS_WINDOW_MS)
            gaps = [(g.start_ms, g.end_ms) for g in overlapping(conn, window[0], window[1], now_ms, ("all", "screen", "mic", "system"))]
            for r in fg:
                a, b = max(r["t0"], window[0]), min(r["t1"], window[1])
                for sec in range(a // 1000, b // 1000):
                    t = sec * 1000
                    total += 1000
                    i = np.searchsorted(frame_ts, t)
                    near = any(0 <= abs(frame_ts[j] - t) <= 10_000 for j in (i - 1, i) if 0 <= j < len(frame_ts))
                    in_seg = any(s <= t < e for s, e in seg)
                    in_gap = any(g0 <= t < g1 for g0, g1 in gaps)
                    lost += 0 if (near or in_seg or in_gap) else 1000
        out[kind] = {"lost_seconds": lost / 1000, "truth_seconds": total / 1000, "events": len(spans)} if total else nul("no truth activity after the event")
    return {"M13.1": out or nul("no life event happened")}


def guardrails(tr: Truth) -> dict:
    """G1-G4 from the guard rows (a row covers 10 s: the 1 Hz CPU samples, the window's peak VRAM, per process of every spawn pid's tree)."""
    cpu, vram, io = defaultdict(list), defaultdict(list), defaultdict(lambda: defaultdict(list))
    held_other = defaultdict(bool)  # tree label -> some sample held a process that is not a spawn pid
    store = []
    for g in tr.guard:
        if g.get("t") != "guard" or g.get("phase") == "reference":
            continue
        for label, procs in g["trees"].items():
            if label == "game":
                continue
            held_other[label] |= any(not p["root"] for p in procs.values())
            for pid, p in procs.items():
                name = f"{label}:{p['exe']}"
                cpu[name] += p["cpu_pct_1hz"]
                vram[name].append(p["vram_b"] / (1 << 20))
                io[name][pid].append(p["io_w"] or 0)
        if "store_bytes" in g:
            store.append((g["wall_ms"], g["store_bytes"]))
    never = {label for label, held in held_other.items() if not held}
    keep = lambda name: name.split(":")[0] not in never
    reason = "the tree never held a process other than the spawned launcher"
    g1 = {k: {"p50": pctl(v, 50), "p95": pctl(v, 95), "n": len(v)} for k, v in cpu.items() if keep(k)}
    g2 = {k: {"max_mib": max(v), "p95_mib": pctl(v, 95), "n": len(v)} for k, v in vram.items() if keep(k)}
    hours = max((tr.t1 - tr.t0) / 3_600_000, 1e-9)
    g4 = {"bytes_written_per_hour": {k: sum(max(v) - min(v) for v in pids.values() if v) / hours for k, pids in io.items() if keep(k)}}
    g4["store_growth_per_day_bytes"] = (store[-1][1] - store[0][1]) / max(store[-1][0] - store[0][0], 1) * 86_400_000 if len(store) >= 2 else None

    def frame_stats(log):
        by_mode = defaultdict(lambda: {"p50": [], "p99": [], "fps": [], "max": []})
        for e in log:
            if e["type"] == "frames":
                for k in ("p50", "p99", "fps", "max"):
                    by_mode[e["mode"]][k].append(e[k])
        return {m: {k: statistics.median(v) if k != "max" else max(v) for k, v in d.items()} for m, d in by_mode.items()}

    share = defaultdict(list)  # phase, or Outer Wilds' window mode -> 3D-engine share of the game processes
    modes = tr.outer_wilds_modes()
    mode_times = [m["t"] for m in modes]
    for g in tr.guard:
        if g.get("t") == "guard":
            i = bisect.bisect_right(mode_times, g["wall_ms"]) - 1
            for p in g["trees"].get("game", {}).values():
                if p.get("gpu3d_pct") is not None:
                    outer_wilds = p["exe"].lower().startswith("outerwilds")
                    share[f"outer_wilds/{modes[i]['mode'] if i >= 0 else 'unknown'}" if outer_wilds else g.get("phase", "run")].append(p["gpu3d_pct"])
    g3 = {"with_akashic": frame_stats(tr.game), "without_akashic": frame_stats(tr.game_ref) if tr.game_ref else nul("no reference pass"),
          "game_tree_3d_share_pct": {ph: {"p50": pctl(v, 50), "p95": pctl(v, 95), "n": len(v)} for ph, v in share.items()} or nul("no 3D samples")}
    return {"G1": g1 or nul("no samples"), "G2": g2 or nul("no samples"), "G3": g3, "G4": g4,
            "null_trees": {label: reason for label in sorted(never)}}


def fidelity(tr: Truth, profile: dict) -> dict:
    """The profile figures as the OS and player logs show them: foreground runs, audible spans, spoken
    utterances and title changes."""
    runs = []
    for r in tr.fg_runs():
        if r["app"] and "#private" not in str(r["window"]):
            block = next((b["kind"] for b in tr.scn["blocks"] if b["t0_ms"] <= r["t0"] - tr.t0 < b["t1_ms"]), "desk")
            runs.append({"t0_ms": r["t0"] - tr.t0, "t1_ms": r["t1"] - tr.t0, "cat": sc.category(r["app"], "-v" in r["window"]), "monitor": r["monitor"], "block": block})
    aud = audible_spans(tr)
    call_heard = merge(list(tr.call_spans().values()))
    bg = {}
    for kind in ("gaming", "desk"):
        blocks = [(tr.t0 + b["t0_ms"], tr.t0 + b["t1_ms"]) for b in tr.scn["blocks"] if b["kind"] == kind]
        secs = sum(b - a for a, b in blocks)
        call, game = call_heard, merge(aud.get("qualitygame.exe", []))
        both = [(max(a, c), min(b, d)) for a, b in call for c, d in game if max(a, c) < min(b, d)]
        bg[kind] = {k: overlap_ms(v, blocks) / secs for k, v in (("call", call), ("game", game), ("both", both))} if secs else {}
    langs = Counter()
    for u in tr.utterances():
        if u["source"] != "video":
            langs[u["lang"]] += u["t1"] - u["t0"]
    total = sum(langs.values()) or 1
    titles = {"gaming": 0, "desk": 0}
    for d in tr.driver:
        if d["action"] == "title_change":
            titles["gaming" if any(b["kind"] == "gaming" and b["t0_ms"] <= d["wall_ms"] - tr.t0 < b["t1_ms"] for b in tr.scn["blocks"]) else "desk"] += 1
    m = sc.measure(runs, tr.scn["blocks"], bg, titles, {k: v / total for k, v in langs.items()})
    rows = sc.compare(m, profile, tr.scn["length"], sc.LENGTH_APPS[tr.scn["length"]], tr.scn["run_p90_s"])
    return {"rows": rows, "source": "OS and player logs", "failed": [r["name"] for r in rows if not r["ok"]]}


def counters(tr: Truth) -> dict:
    refused = sum(1 for e in tr.pages if e.get("type") in ("refused", "refused_pip"))
    return {"late_actions_over_1s": sum(1 for d in tr.driver if d.get("lateness_ms", 0) > 1000),
            "focus_misses": sum(1 for d in tr.driver if d["action"] in ("focus_miss", "click_miss")), "refused_plays": refused,
            "launch_failures": sum(1 for d in tr.driver if d["action"] == "launch_failed")}


# --- coverage and report --------------------------------------------------------------------


def happened(tr: Truth) -> set[str]:
    """Ids of the steps the logs show were carried out: focus honoured, play event, player start, event action."""
    out = {d["step"] for d in tr.driver if d.get("step") and d["action"] in (
        "focus", "title_change", "canary", "busy_disk", "pause", "resume", "private_page", "vault_page", "popup_open", "lock_done", "sleep_done", "device_change_done")}
    played = {tr.pages_video(e) for e in tr.pages if e.get("type") == "media" and e.get("ev") == "play"}
    pip = [e["wall_ms"] for e in tr.pages if e.get("type") == "pip" and e.get("state") == "enter"]
    started = {e["id"].rsplit("-", 1)[0] for log in tr.players.values() for e in log if e.get("type") == "start"}
    sound = merge([span for spans in audible_spans(tr).values() for span in spans])
    steps = tr.scn["steps"]
    for s in steps:
        a, b = tr.span(s)
        kind = s["kind"]
        if kind == "video" and s["params"]["title_id"] in played:
            out.add(s["id"])
        elif kind == "pip" and any(a <= t <= b for t in pip):
            out.add(s["id"])
        elif kind in ("call", "game_sound", "music", "voice_elsewhere") and s["id"] in started:
            out.add(s["id"])
        elif kind == "both" and any(x["kind"] == "call" and x["id"] in started and x["t0_ms"] < s["t1_ms"] and s["t0_ms"] < x["t1_ms"] for x in steps):
            out.add(s["id"])
        elif kind == "silence" and overlap_ms(sound, [(a, b)]) < 0.2 * (b - a):
            out.add(s["id"])
    return out


def coverage(tr: Truth, coverage_toml: dict) -> dict:
    """n per cell from the steps the logs show were carried out; a planned step that failed fills nothing."""
    done = happened(tr)
    counts = sc.cell_counts([s for s in tr.scn["steps"] if s["id"] in done], coverage_toml)
    length = tr.scn["length"]
    required = {f"{d}|{c}" for d, c in sc.required_cells(coverage_toml, length)}
    missing = sorted(k for k in required if counts[k] == 0)
    return {"length": length, "n": counts, "required_cells": len(required), "missing": missing,
            "impossible": {f"{d}|{c}": r for (d, c), r in sc.impossible_cells(coverage_toml, length).items()}}


def score(run_id: str, now_ms: int | None = None) -> dict:
    import time

    run = RESULTS / run_id
    tr = Truth(run)
    profile = load_toml(sc.PROFILE)
    cov_toml = load_toml(sc.COVERAGE)
    paths, key, conn, media = open_store(run_id)
    scan = FrameScan(conn, media)
    names = monitor_names(conn)
    now = now_ms or int(time.time() * 1000)
    scores: dict = {"run_id": run_id, "length": tr.scn["length"], "seed": tr.scn["seed"], "drive_seconds": (tr.t1 - tr.t0) / 1000}
    scores["driver"] = counters(tr)
    scores["drain_timed_out"] = next((e["timed_out"] for e in read_jsonl(run / "truth" / "run.jsonl") if e.get("type") == "drain_result"), None)
    for part in (lambda: m1(tr, conn, scan, names), lambda: m2(tr, conn), lambda: m3(tr, conn), lambda: m4(tr, conn), lambda: m5(tr, conn, now),
                 lambda: m6(tr, conn), lambda: m7(tr, conn), lambda: m8_m9(tr, conn), lambda: m10(tr, run, run_id, conn, scan, paths),
                 lambda: m11(tr, conn), lambda: m12(tr, conn), lambda: m13(tr, conn, now)):
        scores.update(part())
    scores.update(guardrails(tr))
    if tr.scn["length"] in ("standard", "day"):
        scores["profile_fidelity"] = fidelity(tr, profile)
    cov = coverage(tr, cov_toml)
    write_json(run / "scores.json", scores)
    write_json(run / "coverage.json", cov)
    (run / "summary.md").write_text(summary(scores, cov), encoding="utf-8", newline="\n")
    conn.close()
    return scores


def summary(scores: dict, cov: dict) -> str:
    lines = [f"# Run {scores['run_id']}", "", f"Length {scores['length']}, seed {scores['seed']}, drive {scores['drive_seconds']:.0f} s.", ""]
    lines += ["| Metric | Value |", "| --- | --- |"]
    for k in sorted(scores):
        v = scores[k]
        if k.startswith(("M", "G")) and isinstance(v, dict):
            head = v.get("value", v.get("reason", ""))
            lines.append(f"| {k} | {json.dumps(head, default=str)[:90] if head != '' else 'see scores.json'} |")
    lines += ["", f"Coverage: {len(cov['missing'])} required cells missing of {cov['required_cells']}."]
    if scores.get("profile_fidelity"):
        lines.append(f"Profile fidelity out of tolerance: {', '.join(scores['profile_fidelity']['failed']) or 'none'}.")
    lines.append("Deferred M10.1 location not scanned: export (app shell out of scope for this harness).")
    return "\n".join(lines) + "\n"


def coverage_cmd(run_id: str) -> int:
    cov = json.loads((RESULTS / run_id / "coverage.json").read_text(encoding="utf-8"))
    if cov["missing"]:
        print("cells with n = 0 and no impossible entry:\n" + "\n".join(cov["missing"]))
        return 1
    print("coverage complete")
    return 0


def flat(d, prefix=""):
    for k, v in d.items():
        if isinstance(v, dict):
            yield from flat(v, f"{prefix}{k}.")
        elif isinstance(v, (int, float)) and not isinstance(v, bool):
            yield f"{prefix}{k}", v


def compare(a: str, b: str) -> int:
    ma = json.loads((RESULTS / a / "machine.json").read_text(encoding="utf-8"))
    mb = json.loads((RESULTS / b / "machine.json").read_text(encoding="utf-8"))
    if ma["fingerprint"] != mb["fingerprint"]:
        print("refused: the runs come from different machines")
        return 2
    sa = json.loads((RESULTS / a / "scores.json").read_text(encoding="utf-8"))
    sb = json.loads((RESULTS / b / "scores.json").read_text(encoding="utf-8"))
    fa, fb = dict(flat(sa)), dict(flat(sb))
    scn_a, scn_b = RESULTS / a / "scenario.json", RESULTS / b / "scenario.json"
    ha, hb = json.loads(scn_a.read_text(encoding="utf-8"))["hashes"], json.loads(scn_b.read_text(encoding="utf-8"))["hashes"]
    for h in ("profile", "coverage", "manifest"):
        if ha[h] != hb[h]:
            print(f"flag: {h} hash differs")
    if sha256_file(scn_a) != sha256_file(scn_b):
        print("flag: scenario differs")
    diffs = 0
    for k in sorted(set(fa) & set(fb)):
        x, y = fa[k], fb[k]
        if k.endswith(".n") or (x == 0 and y == 0):
            continue
        if abs(x - y) > 0.05 * max(abs(x), abs(y)):
            diffs += 1
            n_key = k.rsplit(".", 1)[0] + ".n"
            print(f"{k}: {x:.4g} vs {y:.4g} (n {fa.get(n_key, '?')} vs {fb.get(n_key, '?')})")
    return 1 if diffs else 0


if __name__ == "__main__":
    score(sys.argv[1])
