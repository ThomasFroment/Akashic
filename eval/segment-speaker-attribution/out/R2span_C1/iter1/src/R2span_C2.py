r"""QA-R2-C2 (reduced): C1's windows, split-channel merge and span statistics, with C2/C1's hand
thresholds replaced by one logistic gate (R2span_gate.FEATS) fitted 2-fold over speakers to
wrong <= 0.8% (R2span_C2fit.py). The posterior-proposed spans of the block are dropped: gate (b)
failed (out\R2span_gates\gate_b.json), so the span statistics run over all spans as in C1.

k* = C2's fused argmax over the merged groups; accept iff gate score >= the threshold of k*'s
fold (fitted on the other fold); created profiles need both folds' gates. Updates and new
profiles as C1 (C2's rule).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

import R2span_C1 as c1  # noqa: E402
from C2_decide import Params, decide  # noqa: E402
from C2_lib import SR, bucket  # noqa: E402
from common import OUT, Attribution, Segment, SharedModels, unit  # noqa: E402
from R2span_core import analyse, ensure_bank, group_spans, groups_for, loo_stats, turns_and_ovl  # noqa: E402
from R2span_gate import ed_max, lsame_max, score, vector  # noqa: E402
from R2span_lib import FrameEmbedder  # noqa: E402

from akashic.storage.speakers import MIN_UPDATE_MS  # noqa: E402

NAME = os.environ.get("R2SPAN_NAME", "R2span_C2")
_GATE = {int(f): v for f, v in json.loads((OUT / "R2span_C1" / os.environ.get("R2SPAN_GATE", "gate.json")).read_text(encoding="utf-8")).items()}
LOOSE = Params(-1e9, -1e9, 1e9, -1e9)

Store, make_store, enroll, centroid = c1.Store, c1.make_store, c1.enroll, c1.centroid


def _gates(fold: int | None) -> list[dict]:
    return [_GATE[1 - fold]] if fold is not None else [_GATE[0], _GATE[1]]


def attribute(store, models: SharedModels, seg: Segment) -> Attribution:
    if store.fe is None or store.fe.counter is not models.counter:
        store.fe = FrameEmbedder(models.counter)
    fe = store.fe
    lead = seg.lead_ms * SR // 1000
    body = seg.samples[lead:]
    f, logits = analyse(models, fe, body, banks=False, keep_logits=True)
    pids = [p for p in store.sum if store.n[p] > 0]
    if pids:
        C = np.stack([unit(store.sum[p]) for p in pids])
        K = np.stack([c1._consts(store.fold[p]) for p in pids])
    else:
        C, K = np.zeros((0, 256), np.float32), np.zeros((0, 5, 2))

    singles = [[i] for i in range(len(f.chans))]
    turns, ovl, _ = turns_and_ovl(fe.head, f, singles)
    k, info = decide(turns, C, K, LOOSE, ovl)
    groups = singles
    if k is not None:
        g2 = groups_for(f, c1._merge_rules(store.fold[pids[k]]))
        if len(g2) < len(singles):
            groups = g2
            turns, ovl, _ = turns_and_ovl(fe.head, f, groups)
            k, info = decide(turns, C, K, LOOSE, ovl)
    author = None if k is None else pids[k]
    reason = info.get("reason")
    if author is not None:
        fold = store.fold[author]
        chans = [{"win": c.win, "emb": c.emb, "ms": c.speech_ms} for c in f.chans]
        me, md = ed_max(logits)
        for g_rule, (w_span, _ts, _tk), (w_pair, _tm) in zip(_gates(fold), c1._loo_rules(fold), c1._merge_rules(fold), strict=True):
            s1 = s2 = -np.inf
            for g, t in zip(groups, turns, strict=True):
                if t[3] is None:
                    continue
                for i in g:
                    ensure_bank(fe, f.chans[i])
                a, b = loo_stats(group_spans(fe.head, f, g), k, C, K, w_span)
                s1, s2 = max(s1, a), max(s2, b)
            v = vector(info, k, turns, ovl, lsame_max(chans, groups, w_pair), me, md, s1, s2)
            if score(v, g_rule) < g_rule["thr"]:
                author, reason = None, "gate"
                break
    k = None if author is None else k

    sole = not c1._two_groups(turns) and max(ovl, default=0.0) < c1._params(None).ovl_ms
    span_of = [(min(f.chans[i].start_ms for i in g) + seg.lead_ms, max(f.chans[i].end_ms for i in g) + seg.lead_ms)
               for g in groups]  # fmt: skip
    updates: list[tuple[int, str, int, int]] = []
    llr = info.get("llr")
    emb_idx = info.get("emb_idx", [])
    if author is not None and sole:
        tau_u = c1._TAU_U[1 - store.fold[author]] if store.fold[author] is not None else (c1._TAU_U[0] + c1._TAU_U[1]) / 2
        for j, i in enumerate(emb_idx):
            ms = turns[i][2]
            if ms < MIN_UPDATE_MS:
                continue
            row = llr[j]
            others = np.delete(row, k)
            m = float(row[k] - others.max()) if len(others) else float("inf")
            if row[k] >= tau_u[bucket(ms)] and m >= c1.M_UPD:
                store.add(author, unit(turns[i][3]))
                updates.append((author, "update", *span_of[i]))
    elif author is None and sole and reason not in ("seg_veto", "gate"):
        long_ = [j for j, i in enumerate(emb_idx) if turns[i][2] >= MIN_UPDATE_MS]
        rejects = llr is None or all(float(llr[j].max()) < -c1.NEW_LLR for j in range(len(emb_idx)))
        if long_ and rejects:
            pid = store.new(None)
            for j in long_:
                store.add(pid, unit(turns[emb_idx[j]][3]))
            updates.append((pid, "create", *span_of[emb_idx[long_[0]]]))
            author = pid
    _log_reason(seg, author, reason, groups, f)
    return Attribution(author, [(a, b, author) for a, b in span_of], updates)


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
