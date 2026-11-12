"""C2: calibrated duration-bucketed evidence fusion with margin and veto (with the refuter's fix).

Per segment (the lead of a continuation is dropped: its voice is never credited):
1. Windows over the body: fixed 10 s from 0, the last one end-aligned (a full 10 s ending at the
   end of the audio, all its frames counted), a tail under 300 ms skipped.
2. Every local channel with >= 300 ms of solo speech is embedded (WeSpeaker, the app's embed).
3. Cosine -> LLR per duration bucket {0.3-1, 1-2, 2-4, 4-8, >=8 s}: LLR = a_b * cos + b_b,
   constants fitted offline (C2_fit.py, numpy IRLS, 2-fold over speakers: a profile of fold f
   uses constants and thresholds fitted on the other fold's speakers only).
4. k* = argmax_k sum_i clip(LLR_ik, +-8). Accept iff sum >= tau_a, margin to the runner-up
   >= m, min_i LLR_ik* >= -tau_v, and every window holding two channels with >= 300 ms solo
   speech each has both channels at LLR(k*) >= tau_c (segmentation veto).
5. Updates: only a sole-body-speaker segment (no two-channel window), turns >= 2 s with
   LLR >= tau_u (the 0.1% false-accept point of the bucket) and per-turn margin >= M_UPD;
   the LLR is stored with the update. A new unnamed profile only when every embedded turn
   >= 2 s has max LLR < -NEW_LLR against all profiles (and sole body speaker).
Not included: conversation-scoped candidate sets (the fixture has no stream/time order) and the
one-time drift repair.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

from C2_decide import VETO_MS, Params, decide  # noqa: E402
from C2_lib import SR, bucket, segment_turns  # noqa: E402
from common import FIXTURE, OUT, Attribution, Segment, SharedModels, unit  # noqa: E402

from akashic.storage.speakers import MIN_UPDATE_MS  # noqa: E402

NAME = "C2_calib"
M_UPD = 2.0  # per-turn LLR margin to the runner-up profile for an update
NEW_LLR = 2.0  # create a profile only when every profile rejects with LLR < -NEW_LLR

# C2_CALIB_FILE picks an iteration's calibration (calib_iter1.json / calib_iter2.json)
_CAL = json.loads((OUT / "C2_calib" / os.environ.get("C2_CALIB_FILE", "calib.json")).read_text(encoding="utf-8"))
_FOLD = _CAL["folds"]
_CONSTS = {int(f): np.array(v, float) for f, v in _CAL["consts"].items()}
_TAU_U = {int(f): np.array(v, float) for f, v in _CAL["tau_u"].items()}
_PARAMS = {
    int(f): Params(v["tau_a"], v["margin"], v["tau_v"], v["tau_c"], v.get("ovl_ms", 1e9))
    for f, v in _CAL["params"].items()
}
# Fold bookkeeping only: the harness enrols the enrolled speakers in sorted order, so the i-th
# enroll() call is the i-th name; the name selects which fold's numbers the profile must NOT use.
_index = json.loads((FIXTURE / "index.json").read_text(encoding="utf-8"))
_ENROLLED = sorted(s for s, r in _index["roles"].items() if r == "enrolled")
del _index


def _other(fold: int | None, table: dict):
    """Numbers for a profile of ``fold``: the other fold's; mean of both for created ones."""
    if fold is None:
        return (table[0] + table[1]) / 2 if isinstance(table[0], np.ndarray) else None
    return table[1 - fold]


def _params(fold: int | None) -> Params:
    if fold is None:
        a, b = _PARAMS[0], _PARAMS[1]
        return Params(max(a.tau_a, b.tau_a), max(a.margin, b.margin), min(a.tau_v, b.tau_v),
                      max(a.tau_c, b.tau_c), min(a.ovl_ms, b.ovl_ms))  # fmt: skip
    return _PARAMS[1 - fold]


class Store:
    def __init__(self) -> None:
        self.sum: dict[int, np.ndarray] = {}
        self.n: dict[int, int] = {}
        self.fold: dict[int, int | None] = {}
        self.next_id = 1
        self.n_enrolled = 0
        self.log: list[tuple[int, int, str, float]] = []  # (segment, profile, kind, llr)

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
    """As the reference: every >= 2 s turn of the clips (app windowing) summed."""
    name = _ENROLLED[store.n_enrolled] if store.n_enrolled < len(_ENROLLED) else None
    store.n_enrolled += 1
    pid = store.new(None if name is None else _FOLD[name])
    for clip in clips:
        for t in models.turns(clip):
            if t.speech_ms >= MIN_UPDATE_MS:
                store.add(pid, unit(models.embed(t.samples)))
    return pid


def attribute(store: Store, models: SharedModels, seg: Segment) -> Attribution:
    lead = seg.lead_ms * SR // 1000
    body = seg.samples[lead:]
    turns, ovl = segment_turns(models, body)
    tt = [(t.win, t.ch, t.speech_ms, None if t.samples is None else unit(models.embed(t.samples)))
          for t in turns]  # fmt: skip
    pids = [p for p in store.sum if store.n[p] > 0]
    if pids:
        C = np.stack([unit(store.sum[p]) for p in pids])
        K = np.stack([_other(store.fold[p], _CONSTS) for p in pids])
    else:
        C, K = np.zeros((0, 256), np.float32), np.zeros((0, 5, 2))
    k, info = decide(tt, C, K, lambda j: _params(store.fold[pids[j]]), ovl)
    author = None if k is None else pids[k]

    sole = not _two_channels(tt) and max(ovl, default=0.0) < _params(None).ovl_ms
    updates: list[tuple[int, str, int, int]] = []
    llr = info.get("llr")
    emb_idx = info.get("emb_idx", [])
    if author is not None and sole:
        tau_u = _other(store.fold[author], _TAU_U)
        for j, i in enumerate(emb_idx):
            ms = tt[i][2]
            if ms < MIN_UPDATE_MS:
                continue
            row = llr[j]
            others = np.delete(row, k)
            m = float(row[k] - others.max()) if len(others) else float("inf")
            if row[k] >= tau_u[bucket(ms)] and m >= M_UPD:
                store.add(author, tt[i][3])
                store.log.append((seg.segment_id, author, "update", float(row[k])))
                updates.append((author, "update", turns[i].start_ms + seg.lead_ms,
                                turns[i].end_ms + seg.lead_ms))  # fmt: skip
    elif author is None and sole and info.get("reason") != "seg_veto":
        long_ = [j for j, i in enumerate(emb_idx) if tt[i][2] >= MIN_UPDATE_MS]
        rejects = llr is None or all(float(llr[j].max()) < -NEW_LLR for j in range(len(emb_idx)))
        if long_ and rejects:
            pid = store.new(None)
            for j in long_:
                store.add(pid, tt[emb_idx[j]][3])
            first = turns[emb_idx[long_[0]]]
            store.log.append((seg.segment_id, pid, "create", float("nan")))
            updates.append((pid, "create", first.start_ms + seg.lead_ms,
                            first.end_ms + seg.lead_ms))  # fmt: skip
            author = pid
    return Attribution(
        author,
        [(t.start_ms + seg.lead_ms, t.end_ms + seg.lead_ms, author) for t in turns],
        updates,
    )


def _two_channels(tt) -> bool:
    by: dict[int, int] = {}
    for w, _c, ms, _e in tt:
        if ms >= VETO_MS:
            by[w] = by.get(w, 0) + 1
    return any(v >= 2 for v in by.values())


def centroid(store: Store, profile_id: int) -> np.ndarray | None:
    s = store.sum.get(profile_id)
    return None if s is None else unit(s)
