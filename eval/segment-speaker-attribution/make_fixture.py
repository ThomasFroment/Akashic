"""Deterministic labelled fixture for segment speaker attribution (seed SEED).

Input: data/sources (prepare_sources.py). Output: data/fixture/
  segments.bin   every segment as the app stores it: 24 kbps Ogg/Opus (storage/opus.encode_opus),
                 after a Discord-like pass per speaker track (libopus 48 kHz, application voip,
                 24/32/64 kbps) and optional pink/brown noise at 5-20 dB SNR;
  index.json     per segment: kind, subsets, length, lead, continues, per-speaker speech after the
                 lead, label set (>= 300 ms), expected author, speech spans; enrolment clips;
                 speaker roles; similar pairs; drift stream.
  centroids.npz  clean enrolment centroid per speaker (mean of unit WeSpeaker embeddings of its
                 enrolment clips, app embed()) used for pair selection and the drift metric.

Utterances are split per speaker: enrolment clips and test material never share an utterance;
impostors (unenrolled) never enrol. Segments mimic the app's segmenter (capture/audio.py): 300 ms
preroll, no internal pause >= 1 s (longer corpus pauses are shortened to 0.38 s), 200 ms tail,
cut at 30.016 s with the next segment re-starting 1.984 s before the cut.

    .venv/Scripts/python.exe make_fixture.py
"""

from __future__ import annotations

import io
import json
from collections import defaultdict

import av
import numpy as np

from common import FIXTURE, LEAD_MS, MAX_SEGMENT_MS, SR, SharedModels, app_opus, unit

SEED = 20261110
FRAME = 512  # 32 ms activity frames
LABEL_MIN_MS = 300
PREROLL = int(0.3 * SR)
TAIL = int(0.2 * SR)
BITRATES = (24, 32, 64)
N_EN, N_FR = 30, 26  # enrolled speakers per language (rest of the pool: impostors, up to 10)
N_IMP = 10
SIMILAR_PAIRS = 8
DRIFT_N = 500


# --- sources -------------------------------------------------------------------------------


def activity(x: np.ndarray) -> np.ndarray:
    n = len(x) // FRAME
    rms = np.sqrt(np.mean(x[: n * FRAME].reshape(n, FRAME) ** 2, axis=1) + 1e-12)
    ref = np.percentile(rms, 95) if n else 0.0
    act = rms > max(0.003, ref * 10 ** (-30 / 20))
    # one-frame dilation: word edges
    act = act | np.r_[act[1:], False] | np.r_[False, act[:-1]]
    return act


def clean_utt(x: np.ndarray) -> tuple[np.ndarray, np.ndarray] | None:
    """Trim silence at the ends and shorten internal pauses > 0.7 s to 0.38 s."""
    act = activity(x)
    idx = np.flatnonzero(act)
    if len(idx) < 10:
        return None
    act = act[idx[0] : idx[-1] + 1]
    x = x[idx[0] * FRAME : (idx[-1] + 1) * FRAME]
    keep = np.ones(len(act), dtype=bool)
    run = 0
    for i, a in enumerate(act):
        run = 0 if a else run + 1
        if run > 12:
            keep[i] = False  # drop frames beyond 12 silent ones (0.384 s)
    frames = x.reshape(-1, FRAME)[keep]
    return frames.reshape(-1).astype(np.float32), act[keep]


class Bank:
    def __init__(self, spk: str, rng: np.random.Generator) -> None:
        from common import DATA

        z = np.load(DATA / "sources" / f"{spk}.npz")
        audio = z["audio"].astype(np.float32) / 32767
        off = z["offsets"]
        utts = [clean_utt(audio[a:b]) for a, b in zip(off[:-1], off[1:], strict=True)]
        utts = [u for u in utts if u is not None]
        order = rng.permutation(len(utts))
        self.utts = [utts[i] for i in order]
        self.spk = spk
        self.enrol: list[tuple[np.ndarray, np.ndarray]] = []
        self.test = self.utts

    def split_enrol(self, rng: np.random.Generator) -> list[np.ndarray]:
        """3-5 enrolment clips of 4-12 s, each from its own utterances (removed from test)."""
        n = int(rng.integers(3, 6))
        clips, used = [], 0
        pool = self.utts
        i = 0
        while len(clips) < n and i < len(pool):
            want = rng.uniform(4.0, 12.0)
            parts, length = [], 0.0
            while length < want and i < len(pool):
                parts.append(pool[i][0])
                length += len(pool[i][0]) / SR
                i += 1
            if length >= 4.0:
                clip = np.concatenate(parts)[: int(want * SR)]
                clips.append(clip)
        used = i
        self.test = pool[used:]
        return clips

    def stream(self, rng: np.random.Generator, seconds: float) -> tuple[np.ndarray, np.ndarray]:
        """Utterances of the test pool joined by 0.15-0.7 s pauses: audio and frame activity."""
        need = int(seconds * SR) + FRAME
        i = int(rng.integers(len(self.test)))
        parts, acts, total = [], [], 0
        while total < need:
            x, a = self.test[i % len(self.test)]
            gap = int(rng.uniform(0.15, 0.7) * SR) // FRAME
            parts += [x, np.zeros(gap * FRAME, np.float32)]
            acts += [a, np.zeros(gap, bool)]
            total += len(x) + gap * FRAME
            i += 1
        return np.concatenate(parts), np.concatenate(acts)

    def piece(self, rng: np.random.Generator, lo: float, hi: float) -> tuple[np.ndarray, np.ndarray]:
        """A short span (lo-hi s) cut where the speaker talks, with >= 300 ms of activity."""
        for _ in range(200):
            x, a = self.test[int(rng.integers(len(self.test)))]
            n = int(rng.uniform(lo, hi) * SR) // FRAME
            if len(a) <= n:
                continue
            s = int(rng.integers(len(a) - n))
            if a[s] and a[s : s + n].sum() * FRAME / SR * 1000 >= LABEL_MIN_MS:
                return x[s * FRAME : (s + n) * FRAME], a[s : s + n]
        raise RuntimeError("no piece")


# --- codec ---------------------------------------------------------------------------------


def discord(x: np.ndarray, kbps: int) -> np.ndarray:
    """One speaker's voice through Discord-like libopus: 48 kHz, application=voip."""
    buf = io.BytesIO()
    with av.open(buf, "w", format="ogg") as c:
        s = c.add_stream("libopus", rate=48_000, options={"application": "voip"})
        s.bit_rate = kbps * 1000
        s.layout = "mono"
        f = av.AudioFrame.from_ndarray(np.clip(x, -1, 1)[None, :].astype(np.float32), format="flt", layout="mono")
        f.sample_rate = SR
        c.mux(s.encode(f))
        c.mux(s.encode(None))
    y = app_opus.decode_opus(buf.getvalue())
    out = np.zeros(len(x), np.float32)
    out[: min(len(x), len(y))] = y[: len(x)]
    return out


def colored_noise(rng: np.random.Generator, n: int) -> np.ndarray:
    spec = np.fft.rfft(rng.standard_normal(n))
    f = np.maximum(np.fft.rfftfreq(n, 1 / SR), 20.0)
    power = 1.0 if rng.random() < 0.5 else 2.0  # pink or brown
    y = np.fft.irfft(spec / f ** (power / 2), n)
    return (y / (np.std(y) + 1e-12)).astype(np.float32)


class Track:
    """One speaker's signal in a segment and its frame activity (32 ms)."""

    def __init__(self, length: int) -> None:
        self.x = np.zeros(length, np.float32)
        self.act = np.zeros(length // FRAME + 1, bool)

    def put(self, at: int, x: np.ndarray, act: np.ndarray) -> None:
        at = (at // FRAME) * FRAME
        end = min(len(self.x), at + len(x))
        if end <= at:
            return
        self.x[at:end] += x[: end - at]
        f0 = at // FRAME
        na = (end - at) // FRAME
        self.act[f0 : f0 + na] |= act[:na]


def render(tracks: dict[str, Track], rng: np.random.Generator) -> tuple[bytes, dict]:
    length = len(next(iter(tracks.values())).x)
    mix = np.zeros(length, np.float32)
    info = {}
    for spk, t in tracks.items():
        if not t.act.any():
            continue
        voiced = np.repeat(t.act, FRAME)[:length]
        rms = float(np.sqrt(np.mean(t.x[voiced[: len(t.x)]] ** 2) + 1e-12))
        x = t.x * (rng.uniform(0.05, 0.12) / rms)
        snr = None
        if rng.random() < 0.5:
            snr = float(rng.uniform(5, 20))
            noise = colored_noise(rng, length)
            sp = float(np.sqrt(np.mean(x[voiced[: len(x)]] ** 2)))
            x = x + noise * sp / 10 ** (snr / 20)
        kbps = int(rng.choice(BITRATES))
        mix += discord(x, kbps)
        info[spk] = {"kbps": kbps, "snr_db": None if snr is None else round(snr, 1)}
    peak = float(np.max(np.abs(mix)))
    if peak > 0.95:
        mix *= 0.95 / peak
    return app_opus.encode_opus(mix), info


def label(tracks: dict[str, Track], lead_ms: int, length: int) -> dict:
    lead_f = int(np.ceil(lead_ms * SR / 1000 / FRAME))
    nf = length // FRAME
    speech = {}
    spans = {}
    for spk, t in tracks.items():
        a = t.act[:nf]
        ms = int(a[lead_f:].sum() * FRAME * 1000 / SR)
        if a.any():
            speech[spk] = ms
            idx = np.flatnonzero(a)
            br = np.flatnonzero(np.diff(idx) > 1)
            firsts = np.r_[idx[0], idx[br + 1]]
            lasts = np.r_[idx[br], idx[-1]]
            spans[spk] = [[int(f * FRAME * 1000 / SR), int((l + 1) * FRAME * 1000 / SR)] for f, l in zip(firsts, lasts, strict=True)]
    labels = sorted(s for s, ms in speech.items() if ms >= LABEL_MIN_MS)
    return {"speech_ms_after_lead": speech, "labels": labels, "spans": spans}


# --- build ---------------------------------------------------------------------------------


def main() -> None:
    from common import DATA

    rng = np.random.default_rng(SEED)
    meta = json.loads((DATA / "sources" / "speakers.json").read_text(encoding="utf-8"))
    roles: dict[str, str] = {}
    for lang, n_en in (("en", N_EN), ("fr", N_FR)):
        pool = sorted(s for s, m in meta.items() if m["lang"] == lang and m["seconds"] >= 240)
        by_g = {g: [s for s in pool if meta[s]["gender"] == g] for g in ("F", "M")}
        for g in by_g:
            by_g[g] = [by_g[g][i] for i in rng.permutation(len(by_g[g]))]
        n_imp = min(N_IMP, len(pool) - n_en)
        enrolled = by_g["F"][: n_en // 2] + by_g["M"][: n_en - n_en // 2]
        rest = [s for s in by_g["F"][n_en // 2 :] + by_g["M"][n_en - n_en // 2 :]]
        for s in enrolled:
            roles[s] = "enrolled"
        for s in [rest[i] for i in rng.permutation(len(rest))][:n_imp]:
            roles[s] = "impostor"
    speakers = sorted(roles)
    banks = {s: Bank(s, np.random.default_rng([SEED, int(s[2:]), ord(s[0])])) for s in speakers}

    FIXTURE.mkdir(parents=True, exist_ok=True)
    blob = open(FIXTURE / "segments.bin", "wb")  # noqa: SIM115
    entries: list[dict] = []

    def emit(kind: str, subsets: list[str], tracks: dict[str, Track], lead_ms: int = 0, prev: int | None = None, extra: dict | None = None) -> int:
        length = len(next(iter(tracks.values())).x)
        data, codec = render(tracks, rng)
        off = blob.tell()
        blob.write(data)
        lab = label(tracks, lead_ms, length)
        single = lab["labels"][0] if len(lab["labels"]) == 1 else None
        expected = single if single is not None and roles[single] == "enrolled" else None
        e = {
            "id": len(entries) + 1, "kind": kind, "subsets": subsets, "length_ms": length * 1000 // SR,
            "lead_ms": lead_ms, "continues": prev, "offset": off, "size": len(data),
            "lang": meta[next(iter(tracks))]["lang"], "codec": codec,
            "single": single, "expected": expected, **lab, **(extra or {}),
        }
        entries.append(e)
        return e["id"]

    def solo(spk: str, seconds: float, subsets: list[str], kind: str = "solo") -> int:
        n = int(seconds * SR)
        x, a = banks[spk].stream(rng, seconds)
        t = Track(n)
        body = n - PREROLL - TAIL
        t.put(PREROLL, x[:body], a[: body // FRAME])
        return emit(kind, subsets, {spk: t})

    # enrolment
    models = SharedModels()
    enrol: dict[str, list[dict]] = {}
    centroids: dict[str, np.ndarray] = {}
    for s in speakers:
        clips = banks[s].split_enrol(rng)
        enrol[s] = []
        embs = []
        for clip in clips:
            n = len(clip) + PREROLL + TAIL
            t = Track(n)
            t.put(PREROLL, clip, activity(clip))
            data, codec = render({s: t}, rng)
            off = blob.tell()
            blob.write(data)
            enrol[s].append({"offset": off, "size": len(data), "length_ms": n * 1000 // SR, "codec": codec})
            decoded = app_opus.decode_opus(data)
            embs.append(unit(models.embed(decoded)))
        centroids[s] = unit(np.mean(embs, axis=0))
        print("enrol", s, roles[s], len(clips), flush=True)

    enrolled = [s for s in speakers if roles[s] == "enrolled"]
    impostors = [s for s in speakers if roles[s] == "impostor"]

    # similar same-gender pairs among enrolled speakers (greedy, disjoint)
    cand = []
    for i, a in enumerate(enrolled):
        for b in enrolled[i + 1 :]:
            if meta[a]["gender"] == meta[b]["gender"]:
                cand.append((float(centroids[a] @ centroids[b]), a, b))
    cand.sort(reverse=True)
    pairs, taken = [], set()
    for c, a, b in cand:
        if a not in taken and b not in taken:
            pairs.append((a, b, round(c, 4)))
            taken |= {a, b}
        if len(pairs) == SIMILAR_PAIRS:
            break
    imp_sim = {s: max(float(centroids[s] @ centroids[e]) for e in enrolled) for s in impostors}

    # solo segments
    for s in enrolled:
        for _ in range(4):
            solo(s, rng.uniform(5.0, MAX_SEGMENT_MS / 1000), ["solo"])
        for _ in range(3):
            k = int(rng.integers(1, 3))
            solo(s, 10 * k + rng.uniform(0.5, 2.0), ["solo", "solo_10k"])
        solo(s, rng.uniform(10.0, 10.49), ["solo", "solo_10_0"])
    for s in impostors:
        for _ in range(4):
            solo(s, rng.uniform(5.0, MAX_SEGMENT_MS / 1000), ["impostor"], kind="impostor")
    for a, b, _c in pairs:
        for s in (a, b):
            for _ in range(4):
                solo(s, rng.uniform(5.0, 20.0), ["solo", "similar"])

    # two-speaker segments
    def partner(a: str, allow_imp: bool) -> str:
        lang = meta[a]["lang"]
        pool = [s for s in (enrolled + (impostors if allow_imp and rng.random() < 0.3 else [])) if s != a and meta[s]["lang"] == lang]
        return pool[int(rng.integers(len(pool)))]

    def alternating(a: str, b: str, seconds: float, subsets: list[str]) -> None:
        n = int(seconds * SR)
        tr = {a: Track(n), b: Track(n)}
        pos, who = PREROLL, 0
        while pos < n - TAIL - SR // 2:
            spk = (a, b)[who]
            x, act = banks[spk].piece(rng, 2.0, 8.0) if rng.random() < 0.5 else banks[spk].stream(rng, rng.uniform(2.0, 8.0))
            x = x[: int(rng.uniform(2.0, 8.0) * SR)]
            room = n - TAIL - pos
            tr[spk].put(pos, x[:room], act[: room // FRAME])
            pos += len(x[:room]) + int(rng.uniform(0.2, 0.8) * SR)
            who ^= 1
        emit("mixed", subsets, tr)

    def backchannel(a: str, b: str, seconds: float, subsets: list[str], last_window: bool, reply: bool = False) -> None:
        n = int(seconds * SR)
        tr = {a: Track(n), b: Track(n)}
        body = n - PREROLL - TAIL
        x, act = banks[a].stream(rng, seconds)
        if reply:
            lo, hi = 1.0, 2.5
        else:
            lo, hi = 0.3, 1.0
        y, bact = banks[b].piece(rng, lo, hi)
        if reply:
            tr[a].put(PREROLL, x[: body - len(y) - SR // 4], act[: (body - len(y) - SR // 4) // FRAME])
            tr[b].put(n - TAIL - len(y), y, bact)
        else:
            tr[a].put(PREROLL, x[:body], act[: body // FRAME])
            last = ((n - 1) // (10 * SR)) * 10 * SR
            lo_at = max(PREROLL, last) if last_window else PREROLL
            hi_at = max(lo_at + 1, n - TAIL - len(y))
            tr[b].put(int(rng.integers(lo_at, hi_at)), y, bact)
        emit("mixed", subsets, tr)

    for _ in range(60):
        a = enrolled[int(rng.integers(len(enrolled)))]
        alternating(a, partner(a, True), rng.uniform(5.0, 30.0), ["mixed", "mixed_alt"])
    for i in range(60):
        a = enrolled[int(rng.integers(len(enrolled)))]
        backchannel(a, partner(a, True), rng.uniform(6.0, 30.0), ["mixed", "mixed_backchannel"] + (["mixed_lastwin"] if i % 2 else []), last_window=bool(i % 2))
    for _ in range(40):
        a = enrolled[int(rng.integers(len(enrolled)))]
        backchannel(a, partner(a, True), rng.uniform(8.0, 30.0), ["mixed", "mixed_reply", "mixed_lastwin"], last_window=True, reply=True)
    for a, b, _c in pairs:
        alternating(a, b, rng.uniform(8.0, 30.0), ["mixed", "similar"])
        backchannel(a, b, rng.uniform(8.0, 30.0), ["mixed", "similar"], last_window=True)

    # continuation pairs
    cut = MAX_SEGMENT_MS * SR // 1000
    lead = LEAD_MS * SR // 1000
    for i in range(120):
        mode = ("same", "switch", "switch_early")[0 if i < 40 else 1 if i < 90 else 2]
        a = enrolled[int(rng.integers(len(enrolled)))]
        b = partner(a, True) if mode != "same" else a
        l2 = int(rng.uniform(5.0, 20.0) * SR)
        total = cut - lead + l2
        tr = {a: Track(total)} if a == b else {a: Track(total), b: Track(total)}
        if mode == "same":
            x, act = banks[a].stream(rng, total / SR)
            tr[a].put(PREROLL, x[: total - PREROLL - TAIL], act[: (total - PREROLL - TAIL) // FRAME])
        else:
            a_end = cut - int(rng.uniform(0.0, 0.3) * SR) if mode == "switch" else cut - int(rng.uniform(0.0, 0.3) * SR) - lead // 2
            b_at = cut + int(rng.uniform(0.1, 0.6) * SR) if mode == "switch" else cut - int(rng.uniform(0.3, 1.0) * SR)
            b_at = max(b_at, a_end + FRAME)
            x, act = banks[a].stream(rng, a_end / SR)
            tr[a].put(PREROLL, x[: a_end - PREROLL], act[: (a_end - PREROLL) // FRAME])
            y, bact = banks[b].stream(rng, (total - b_at) / SR)
            tr[b].put(b_at, y[: total - TAIL - b_at], bact[: (total - TAIL - b_at) // FRAME])
        t1 = {s: Track(cut) for s in tr}
        t2 = {s: Track(l2) for s in tr}
        for s, t in tr.items():
            t1[s].x[:] = t.x[:cut]
            t1[s].act[:] = t.act[: len(t1[s].act)]
            t2[s].x[:] = t.x[cut - lead : cut - lead + l2]
            f0 = (cut - lead) // FRAME
            seg_act = t.act[f0 : f0 + len(t2[s].act)]
            t2[s].act[: len(seg_act)] = seg_act
        # same codec draw for both halves would need one render; render each half (as the app
        # stores each segment separately) — the Discord pass differs slightly at the seam only.
        first = emit("cont_first", ["solo", "cont_first"] if mode != "switch_early" else ["cont_first"], t1, extra={"cont_mode": mode})
        lead_owner = a if mode != "switch_early" else "mixed"
        emit("cont_second", ["cont_second", f"cont_{mode}"], t2, lead_ms=LEAD_MS, prev=first, extra={"cont_mode": mode, "lead_owner": lead_owner})

    # drift stream: the most similar enrolled pair, 500 solo segments, interleaved at random
    da, db, dc = pairs[0]
    drift = []
    for _ in range(DRIFT_N):
        s = (da, db)[int(rng.integers(2))]
        drift.append(solo(s, rng.uniform(5.0, 20.0), ["drift"], kind="drift"))

    blob.close()
    np.savez(FIXTURE / "centroids.npz", **centroids)
    index = {
        "seed": SEED,
        "roles": roles,
        "meta": {s: meta[s] for s in speakers},
        "similar_pairs": pairs,
        "impostor_max_cos": imp_sim,
        "enrol": enrol,
        "drift": {"pair": [da, db], "cos": dc, "ids": drift},
        "segments": entries,
    }
    (FIXTURE / "index.json").write_text(json.dumps(index), encoding="utf-8")
    counts = defaultdict(int)
    for e in entries:
        for t in e["subsets"]:
            counts[t] += 1
    print(len(entries), "segments", dict(counts), "pairs", pairs[:3])


if __name__ == "__main__":
    main()
