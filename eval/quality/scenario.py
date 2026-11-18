"""Seeded scenario generator: (profile, coverage, length, seed) -> scenario.json, byte-identical.

    python scenario.py LENGTH [--seed N] [--out scenario.json]

Times in the file are milliseconds from the start of the drive. Monitors are named
primary / right / left and resolved to rectangles at run time.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import statistics
import sys
from pathlib import Path

from common import ASSETS, LENGTHS, QUALITY, load_toml, sha256_file

PROFILE = QUALITY / "profile.toml"
COVERAGE = QUALITY / "coverage.toml"
MANIFEST = ASSETS / "manifest.json"
MONITORS = ("primary", "right", "left")
ATTEMPTS = 300
MIN_RUN_MS = 2_000
MIN_SHAPED_RUNS = 8  # below this a block has too few runs to pin a median and a p90
P90_SCALE = {("standard", "desk"): 0.5}  # a ~42 min desk block cannot hold the profile's 600 s p90 with its focus shares
MID_SHAPE = 3 # keeps the runs between the median and the p90 short, so the tail can carry the time
HUNG_MAX_MS = 90_000
HUNG_PRELAUNCH_MS = 8_000
POPUP_MIN_MS = 10_000  # a popup needs time to open and close inside its run
PIP_TAIL_S = 30  # Picture in Picture outlives the video page's last run
LOCAL_SEARCH_STEPS = 4000
SLOT_CANDIDATES = 100
PRIVATE_MIN_MS = 12_000  # a private window needs time to open and load its page
VIDEO_PAGE_LOOK = (16, "dark", "en")  # font px, theme, language
SHARE_GROUPS = ("gaming.focus", "desk.focus", "gaming.monitor", "desk.monitor", "shared.language")
LANGS = ("fr", "en", "mixed")
DESK_OVERLAP_S = 60  # seconds of the desk call that overlap a video
DESK_OVERLAP_MIN_S = 20
LANG_OVER_PP = 0.03  # a language that is this far over its share is skipped for the next utterance
FONTS = (9, 10, 11, 13, 14, 16, 18, 20, 24)
SMALL_FONT_PX = 11
SILENCE_MIN_S = 20
TOL = {  # standard / day
    "focus": (5, 3), "monitor": (5, 3), "switches": (0.15, 0.15), "run": (0.25, 0.25),
    "background": (5, 5), "titles": (0.20, 0.20), "language": (5, 5),
}
DURATION_MS = {"smoke": 300_000, "standard": 3_900_000, "attended": 720_000}
ATTENDED_EVENTS = (("lock", 108_000, 30_000), ("sleep", 274_000, 60_000),
                   ("device_change", 418_000, 20_000), ("elevated", 562_000, 30_000))

# app -> (case tag or None, allowed monitors, kind)
APPS = {
    "game": (None, ("primary",), "game"),
    "outer_wilds": (None, ("primary",), "game"),
    "discord": ("discord", ("left",), ""),  # one window, so one monitor
    "chrome": ("chrome", MONITORS, "browser"),
    "edge": ("edge", MONITORS, "browser"),
    "zen": ("zen", MONITORS, "browser"),
    "vscode": ("code_project", MONITORS, "text"),
    "terminal": (None, MONITORS, "text"),
    "explorer": (None, MONITORS, ""),
    "notepad": (None, MONITORS, "text"),
    "calculator": ("uwp", MONITORS, ""),
    "assistant": ("pwa", MONITORS, "browser"),
    "steam": ("launcher", MONITORS, ""),
    "hung": ("hung_ghost", MONITORS, ""),
    "elevated": ("elevated", MONITORS, ""),
}
# profile focus category -> apps that can show it
CAT_APPS = {
    "game": ("game", "outer_wilds"), "voice_chat": ("discord",), "browser": ("chrome", "edge", "zen"), "editor": ("vscode",),
    "terminal": ("terminal",), "assistant": ("assistant",), "video": ("chrome", "edge"),
    "other": ("explorer", "notepad", "calculator", "steam", "hung"),
}
APP_CAT = {app: cat for cat, members in CAT_APPS.items() if cat != "video" for app in members}
LENGTH_APPS = {
    "smoke": {"game", "discord", "chrome", "vscode"},
    "standard": set(APPS) - {"elevated"},
    "day": set(APPS) - {"elevated"},
    "attended": {"game", "discord", "chrome", "vscode", "calculator"},
}
PAGE_KINDS = ("article", "forum", "docs_code", "chat_growing")
SCROLL_PAGES = ("article", "forum", "docs_code")
UNATTENDED_EVENTS = ("pause", "excluded_pages", "canary", "busy_disk")
PRIVATE_KINDS = ("chrome_incognito", "vault", "edge_inprivate", "zen_private")
PRIVATE_BROWSER = {"chrome_incognito": "chrome", "vault": None, "edge_inprivate": "edge", "zen_private": "zen"}
CASE_GROUPS = ("apps", "content", "audio", "life")
EVENT_DIMS = (10, 13)  # privacy canaries and life-event loss


class ProfileError(SystemExit):
    pass


def leaves(table: dict, prefix: str = ""):
    for key, val in table.items():
        path = f"{prefix}{key}"
        if isinstance(val, dict) and not ({"value", "source"} & val.keys()):
            yield from leaves(val, path + ".")
        else:
            yield path, val


def validate_profile(profile: dict) -> None:
    for path, leaf in leaves(profile):
        if not isinstance(leaf, dict) or "value" not in leaf or not leaf.get("source"):
            raise ProfileError(f"profile.toml: leaf {path} has no value or no source")
    for group in SHARE_GROUPS:
        node = profile
        for part in group.split("."):
            node = node[part]
        total = sum(v["value"] for v in node.values())
        if abs(total - 1) > 0.001:
            raise ProfileError(f"profile.toml: share group {group} sums to {total:.4f}, not 1")


def pv(profile: dict, path: str):
    node = profile
    for part in path.split("."):
        node = node[part]
    return node["value"]


def shares(profile: dict, path: str) -> dict[str, float]:
    node = profile
    for part in path.split("."):
        node = node[part]
    return {k: v["value"] for k, v in node.items()}


def case_ids(coverage: dict) -> dict[str, dict]:
    return {c["id"]: c for c in coverage["case"]}


def expand(spec, coverage: dict, universe: list):
    groups = {f"@{g}": [c["id"] for c in coverage["case"] if c["group"] == g] for g in CASE_GROUPS}
    if spec == "all":
        return list(universe)
    if isinstance(spec, str):
        return groups[spec]
    return [c for x in spec for c in (groups[x] if isinstance(x, str) and x.startswith("@") else [x])]


def impossible_cells(coverage: dict, length: str) -> dict[tuple[int, str], str]:
    out: dict[tuple[int, str], str] = {}
    dims = [int(d) for d in coverage["dimensions"]]
    cases = [c["id"] for c in coverage["case"]]
    for entry in coverage["impossible"]:
        if "lengths" in entry and length not in entry["lengths"]:
            continue
        for d in expand(entry["dims"], coverage, dims):
            for c in expand(entry["cases"], coverage, cases):
                out[(d, c)] = entry["reason"]
    return out


def required_cells(coverage: dict, length: str) -> list[tuple[int, str]]:
    """Cells the length must fill. Smoke: core cases; attended: the attended-only cases."""
    skip = impossible_cells(coverage, length)
    cases = coverage["case"]
    if length == "smoke":
        cases = [c for c in cases if c.get("core")]
    elif length == "attended":
        cases = [c for c in cases if c["id"] in {x[0] for x in ATTENDED_EVENTS}]
    return [(int(d), c["id"]) for c in cases for d in coverage["dimensions"] if (int(d), c["id"]) not in skip]


def cell_counts(steps: list[dict], coverage: dict) -> dict[str, int]:
    """n per cell ("d|case"): truth items of dimension d overlapping a step tagged with the case. Items of the
    planted-event dimensions count only under the tags of their own step: they are attributed to the event."""
    by_case: dict[str, list[tuple[int, int]]] = {}
    for s in steps:
        for c in s["tags"]:
            by_case.setdefault(c, []).append((s["t0_ms"], s["t1_ms"]))
    counts = {f"{d}|{c['id']}": 0 for d in coverage["dimensions"] for c in coverage["case"]}
    for item in steps:
        for d in item["dims"]:
            if d in EVENT_DIMS:
                for c in item["tags"]:
                    counts[f"{d}|{c}"] = 1
                continue
            for c, spans in by_case.items():
                if any(a < item["t1_ms"] and item["t0_ms"] < b for a, b in spans):
                    counts[f"{d}|{c}"] = 1
    return counts


def cells_added(step: dict, steps: list[dict]) -> set[tuple[int, str]]:
    """Cells the step would fill next to the steps it overlaps (same attribution as cell_counts)."""
    out = {(d, c) for d in step["dims"] if d in EVENT_DIMS for c in step["tags"]}
    for other in steps:
        if other["t0_ms"] < step["t1_ms"] and step["t0_ms"] < other["t1_ms"]:
            out |= {(d, c) for d in step["dims"] if d not in EVENT_DIMS for c in other["tags"]}
            out |= {(d, c) for d in other["dims"] if d not in EVENT_DIMS for c in step["tags"]}
    return out


def best_slot(step: dict, length_ms: int, starts: list[int], steps: list[dict], coverage: dict, length: str) -> int:
    """The start that fills the most required cells no other step fills yet and, above all, no impossible cell."""
    open_cells = {(int(k.split("|")[0]), k.split("|")[1]) for k, n in cell_counts(steps, coverage).items() if not n} & set(required_cells(coverage, length))
    forbidden = set(impossible_cells(coverage, length))

    def score(t: int) -> int:
        cells = cells_added(step | {"t0_ms": t, "t1_ms": t + length_ms}, steps)
        return len(cells & open_cells) - 1000 * len(cells & forbidden)

    return max(starts, key=score)


def cell_problems(steps: list[dict], coverage: dict, length: str) -> list[str]:
    """Required cells with n = 0, and cells coverage.toml calls impossible that the steps fill anyway."""
    counts = cell_counts(steps, coverage)
    missing = [f"cell {d}|{c} is empty" for d, c in required_cells(coverage, length) if counts[f"{d}|{c}"] == 0]
    filled = [f"impossible cell {d}|{c} is filled" for d, c in impossible_cells(coverage, length) if counts[f"{d}|{c}"]]
    return missing + filled


# --- run lengths and who is focused -------------------------------------------------------------


def block_plan(length: str, profile: dict, rng: random.Random) -> tuple[int, list[dict]]:
    share = pv(profile, "blocks.gaming_share")
    if length != "day":
        total = DURATION_MS[length]
        g = round(total * share)
        return total, [{"kind": "gaming", "t0_ms": 0, "t1_ms": g}, {"kind": "desk", "t0_ms": g, "t1_ms": total}]
    total = round((6 + 2 * rng.random()) * 60) * 60_000
    g_total = round(total * share)
    block_pair_min = pv(profile, "blocks.gaming_block_min") + pv(profile, "blocks.desk_block_min")
    cycles = min(pv(profile, "shared.videos.per_day_max"), max(3, round(total / 60_000 / block_pair_min)))  # a video per desk block

    def split(amount: int) -> list[int]:
        w = [0.6 + rng.random() * 0.8 for _ in range(cycles)]
        parts = [round(amount * x / sum(w)) for x in w]
        parts[-1] += amount - sum(parts)
        return parts

    blocks, t = [], 0
    for g, d in zip(split(g_total), split(total - g_total), strict=True):
        blocks += [{"kind": "gaming", "t0_ms": t, "t1_ms": t + g}, {"kind": "desk", "t0_ms": t + g, "t1_ms": t + g + d}]
        t += g + d
    return total, blocks


def run_lengths(n: int, median_ms: float, p90_ms: float, total_ms: int, rng: random.Random) -> list[int] | None:
    """Ascending run lengths with the median and the p90 pinned by rank and a heavy tail that takes the
    rest of the block. None when the tail would need negative time."""
    if n < MIN_SHAPED_RUNS:
        return None
    med_lo, med_hi = (n - 1) // 2, n // 2
    pos = 0.9 * (n - 1)
    p_lo, p_hi = math.floor(pos), math.ceil(pos)
    tail_start = p_hi if p_hi > p_lo else p_lo + 1
    if p_lo <= med_hi or tail_start >= n:
        return None
    lens = [0.0] * n
    for k in range(tail_start):
        if k < med_lo:
            lens[k] = min(median_ms, max(MIN_RUN_MS, (MIN_RUN_MS + (median_ms - MIN_RUN_MS) * k / med_lo) * rng.uniform(0.9, 1.1)))
        elif k <= med_hi:
            lens[k] = median_ms
        elif k < p_lo:
            f = (k - med_hi) / (p_lo - med_hi)
            lens[k] = min(p90_ms, max(median_ms, median_ms * (p90_ms / median_ms) ** (f ** MID_SHAPE) * rng.uniform(0.95, 1.05)))
        else:
            lens[k] = p90_ms
    weights = [((k - tail_start + 1) / (n - tail_start)) ** 2 for k in range(tail_start, n)]
    stretch = (total_ms - sum(lens) - p90_ms * len(weights)) / (p90_ms * sum(weights))
    if stretch < 0:
        return None
    for k, w in zip(range(tail_start, n), weights, strict=True):
        lens[k] = p90_ms * (1 + stretch * w)
    out = [round(x) for x in lens]
    out[-1] += total_ms - sum(out)
    return out


# deviations from the profile tried in order: fewer switches and a lower p90 stay inside the tolerances
DEVIATIONS = sorted(((ns, qs) for ns in (1.0, 0.95, 0.9, 0.87) for qs in (1.0, 0.9, 0.8, 0.75)), key=lambda d: (1 - d[0]) / 0.15 + (1 - d[1]) / 0.25)


def plan_lengths(n_profile: float, n_min: int, median_s: float, p90_s: float, total_ms: int, rng: random.Random) -> list[int]:
    n_exact = max(round(n_profile), n_min)
    for ns, qs in DEVIATIONS:
        n = max(round(n_exact * ns), n_min)
        lens = run_lengths(n, median_s * 1000, p90_s * 1000 * qs, total_ms, rng)
        if lens:
            return lens
    raw = [rng.uniform(0.6, 1.4) for _ in range(n_exact)]
    out = [round(total_ms * x / sum(raw)) for x in raw]
    out[-1] += total_ms - sum(out)
    return out


def assign_greedy(items: list[int], weights: dict[str, float], total: int, allowed) -> dict[int, str]:
    """Largest item first to the key with the biggest deficit. allowed(key, item) may refuse."""
    got = dict.fromkeys(weights, 0)
    out: dict[int, str] = {}
    for i in sorted(range(len(items)), key=lambda i: -items[i]):
        keys = [k for k in weights if allowed(k, items[i])] or list(weights)
        k = max(keys, key=lambda k: weights[k] * total - got[k])
        out[i] = k
        got[k] += items[i]
    return out


def balance(lens: list[int], targets: dict[str, float], floors: dict[str, int], rng: random.Random) -> list[str]:
    """A category per run so each category's share of the time is close to its target. At most half
    the runs may share a category (consecutive runs must differ) and each category gets at least
    its floor of runs when the block has enough runs for all floors."""
    n, total = len(lens), sum(lens)
    keys = list(targets)
    cap = (n + 1) // 2
    if n < sum(floors.values()):
        floors = dict.fromkeys(keys, 0)
    cat = [""] * n
    sums, counts = dict.fromkeys(keys, 0), dict.fromkeys(keys, 0)
    for i in sorted(range(n), key=lambda i: -lens[i]):
        ok = [k for k in keys if counts[k] < cap] or keys
        k = max(ok, key=lambda k: targets[k] * total - sums[k])
        cat[i] = k
        sums[k] += lens[i]
        counts[k] += 1
    for k in keys:
        while counts[k] < floors[k]:
            donors = [i for i in range(n) if counts[cat[i]] > floors[cat[i]]]
            i = min(donors, key=lambda i: lens[i])
            sums[cat[i]] -= lens[i]
            counts[cat[i]] -= 1
            cat[i] = k
            sums[k] += lens[i]
            counts[k] += 1

    def error(s: dict[str, int]) -> float:
        return sum(abs(s[k] / total - targets[k]) for k in keys)

    best = error(sums)
    for _ in range(LOCAL_SEARCH_STEPS):
        i = rng.randrange(n)
        if rng.random() < 0.5:
            k = rng.choice(keys)
            old = cat[i]
            if k == old or counts[k] >= cap or counts[old] <= floors[old]:
                continue
            trial = {**sums, old: sums[old] - lens[i], k: sums[k] + lens[i]}
            if error(trial) < best - 1e-12:
                best, sums = error(trial), trial
                counts[old] -= 1
                counts[k] += 1
                cat[i] = k
        else:
            j = rng.randrange(n)
            a, b = cat[i], cat[j]
            if a == b:
                continue
            trial = {**sums, a: sums[a] - lens[i] + lens[j], b: sums[b] - lens[j] + lens[i]}
            if error(trial) < best - 1e-12:
                best, sums = error(trial), trial
                cat[i], cat[j] = b, a
    return cat


def interleave(cats: list[str], rng: random.Random) -> list[int]:
    """Run indices in a random order where consecutive runs differ in category when that is possible."""
    pending: dict[str, list[int]] = {}
    for i, c in enumerate(cats):
        pending.setdefault(c, []).append(i)
    for idxs in pending.values():
        rng.shuffle(idxs)
    order: list[int] = []
    prev = None
    while len(order) < len(cats):
        left = len(cats) - len(order)
        live = [k for k in pending if pending[k]]
        forced = [k for k in live if len(pending[k]) * 2 > left]
        options = forced or [k for k in live if k != prev] or live
        k = rng.choices(options, [len(pending[x]) for x in options])[0]
        order.append(pending[k].pop())
        prev = k
    return order


def cat_apps_in(cat: str, apps: set[str]) -> list[str]:
    return [a for a in CAT_APPS[cat] if a in apps]


def run_p90_s(profile: dict, length: str) -> dict[str, float]:
    """The p90 of the focus runs each block is generated and checked against, recorded in scenario.json."""
    return {kind: pv(profile, f"{kind}.run.p90_s") * P90_SCALE.get((length, kind), 1.0) for kind in ("gaming", "desk")}


def block_runs(kind: str, bi: int, block: dict, p90_s: float, profile: dict, apps: set[str], rng: random.Random) -> list[dict]:
    dur = block["t1_ms"] - block["t0_ms"]
    focus = {c: v for c, v in shares(profile, f"{kind}.focus").items() if cat_apps_in(c, apps)}
    norm = sum(focus.values())
    focus = {c: v / norm for c, v in focus.items()}
    floors = {c: len(cat_apps_in(c, apps)) if c == "other" else 1 for c in focus}
    per_hour = pv(profile, f"{kind}.run.switches_per_hour")
    lens = plan_lengths(per_hour * dur / 3_600_000, len(focus), pv(profile, f"{kind}.run.median_s"), p90_s, dur, rng)
    cats = balance(lens, focus, floors, rng)
    runs, t = [], block["t0_ms"]
    for i in interleave(cats, rng):
        runs.append({"t0_ms": t, "t1_ms": t + lens[i], "cat": cats[i], "block": kind, "bi": bi})
        t += lens[i]
    return runs


def resolve_apps(runs: list[dict], profile: dict, apps: set[str], rng: random.Random) -> None:
    """Pick the app of each run from its category: Outer Wilds takes a share of the game time, the browser
    categories rotate over the installed browsers (longest runs first, so each browser gets a long one), and each block's "other" runs rotate over the other apps
    (the shortest run goes to the hung window, which cannot outlive HUNG_MAX_MS)."""
    for r in runs:
        r["app"] = cat_apps_in(r["cat"], apps)[0]
    game = [r for r in runs if r["cat"] == "game"]
    if game and "outer_wilds" in apps:
        ow = pv(profile, "gaming.game.outer_wilds_share")
        total = sum(r["t1_ms"] - r["t0_ms"] for r in game)
        pick = assign_greedy([r["t1_ms"] - r["t0_ms"] for r in game], {"game": 1 - ow, "outer_wilds": ow}, total, lambda *_: True)
        for i, r in enumerate(game):
            r["app"] = pick[i]
    browsers = cat_apps_in("browser", apps)
    start = rng.randrange(len(browsers)) if browsers else 0
    for k, r in enumerate(sorted((r for r in runs if r["cat"] == "browser"), key=lambda r: r["t0_ms"] - r["t1_ms"])):  # longest first
        r["app"] = browsers[(start + k) % len(browsers)]
    for bi in {r["bi"] for r in runs if r["cat"] == "other"}:
        others = sorted((r for r in runs if r["cat"] == "other" and r["bi"] == bi), key=lambda r: r["t1_ms"] - r["t0_ms"])
        pool = [a for a in cat_apps_in("other", apps) if a != "hung"]
        if "hung" in apps and others[0]["t1_ms"] - others[0]["t0_ms"] <= HUNG_MAX_MS:
            others.pop(0)["app"] = "hung"
        for k, r in enumerate(sorted(others, key=lambda r: r["t0_ms"])):
            r["app"] = pool[k % len(pool)]


def plan_videos(runs: list[dict], length: str, total: int, profile: dict, apps: set[str]) -> list[dict]:
    """Group the video-category runs into videos. A video plays across the runs of one desk block."""
    day_h = pv(profile, "shared.videos.day_hours")
    lo, hi = pv(profile, "shared.videos.per_day_min"), pv(profile, "shared.videos.per_day_max")
    n = 1 if length in ("smoke", "attended") else max(lo, min(hi, round(2.5 * total / 3_600_000 / day_h)))
    groups: list[list[dict]] = []
    for bi in sorted({r["bi"] for r in runs if r["cat"] == "video"}):
        groups.append([r for r in runs if r["cat"] == "video" and r["bi"] == bi])
    while len(groups) < n and any(len(g) > 1 for g in groups):  # one block holds several videos
        big = max(groups, key=len)
        groups[groups.index(big):groups.index(big) + 1] = [big[:len(big) // 2], big[len(big) // 2:]]
    keep = sorted(groups, key=lambda g: g[0]["t0_ms"])
    browsers = [a for a in CAT_APPS["video"] if a in apps]
    videos = []
    for k, group in enumerate(keep):
        meta = {"title_id": f"v{k + 1}", "episode": k == 0 and length in ("standard", "day"), "season": 1 + k, "number": 3 + k,
                "span_s": (group[-1]["t1_ms"] - group[0]["t0_ms"]) // 1000 + PIP_TAIL_S}
        videos.append({"k": k + 1, "app": browsers[k % len(browsers)], "runs": group, "meta": meta})
        for j, r in enumerate(group):
            r.update(app=videos[-1]["app"], video=meta, video_k=k + 1, page="video_episode" if meta["episode"] else "video_page")
            if j:
                r["keep_page"] = True
    return videos


def assign_monitors(runs: list[dict], profile: dict) -> None:
    """Per block kind, a monitor per unit (a run, or all runs of one video) so the shares match."""
    for kind in ("gaming", "desk"):
        rs = [r for r in runs if r["block"] == kind]
        if not rs:
            continue
        total = sum(r["t1_ms"] - r["t0_ms"] for r in rs)
        target = shares(profile, f"{kind}.monitor")
        units: dict[object, list[dict]] = {}
        for i, r in enumerate(rs):
            units.setdefault(("video", r["video_k"]) if "video_k" in r else i, []).append(r)
        got = dict.fromkeys(MONITORS, 0)
        for unit in sorted(units.values(), key=lambda u: -sum(r["t1_ms"] - r["t0_ms"] for r in u)):
            ok = [m for m in MONITORS if all(m in APPS[r["app"]][1] for r in unit)]
            m = max(ok, key=lambda m: target[m] * total - got[m])
            for r in unit:
                r["monitor"] = m
            got[m] += sum(r["t1_ms"] - r["t0_ms"] for r in unit)


def carve(runs: list[dict], spans: list[tuple[int, int]]) -> list[dict]:
    """Remove the spans from the runs (lock and sleep stop all activity)."""
    out = runs
    for a, b in spans:
        nxt = []
        for r in out:
            if r["t1_ms"] <= a or r["t0_ms"] >= b:
                nxt.append(r)
                continue
            if r["t0_ms"] < a:
                nxt.append({**r, "t1_ms": a})
            if r["t1_ms"] > b:
                nxt.append({**r, "t0_ms": b})
        out = nxt
    return out


def assign_quota(runs: list[dict], keys: dict[str, float]) -> dict[int, str]:
    total = sum(r["t1_ms"] - r["t0_ms"] for r in runs) or 1
    return assign_greedy([r["t1_ms"] - r["t0_ms"] for r in runs], keys, total, lambda *_: True)


# --- fidelity ---------------------------------------------------------------------------------


def percentile(xs: list[float], q: float) -> float:
    xs = sorted(xs)
    pos = (len(xs) - 1) * q
    lo = math.floor(pos)
    return xs[lo] + (xs[min(lo + 1, len(xs) - 1)] - xs[lo]) * (pos - lo)


def category(app: str, video: bool) -> str:
    """The profile focus category of a focused window, from the OS log's window and app."""
    return "video" if video else APP_CAT.get(app, "other")


def measure(runs: list[dict], blocks: list[dict], bg: dict[str, dict[str, float]], titles: dict[str, int], langs: dict[str, float]) -> dict:
    """Profile figures of a timeline. Used on the scenario and, by the scorer, on the OS and player logs.
    Runs carry t0_ms, t1_ms, cat, monitor and block."""
    out: dict = {"blocks": {}, "language": langs}
    for kind in ("gaming", "desk"):
        hours = sum(b["t1_ms"] - b["t0_ms"] for b in blocks if b["kind"] == kind) / 3_600_000
        rs = [r for r in runs if r["block"] == kind]
        if not rs:
            continue
        dur = sum(r["t1_ms"] - r["t0_ms"] for r in rs)
        lens = [(r["t1_ms"] - r["t0_ms"]) / 1000 for r in rs]
        focus: dict[str, float] = {}
        mon: dict[str, float] = {}
        for r in rs:
            focus[r["cat"]] = focus.get(r["cat"], 0) + (r["t1_ms"] - r["t0_ms"]) / dur
            mon[r["monitor"]] = mon.get(r["monitor"], 0) + (r["t1_ms"] - r["t0_ms"]) / dur
        out["blocks"][kind] = {
            "hours": hours, "focus": focus, "monitor": mon, "switches_per_hour": len(rs) / hours,
            "run_median_s": statistics.median(lens), "run_p90_s": percentile(lens, 0.9), "background": bg.get(kind, {}),
            "title_changes_per_hour": titles.get(kind, 0) / hours,
        }
    return out


def compare(measured: dict, profile: dict, length: str, apps: set[str], p90_s: dict[str, float]) -> list[dict]:
    col = 1 if length == "day" else 0
    rows: list[dict] = []

    def add(name: str, got, want, tol: float, pp: bool) -> None:
        got = got or 0.0
        err = abs(got - want) * 100 if pp else (abs(got - want) / want if want else abs(got))
        rows.append({"name": name, "measured": got, "profile": want, "tol": tol, "ok": err <= tol})

    for kind, m in measured["blocks"].items():
        focus = {c: v for c, v in shares(profile, f"{kind}.focus").items() if cat_apps_in(c, apps)}
        norm = sum(focus.values())
        for c, want in focus.items():
            add(f"{kind}.focus.{c}", m["focus"].get(c, 0), want / norm, TOL["focus"][col], True)
        for mon_name, want in shares(profile, f"{kind}.monitor").items():
            add(f"{kind}.monitor.{mon_name}", m["monitor"].get(mon_name, 0), want, TOL["monitor"][col], True)
        add(f"{kind}.switches_per_hour", m["switches_per_hour"], pv(profile, f"{kind}.run.switches_per_hour"), TOL["switches"][col], False)
        add(f"{kind}.run_median_s", m["run_median_s"], pv(profile, f"{kind}.run.median_s"), TOL["run"][col], False)
        add(f"{kind}.run_p90_s", m["run_p90_s"], p90_s[kind], TOL["run"][col], False)
        add(f"{kind}.title_changes_per_hour", m["title_changes_per_hour"], pv(profile, f"{kind}.run.title_changes_per_hour"), TOL["titles"][col], False)
        for key, name in (("call", "call_audible"), ("game", "game_sound"), ("both", "both")):
            if m["background"]:
                add(f"{kind}.background.{name}", m["background"].get(key, 0), pv(profile, f"{kind}.background.{name}"), TOL["background"][col], True)
    for lang, want in shares(profile, "shared.language").items():
        add(f"language.{lang}", measured["language"].get(lang, 0), want, TOL["language"][col], True)
    return rows


# --- tracks and events ------------------------------------------------------------------------


def take(mask: list[bool], amount: int, rng: random.Random, min_run: int = 5) -> list[bool]:
    """Random contiguous chunks of the True seconds of mask, `amount` seconds in total."""
    runs, i = [], 0
    while i < len(mask):
        if mask[i]:
            j = i
            while j < len(mask) and mask[j]:
                j += 1
            runs.append((i, j))
            i = j
        else:
            i += 1
    rng.shuffle(runs)
    out = [False] * len(mask)
    left = amount
    for a, b in runs:
        if left <= 0:
            break
        size = min(b - a, left)
        if size < min_run and left > min_run:
            continue
        start = a + (rng.randrange(b - a - size + 1) if b - a > size else 0)
        out[start:start + size] = [True] * size
        left -= size
    return out


def spans_of(mask: list[bool], base_ms: int) -> list[tuple[int, int]]:
    out, i = [], 0
    while i < len(mask):
        if mask[i]:
            j = i
            while j < len(mask) and mask[j]:
                j += 1
            out.append((base_ms + i * 1000, base_ms + j * 1000))
            i = j
        else:
            i += 1
    return out


def overlap_spans(a: list[tuple[int, int]], b: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Intersections of two lists of spans (each list's spans do not overlap)."""
    return [(max(x0, y0), min(x1, y1)) for x0, x1 in a for y0, y1 in b if max(x0, y0) < min(x1, y1)]


def overlap_s(a: list[tuple[int, int]], b: list[tuple[int, int]]) -> float:
    return sum(x1 - x0 for x0, x1 in overlap_spans(a, b)) / 1000


def background_tracks(blocks: list[dict], profile: dict, rng: random.Random, length: str, video_spans: list[tuple[int, int]]) -> tuple[dict, dict]:
    """Call, game sound, both, music as spans, plus the achieved shares per block kind. In the desk block part
    of the call overlaps a video and part of the music overlaps the call, so those cases always occur."""
    spans: dict[str, list[tuple[int, int]]] = {"call": [], "game": [], "both": [], "music": []}
    achieved: dict[str, dict[str, float]] = {}
    for kind in ("gaming", "desk"):
        secs = 0
        got = {"call": 0, "game": 0, "both": 0}
        for b in blocks:
            if b["kind"] != kind:
                continue
            n = (b["t1_ms"] - b["t0_ms"]) // 1000
            secs += n
            want_call = round(pv(profile, f"{kind}.background.call_audible") * n)
            call = take([True] * n, want_call, rng, 90)
            if kind == "desk" and video_spans:
                on_video = [any(a <= b["t0_ms"] + 1000 * i < z for a, z in video_spans) for i in range(n)]
                with_video = take(on_video, min(want_call, DESK_OVERLAP_S), rng, DESK_OVERLAP_MIN_S)
                call = [x or y for x, y in zip(with_video, take([not x for x in with_video], want_call - sum(with_video), rng, 90), strict=True)]
            want_game = round(pv(profile, f"{kind}.background.game_sound") * n)
            want_both = round(pv(profile, f"{kind}.background.both") * n)
            inside = take(call, want_both, rng) if want_both else [False] * n
            outside = take([not c for c in call], want_game - want_both, rng) if want_game > want_both else [False] * n
            game = [a or b2 for a, b2 in zip(inside, outside, strict=True)]
            both = [a and b2 for a, b2 in zip(call, game, strict=True)]
            for name, m in (("call", call), ("game", game), ("both", both)):
                spans[name] += spans_of(m, b["t0_ms"])
                got[name] += sum(m)
            if kind == "desk" and length != "attended":
                want_music = round(pv(profile, "shared.audio.music_share_desk") * n)
                under_call = take(call, min(want_music // 2, sum(call)), rng, DESK_OVERLAP_MIN_S)
                music = [x or y for x, y in zip(under_call, take([not x for x in under_call], want_music - sum(under_call), rng, 60), strict=True)]
                spans["music"] += spans_of(music, b["t0_ms"])
        achieved[kind] = {k: v / secs for k, v in got.items()} if secs else {}
    return spans, achieved


class Clips:
    """Rotating picks from the asset manifest, so a seed always picks the same clips."""

    def __init__(self, manifest: dict) -> None:
        self.by_speaker: dict[str, list[dict]] = {}
        for clip in manifest["clips"]:
            if clip["kind"] == "speech":
                self.by_speaker.setdefault(clip["speaker"], []).append(clip)
        self.idx: dict[str, int] = {}
        self.speakers = manifest["speakers"]

    def next(self, speaker: str) -> dict:
        pool = self.by_speaker[speaker]
        i = self.idx.get(speaker, 0)
        self.idx[speaker] = i + 1
        return pool[i % len(pool)]

    def of(self, role: str, lang: str | None = None) -> list[str]:
        return [s for s, meta in self.speakers.items() if meta["role"] == role and lang in (None, meta["lang"])]


class LangMix:
    """Draws the language of each spoken utterance from shared.language. The draw is weighted by the share
    over the mean clip length, so the seconds follow the shares; a language already over its share is skipped."""

    def __init__(self, want: dict[str, float], clips: Clips, rng: random.Random) -> None:
        self.want, self.rng = want, rng
        self.secs = dict.fromkeys(LANGS, 0.0)
        pool = {lang: [c["dur_s"] for spk, cs in clips.by_speaker.items() for c in cs if clips.speakers[spk]["lang"] == lang and clips.speakers[spk]["role"] in ("call", "user", "extra")] for lang in LANGS}
        self.mean = {lang: statistics.mean(durs) for lang, durs in pool.items()}

    def draw(self) -> str:
        total = sum(self.secs.values())
        open_langs = [lang for lang in LANGS if self.secs[lang] <= (self.want[lang] + LANG_OVER_PP) * total] or list(LANGS)
        return self.rng.choices(open_langs, [self.want[lang] / self.mean[lang] for lang in open_langs])[0]

    def add(self, lang: str, seconds: float) -> None:
        self.secs[lang] += seconds

    def shares(self) -> dict[str, float]:
        total = sum(self.secs.values()) or 1.0
        return {lang: v / total for lang, v in self.secs.items()}


def call_session(t0: int, t1: int, cast: list[str], clips: Clips, rng: random.Random, mix: LangMix, elsewhere: bool = False) -> tuple[list[dict], bool]:
    """Back-to-back turns of the cast and, in a call, the user's own voice. The language of each turn is
    drawn from the mix; mixed turns are a spliced FR/EN clip. Every 8th turn overlaps the previous one."""
    out, t, overlapped = [], t0 + 500, False
    own = {"fr": "user_fr1", "en": "user_en1", "mixed": "mixed1"}
    while True:
        lang = mix.draw()
        is_user = len(out) % 3 == 2 and not elsewhere
        if is_user:
            who = own[lang]
        elif lang == "mixed":
            who = "mixed1"
        else:
            who = rng.choice([s for s in cast if clips.speakers[s]["lang"] == lang])
        clip = clips.next(who)
        end = t + round(clip["dur_s"] * 1000)
        if end > t1:
            return out, overlapped
        out.append({"t_ms": t, "dur_ms": round(clip["dur_s"] * 1000), "speaker": who, "clip": clip["id"], "stream": "own" if is_user else ("elsewhere" if elsewhere else "call")})
        mix.add(lang, clip["dur_s"])
        gap = rng.choice((400, 900, 1600, 2600))
        if len(out) % 8 == 7:
            gap, overlapped = -600, True
        t = end + gap


def fill_ids(step: dict, n: int) -> dict:
    step["id"] = f"s{n:04d}"
    return step


def build(length: str, seed: int, profile: dict, coverage: dict, manifest: dict, hashes: dict) -> dict:
    apps = LENGTH_APPS[length]
    last_error = "no attempt"
    for attempt in range(ATTEMPTS):
        rng = random.Random(f"{seed}/{length}/{attempt}")
        scn, problems = attempt_build(length, seed, profile, coverage, manifest, apps, rng)
        if not problems:
            scn["hashes"] = hashes
            scn["attempt"] = attempt
            return scn
        last_error = "; ".join(problems[:6])
    raise SystemExit(f"scenario {length} seed {seed}: no attempt met profile and coverage ({last_error})")


def window_id(r: dict, hung_n: int) -> str:
    if r["app"] == "hung":
        return f"hung#{hung_n}"
    if "video_k" in r:
        return f"{r['app']}-v{r['video_k']}@{r['monitor']}"
    return f"{r['app']}@{r['monitor']}"


def plan_private(n: int, pool: list[dict], apps: set[str], rng: random.Random) -> None:
    """Mark n runs for an excluded or private page. The private modes cycle from a random start; the vault
    page goes into a browser no private mode covers, so Chrome, Edge and Zen each host one page."""
    kinds = [k for k in PRIVATE_KINDS if PRIVATE_BROWSER[k] in (None, *apps)]
    start = rng.randrange(len(kinds))
    chosen = [kinds[(start + i) % len(kinds)] for i in range(n)]
    covered = {PRIVATE_BROWSER[k] for k in chosen}
    for kind in chosen:
        want = PRIVATE_BROWSER[kind]
        if want is None:
            want = next((b for b in ("zen", "edge", "chrome") if b in apps and b not in covered), None)
            covered.add(want)
        pick = [r for r in pool if "private" not in r and want in (None, r["app"])]
        if pick:
            rng.choice(pick)["private"] = kind


def attempt_build(length, seed, profile, coverage, manifest, apps, rng):  # noqa: PLR0912, PLR0915
    total, blocks = block_plan(length, profile, rng)
    runs: list[dict] = []
    p90_s = run_p90_s(profile, length)
    for bi, b in enumerate(blocks):
        runs += block_runs(b["kind"], bi, b, p90_s[b["kind"]], profile, apps, rng)
    events: list[dict] = []
    if length == "attended":
        runs = carve(runs, [(t, t + d) for name, t, d in ATTENDED_EVENTS if name in ("lock", "sleep", "elevated")])
    resolve_apps(runs, profile, apps, rng)
    videos = plan_videos(runs, length, total, profile, apps)
    assign_monitors(runs, profile)
    if length == "attended":
        for name, t, d in ATTENDED_EVENTS:
            events.append({"kind": name, "t0_ms": t, "t1_ms": t + d, "monitor": "primary"})
            if name == "elevated":
                runs.append({"t0_ms": t, "t1_ms": t + d, "app": "elevated", "cat": "elevated", "monitor": "primary", "block": "desk"})
        runs.sort(key=lambda r: r["t0_ms"])
    # content of each run
    text_runs = [r for r in runs if APPS[r["app"]][2] in ("browser", "text") and "video_k" not in r]
    lang = assign_quota(text_runs, shares(profile, "shared.language"))
    for i, r in enumerate(text_runs):
        r["lang"] = lang[i]
    pages = [r for r in runs if APPS[r["app"]][2] == "browser" and r["app"] != "assistant" and "video_k" not in r]
    for k, r in enumerate(pages):
        r["page"] = PAGE_KINDS[k % len(PAGE_KINDS)]
    for k, r in enumerate(r for r in pages if r["t1_ms"] - r["t0_ms"] >= POPUP_MIN_MS):
        r["popups"] = k % 3 == 1
    for r in (r for r in runs if "page" in r):
        r["font_px"], r["theme"] = FONTS[rng.randrange(len(FONTS))], rng.choice(("dark", "light"))
        if "video_k" in r:
            r["font_px"], r["theme"], r["lang"] = VIDEO_PAGE_LOOK  # video pages share one look, so no media session is tied to a theme or font
    steps: list[dict] = []
    windows: dict[str, dict] = {}
    layout: dict[str, list[str]] = {m: [] for m in MONITORS}
    event_plan = event_counts(length, total, profile)
    plan_private(event_plan["excluded_pages"], [r for r in pages if r["t1_ms"] - r["t0_ms"] >= PRIVATE_MIN_MS], apps, rng)
    hung_n = 0
    sessions_g = game_sessions(blocks, length)
    visible_at: list[tuple[dict, list[str]]] = []
    for r in runs:
        app, mon = r["app"], r["monitor"]
        if app == "game":
            mid = (r["t0_ms"] + r["t1_ms"]) // 2
            r["mode"] = next(s["mode"] for s in sessions_g if s["t0_ms"] <= mid < s["t1_ms"])
        if app == "hung":
            hung_n += 1
            r["launch_ms"] = max(0, r["t0_ms"] - HUNG_PRELAUNCH_MS)
        wid = window_id(r, hung_n)
        r["window"] = wid
        windows.setdefault(wid, {"id": wid, "app": app, "monitor": mon})
        slot = layout[mon]
        per_monitor = pv(profile, f"{r['block']}.layout.visible_windows_per_monitor")
        if wid in slot:
            slot.remove(wid)
        slot.append(wid)
        for m in MONITORS:
            for filler in (f"chrome@{m}", f"edge@{m}"):
                if len(layout[m]) < per_monitor and filler not in layout[m]:
                    windows.setdefault(filler, {"id": filler, "app": filler.split("@")[0], "monitor": m})
                    layout[m].append(filler)
            while len(layout[m]) > per_monitor:
                layout[m].pop(0)
        visible = [w for m in MONITORS for w in layout[m]]
        visible_at.append((r, visible))
        side_page = any(windows[w]["monitor"] != mon and APPS[windows[w]["app"]][2] == "browser" for w in visible)
        steps.append({
            "kind": "focus", "t0_ms": r["t0_ms"], "t1_ms": r["t1_ms"], "monitor": mon, "windows": [wid] + [w for w in visible if w != wid],
            "tags": focus_tags(r, side_page), "dims": focus_dims(app, "video_k" in r),
            "params": {k: v for k, v in r.items() if k not in ("t0_ms", "t1_ms", "monitor", "cat", "block", "bi", "window")},
        })
        if wid.startswith("hung#"):
            layout[mon].remove(wid)
    # video, PiP and sound
    for v in videos:
        first, last = v["runs"][0], v["runs"][-1]
        base = {"t0_ms": first["t0_ms"], "t1_ms": last["t1_ms"], "monitor": first["monitor"], "windows": [first["window"]]}
        steps.append(base | {"kind": "video", "tags": ["tts_voices", APPS[v["app"]][0]], "dims": [7, 8, 9], "params": v["meta"] | {"app": v["app"]}})
        if v is videos[-1] and length in ("standard", "day"):
            steps.append(base | {"kind": "pip", "t0_ms": last["t0_ms"] + 3_000, "t1_ms": last["t1_ms"] + PIP_TAIL_S * 1000, "tags": ["pip", APPS[v["app"]][0]], "dims": [], "params": {"app": v["app"]}})
    # audio tracks
    spans, achieved = background_tracks(blocks, profile, rng, length, [(v["runs"][0]["t0_ms"], v["runs"][-1]["t1_ms"]) for v in videos])
    asleep = [(t, t + d) for name, t, d in ATTENDED_EVENTS if name == "sleep" and length == "attended"]  # nothing plays or records
    spans = {k: [(r["t0_ms"], r["t1_ms"]) for r in carve([{"t0_ms": a, "t1_ms": b} for a, b in v], asleep)] for k, v in spans.items()}
    clips = Clips(manifest)
    mix = LangMix(shares(profile, "shared.language"), clips, rng)
    call_cast = clips.of("call")
    cast_n = [pv(profile, "shared.call.speakers_min"), pv(profile, "shared.call.speakers_max")]
    gaming = [(b["t0_ms"], b["t1_ms"]) for b in blocks if b["kind"] == "gaming"]
    heard: set[str] = set()
    call_heard: list[tuple[int, int]] = []
    discord_win = next(w for w in sorted(windows.values(), key=lambda w: w["id"]) if w["app"] == "discord")
    for k, (a, b) in enumerate(spans["call"]):
        cast = [clips.of("call", "fr")[k % 2], clips.of("call", "en")[k % 2]]
        rest = [s for s in call_cast if s not in cast]
        cast += rng.sample(rest, min(len(rest), rng.randint(cast_n[0], cast_n[1]) - 2))
        utts, overlapped = call_session(a, b, cast, clips, rng, mix)
        tags = ["discord", "call_across_gaming"] if any(g0 <= a and b <= g1 + 1 for g0, g1 in gaming) else ["discord"]
        tags += ["overlap_speakers"] * overlapped + ["returning_speaker"] * bool(heard & set(cast))
        heard |= set(cast)
        steps.append({"kind": "call", "t0_ms": a, "t1_ms": b, "monitor": discord_win["monitor"], "windows": [discord_win["id"]], "tags": tags, "dims": [7, 8, 9, 11, 12],
                      "params": {"t0_ms": a, "t1_ms": b, "cast": cast, "utterances": utts}})
        if utts:
            call_heard.append((utts[0]["t_ms"], max(u["t_ms"] + u["dur_ms"] for u in utts)))
    for kind, got in achieved.items():  # "call audible" is the stretch from a call's first to its last utterance
        kind_blocks = [(b["t0_ms"], b["t1_ms"]) for b in blocks if b["kind"] == kind]
        secs = sum(b - a for a, b in kind_blocks) / 1000
        got["call"] = overlap_s(call_heard, kind_blocks) / secs
        got["both"] = overlap_s(call_heard, overlap_spans(spans["game"], kind_blocks)) / secs

    for a, b in spans["game"]:
        steps.append({"kind": "game_sound", "t0_ms": a, "t1_ms": b, "monitor": "primary", "windows": [], "tags": [], "dims": [7], "params": {}})
    for a, b in spans["both"]:
        steps.append({"kind": "both", "t0_ms": a, "t1_ms": b, "monitor": "primary", "windows": [], "tags": [], "dims": [7, 8, 9], "params": {}})
    for a, b in spans["music"]:
        steps.append({"kind": "music", "t0_ms": a, "t1_ms": b, "monitor": "primary", "windows": [], "tags": ["music"], "dims": [7], "params": {}})
    # overlay events
    for kind, secs, dims in (("pause", 60, [5, 13]), ("busy_disk", 60, [1, 12, 13]), ("canary", 25, [10, 7]), ("voice_elsewhere", pv(profile, "shared.audio.voice_elsewhere_s"), [7, 8, 9])):
        n = 1 if kind == "voice_elsewhere" else event_plan[kind]
        lo, hi = 60_000, total - 60_000 - secs * 1000
        for i in range(n if hi > lo else 0):
            step = {"kind": kind, "monitor": rng.choice(MONITORS), "windows": [], "tags": [] if kind == "voice_elsewhere" else [kind], "dims": dims, "params": {}}
            t = best_slot(step, secs * 1000, [round(lo + (hi - lo) * (i + rng.random()) / n) for _ in range(SLOT_CANDIDATES)], steps, coverage, length)
            step |= {"t0_ms": t, "t1_ms": t + secs * 1000}
            if kind == "voice_elsewhere":
                cast = [clips.of("call", "fr")[0], clips.of("call", "en")[0]]
                step["params"] = {"utterances": call_session(t, t + secs * 1000, cast, clips, rng, mix, elsewhere=True)[0]}
            steps.append(step)
    for r in runs:
        if "private" in r:
            steps.append({"kind": "excluded_pages", "t0_ms": r["t0_ms"], "t1_ms": r["t1_ms"], "monitor": r["monitor"], "windows": [r["window"]], "tags": ["excluded_pages", APPS[r["app"]][0]], "dims": [10, 2], "params": {"private": r["private"]}})
    for e in events:
        steps.append({"kind": e["kind"], "t0_ms": e["t0_ms"], "t1_ms": e["t1_ms"], "monitor": "primary", "windows": [], "tags": [e["kind"]] if e["kind"] != "elevated" else [], "dims": [5, 13, 7] if e["kind"] == "device_change" else [5, 13], "params": {}})
    # title changes: a window shows a new title for about 2 s
    titles = {"gaming": 0, "desk": 0}
    for kind in titles:
        window_ms = pv(profile, f"{kind}.layout.title_change_s") * 1000
        hosts = [(r, [w for w in vis if APPS[windows[w]["app"]][2] == "browser" and "-v" not in w]) for r, vis in visible_at
                 if r["block"] == kind and APPS[r["app"]][2] in ("browser", "game")]
        hosts = [(r, ws) for r, ws in hosts if ws and r["t1_ms"] - r["t0_ms"] > window_ms and "private" not in r]
        hours = sum(b["t1_ms"] - b["t0_ms"] for b in blocks if b["kind"] == kind) / 3_600_000
        n_titles = round(pv(profile, f"{kind}.run.title_changes_per_hour") * hours)
        for r, ws in rng.choices(hosts, [r["t1_ms"] - r["t0_ms"] for r, _ in hosts], k=n_titles) if hosts else []:
            wid = rng.choice(ws)
            t = r["t0_ms"] + rng.randrange(r["t1_ms"] - r["t0_ms"] - window_ms)
            tags = ["title_churn", res_tag(windows[wid]["monitor"])] + ["side_monitor_change"] * (windows[wid]["monitor"] != r["monitor"])
            steps.append({"kind": "title_change", "t0_ms": t, "t1_ms": t + window_ms, "monitor": windows[wid]["monitor"], "windows": [wid], "tags": tags, "dims": [1, 2, 4], "params": {}})
            titles[kind] += 1
    steps += silence_steps(steps, total, asleep)
    steps.sort(key=lambda s: (s["t0_ms"], s["kind"], s["t1_ms"]))
    steps = [fill_ids(s, i) for i, s in enumerate(steps)]
    # A window shown only beside the focused one loads its own page: give it a look drawn from the
    # same language mix and fonts as focused pages (its own rng, so the timeline above is unchanged).
    look_rng = random.Random(f"{seed}-side-looks")
    lang_shares = shares(profile, "shared.language")
    for wdef in sorted(windows.values(), key=lambda x: x["id"]):
        if APPS[wdef["app"]][2] == "browser" and "-v" not in wdef["id"]:
            x, acc = look_rng.random(), 0.0
            lang = next((k for k, v in sorted(lang_shares.items()) if (acc := acc + v) > x), sorted(lang_shares)[-1])
            wdef["look"] = {"lang": lang, "font_px": FONTS[look_rng.randrange(len(FONTS))], "theme": look_rng.choice(("dark", "light"))}
    tag_overlaps(steps)
    for s in steps:
        s["tags"] = sorted(set(s["tags"]))
    valid = set(case_ids(coverage))
    bad = {t for s in steps for t in s["tags"]} - valid
    if bad:
        raise SystemExit(f"scenario tags not in coverage.toml: {sorted(bad)}")
    problems: list[str] = []
    if length in ("standard", "day"):
        m = measure(runs, blocks, achieved, titles, mix.shares())
        problems += [f"{r['name']} {r['measured']:.3g} vs {r['profile']:.3g}" for r in compare(m, profile, length, apps, p90_s) if not r["ok"]]
    if length == "attended":
        problems += attended_gaps(steps, events)
    problems += cell_problems(steps, coverage, length)
    used = {u["speaker"] for s in steps for u in s["params"].get("utterances", [])} | {c for s in steps for c in s["params"].get("cast", [])}
    scn = {
        "version": 1, "length": length, "seed": seed, "duration_ms": total, "blocks": blocks, "run_p90_s": p90_s,
        "windows": sorted(windows.values(), key=lambda w: w["id"]), "steps": steps,
        "game_sessions": sessions_g,
        "cast": {k: v for k, v in manifest["speakers"].items() if k in used},
    }
    return scn, problems


def silence_steps(steps: list[dict], total: int, asleep: list[tuple[int, int]]) -> list[dict]:
    """Stretches of at least SILENCE_MIN_S with no call, game, music, video or canary sound, and not asleep."""
    sound = [any(a <= sec * 1000 < b for a, b in asleep) for sec in range(total // 1000)]
    for s in steps:
        if s["kind"] in ("call", "game_sound", "music", "canary", "voice_elsewhere", "video"):
            for sec in range(s["t0_ms"] // 1000, min(len(sound), s["t1_ms"] // 1000)):
                sound[sec] = True
    return [{"kind": "silence", "t0_ms": a, "t1_ms": b, "monitor": "primary", "windows": [], "tags": ["silence"], "dims": [7, 8, 9], "params": {}}
            for a, b in spans_of([not x for x in sound], 0) if b - a >= SILENCE_MIN_S * 1000]


def tag_overlaps(steps: list[dict]) -> None:
    """Tags that depend on what else sounds at the same time: a video during a call, the mic with the system sound."""
    calls = [s for s in steps if s["kind"] == "call"]
    others = [s for s in steps if s["kind"] in ("video", "music", "game_sound")]
    overlap = lambda a, b: a["t0_ms"] < b["t1_ms"] and b["t0_ms"] < a["t1_ms"]
    for c in calls:
        if any(overlap(c, v) for v in steps if v["kind"] == "video"):
            c["tags"].append("video_during_call")
            for v in steps:
                if v["kind"] == "video" and overlap(c, v):
                    v["tags"].append("video_during_call")
        if any(u["stream"] == "own" for u in c["params"]["utterances"]) and any(overlap(c, o) for o in others):
            c["tags"].append("mic_and_system")


def event_counts(length: str, total_ms: int, profile: dict) -> dict[str, int]:
    out = {}
    for k in UNATTENDED_EVENTS:
        per_day = pv(profile, f"events.per_day.{k}")
        if length == "day":
            out[k] = per_day
        elif length == "attended":
            out[k] = 0
        else:
            out[k] = max(1, round(per_day * total_ms / 3_600_000 / pv(profile, "shared.videos.day_hours")))
    if length == "standard":
        out["excluded_pages"] = max(out["excluded_pages"], len(PRIVATE_KINDS))
    return out


def game_sessions(blocks: list[dict], length: str) -> list[dict]:
    """Game process lifetimes: each gaming block is two halves, one per window mode (smoke and
    attended stay borderless)."""
    out = []
    for i, b in enumerate(b for b in blocks if b["kind"] == "gaming"):
        if length in ("smoke", "attended"):
            out.append({"t0_ms": b["t0_ms"], "t1_ms": b["t1_ms"], "mode": "borderless"})
            continue
        mid = (b["t0_ms"] + b["t1_ms"]) // 2
        modes = ("borderless", "exclusive") if i % 2 == 0 else ("exclusive", "borderless")
        out += [{"t0_ms": b["t0_ms"], "t1_ms": mid, "mode": modes[0]}, {"t0_ms": mid, "t1_ms": b["t1_ms"], "mode": modes[1]}]
    return out


def res_tag(monitor: str) -> str:
    """The target PC's primary monitor is 4K and its side monitors are 1440p."""
    return "res_4k" if monitor == "primary" else "res_1440p"


def focus_tags(r: dict, side_page: bool) -> list[str]:
    app = r["app"]
    tags = [APPS[app][0]] if APPS[app][0] else []
    tags.append(res_tag(r["monitor"]))
    if app == "game":
        tags.append("game_" + r["mode"])
    if r.get("page") == "chat_growing":
        tags.append("chat_growing")
    if r.get("page") in SCROLL_PAGES:
        tags.append("scrolling")
    if "theme" in r:
        tags.append("theme_" + r["theme"])
    if "lang" in r:
        tags.append("text_" + r["lang"])
    if r.get("font_px", 99) <= SMALL_FONT_PX:
        tags.append("small_fonts")
    if r.get("popups"):
        tags.append("popups")
    if side_page:
        tags.append("side_monitor_change")
    return tags


def focus_dims(app: str, video: bool) -> list[int]:
    dims = [1, 2, 5, 11, 12]
    kind = APPS[app][2]
    if kind in ("browser", "text") or app in ("notepad", "explorer"):
        dims.append(3)
    if kind in ("browser", "game"):
        dims.append(4)
    if kind == "game" or video:
        dims.append(6)
    return dims


def attended_gaps(steps: list[dict], events: list[dict]) -> list[str]:
    ends = sorted((e["t0_ms"], e["t1_ms"]) for e in events)
    out = []
    for (_, end), (nxt, _) in zip(ends, ends[1:] + [(10**12, 0)], strict=True):
        active = sum(min(s["t1_ms"], nxt) - max(s["t0_ms"], end) for s in steps if s["kind"] == "focus" and s["t0_ms"] < nxt and s["t1_ms"] > end)
        if active < 60_000 and nxt < 10**12:
            out.append(f"less than 60 s of activity after event ending {end}")
    return out


def cli_hashes(profile_path: Path, coverage_path: Path, manifest_path: Path) -> dict:
    return {"profile": sha256_file(profile_path), "coverage": sha256_file(coverage_path), "manifest": sha256_file(manifest_path)}


def generate(length: str, seed: int | None = None, profile_path: Path = PROFILE, coverage_path: Path = COVERAGE, manifest_path: Path = MANIFEST) -> str:
    profile, coverage = load_toml(profile_path), load_toml(coverage_path)
    validate_profile(profile)
    if len(coverage["case"]) != 41:
        raise SystemExit(f"coverage.toml has {len(coverage['case'])} cases, not 41")
    if not manifest_path.is_file():
        raise SystemExit("asset manifest missing: run assets.py build")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    seed = pv(profile, f"seed.{length}") if seed is None else seed
    scn = build(length, seed, profile, coverage, manifest, cli_hashes(profile_path, coverage_path, manifest_path))
    return json.dumps(scn, indent=1, sort_keys=True) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("length", choices=LENGTHS)
    ap.add_argument("--seed", type=int)
    ap.add_argument("--out", type=Path)
    args = ap.parse_args()
    text = generate(args.length, args.seed)
    if args.out:
        args.out.write_text(text, encoding="utf-8", newline="\n")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
