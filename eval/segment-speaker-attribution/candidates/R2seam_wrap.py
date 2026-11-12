"""QA-R2-C3: exact-copy seam reconciliation with a deferred author, as a wrapper over a base
candidate (run with candidates/R2seam_harness.py, which applies late author revisions).

Environment
- SEAM_BASE: base candidate file (default candidates/C2_calib.py; C2_CALIB_FILE defaults to
  calib_iter2.json).
- SEAM_TRIMMED: 0 (default; the fixture has no transcript-trim flag, so trimmed is unknown and
  N+1's lead counts as body: the base gets lead_ms = 0) or 1 (the lead is trimmed: the base gets
  the segment as the fixture gives it, lead_ms = 1984).
- SEAM_RULE: 1 (default) or 0 (control: only the SEAM_TRIMMED handling, no deferral, no rule).
- SEAM_IDENT_LLR: C2 bases only, the calibrated LLR a seam channel's best profile must reach
  (default 0.0 = "more likely this profile than not").

Rule (never assigns an author, only removes one)
- A segment cut at the 30 s cap (length >= 30 000 ms) that the base authors as k_N is returned
  with author NULL (pending); its profile updates of existing profiles are held back too.
- When its continuation N+1 arrives, after the base has processed N+1:
  copy check: 32 ms log-envelope correlation of N+1's lead vs N's last lead_ms >= 0.8, else N is
  decided alone (k_N);
  every local channel of N+1's first 10 s window (>= 8 s of right context past the seam) with
  >= 300 ms of activity inside the lead needs >= 1 s of solo speech in that window, embedded and
  identified against the profiles with the base's own scoring; N keeps k_N only if every such
  channel is k_N, otherwise N becomes NULL. Kept: N's held updates are applied; NULL: dropped.
- No continuation (the next segment is not N's continuation, or the stream ends): N is decided
  alone (stands for the 120 s timeout).
The seam window's segmentation and channel embeddings are memoised per segment, so they are free
when the base already computed the same window (C2 with the lead as body).
"""

import hashlib
import importlib.util
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
for p in (HERE, HERE.parent):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))
os.environ.setdefault("C2_CALIB_FILE", "calib_iter2.json")

from C2_lib import WINDOW, window_turns  # noqa: E402
from common import LEAD_MS, MAX_SEGMENT_MS, SR, Attribution, PrevSegment, Segment  # noqa: E402

BASE_PATH = Path(os.environ.get("SEAM_BASE", str(HERE / "C2_calib.py")))
if not BASE_PATH.is_absolute():
    BASE_PATH = (HERE.parent / BASE_PATH).resolve()
TRIMMED = os.environ.get("SEAM_TRIMMED", "0") == "1"
RULE = os.environ.get("SEAM_RULE", "1") == "1"
IDENT_LLR = float(os.environ.get("SEAM_IDENT_LLR", "0"))
# Iteration 2 (SEAM_ITER=2, default): copy check on the decoded waveform's correlation (>= 0.5)
# instead of the log envelope, and the seam channels taken from one seam-centred 10 s window
# spliced at the exact copy, N[-5 s:] + N+1[lead : lead + 5 s] (cut at the window centre, 5 s
# of context on each side), instead of N+1's first window (seam at its left edge).
# SEAM_ITER=1: iteration 1 as first measured (log envelope >= 0.8, N+1's first window).
ITER = int(os.environ.get("SEAM_ITER", "2"))
WAVE_CORR = 0.5

_spec = importlib.util.spec_from_file_location(BASE_PATH.stem, BASE_PATH)
base = importlib.util.module_from_spec(_spec)
sys.modules[BASE_PATH.stem] = base
_spec.loader.exec_module(base)

NAME = f"R2seam_{base.NAME}_t{int(TRIMMED)}" + ("" if RULE else "_norule") + (
    f"_llr{IDENT_LLR:g}" if IDENT_LLR != 0 and hasattr(base, "_CONSTS") else ""
) + ("_i1" if ITER == 1 and RULE else "")

CAP_MS = 30_000  # capture/audio.py cuts at >= 30 000 ms (30 016 with the 32 ms grid)
ENV = 512  # 32 ms at 16 kHz
COPY_CORR = 0.8
SEAM_MIN_MS = 300
EVIDENCE_MS = 1_000
FRAMES = 589
PER_FRAME = WINDOW / FRAMES
assert MAX_SEGMENT_MS >= CAP_MS and LEAD_MS == 1984


# --- per-run state (reset by make_store) ------------------------------------------------------


@dataclass
class _Pending:
    segment_id: int
    author: int
    samples: np.ndarray
    delta: dict  # profile id -> (vector delta, count delta) held back


@dataclass
class _State:
    pending: _Pending | None = None
    revisions: list = field(default_factory=list)  # (segment_id, author or None)
    deferred: set = field(default_factory=set)
    log: dict = field(default_factory=dict)  # segment_id -> seam outcome


S = _State()


def make_store():
    global S
    S = _State()
    return base.make_store()


def enroll(store, models, clips):
    return base.enroll(store, models, clips)


def centroid(store, profile_id):
    return base.centroid(store, profile_id)


def drain_revisions() -> list:
    """[(segment_id, final author or None, provisional author)]"""
    out, S.revisions = S.revisions, []
    return out


def finish(store, models) -> None:
    """End of stream: a pending segment is decided alone (stands for the timeout)."""
    if S.pending is not None:
        _resolve(store, S.pending, True, "timeout")


def extras() -> dict:
    return {"deferred": sorted(S.deferred), "seam": {str(k): v for k, v in S.log.items()}}


# --- memoised models ------------------------------------------------------------------------


class _Memo:
    def __init__(self, models) -> None:
        self._m = models
        self._seg: dict = {}
        self._emb: dict = {}

    def __getattr__(self, name):
        return getattr(self._m, name)

    def segment_logits(self, window: np.ndarray) -> np.ndarray:
        padded = np.zeros(WINDOW, np.float32)
        n = min(len(window), WINDOW)
        padded[:n] = window[:n]
        k = hashlib.blake2b(padded.tobytes(), digest_size=16).digest()
        if k not in self._seg:
            self._seg[k] = self._m.segment_logits(padded)
        return self._seg[k]

    def embed(self, samples: np.ndarray) -> np.ndarray:
        x = np.ascontiguousarray(samples, np.float32)
        k = hashlib.blake2b(x.tobytes(), digest_size=16).digest()
        if k not in self._emb:
            self._emb[k] = self._m.embed(x)
        return self._emb[k]


# --- store adapters (C2: sum/n; C1: profiles[pid] = [sum, n]) -------------------------------


def _state(store) -> dict:
    if hasattr(store, "sum"):
        return {p: (store.sum[p].copy(), store.n[p]) for p in store.sum}
    return {p: (v[0].copy(), v[1]) for p, v in store.profiles.items()}


def _delta(before: dict, after: dict) -> dict:
    d = {}
    for p, (v, n) in after.items():
        if p in before and (n != before[p][1] or not np.array_equal(v, before[p][0])):
            d[p] = (v - before[p][0], n - before[p][1])
    return d


def _apply(store, delta: dict, sign: int) -> None:
    for p, (dv, dn) in delta.items():
        if hasattr(store, "sum"):
            store.sum[p] = store.sum[p] + sign * dv
            store.n[p] += sign * dn
        else:
            store.profiles[p][0] = store.profiles[p][0] + sign * dv
            store.profiles[p][1] += sign * dn


def _identify(store, emb: np.ndarray, ms: float) -> int | None:
    """One channel's identity with the base's own scoring."""
    if hasattr(base, "_CONSTS"):  # C2: calibrated per-bucket LLR, best profile >= IDENT_LLR
        from C2_decide import llr_matrix

        from common import unit

        pids = [p for p in store.sum if store.n[p] > 0]
        if not pids:
            return None
        C = np.stack([unit(store.sum[p]) for p in pids])
        K = np.stack([base._other(store.fold[p], base._CONSTS) for p in pids])  # noqa: SLF001
        _, llr = llr_matrix(unit(emb)[None], np.array([ms], float), C, K)
        j = int(np.argmax(llr[0]))
        return pids[j] if llr[0, j] >= IDENT_LLR else None
    if hasattr(base, "_match"):  # C1: best cosine >= 0.5
        return base._match(store, emb)[0]  # noqa: SLF001
    from common import unit

    best, score = None, -1.0
    for p in getattr(store, "profiles", {}):
        c = base.centroid(store, p)
        if c is not None and float(unit(emb) @ c) > score:
            best, score = p, float(unit(emb) @ c)
    return best if score >= 0.5 else None


# --- seam -------------------------------------------------------------------------------------


def _envelope(x: np.ndarray) -> np.ndarray:
    n = len(x) // ENV
    e = (x[: n * ENV].astype(np.float64) ** 2).reshape(n, ENV).mean(1)
    return np.log(e + 1e-10)


def _copy_ok(prev: np.ndarray, cur: np.ndarray, lead: int) -> bool:
    if lead < ENV * 4 or len(prev) < lead or len(cur) < lead:
        return False
    if ITER >= 2:  # the same PCM through two independent Opus encodes: waveforms align
        a, b = cur[:lead].astype(np.float64), prev[-lead:].astype(np.float64)
        if a.std() < 1e-6 or b.std() < 1e-6:
            return False
        return float(np.corrcoef(a, b)[0, 1]) >= WAVE_CORR
    a, b = _envelope(cur[:lead]), _envelope(prev[-lead:])
    if a.std() < 1e-6 or b.std() < 1e-6:
        return False
    return float(np.corrcoef(a, b)[0, 1]) >= COPY_CORR


def _seam(store, memo: _Memo, seg: Segment, pend: _Pending) -> tuple[bool, str]:
    lead_ms = min(max(seg.lead_ms, 0), LEAD_MS)
    lead = lead_ms * SR // 1000
    if lead_ms == 0:
        return True, "no_lead"
    if not _copy_ok(pend.samples, seg.samples, lead):
        return True, "copy_unconfirmed"  # decided alone
    if ITER >= 2:  # seam-centred window spliced at the copy; seam = [half - lead, half)
        half = WINDOW // 2
        left = pend.samples[-half:]
        right = seg.samples[lead : lead + half]
        chunk = np.concatenate([np.zeros(half - len(left), np.float32), left, right])
        seam_lo, seam_hi = half - lead, half
    else:
        chunk = seg.samples[:WINDOW]
        seam_lo, seam_hi = 0, lead
    padded = np.zeros(WINDOW, np.float32)
    padded[: len(chunk)] = chunk
    logits = memo.segment_logits(padded)
    from C2_lib import decode

    active = decode(logits)
    real = min(len(active), int(len(chunk) / PER_FRAME))
    centre = (np.arange(len(active)) + 0.5) * PER_FRAME
    in_seam = (centre >= seam_lo) & (centre < seam_hi)
    turns = {t.ch: t for t in window_turns(padded, logits, len(chunk), 0, 0, min_ms=1)}
    n_seam = 0
    for ch in range(3):
        seam_ms = (active[:real, ch] & in_seam[:real]).sum() * PER_FRAME * 1000 / SR
        if seam_ms < SEAM_MIN_MS:
            continue
        n_seam += 1
        t = turns.get(ch)
        if t is None or t.speech_ms < EVIDENCE_MS or t.samples is None:
            return False, f"short_evidence:{0 if t is None else t.speech_ms}"
        who = _identify(store, memo.embed(t.samples), t.speech_ms)
        if who != pend.author:
            return False, "other_identity" if who is not None else "no_identity"
    return True, "confirmed" if n_seam else "empty_seam"


def _resolve(store, pend: _Pending, keep: bool, why: str) -> None:
    if keep:
        _apply(store, pend.delta, +1)
    S.revisions.append((pend.segment_id, pend.author if keep else None, pend.author))
    S.log[pend.segment_id] = why
    S.pending = None


# --- the job ----------------------------------------------------------------------------------


def attribute(store, models, seg: Segment) -> Attribution:
    memo = _Memo(models)
    pend = S.pending
    is_cont = pend is not None and seg.prev is not None and seg.prev.segment_id == pend.segment_id
    if pend is not None and not is_cont:
        _resolve(store, pend, True, "timeout")
        pend = None
    prev = seg.prev
    if is_cont:  # the base sees N's provisional author (what N decided alone)
        prev = PrevSegment(prev.segment_id, pend.author, prev.samples)
    bseg = Segment(seg.segment_id, seg.samples, seg.sr, seg.lead_ms if TRIMMED else 0, prev)
    before = _state(store) if RULE else None
    att = base.attribute(store, memo, bseg)
    if is_cont:
        keep, why = _seam(store, memo, seg, pend)
        _resolve(store, pend, keep, why)
    length_ms = len(seg.samples) * 1000 // seg.sr
    if RULE and length_ms >= CAP_MS and att.author is not None:
        delta = _delta(before, _state(store))
        _apply(store, delta, -1)  # held back until the continuation confirms the author
        S.pending = _Pending(seg.segment_id, att.author, seg.samples, delta)
        S.deferred.add(seg.segment_id)
        att = Attribution(None, att.turns, att.updates)
    return att
