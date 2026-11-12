r"""QA-R2-C1: closed-form span embeddings + leave-one-out change test + split-channel merge, on
round-1 C2_calib's calibrated fusion (C2_decide.decide, reused unchanged).

Per segment (the lead of a continuation is dropped, as C2):
1. Windows: the app's count, starts spread evenly, frames owned by the nearest window centre
   (R2span_lib). Every local channel's owned solo speech >= 300 ms is embedded with the derived
   ResNet34-LM (data\models_derived, sha256 pinned): one forward gives the app's embedding and
   the pre-pooling frame map, kept as float64 prefix sums.
2. Pass 1: C2's decision on the unmerged channels gives k*. Split-channel merge: two channels of
   one window merge when the same-voice pair LLR (calibrated on the other fold than k*'s profile)
   >= tau_m; their statistics are summed exactly, the group's embedding is one Gemm, and the
   pair-class overlap frames between them stop counting as overlap. Pass 2: C2's decision on the
   groups (fusion, margin, Wald veto, two-channel veto, overlap veto).
3. Change test (only ever NULLs): spans of every group (6-step sub-windows at hop 2, edge spans
   0.4-2.5 s; join steps skipped) against the rest of their group: z = same-voice LLR,
   Delta = best other profile's LLR - k*'s on the span. NULL if max(-z) > tau_s or
   max(Delta) > tau_k (thresholds of k*'s fold, fitted on the other fold).
4. Updates / new profiles: C2's rule, with the merged groups as turns (full-group vectors).
Fold bookkeeping as C2_calib (profile fold from the enrolment order); created profiles use both
folds' numbers, strictest.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

from C2_decide import VETO_MS, Params, decide  # noqa: E402
from C2_lib import SR, bucket  # noqa: E402
from common import FIXTURE, OUT, Attribution, Segment, SharedModels, unit  # noqa: E402
from R2span_core import analyse, ensure_bank, group_spans, groups_for, loo_stats, turns_and_ovl  # noqa: E402
from R2span_lib import FrameEmbedder  # noqa: E402

from akashic.storage.speakers import MIN_UPDATE_MS  # noqa: E402

NAME = os.environ.get("R2SPAN_NAME", "R2span_C1")
M_UPD = 2.0
NEW_LLR = 2.0

_CAL = json.loads((OUT / "R2span_C1" / os.environ.get("R2SPAN_CALIB", "calib.json")).read_text(encoding="utf-8"))
_FOLD = _CAL["folds"]
_CONSTS = {int(f): np.array(v, float) for f, v in _CAL["consts"].items()}
_TAU_U = {int(f): np.array(v, float) for f, v in _CAL["tau_u"].items()}
_PAIR = {int(f): (np.array(_CAL["w_pair"][f], float), float(_CAL["tau_m"][f])) for f in _CAL["w_pair"]}
_SPAN = {int(f): np.array(v, float) for f, v in _CAL["w_span"].items()}
_P = {int(f): v for f, v in _CAL["params"].items()}
_index = json.loads((FIXTURE / "index.json").read_text(encoding="utf-8"))
_ENROLLED = sorted(s for s, r in _index["roles"].items() if r == "enrolled")
del _index


def _params(fold: int | None) -> Params:
    if fold is None:
        a, b = _P[0], _P[1]
        return Params(max(a["tau_a"], b["tau_a"]), max(a["margin"], b["margin"]), min(a["tau_v"], b["tau_v"]),
                      max(a["tau_c"], b["tau_c"]), min(a["ovl_ms"], b["ovl_ms"]))  # fmt: skip
    v = _P[1 - fold]
    return Params(v["tau_a"], v["margin"], v["tau_v"], v["tau_c"], v["ovl_ms"])


def _loo_rules(fold: int | None) -> list[tuple[np.ndarray, float, float]]:
    """(w_span, tau_s, tau_k) to apply; all of them for a created profile (strictest)."""
    fs = [1 - fold] if fold is not None else [0, 1]
    return [(_SPAN[g], float(_P[g]["tau_s"]), float(_P[g]["tau_k"])) for g in fs]


def _merge_rules(fold: int | None):
    fs = [1 - fold] if fold is not None else [0, 1]
    return [_PAIR[g] for g in fs]


def _consts(fold: int | None) -> np.ndarray:
    return (_CONSTS[0] + _CONSTS[1]) / 2 if fold is None else _CONSTS[1 - fold]


class Store:
    def __init__(self) -> None:
        self.sum: dict[int, np.ndarray] = {}
        self.n: dict[int, int] = {}
        self.fold: dict[int, int | None] = {}
        self.next_id = 1
        self.n_enrolled = 0
        self.fe: FrameEmbedder | None = None
        self.log: list[tuple[int, int, str, float]] = []

    def new(self, fold: int | None) -> int:
        pid = self.next_id
        self.next_id += 1
        self.fold[pid] = fold
        self.n[pid] = 0
        return pid

    def add(self, pid: int, emb: np.ndarray) -> None:
        self.sum[pid] = emb.copy() if pid not in self.sum else self.sum[pid] + emb
        self.n[pid] += 1


def make_store() -> Store:
    return Store()


def enroll(store: Store, models: SharedModels, clips: list[np.ndarray]) -> int:
    name = _ENROLLED[store.n_enrolled] if store.n_enrolled < len(_ENROLLED) else None
    store.n_enrolled += 1
    pid = store.new(None if name is None else _FOLD[name])
    for clip in clips:
        for t in models.turns(clip):
            if t.speech_ms >= MIN_UPDATE_MS:
                store.add(pid, unit(models.embed(t.samples)))
    return pid


def _two_groups(turns) -> bool:
    by: dict[int, int] = {}
    for w, _g, ms, _e in turns:
        if ms >= VETO_MS:
            by[w] = by.get(w, 0) + 1
    return any(v >= 2 for v in by.values())


def attribute(store: Store, models: SharedModels, seg: Segment) -> Attribution:
    if store.fe is None or store.fe.counter is not models.counter:
        store.fe = FrameEmbedder(models.counter)
    fe = store.fe
    lead = seg.lead_ms * SR // 1000
    body = seg.samples[lead:]
    f = analyse(models, fe, body, banks=False)
    pids = [p for p in store.sum if store.n[p] > 0]
    if pids:
        C = np.stack([unit(store.sum[p]) for p in pids])
        K = np.stack([_consts(store.fold[p]) for p in pids])
    else:
        C, K = np.zeros((0, 256), np.float32), np.zeros((0, 5, 2))
    pfun = lambda j: _params(store.fold[pids[j]])  # noqa: E731

    singles = [[i] for i in range(len(f.chans))]
    turns, ovl, _ = turns_and_ovl(fe.head, f, singles)
    k, info = decide(turns, C, K, pfun, ovl)
    groups = singles
    if pids and "k" in info:
        g2 = groups_for(f, _merge_rules(store.fold[pids[info["k"]]]))
        if len(g2) < len(singles):
            groups = g2
            turns, ovl, _ = turns_and_ovl(fe.head, f, groups)
            k, info = decide(turns, C, K, pfun, ovl)
    author = None if k is None else pids[k]
    reason = info.get("reason")

    if author is not None:
        for w_span, tau_s, tau_k in _loo_rules(store.fold[author]):
            s1 = s2 = -np.inf
            for g, t in zip(groups, turns, strict=True):
                if t[3] is None:
                    continue
                for i in g:
                    ensure_bank(fe, f.chans[i])
                a, b = loo_stats(group_spans(fe.head, f, g), k, C, K, w_span)
                s1, s2 = max(s1, a), max(s2, b)
            if s1 > tau_s or s2 > tau_k:
                author, k, reason = None, None, "loo"
                break

    sole = not _two_groups(turns) and max(ovl, default=0.0) < _params(None).ovl_ms
    span_of = [(min(f.chans[i].start_ms for i in g) + seg.lead_ms, max(f.chans[i].end_ms for i in g) + seg.lead_ms)
               for g in groups]  # fmt: skip
    updates: list[tuple[int, str, int, int]] = []
    llr = info.get("llr")
    emb_idx = info.get("emb_idx", [])
    if author is not None and sole:
        tau_u = _TAU_U[1 - store.fold[author]] if store.fold[author] is not None else (_TAU_U[0] + _TAU_U[1]) / 2
        for j, i in enumerate(emb_idx):
            ms = turns[i][2]
            if ms < MIN_UPDATE_MS:
                continue
            row = llr[j]
            others = np.delete(row, k)
            m = float(row[k] - others.max()) if len(others) else float("inf")
            if row[k] >= tau_u[bucket(ms)] and m >= M_UPD:
                store.add(author, unit(turns[i][3]))
                store.log.append((seg.segment_id, author, "update", float(row[k])))
                updates.append((author, "update", *span_of[i]))
    elif author is None and sole and reason not in ("seg_veto", "loo"):
        long_ = [j for j, i in enumerate(emb_idx) if turns[i][2] >= MIN_UPDATE_MS]
        rejects = llr is None or all(float(llr[j].max()) < -NEW_LLR for j in range(len(emb_idx)))
        if long_ and rejects:
            pid = store.new(None)
            for j in long_:
                store.add(pid, unit(turns[emb_idx[j]][3]))
            store.log.append((seg.segment_id, pid, "create", float("nan")))
            updates.append((pid, "create", *span_of[emb_idx[long_[0]]]))
            author = pid
    _log_reason(seg, author, reason, groups, f)
    return Attribution(author, [(a, b, author) for a, b in span_of], updates)


def centroid(store: Store, profile_id: int) -> np.ndarray | None:
    s = store.sum.get(profile_id)
    return None if s is None else unit(s)


def _log_reason(seg, author, reason, groups, f) -> None:
    """Dev-only side channel for the failure analysis (R2SPAN_REASONS=1): why a segment got NULL."""
    if os.environ.get("R2SPAN_REASONS") != "1":
        return
    d = OUT / NAME
    d.mkdir(parents=True, exist_ok=True)
    row = {"id": seg.segment_id, "author": author, "reason": reason,
           "merged": sum(1 for g in groups if len(g) > 1), "n_chan": len(f.chans)}
    with (d / "reasons.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + chr(10))
