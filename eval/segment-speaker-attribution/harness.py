"""Score a speaker-attribution candidate on the fixture.

    ..\\.venv\\Scripts\\python.exe harness.py candidates\\reference.py [--limit N] [--no-drift]

A candidate is a module (see common.py) exposing NAME, make_store(), enroll(), attribute(),
centroid(). The harness:
1. builds one store, enrols every enrolled speaker from its clips (profile id -> speaker);
2. runs the evaluation stream (solo, impostor, two-speaker, continuation pairs in order) through
   attribute(), in one fixed shuffled order, so profile updates carry over as in the app;
   profiles a candidate creates are mapped to the true speaker who spoke most in the turn span
   that created them;
3. runs the drift stream (500 solo segments of the most similar enrolled same-gender pair,
   interleaved) on a fresh store, tracking cosine of each profile centroid to the clean
   enrolment centroid;
4. writes out/<NAME>/metrics.txt (one line), details.json and side.png (vs out/reference).

Scoring per segment: ``single`` = the only speaker with >= 300 ms of speech after the lead
(None if 0 or >= 2). ``expected`` = single if enrolled, else None.
  authored  = expected is not None and the author maps to expected;
  wrong     = author is set and (single is None, or it maps to someone other than single).
An unenrolled speaker's segment attributed to a profile created from that same speaker is not
wrong (the app makes a new unnamed voice); attributing it to an enrolled profile is.
Timing: wall time of attribute() per segment (decode excluded), onnxruntime CPU provider with
default session options as OnnxVoiceModels; ONNX forward time and counts reported separately.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import time
from collections import defaultdict
from pathlib import Path

import numpy as np

from common import FIXTURE, OUT, SR, PrevSegment, Segment, SharedModels, app_opus, unit

SUBSETS = [
    "solo", "solo_10k", "solo_10_0", "cont_first", "cont_second", "cont_same", "cont_switch",
    "cont_switch_early", "similar", "mixed", "mixed_alt", "mixed_backchannel", "mixed_lastwin",
    "mixed_reply", "impostor",
]  # fmt: skip
BUCKETS = [(5, 10), (10, 15), (15, 20), (20, 25), (25, 31)]


def load_candidate(path: str):
    spec = importlib.util.spec_from_file_location(Path(path).stem, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def decode(blob: bytes, e: dict) -> np.ndarray:
    x = app_opus.decode_opus(blob[e["offset"] : e["offset"] + e["size"]])
    n = e["length_ms"] * SR // 1000
    out = np.zeros(n, np.float32)
    out[: min(n, len(x))] = x[:n]
    return out


def owner_of(span: tuple[int, int], spans: dict[str, list[list[int]]]) -> str | None:
    best, who = 0, None
    for spk, ss in spans.items():
        ov = sum(max(0, min(b, span[1]) - max(a, span[0])) for a, b in ss)
        if ov > best:
            best, who = ov, spk
    return who


def enrol_all(cand, store, models, index, blob) -> dict[int, str]:
    owners = {}
    for spk, role in sorted(index["roles"].items()):
        if role != "enrolled":
            continue
        clips = [decode(blob, c) for c in index["enrol"][spk]]
        owners[cand.enroll(store, models, clips)] = spk
    return owners


def run_stream(cand, store, models, segs, blob, owners, on_step=None) -> list[dict]:
    enrolled = set(owners)  # profiles that exist before the stream are the enrolled ones
    results, prev_info = [], {}
    for i, e in enumerate(segs):
        samples = decode(blob, e)
        prev = None
        if e["continues"] is not None and e["continues"] in prev_info:
            pid, pauthor, psamples = prev_info[e["continues"]]
            prev = PrevSegment(pid, pauthor, psamples)
        seg = Segment(e["id"], samples, SR, e["lead_ms"] if prev else 0, prev)
        models.reset_counter()
        t = time.perf_counter()
        att = cand.attribute(store, models, seg)
        dt = time.perf_counter() - t
        for pid, kind, a, b in att.updates:
            if kind == "create" and pid not in owners:
                owners[pid] = owner_of((a, b), e["spans"]) or f"unknown{pid}"
        if e["kind"] == "cont_first":
            prev_info[e["id"]] = (e["id"], att.author, samples)
        author = None if att.author is None else owners.get(att.author, f"unknown{att.author}")
        single, expected = e["single"], e["expected"]
        r = {
            "id": e["id"], "kind": e["kind"], "subsets": e["subsets"], "length_ms": e["length_ms"],
            "single": single, "expected": expected, "author": author,
            "author_enrolled": att.author is not None and att.author in enrolled,
            "authored": expected is not None and author == expected,
            "wrong": author is not None and (single is None or author != single),
            "turns": att.turns, "ms": dt * 1000,
            "seg_n": int(models.counter["seg_n"]), "seg_ms": models.counter["seg_s"] * 1000,
            "emb_n": int(models.counter["emb_n"]), "emb_ms": models.counter["emb_s"] * 1000,
        }
        results.append(r)
        if on_step:
            on_step(i, r)
        if (i + 1) % 100 == 0:
            print(f"  {i + 1}/{len(segs)}", flush=True)
    return results


def rate(rows, key) -> tuple[float | None, int]:
    return (float(np.mean([r[key] for r in rows])) if rows else None), len(rows)


def summarise(results: list[dict]) -> dict:
    out = {"subsets": {}, "buckets": {}}
    has_exp = [r for r in results if r["expected"] is not None]
    out["authorship"] = rate(has_exp, "authored")
    out["authorship_enrolled_profile"] = rate(
        [{"x": r["authored"] and r["author_enrolled"]} for r in has_exp], "x"
    )
    out["wrong"] = rate(results, "wrong")
    for s in SUBSETS:
        rows = [r for r in results if s in r["subsets"]]
        out["subsets"][s] = {
            "authorship": rate([r for r in rows if r["expected"] is not None], "authored"),
            "wrong": rate(rows, "wrong"),
        }
    for lo, hi in BUCKETS:
        rows = [r for r in results if lo * 1000 <= r["length_ms"] < hi * 1000]
        out["buckets"][f"{lo}-{hi}s"] = {
            "authorship": rate([r for r in rows if r["expected"] is not None], "authored"),
            "wrong": rate(rows, "wrong"),
        }
    ms = np.array([r["ms"] for r in results])
    out["timing"] = {
        "p50_ms": float(np.percentile(ms, 50)), "p95_ms": float(np.percentile(ms, 95)),
        "mean_ms": float(ms.mean()),
        "ms_per_audio_s": float(ms.sum() / (sum(r["length_ms"] for r in results) / 1000)),
        "seg_forwards_mean": float(np.mean([r["seg_n"] for r in results])),
        "seg_ms_mean": float(np.mean([r["seg_ms"] for r in results])),
        "emb_forwards_mean": float(np.mean([r["emb_n"] for r in results])),
        "emb_ms_mean": float(np.mean([r["emb_ms"] for r in results])),
    }
    return out


def drift(cand, models, index, blob) -> dict:
    store = cand.make_store()
    owners = enrol_all(cand, store, models, index, blob)
    a, b = index["drift"]["pair"]
    pid = {spk: p for p, spk in owners.items()}
    clean = np.load(FIXTURE / "centroids.npz")
    by_id = {e["id"]: e for e in index["segments"]}
    segs = [by_id[i] for i in index["drift"]["ids"]]
    curve: dict[str, list] = {"step": [], a: [], b: [], "pair": []}

    def cos_now(step: int) -> None:
        curve["step"].append(step)
        cs = {s: cand.centroid(store, pid[s]) for s in (a, b)}
        for s in (a, b):
            curve[s].append(None if cs[s] is None else float(unit(cs[s]) @ clean[s]))
        both = cs[a] is not None and cs[b] is not None
        curve["pair"].append(float(unit(cs[a]) @ unit(cs[b])) if both else None)

    cos_now(0)
    res = run_stream(
        cand, store, models, segs, blob, owners,
        on_step=lambda i, r: cos_now(i + 1) if (i + 1) % 10 == 0 else None,
    )  # fmt: skip
    first, last = res[:100], res[-100:]
    return {
        "pair": [a, b], "pair_cos": index["drift"]["cos"],
        "wrong_first100": rate(first, "wrong")[0], "wrong_last100": rate(last, "wrong")[0],
        "auth_first100": rate(first, "authored")[0], "auth_last100": rate(last, "authored")[0],
        "wrong_all": rate(res, "wrong")[0],
        "wrong_series": [int(r["wrong"]) for r in res],
        "cos_end": {s: curve[s][-1] for s in (a, b)},
        "pair_cos_start": curve["pair"][0], "pair_cos_end": curve["pair"][-1],
        "cos_min": {s: min((v for v in curve[s] if v is not None), default=None) for s in (a, b)},
        "curve": curve,
    }


def fmt(v) -> str:
    return "na" if v is None else f"{v:.4f}"


def metrics_line(name: str, s: dict, d: dict | None) -> str:
    sub = s["subsets"]
    parts = [
        f"cand={name}",
        f"solo_auth={fmt(s['authorship'][0])}",
        f"solo10k_auth={fmt(sub['solo_10k']['authorship'][0])}",
        f"solo10_0_auth={fmt(sub['solo_10_0']['authorship'][0])}",
        f"cont2_auth={fmt(sub['cont_second']['authorship'][0])}",
        f"wrong={fmt(s['wrong'][0])}",
        f"wrong_mixed={fmt(sub['mixed']['wrong'][0])}",
        f"wrong_impostor={fmt(sub['impostor']['wrong'][0])}",
        f"p50_ms={s['timing']['p50_ms']:.1f}",
        f"p95_ms={s['timing']['p95_ms']:.1f}",
    ]
    if d:
        cos_end = min(v for v in d["cos_end"].values() if v is not None) if any(
            v is not None for v in d["cos_end"].values()
        ) else None
        parts += [
            f"drift_wrong_first100={fmt(d['wrong_first100'])}",
            f"drift_wrong_last100={fmt(d['wrong_last100'])}",
            f"drift_cos_end_min={fmt(cos_end)}",
            f"drift_pair_cos_end={fmt(d['pair_cos_end'])}",
        ]
    return " ".join(parts)


# --- figure --------------------------------------------------------------------------------

BG, SURF, INK, INK2, MUTED, GRID, AXIS = (
    "#0d0d0d", "#1a1a19", "#f0efec", "#c3c2b7", "#898781", "#2c2c2a", "#383835",
)  # fmt: skip
C_CAND, C_REF = "#3987e5", "#d95926"
AUTH_BARS = ["solo", "solo_10k", "solo_10_0", "cont_first", "cont_second", "cont_switch", "similar"]
WRONG_BARS = ["solo", "similar", "cont_second", "mixed", "mixed_lastwin", "mixed_reply", "impostor"]


def figure(path: Path, name: str, details: dict, ref: dict | None) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams.update({
        "figure.facecolor": BG, "axes.facecolor": SURF, "axes.edgecolor": AXIS,
        "axes.labelcolor": INK2, "xtick.color": MUTED, "ytick.color": MUTED, "text.color": INK,
        "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.6, "font.size": 9,
        "axes.spines.top": False, "axes.axisbelow": True, "axes.spines.right": False, "legend.frameon": False,
    })  # fmt: skip
    fig, ax = plt.subplots(2, 2, figsize=(13, 8), dpi=110)
    show_ref = ref is not None and name != "reference"
    series = [(name, details, C_CAND)] + ([("reference", ref, C_REF)] if show_ref else [])

    def bars(a, keys, metric, title, ylim):
        w = 0.8 / len(series)
        for j, (label, d, color) in enumerate(series):
            vals = [d["summary"]["subsets"][k][metric][0] for k in keys]
            xs = np.arange(len(keys)) + (j - (len(series) - 1) / 2) * w
            hs = [0 if v is None else v for v in vals]
            a.bar(xs, hs, width=w - 0.02, color=color, label=label, edgecolor=SURF, linewidth=1)
            for x, v in zip(xs, vals, strict=True):
                a.text(x, (v or 0) + ylim * 0.01, "na" if v is None else f"{v:.0%}", ha="center",
                       va="bottom", fontsize=7, color=INK2)  # fmt: skip
        a.set_xticks(range(len(keys)), keys, rotation=25, ha="right")
        a.set_ylim(0, ylim * 1.12)
        a.yaxis.set_major_formatter(matplotlib.ticker.PercentFormatter(1.0))
        a.set_title(title, loc="left", color=INK)
        a.legend(loc="upper center", ncol=2, labelcolor=INK2)
        a.grid(axis="x", visible=False)

    bars(ax[0, 0], AUTH_BARS, "authorship", "Authorship (segments with one enrolled speaker)", 1.0)
    wmax = max(
        [0.05] + [d["summary"]["subsets"][k]["wrong"][0] or 0 for _, d, _ in series for k in WRONG_BARS]
    )
    bars(ax[0, 1], WRONG_BARS, "wrong", "Wrong-author rate", min(1.0, wmax * 1.25))

    a = ax[1, 0]
    for label, d, color in series:
        dr = d.get("drift")
        if not dr:
            continue
        cur = dr["curve"]
        for s, style in zip(dr["pair"], ("-", "--"), strict=True):
            ys = [np.nan if v is None else v for v in cur[s]]
            a.plot(cur["step"], ys, style, color=color, linewidth=2, label=f"{label} {s} vs clean")
        ys = [np.nan if v is None else v for v in cur.get("pair", [])]
        if ys:
            a.plot(cur["step"], ys, ":", color=color, linewidth=2, label=f"{label} A vs B centroids")
    a.set_title("Drift: centroid cosine (to clean enrolment; dotted = between the pair)", loc="left", color=INK)
    a.set_xlabel("drift segments processed")
    if a.lines:
        a.legend(loc="center right", labelcolor=INK2, fontsize=8)

    a = ax[1, 1]
    for label, d, color in series:
        dr = d.get("drift")
        if not dr:
            continue
        w = np.array(dr["wrong_series"], float)
        k = 50
        roll = np.convolve(w, np.ones(k) / k, mode="valid")
        a.plot(np.arange(k, len(w) + 1), roll, color=color, linewidth=2, label=label)
    a.set_title("Drift: wrong-author rate, rolling 50 segments", loc="left", color=INK)
    a.set_xlabel("drift segments processed")
    a.yaxis.set_major_formatter(matplotlib.ticker.PercentFormatter(1.0))
    a.set_ylim(bottom=0)
    if a.lines:
        a.legend(loc="upper left", labelcolor=INK2)

    t = details["summary"]["timing"]
    fig.suptitle(
        f"{name}: authorship {details['summary']['authorship'][0]:.1%}, wrong {details['summary']['wrong'][0]:.2%},"
        f" job p50 {t['p50_ms']:.0f} ms / p95 {t['p95_ms']:.0f} ms",
        color=INK, x=0.01, ha="left", fontsize=11,
    )  # fmt: skip
    fig.tight_layout()
    fig.savefig(path, facecolor=BG)
    plt.close(fig)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("candidate")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--no-drift", action="store_true")
    ap.add_argument("--replot", action="store_true", help="redraw side.png from details.json")
    args = ap.parse_args()
    cand = load_candidate(args.candidate)
    name = getattr(cand, "NAME", Path(args.candidate).stem)
    out = OUT / name
    out.mkdir(parents=True, exist_ok=True)
    ref_path = OUT / "reference" / "details.json"
    if args.replot:
        details = json.loads((out / "details.json").read_text(encoding="utf-8"))
        ref = json.loads(ref_path.read_text(encoding="utf-8")) if ref_path.is_file() else None
        figure(out / "side.png", name, details, ref)
        return
    index = json.loads((FIXTURE / "index.json").read_text(encoding="utf-8"))
    blob = (FIXTURE / "segments.bin").read_bytes()

    models = SharedModels()
    warm = np.zeros(SR * 3, np.float32)
    warm[: SR * 2] = np.random.default_rng(0).standard_normal(SR * 2).astype(np.float32) * 0.05
    models.turns(warm)
    models.embed(warm)  # load sessions outside the timing

    store = cand.make_store()
    owners = enrol_all(cand, store, models, index, blob)

    # evaluation stream: units (a segment, or a continuation pair) in one seeded order
    by_id = {e["id"]: e for e in index["segments"]}
    units = []
    for e in index["segments"]:
        if e["kind"] in ("drift", "cont_second"):
            continue
        unit_ = [e]
        if e["kind"] == "cont_first":
            unit_.append(next(x for x in index["segments"] if x["continues"] == e["id"]))
        units.append(unit_)
    order = np.random.default_rng(index["seed"]).permutation(len(units))
    segs = [s for i in order for s in units[i]]
    if args.limit:
        segs = segs[: args.limit]
    print(f"{name}: {len(owners)} profiles enrolled, {len(segs)} segments", flush=True)
    results = run_stream(cand, store, models, segs, blob, owners)
    summary = summarise(results)
    d = None
    if not args.no_drift:
        print("drift stream", flush=True)
        d = drift(cand, models, index, blob)
    details = {"name": name, "summary": summary, "drift": d, "segments": results,
               "n_profiles_end": len(owners), "by_id_count": len(by_id)}  # fmt: skip
    (out / "details.json").write_text(json.dumps(details, default=float), encoding="utf-8")
    line = metrics_line(name, summary, d)
    (out / "metrics.txt").write_text(line + "\n", encoding="utf-8")
    ref = json.loads(ref_path.read_text(encoding="utf-8")) if ref_path.is_file() else None
    figure(out / "side.png", name, details, ref)
    print(line)


if __name__ == "__main__":
    main()
