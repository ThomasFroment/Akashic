"""The synthetic overlap set: two corpus talkers mixed at a set overlap ratio and SIR, over an
optional music or game bed, with exact references and exact per-process streams.

Grid: overlap ratio {0, .25, .5, .75, 1} x SIR {-6, 0, +6} dB x language {en, fr, fr-en, mixed}
= 60 mixtures, plus 12 solo mixtures (talker A alone, each language over each bed: the clean
reference; overlap = SOLO). The bed (none, music, game) and the process layout (talkers on two processes, or
both on one, like two friends in one Discord call) are cycled over the grid. Talker A is a call
voice, talker B a video or spare voice (distinct speakers); 'mixed' puts a spliced FR/EN clip in A.

Overlap ratio = overlapped voiced time / the shorter talker's voiced length (0 = B starts 0.4 s
after A's speech ends). SIR = A's voiced RMS over B's voiced RMS. Levels: the louder talker at
a voiced RMS near the stored call segments (peaks near 1), beds at half that level, as in the runs.

Each mixture sits in its own 60 s slot of a virtual timeline. The stored-like segments come from
Akashic's own capture Segmenter + Silero VAD over the mixed stream after an Opus round trip
(storage.opus), as the store would hold them. Process streams are exact (no codec).
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass

import numpy as np

import core
import score

T0 = 1_900_000_000_000
SLOT_MS = 60_000
LEAD_MS = 1_000
OVERLAPS = (0.0, 0.25, 0.5, 0.75, 1.0)
SIRS = (-6, 0, 6)
LANGS = ("en", "fr", "fr-en", "mixed")
BEDS = ("none", "music", "game")
SOLO = -1.0  # overlap value of the single-talker mixtures
LEVEL_A = 1.6  # louder talker: voiced RMS about 0.08, as the stored call segments; peaks stay near 1
BED_GAIN = {"music": 0.5 * LEVEL_A, "game": 0.5 * LEVEL_A * 2.25 / 2.4}  # the runs: beds at half the call level
SEED = 20261120


@dataclass
class Mix:
    i: int
    t0: int  # slot start (virtual wall ms)
    overlap: float
    sir: int
    lang: str
    bed: str
    procs: str  # 'two' or 'one'
    talkers: list  # [{"name", "speaker", "text", "lang", "spans": abs, "turns": [(speaker, a, b)], "exe"}]
    length_ms: int


class Set:
    """Same interface as core.Run for the candidate context."""

    id = "synthetic"

    def __init__(self) -> None:
        manifest = json.loads((core.assets.ASSETS / "manifest.json").read_text(encoding="utf-8"))
        self.clips = manifest["clips"]
        self.speakers = manifest["speakers"]
        self.mixes: list[Mix] = []
        self.streams: dict[int, dict[str, np.ndarray]] = {}  # slot -> exe -> samples (slot-relative)
        self.mixed: dict[int, np.ndarray] = {}
        self._build()
        self.segments, self._seg_audio = self._segment()

    # building -------------------------------------------------------------------------------

    def _pool(self, lang: str, roles: tuple[str, ...]) -> list[dict]:
        return [c for c in self.clips if c["kind"] == "speech" and "parts" not in c and c["lang"] == lang and self.speakers[c["speaker"]]["role"] in roles
                and 4.0 <= c["dur_s"] <= 13.0]

    def _build(self) -> None:
        rng = random.Random(SEED)
        mixed_pool = [c for c in self.clips if "parts" in c]
        i = 0
        for io, ov in enumerate(OVERLAPS):
            for isr, sir in enumerate(SIRS):
                for il, lang in enumerate(LANGS):
                    la, lb = {"en": ("en", "en"), "fr": ("fr", "fr"), "fr-en": ("fr", "en"), "mixed": ("mixed", rng.choice(("en", "fr")))}[lang]
                    a = rng.choice(mixed_pool) if la == "mixed" else rng.choice(self._pool(la, ("call",)))
                    b = rng.choice([c for c in self._pool(lb, ("video", "spare")) if c["speaker"] not in (a["speaker"], *(p["speaker"] for p in a.get("parts", [])))])
                    bed = BEDS[(io + isr + il) % 3]
                    procs = ("two", "one")[(io + il) % 2]
                    self._mix(i, a, b, ov, sir, lang, bed, procs)
                    i += 1
        # solo: talker A alone (the clean reference), every language over every bed
        for il, lang in enumerate(LANGS):
            for bed in BEDS:
                a = rng.choice(mixed_pool) if lang == "mixed" else rng.choice(self._pool(lang.split("-")[0], ("call",)))
                b = rng.choice(self._pool("en", ("spare",)))
                self._mix(i, a, b, SOLO, 0, lang, bed, "two")
                i += 1

    def _mix(self, i: int, a: dict, b: dict, ov: float, sir: int, lang: str, bed: str, procs: str) -> None:
        xa = core.bank(a["bank"])[a["off"]:a["off"] + a["n"]].astype(np.float32)
        xb = core.bank(b["bank"])[b["off"]:b["off"] + b["n"]].astype(np.float32)
        a0, a1 = a["spans"][0][0], a["spans"][-1][1]
        b0, b1 = b["spans"][0][0], b["spans"][-1][1]
        sa = LEAD_MS / 1000  # clip A starts at LEAD
        ov_s = max(0.0, ov) * min(a1 - a0, b1 - b0)
        b_voice_start = sa + a1 - ov_s if ov > 0 else sa + a1 + 0.4
        sb = b_voice_start - b0
        if sb < 0:  # B's voice cannot start before the slot
            sa, sb = sa - sb, 0.0
        voiced = lambda x, c: np.concatenate([x[int(p * core.SR):int(q * core.SR)] for p, q in c["spans"]])
        ra, rb = np.sqrt(np.mean(voiced(xa, a) ** 2)), np.sqrt(np.mean(voiced(xb, b) ** 2))
        ga = LEVEL_A
        gb = LEVEL_A * (ra / rb) * 10 ** (-sir / 20)
        top = max(ga * ra, gb * rb) / (LEVEL_A * max(ra, rb))  # the louder talker sits at the calibrated call level
        ga, gb = ga / top, gb / top
        solo = ov == SOLO
        if solo:
            gb, sb = 0.0, sa
        length = int((max(sa + a["dur_s"], (sb + b["dur_s"]) if not solo else 0) + 1.0) * core.SR)
        ta, tb = np.zeros(length, np.float32), np.zeros(length, np.float32)
        ia, ib = int(sa * core.SR), int(sb * core.SR)
        ta[ia:ia + len(xa)] += xa * np.float32(ga)
        tb[ib:ib + len(xb)] += xb * np.float32(gb)
        t0 = T0 + i * SLOT_MS
        exe_a, exe_b = "discord.exe", ("chrome.exe" if procs == "two" else "discord.exe")
        streams = {exe_a: ta.copy()}
        if not solo:
            streams[exe_b] = streams.get(exe_b, np.zeros(length, np.float32)) + tb
        if bed != "none":
            src = core.bank(bed)
            reps = int(np.ceil(length / len(src)))
            bx = np.tile(src, reps)[:length] * np.float32(BED_GAIN[bed])
            streams["spotify.exe" if bed == "music" else "qualitygame.exe"] = bx
        mix = sum(streams.values())
        self.streams[i] = streams
        self.mixed[i] = mix.astype(np.float32)  # float loopback: no clipping (peaks are reported in detail)
        talkers = []
        for name, c, s, exe in (("A", a, sa, exe_a), ("B", b, sb, exe_b))[:1 if solo else 2]:
            st = t0 + round(s * 1000)
            spans = [(st + round(p * 1000), st + round(q * 1000)) for p, q in c["spans"]]
            if "parts" in c:
                cut = st + round(c["split_s"] * 1000)
                turns = [(c["parts"][0]["speaker"], lo, min(hi, cut)) for lo, hi in spans if lo < cut] + [(c["parts"][1]["speaker"], max(lo, cut), hi) for lo, hi in spans if hi > cut]
            else:
                turns = [(c["speaker"], lo, hi) for lo, hi in spans]
            talkers.append({"name": name, "speaker": c["speaker"], "text": c["text"], "lang": c["lang"], "spans": spans, "turns": turns, "exe": exe})
        self.mixes.append(Mix(i, t0, ov, sir, lang, bed, procs, talkers, length * 1000 // core.SR))

    def _segment(self):
        """Akashic's capture segmenter over each mixed slot (after the store's Opus round trip)."""
        from akashic.capture.audio import AudioSettings, Segmenter
        from akashic.capture.vad import WINDOW, SileroVad
        from akashic.storage.opus import decode_opus, encode_opus

        vad = SileroVad()
        segs, audio = [], {}
        sid = 0
        for m in self.mixes:
            x = self.mixed[m.i]
            vad.reset()
            seg = Segmenter(AudioSettings())
            pad = np.zeros(core.SR * 2, np.float32)  # trailing silence closes the last utterance
            full = np.concatenate([x, pad])
            utts = []
            for k in range(0, len(full) - WINDOW + 1, WINDOW):
                w = full[k:k + WINDOW]
                p = vad.probability(w) if w.any() else 0.0
                utts += seg.feed(m.t0 + k * 1000 // core.SR, w, p)
            utts += seg.finish()
            last = None
            for u in utts:
                sid += 1
                srcs = tuple(sorted(e for e, s in self.streams[m.i].items() if np.any(s[max(0, (u.start_ms - m.t0) * core.SR // 1000):(u.end_ms - m.t0) * core.SR // 1000] != 0)))
                segs.append(core.Segment(sid, "system", u.start_ms, u.end_ms, u.speech_ms, srcs, last if u.continues else None))
                audio[sid] = decode_opus(encode_opus(u.samples))
                last = sid
        return segs, audio

    # the Run interface ----------------------------------------------------------------------

    def audio(self, seg: core.Segment) -> np.ndarray:
        return self._seg_audio[seg.id]

    def _slot(self, t: int) -> int:
        return (t - T0) // SLOT_MS

    def processes(self, t0: int, t1: int) -> list[str]:
        i = self._slot(t0)
        if i < 0 or i >= len(self.mixes):
            return []
        m = self.mixes[i]
        a, b = max(0, (t0 - m.t0) * core.SR // 1000), max(0, (t1 - m.t0) * core.SR // 1000)
        return sorted(e for e, s in self.streams[i].items() if np.any(s[a:b] != 0))

    def process_stream(self, exe: str, t0: int, t1: int) -> np.ndarray:
        out = np.zeros((t1 - t0) * core.SR // 1000, np.float32)
        for i in range(max(0, self._slot(t0)), min(len(self.mixes), self._slot(t1 - 1) + 1)):
            m = self.mixes[i]
            s = self.streams[i].get(exe)
            if s is None:
                continue
            off = (m.t0 - t0) * core.SR // 1000  # where the slot starts in out
            lo, hi = max(0, -off), min(len(s), len(out) - off)
            if hi > lo:
                out[off + lo:off + hi] += s[lo:hi]
        return out

    def mic_stream(self, t0: int, t1: int) -> np.ndarray:
        return np.zeros((t1 - t0) * core.SR // 1000, np.float32)


# --- scoring -------------------------------------------------------------------------------------


def cp_assign(refs: list[list[str]], hyps: list[list[str]]):
    """cpWER: one-to-one assignment of hypothesis speakers to reference talkers minimizing errors
    (unmatched refs are all deletions, unmatched hyps all insertions)."""
    from scipy.optimize import linear_sum_assignment

    n, m = len(refs), len(hyps)
    k = n + m
    cost = np.zeros((k, k))
    for i in range(k):
        for j in range(k):
            if i < n and j < m:
                cost[i, j] = score.lev(refs[i], hyps[j])
            elif i < n:
                cost[i, j] = len(refs[i])
            elif j < m:
                cost[i, j] = len(hyps[j])
    rows, cols = linear_sum_assignment(cost)
    per_ref = {i: (cost[i, j], j if j < m else None) for i, j in zip(rows, cols, strict=True) if i < n}
    return float(cost[rows, cols].sum()), per_ref


def der(m: Mix, transcripts) -> tuple[int, int]:
    """Multi-label DER on 10 ms frames with a 250 ms collar around reference turn edges: (errors, ref)."""
    lo = m.t0 // 10
    n = m.length_ms // 10 + 1
    names = sorted({s for t in m.talkers for s, _, _ in t["turns"]})
    ref = np.zeros((len(names), n), bool)
    collar = np.zeros(n, bool)
    for t in m.talkers:
        for s, a, b in t["turns"]:
            ref[names.index(s), max(0, a // 10 - lo):max(0, b // 10 - lo)] = True
            for e in (a, b):
                collar[max(0, (e - score.COLLAR_MS) // 10 - lo):max(0, (e + score.COLLAR_MS) // 10 - lo)] = True
    ids = sorted({spk for tr in transcripts for spk, _, _ in tr.turns if spk is not None})
    hyp = np.zeros((len(ids), n), bool)
    for tr in transcripts:
        for spk, a, b in tr.turns:
            if spk is not None:
                hyp[ids.index(spk), max(0, (tr.start_ms + a) // 10 - lo):max(0, min(n, (tr.start_ms + b) // 10 - lo))] = True
    keep = ~collar
    conf = np.array([[np.sum(ref[i] & hyp[j] & keep) for j in range(len(ids))] for i in range(len(names))]).reshape(len(names), len(ids))
    mapping = score.best_assignment(conf) if conf.size else {}
    nr, nh = ref[:, keep].sum(0), hyp[:, keep].sum(0)
    correct = np.zeros(keep.sum(), int)
    for j, i in mapping.items():
        correct += (ref[i, keep] & hyp[j, keep]).astype(int)
    err = np.maximum(nr, nh) - correct
    return int(err.sum()), int(nr.sum())


def score_set(s: Set, transcripts) -> dict:
    """Per mixture: scorer-style WER (refs joined in start order vs all text in time order), cpWER,
    per-talker WER (assigned stream, and anywhere in the text), DER."""
    from errors import semi_global

    rows = []
    for m in s.mixes:
        mine = sorted((t for t in transcripts if t.stream == "system" and m.t0 <= (t.start_ms + t.end_ms) / 2 < m.t0 + SLOT_MS), key=lambda t: t.start_ms)
        ref_w = [score.fold(t["text"]).split() for t in m.talkers]
        nt = len(m.talkers)
        order = sorted(range(nt), key=lambda k: m.talkers[k]["spans"][0][0])
        joined_ref = [w for k in order for w in ref_w[k]]
        hyp_all = score.fold(" ".join(t.text for t in mine)).split()
        by_spk: dict = {}
        for t in mine:
            by_spk.setdefault(t.speaker_id, []).append(t)
        hyps = [score.fold(" ".join(t.text for t in v)).split() for v in by_spk.values()]
        cp, per_ref = cp_assign(ref_w, hyps)
        de, dr = der(m, mine)
        quiet = None if m.sir == 0 or nt == 1 else (1 if m.sir > 0 else 0)  # B is quieter when SIR > 0
        rows.append({"i": m.i, "overlap": m.overlap, "sir": m.sir, "lang": m.lang, "bed": m.bed, "procs": m.procs,
                     "ref": len(joined_ref), "wer_err": score.lev(joined_ref, hyp_all), "cp_err": cp,
                     "talker": [{"ref": len(ref_w[k]), "cp_err": per_ref[k][0], "any_err": semi_global(ref_w[k], hyp_all),
                                 "role": "solo" if nt == 1 else ("equal" if quiet is None else ("quiet" if k == quiet else "loud"))} for k in range(nt)],
                     "der_err": de, "der_ref": dr, "n_out": len(mine)})
    return {"rows": rows, "summary": summarize(rows)}


def summarize(rows: list[dict]) -> dict:
    def agg(sel, key="wer_err"):
        r = sum(x["ref"] for x in sel)
        return sum(x[key] for x in sel) / r if r else None

    def tal(sel, role, key):
        ts = [t for x in sel for t in x["talker"] if t["role"] == role]
        r = sum(t["ref"] for t in ts)
        return sum(t[key] for t in ts) / r if r else None

    solo = [x for x in rows if x["overlap"] == SOLO]
    rows = [x for x in rows if x["overlap"] != SOLO]
    over = [x for x in rows if x["overlap"] >= 0.5]
    out = {
        "wer": agg(rows), "cpwer": agg(rows, "cp_err"),
        "der": sum(x["der_err"] for x in rows) / max(1, sum(x["der_ref"] for x in rows)),
        "wer_solo": agg(solo), "wer_solo_by_lang": {lang: agg([x for x in solo if x["lang"] == lang]) for lang in LANGS},
        "wer_solo_by_bed": {b: agg([x for x in solo if x["bed"] == b]) for b in BEDS},
        "der_solo": sum(x["der_err"] for x in solo) / max(1, sum(x["der_ref"] for x in solo)),
        "wer_by_overlap": {str(o): agg([x for x in rows if x["overlap"] == o]) for o in OVERLAPS},
        "cpwer_by_overlap": {str(o): agg([x for x in rows if x["overlap"] == o], "cp_err") for o in OVERLAPS},
        "cpwer_by_sir": {str(v): agg([x for x in rows if x["sir"] == v], "cp_err") for v in SIRS},
        "cpwer_by_procs": {p: agg([x for x in rows if x["procs"] == p], "cp_err") for p in ("two", "one")},
        "cpwer_by_bed": {b: agg([x for x in rows if x["bed"] == b], "cp_err") for b in BEDS},
        "wer_ov0_by_lang": {lang: agg([x for x in rows if x["overlap"] == 0 and x["lang"] == lang]) for lang in LANGS},
        "wer_ov0": agg([x for x in rows if x["overlap"] == 0]),
        "talker_any_wer_quiet_ov50": tal(over, "quiet", "any_err"), "talker_any_wer_loud_ov50": tal(over, "loud", "any_err"),
        "talker_any_wer_equal_ov50": tal(over, "equal", "any_err"),
        "talker_cp_wer_quiet_ov50": tal(over, "quiet", "cp_err"), "talker_cp_wer_loud_ov50": tal(over, "loud", "cp_err"),
        "talker_any_wer_quiet_by_overlap": {str(o): tal([x for x in rows if x["overlap"] == o], "quiet", "any_err") for o in OVERLAPS},
        "talker_any_wer_loud_by_overlap": {str(o): tal([x for x in rows if x["overlap"] == o], "loud", "any_err") for o in OVERLAPS},
        "audio_s": None,
    }
    return out
