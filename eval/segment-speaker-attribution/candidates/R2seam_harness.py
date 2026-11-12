"""harness.py plus late author revisions (additive; harness.py is imported, not changed).

    ..\\.venv\\Scripts\\python.exe candidates\\R2seam_harness.py candidates\\R2seam_wrap.py [--limit N] [--no-drift]

Same stream, order, scoring and outputs as harness.py. Continuation pairs are already processed
back to back (N+1 right after N). Extra optional candidate hooks:
- drain_revisions() -> [(segment_id, profile id or None[, provisional profile id])], called after every attribute(): a
  revised author replaces the one recorded for that earlier segment before scoring;
- finish(store, models), called at the end of a stream (then revisions are drained again);
- extras() -> dict, stored in details.json; its "deferred" ids mark rows with deferred=True.
A candidate without these hooks scores exactly as with harness.py.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import harness as H  # noqa: E402
from common import FIXTURE, OUT, SR, PrevSegment, Segment, SharedModels  # noqa: E402


def _score(r: dict, pid, owners: dict, enrolled: set) -> None:
    author = None if pid is None else owners.get(pid, f"unknown{pid}")
    r["author"] = author
    r["author_enrolled"] = pid is not None and pid in enrolled
    r["authored"] = r["expected"] is not None and author == r["expected"]
    r["wrong"] = author is not None and (r["single"] is None or author != r["single"])


def run_stream(cand, store, models, segs, blob, owners, on_step=None) -> list[dict]:
    enrolled = set(owners)
    results, prev_info, row_of = [], {}, {}
    drain = getattr(cand, "drain_revisions", None)

    def revise() -> None:
        for rev in drain() if drain else []:
            sid, pid = rev[0], rev[1]
            r = results[row_of[sid]]
            if len(rev) > 2:  # the author the segment had before the revision (pending)
                r["provisional"] = None if rev[2] is None else owners.get(rev[2], f"unknown{rev[2]}")
            _score(r, pid, owners, enrolled)

    for i, e in enumerate(segs):
        samples = H.decode(blob, e)
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
                owners[pid] = H.owner_of((a, b), e["spans"]) or f"unknown{pid}"
        if e["kind"] == "cont_first":
            prev_info[e["id"]] = (e["id"], att.author, samples)
        r = {
            "id": e["id"], "kind": e["kind"], "subsets": e["subsets"], "length_ms": e["length_ms"],
            "single": e["single"], "expected": e["expected"],
            "turns": att.turns, "ms": dt * 1000,
            "seg_n": int(models.counter["seg_n"]), "seg_ms": models.counter["seg_s"] * 1000,
            "emb_n": int(models.counter["emb_n"]), "emb_ms": models.counter["emb_s"] * 1000,
        }  # fmt: skip
        _score(r, att.author, owners, enrolled)
        row_of[e["id"]] = len(results)
        results.append(r)
        revise()
        if on_step:
            on_step(i, r)
        if (i + 1) % 100 == 0:
            print(f"  {i + 1}/{len(segs)}", flush=True)
    if hasattr(cand, "finish"):
        cand.finish(store, models)
        revise()
    deferred = set(cand.extras().get("deferred", [])) if hasattr(cand, "extras") else set()
    for r in results:
        r["deferred"] = r["id"] in deferred
    return results


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("candidate")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--no-drift", action="store_true")
    args = ap.parse_args()
    cand = H.load_candidate(args.candidate)
    name = getattr(cand, "NAME", Path(args.candidate).stem)
    out = OUT / name
    out.mkdir(parents=True, exist_ok=True)
    H.run_stream = run_stream  # drift() calls run_stream: use the revising one there too
    index = json.loads((FIXTURE / "index.json").read_text(encoding="utf-8"))
    blob = (FIXTURE / "segments.bin").read_bytes()

    models = SharedModels()
    warm = np.zeros(SR * 3, np.float32)
    warm[: SR * 2] = np.random.default_rng(0).standard_normal(SR * 2).astype(np.float32) * 0.05
    models.turns(warm)
    models.embed(warm)

    store = cand.make_store()
    owners = H.enrol_all(cand, store, models, index, blob)
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
    extras = cand.extras() if hasattr(cand, "extras") else {}
    summary = H.summarise(results)
    d = None
    if not args.no_drift:
        print("drift stream", flush=True)
        d = H.drift(cand, models, index, blob)
    details = {"name": name, "summary": summary, "drift": d, "segments": results,
               "n_profiles_end": len(owners), "extras": extras}  # fmt: skip
    (out / "details.json").write_text(json.dumps(details, default=float), encoding="utf-8")
    line = H.metrics_line(name, summary, d)
    (out / "metrics.txt").write_text(line + "\n", encoding="utf-8")
    ref_path = OUT / "reference" / "details.json"
    ref = json.loads(ref_path.read_text(encoding="utf-8")) if ref_path.is_file() else None
    H.figure(out / "side.png", name, details, ref)
    print(line)


if __name__ == "__main__":
    main()
