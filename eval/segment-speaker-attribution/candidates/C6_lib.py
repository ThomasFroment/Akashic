"""C6 short-turn embedder: shared code for the variant files (C6_a_*.py, C6_b_*.py).

Variant (a), single swap: ``reference.py`` unchanged (the app's SpeakersHandler and SpeakerStore
on in-memory SQLite) with the embedding model replaced; ``threshold`` is best_match's cosine
(0.5 today; a swapped model may need its own).

Variant (b), dual model: the reference rules (fixed 10 s windows, identify >= 1 s, update or
create >= 2 s, author only when every turn is the same profile) with two profile sums per voice:
``speakers.vec_sum`` stays in the LM space, an additive ``speaker_vectors(speaker_id, model,
vec_sum, vec_n)`` row holds the short model S. A turn under ``SPLIT_MS`` of speech is scored with
S against profiles with n_S >= 3 (else LM); a longer turn with LM. Every update embeds with both
models so the two sums stay in step. Each space has its own threshold; the best profile is the
one with the largest score minus its space's threshold.
"""

from __future__ import annotations

import importlib.util
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import onnxruntime as ort

HERE = Path(__file__).resolve().parents[1]
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from common import DATA, Attribution, Segment, SharedModels, pipe, unit  # noqa: E402

from akashic.storage.speakers import MIN_IDENTIFY_MS, MIN_UPDATE_MS  # noqa: E402

PATHS = {
    "resnet34": DATA / "models_extra/wespeaker-voxceleb-resnet34/voxceleb_resnet34.onnx",
    "campplus": DATA / "models_extra/wespeaker-voxceleb-campplus/voxceleb_CAM++.onnx",
}

_spec = importlib.util.spec_from_file_location("C6_reference", HERE / "candidates/reference.py")
reference = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(reference)


class ShortModel:
    """A second WeSpeaker ONNX model with the app's front end (pipe.fbank, input ``feats``),
    the app's session options, timed into the harness's embedding counter."""

    def __init__(self, name: str) -> None:
        o = ort.SessionOptions()
        o.log_severity_level = 3
        self.session = ort.InferenceSession(
            str(PATHS[name]), o, providers=["CPUExecutionProvider"]
        )

    def embed(self, shared: SharedModels, samples: np.ndarray) -> np.ndarray:
        f = pipe.fbank(samples)
        if len(f) == 0:
            raise ValueError("audio too short to embed")
        t = time.perf_counter()
        v = self.session.run(None, {"feats": f[None]})[0][0]
        shared.counter["emb_s"] += time.perf_counter() - t
        shared.counter["emb_n"] += 1
        return unit(np.asarray(v, np.float32))


_SHORT: dict[str, ShortModel] = {}


def short(name: str) -> ShortModel:
    if name not in _SHORT:
        _SHORT[name] = ShortModel(name)
    return _SHORT[name]


# --- variant (a): single swap --------------------------------------------------------------


class SwapModels:
    """The app's VoiceModels with embed() replaced; turns() is the app's."""

    def __init__(self, shared: SharedModels, model: ShortModel) -> None:
        self.shared, self.model = shared, model

    def available(self) -> bool:
        return True

    def turns(self, samples):
        return self.shared.turns(samples)

    def embed(self, samples):
        return self.model.embed(self.shared, samples)

    def unload_if_idle(self, *, force: bool = False) -> None:
        pass


def make_swap(model_name: str, threshold: float):
    from akashic.storage.speakers import SpeakerStore

    def make_store():
        st = reference.Store()
        st.speakers = SpeakerStore(st.conn, threshold)
        return st

    def enroll(store, models, clips):
        return reference.enroll(store, SwapModels(models, short(model_name)), clips)

    def attribute(store, models, seg: Segment) -> Attribution:
        return reference.attribute(store, SwapModels(models, short(model_name)), seg)

    def centroid(store, profile_id):
        return None  # not the LM space of the harness's clean centroids

    return make_store, enroll, attribute, centroid


# --- variant (b): dual model ---------------------------------------------------------------

N_S_MIN = 3


@dataclass
class Profile:
    lm_sum: np.ndarray | None = None
    lm_n: int = 0
    s_sum: np.ndarray | None = None  # speaker_vectors(model = S)
    s_n: int = 0


@dataclass
class DualStore:
    profiles: dict[int, Profile] = field(default_factory=dict)
    next_id: int = 1


def make_dual(model_name: str, thr_lm: float, thr_s: float, split_ms: int):
    def make_store():
        return DualStore()

    def add(p: Profile, lm: np.ndarray, s: np.ndarray) -> None:
        p.lm_sum = lm.copy() if p.lm_sum is None else p.lm_sum + lm
        p.lm_n += 1
        p.s_sum = s.copy() if p.s_sum is None else p.s_sum + s
        p.s_n += 1

    def enroll(store: DualStore, models: SharedModels, clips):
        pid = store.next_id
        store.next_id += 1
        p = Profile()
        store.profiles[pid] = p
        for clip in clips:
            for t in models.turns(clip):
                if t.speech_ms >= MIN_UPDATE_MS:
                    add(p, models.embed(t.samples), short(model_name).embed(models, t.samples))
        return pid

    def attribute(store: DualStore, models: SharedModels, seg: Segment) -> Attribution:
        S = short(model_name)
        rows: list[tuple[int, int, int | None]] = []
        updates: list[tuple[int, str, int, int]] = []
        for t in models.turns(seg.samples):
            if t.speech_ms < MIN_IDENTIFY_MS:
                rows.append((t.start_ms, t.end_ms, None))
                continue
            may_update = t.speech_ms >= MIN_UPDATE_MS
            use_s = t.speech_ms < split_ms
            lm = s = None
            need_lm = may_update or not use_s or any(
                p.s_n < N_S_MIN for p in store.profiles.values()
            )
            if need_lm:
                lm = models.embed(t.samples)
            if use_s or may_update:
                s = S.embed(models, t.samples)
            best: tuple[int, float] | None = None
            for pid, p in store.profiles.items():
                if use_s and p.s_n >= N_S_MIN:
                    margin = float(s @ unit(p.s_sum)) - thr_s
                elif p.lm_n > 0:
                    margin = float(lm @ unit(p.lm_sum)) - thr_lm
                else:
                    continue
                if best is None or margin > best[1]:
                    best = (pid, margin)
            if best is not None and best[1] >= 0:
                if may_update:
                    add(store.profiles[best[0]], lm, s)
                    updates.append((best[0], "update", t.start_ms, t.end_ms))
                rows.append((t.start_ms, t.end_ms, best[0]))
            elif may_update:
                pid = store.next_id
                store.next_id += 1
                store.profiles[pid] = Profile()
                add(store.profiles[pid], lm, s)
                updates.append((pid, "create", t.start_ms, t.end_ms))
                rows.append((t.start_ms, t.end_ms, pid))
            else:
                rows.append((t.start_ms, t.end_ms, None))
        ids = {r[2] for r in rows}
        author = next(iter(ids)) if rows and len(ids) == 1 and None not in ids else None
        return Attribution(author, rows, updates)

    def centroid(store: DualStore, profile_id: int):
        p = store.profiles.get(profile_id)
        return None if p is None or p.lm_sum is None else unit(p.lm_sum)

    return make_store, enroll, attribute, centroid
