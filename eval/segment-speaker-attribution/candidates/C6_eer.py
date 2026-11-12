"""C6 step 1: per-duration EER of three WeSpeaker embedders on the fixture's Opus speech.

    ..\\.venv\\Scripts\\python.exe candidates\\C6_eer.py      (from the harness folder)

- Enrolment centroid per enrolled speaker: every >= 2 s pyannote turn of its enrolment clips
  (the app's turns(), as reference.enroll), embedded by the model under test, summed, unit.
- Test pieces: solo and impostor segments (one speaker, no lead), the app's turns(); from each
  turn a random contiguous crop of its joined speech per duration bucket that fits. Enrolment
  utterances never appear in test segments (make_fixture), impostors never enrol.
- Trials: every piece against every enrolled centroid; target = same speaker.
- Also: with best_match at a threshold (0.5, and each model's own EER threshold measured on
  [1, 2) s), the share of enrolled-speaker pieces matched to their own profile and of all pieces
  matched to a wrong profile (the app's decision, per piece).
- Timing: per-embedding ms (fbank + forward) at fixed lengths, CPU provider, the app's session
  options (log level 3, default threads).

Writes out/C6_eer/eer.json and eer.txt.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

import onnxruntime as ort  # noqa: E402

from common import DATA, FIXTURE, OUT, SR, SharedModels, pipe, unit  # noqa: E402
from harness import decode  # noqa: E402

MODELS = {
    "resnet34_LM": DATA / "models/wespeaker-voxceleb-resnet34-lm/voxceleb_resnet34_LM.onnx",
    "resnet34": DATA / "models_extra/wespeaker-voxceleb-resnet34/voxceleb_resnet34.onnx",
    "campplus": DATA / "models_extra/wespeaker-voxceleb-campplus/voxceleb_CAM++.onnx",
}
BUCKETS = [(0.3, 1.0), (1.0, 2.0), (2.0, 4.0), (4.0, 8.0), (8.0, 10.0)]
BUCKET_NAMES = ["0.3-1", "1-2", "2-4", "4-8", ">=8"]


def session(path: Path):
    o = ort.SessionOptions()
    o.log_severity_level = 3
    return ort.InferenceSession(str(path), o, providers=["CPUExecutionProvider"])


def embed(sess, samples: np.ndarray) -> np.ndarray:
    f = pipe.fbank(samples)
    return unit(np.asarray(sess.run(None, {"feats": f[None]})[0][0], np.float32))


def eer(tar: np.ndarray, non: np.ndarray) -> tuple[float, float]:
    s = np.concatenate([tar, non])
    y = np.concatenate([np.ones(len(tar)), np.zeros(len(non))])
    o = np.argsort(-s, kind="stable")
    s, y = s[o], y[o]
    tp = np.cumsum(y)
    fp = np.cumsum(1 - y)
    frr = 1 - tp / len(tar)
    far = fp / len(non)
    i = int(np.argmin(np.abs(frr - far)))
    return float((frr[i] + far[i]) / 2), float(s[i])


def main() -> None:
    index = json.loads((FIXTURE / "index.json").read_text(encoding="utf-8"))
    blob = (FIXTURE / "segments.bin").read_bytes()
    roles, meta = index["roles"], index["meta"]
    shared = SharedModels()
    sess = {k: session(p) for k, p in MODELS.items()}
    rng = np.random.default_rng(6)
    enrolled = sorted(s for s, r in roles.items() if r == "enrolled")

    # enrolment centroids
    cent = {k: [] for k in MODELS}
    for spk in enrolled:
        turns = [
            t for c in index["enrol"][spk] for t in shared.turns(decode(blob, c))
            if t.speech_ms >= 2000
        ]  # fmt: skip
        for k, s in sess.items():
            cent[k].append(unit(np.sum([embed(s, t.samples) for t in turns], axis=0)))
    C = {k: np.stack(v) for k, v in cent.items()}

    # test pieces
    pieces: list[tuple[str, int, np.ndarray]] = []  # speaker, bucket, samples
    segs = [
        e for e in index["segments"]
        if e["kind"] in ("solo", "impostor") and e["single"] is not None and e["lead_ms"] == 0
        and len(e["spans"]) == 1
    ]  # fmt: skip
    for e in segs:
        x = decode(blob, e)
        for t in shared.turns(x):
            n = len(t.samples)
            for b, (lo, hi) in enumerate(BUCKETS):
                d = int(rng.uniform(lo, hi) * SR)
                if n < d:
                    continue
                a = int(rng.integers(0, n - d + 1))
                pieces.append((e["single"], b, t.samples[a : a + d]))
    print(f"{len(segs)} test segments, {len(pieces)} pieces", flush=True)

    spk_idx = {s: i for i, s in enumerate(enrolled)}
    res: dict = {"n_pieces": {}, "models": {}}
    for b, name in enumerate(BUCKET_NAMES):
        res["n_pieces"][name] = sum(1 for p in pieces if p[1] == b)
    emb_ms: dict[str, list[float]] = {k: [] for k in MODELS}
    E: dict[str, np.ndarray] = {}
    for k, s in sess.items():
        out = []
        for _, _, smp in pieces:
            t = time.perf_counter()
            out.append(embed(s, smp))
            emb_ms[k].append((time.perf_counter() - t) * 1000)
        E[k] = np.stack(out)
        print(f"embedded {k}", flush=True)
    who = np.array([spk_idx.get(p[0], -1) for p in pieces])
    bucket = np.array([p[1] for p in pieces])
    lang = np.array([meta[p[0]].get("lang", p[0][:2]) for p in pieces])
    gender = np.array([meta[p[0]].get("gender") for p in pieces])
    c_lang = np.array([meta[s].get("lang", s[:2]) for s in enrolled])
    c_gender = np.array([meta[s].get("gender") for s in enrolled])

    for k in MODELS:
        S = E[k] @ C[k].T  # pieces x enrolled
        m: dict = {"eer": {}, "eer_hard": {}, "thr_eer": {}, "tar_mean": {}, "non_mean": {}}
        for b, name in enumerate(BUCKET_NAMES):
            rows = np.flatnonzero(bucket == b)
            tgt = np.zeros_like(S[rows], bool)
            ok = who[rows] >= 0
            tgt[np.flatnonzero(ok), who[rows][ok]] = True
            hard = (lang[rows][:, None] == c_lang[None]) & (gender[rows][:, None] == c_gender[None])
            sc = S[rows]
            m["eer"][name], m["thr_eer"][name] = eer(sc[tgt], sc[~tgt])
            m["eer_hard"][name] = eer(sc[tgt], sc[~tgt & hard])[0]
            m["tar_mean"][name] = float(sc[tgt].mean())
            m["non_mean"][name] = float(sc[~tgt].mean())
        # the app's decision per piece (best_match) at 0.5 and at the [1,2) EER threshold
        for label, thr in (("0.5", 0.5), ("eer12", m["thr_eer"]["1-2"])):
            best = S.argmax(1)
            top = S.max(1)
            acc = top >= thr
            d = {}
            for b, name in enumerate(BUCKET_NAMES):
                rows = bucket == b
                en = rows & (who >= 0)
                d[name] = {
                    "own": float(np.mean(acc[en] & (best[en] == who[en]))),
                    "wrong": float(np.mean(acc[rows] & (best[rows] != who[rows]))),
                    "wrong_impostor": float(np.mean(acc[rows & (who < 0)])),
                }
            m[f"decide_{label}"] = {"thr": float(thr), "by_bucket": d}
        m["piece_ms_mean"] = float(np.mean(emb_ms[k]))
        res["models"][k] = m

    # timing at fixed lengths, median of 15 after 3 warm-ups
    noise = np.random.default_rng(1).standard_normal(10 * SR).astype(np.float32) * 0.05
    for k, s in sess.items():
        tm = {}
        for sec in (0.5, 1, 2, 4, 8, 10):
            x = noise[: int(sec * SR)]
            for _ in range(3):
                embed(s, x)
            ts = []
            for _ in range(15):
                t = time.perf_counter()
                embed(s, x)
                ts.append((time.perf_counter() - t) * 1000)
            tm[str(sec)] = float(np.median(ts))
        res["models"][k]["ms_fixed"] = tm

    od = OUT / "C6_eer"
    od.mkdir(parents=True, exist_ok=True)
    (od / "eer.json").write_text(json.dumps(res, indent=1), encoding="utf-8")
    lines = [f"pieces per bucket: {res['n_pieces']}"]
    hdr = "model        " + " ".join(f"{n:>7}" for n in BUCKET_NAMES)
    for key, title in (("eer", "EER (all non-target)"), ("eer_hard", "EER (same lang+gender)")):
        lines += ["", title, hdr]
        for k, m in res["models"].items():
            lines.append(f"{k:12} " + " ".join(f"{m[key][n]:7.2%}" for n in BUCKET_NAMES))
    lines += ["", "EER threshold (cosine)", hdr]
    for k, m in res["models"].items():
        lines.append(f"{k:12} " + " ".join(f"{m['thr_eer'][n]:7.3f}" for n in BUCKET_NAMES))
    for label in ("0.5", "eer12"):
        lines += ["", f"best_match at {label}: own / wrong (per piece)", hdr]
        for k, m in res["models"].items():
            dd = m[f"decide_{label}"]
            lines.append(
                f"{k:12} " + " ".join(
                    f"{dd['by_bucket'][n]['own']:.2f}/{dd['by_bucket'][n]['wrong']:.3f}"
                    for n in BUCKET_NAMES
                ) + f"  thr={dd['thr']:.3f}"
            )  # fmt: skip
    lines += ["", "ms per embedding (fbank+forward), median", "model        " + " ".join(
        f"{s:>6}s" for s in ("0.5", "1", "2", "4", "8", "10"))]  # fmt: skip
    for k, m in res["models"].items():
        lines.append(f"{k:12} " + " ".join(f"{v:7.1f}" for v in m["ms_fixed"].values())
                     + f"  pieces mean {m['piece_ms_mean']:.1f}")  # fmt: skip
    txt = "\n".join(lines)
    (od / "eer.txt").write_text(txt + "\n", encoding="utf-8")
    print(txt)


if __name__ == "__main__":
    main()
