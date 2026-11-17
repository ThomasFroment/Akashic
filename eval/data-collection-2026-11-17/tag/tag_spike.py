"""Speech vs song/singing/instrumental/SFX: Silero v6 (production gate) vs CED-mini tagger.
Public data: FLEURS dev (CC-BY-4.0), MUSDB18 7 s stems (research/non-commercial), OpenGameArt
CC0 SFX. CPU, below-normal priority. Numbers only. usage: tag_spike.py DATA_DIR"""

from __future__ import annotations

import ctypes
import csv
import io
import json
import sys
import tarfile
import time
import urllib.request
import zipfile
from pathlib import Path

import av
import numpy as np
import onnxruntime as ort

ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x4000)
DATA = Path(sys.argv[1])
RNG = np.random.default_rng(20261117)
SR = 16000


def decode(data: bytes, stream: int = 0) -> np.ndarray:
    with av.open(io.BytesIO(data)) as c:
        rs = av.AudioResampler(format="flt", layout="mono", rate=SR)
        st = c.streams.audio[stream]
        parts = [f.to_ndarray().reshape(-1) for fr in c.decode(st) for f in rs.resample(fr)]
    return np.concatenate(parts).astype(np.float32)


def fleurs(lang, n):
    url = f"https://huggingface.co/datasets/google/fleurs/resolve/main/data/{lang}/audio/dev.tar.gz"
    out = []
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "spike"}), timeout=120) as r, \
            tarfile.open(fileobj=r, mode="r|gz") as tar:
        for m in tar:
            if m.isfile() and m.name.endswith(".wav"):
                x = decode(tar.extractfile(m).read())
                if len(x) > 4 * SR:
                    out.append(x[: 10 * SR])
            if len(out) >= n:
                break
    return out


def musdb(n):
    z = zipfile.ZipFile(DATA / "musdb7.zip")
    names = [i for i in z.namelist() if i.endswith(".stem.mp4")]
    RNG.shuffle(names)
    out = []
    for name in names[:n]:
        raw = z.read(name)
        mix, drums, bass, other, vocals = (decode(raw, s) for s in range(5))
        k = min(map(len, (mix, drums, bass, other, vocals)))
        out.append({"mix": mix[:k], "vocals": vocals[:k], "accomp": (drums[:k] + bass[:k] + other[:k])})
    return out


def sfx(n):
    p = DATA / "sfx.zip"
    if not p.exists():
        urllib.request.urlretrieve("https://opengameart.org/sites/default/files/100-CC0-SFX_0.zip", p)
    z = zipfile.ZipFile(p)
    names = [i for i in z.namelist() if i.lower().endswith((".ogg", ".wav", ".mp3"))]
    out = []
    for name in names:
        try:
            x = decode(z.read(name))
        except Exception:  # noqa: BLE001
            continue
        if len(x) > 0.5 * SR:
            # chain several effects into ~6 s, like a busy game scene
            out.append(x[: 6 * SR])
    chained = []
    for i in range(0, len(out) - 3, 4):
        chained.append(np.concatenate(out[i:i + 4])[: 8 * SR])
    return chained[:n]


def mix_at(speech, music, snr_db):
    k = min(len(speech), len(music))
    s, m = speech[:k], music[:k]
    ps, pm = np.mean(s**2) + 1e-12, np.mean(m**2) + 1e-12
    m = m * np.sqrt(ps / (pm * 10 ** (snr_db / 10)))
    y = s + m
    return (y / max(1.0, np.max(np.abs(y)))).astype(np.float32)


class Silero:
    def __init__(self):
        import sys as _s
        path = Path(_s.prefix) / "Lib" / "site-packages" / "faster_whisper" / "assets" / "silero_vad_v6.onnx"
        o = ort.SessionOptions(); o.intra_op_num_threads = 1
        self.s = ort.InferenceSession(str(path), o, providers=["CPUExecutionProvider"])

    def share(self, x):
        h = np.zeros((1, 1, 128), np.float32); c = np.zeros((1, 1, 128), np.float32); ctx = np.zeros(64, np.float32)
        p = []
        for i in range(0, len(x) - 511, 512):
            w = x[i:i + 512]
            r, h, c = self.s.run(None, {"input": np.concatenate([ctx, w])[None, :], "h": h, "c": c})
            ctx = w[-64:].copy(); p.append(float(r[0]))
        p = np.array(p)
        return float(np.mean(p >= 0.5)), float(np.sum(p >= 0.5) * 0.032)


SPEECH = [0, 1, 2, 3, 4, 5]  # Speech, Male, Female, Child speech, Conversation, Narration
SING = [27, 28, 29, 30, 31, 32, 33, 34, 35, 36, 37]
MUSIC = [137]


class Ced:
    def __init__(self):
        o = ort.SessionOptions(); o.intra_op_num_threads = 2
        self.s = ort.InferenceSession(str(DATA / "model.onnx"), o, providers=["CPUExecutionProvider"])

    def __call__(self, x):
        t0 = time.perf_counter()
        logits = self.s.run(None, {"waveform": x[None, :]})[0][0]
        p = 1 / (1 + np.exp(-logits))
        return {"speech": float(p[SPEECH].max()), "singing": float(p[SING].max()), "music": float(p[MUSIC].max()),
                "ms": (time.perf_counter() - t0) * 1000}


def auc(pos, neg):
    pos, neg = np.asarray(pos), np.asarray(neg)
    return float(np.mean([(p > neg).mean() + 0.5 * (p == neg).mean() for p in pos]))


if __name__ == "__main__":
    speech = fleurs("fr_fr", 20) + fleurs("en_us", 20)
    songs = musdb(40)
    effects = sfx(20)
    clips = []
    for x in speech:
        clips.append(("speech", True, x))
    for i, s in enumerate(songs):
        clips.append(("song", False, s["mix"]))
        clips.append(("a_cappella", False, s["vocals"]))
        clips.append(("instrumental", False, s["accomp"]))
        sp = speech[i % len(speech)]
        clips.append(("speech_over_music_0dB", True, mix_at(sp, s["accomp"], 0)))
        clips.append(("speech_over_music_10dB", True, mix_at(sp, s["accomp"], 10)))
    for x in effects:
        clips.append(("game_sfx", False, x))
    vad, ced = Silero(), Ced()
    rows = []
    for kind, keep, x in clips:
        share, secs = vad.share(x)
        t = ced(x)
        rows.append({"kind": kind, "keep": keep, "silero_share": share, "silero_speech_s": secs,
                     "segment_today": secs >= 0.4, **t, "secs": len(x) / SR})
    (DATA / "tag_rows.json").write_text(json.dumps(rows))
    out = {"per_kind": {}}
    for kind in sorted({r["kind"] for r in rows}):
        rs = [r for r in rows if r["kind"] == kind]
        out["per_kind"][kind] = {
            "n": len(rs),
            "silero_would_record": round(np.mean([r["segment_today"] for r in rs]), 3),
            "silero_speech_share": round(np.mean([r["silero_share"] for r in rs]), 3),
            "ced_speech_median": round(float(np.median([r["speech"] for r in rs])), 3),
            "ced_singing_median": round(float(np.median([r["singing"] for r in rs])), 3),
            "ced_music_median": round(float(np.median([r["music"] for r in rs])), 3),
        }
    pos = [r for r in rows if r["keep"]]; neg = [r for r in rows if not r["keep"]]
    scores = {
        "silero_share": lambda r: r["silero_share"],
        "ced_speech": lambda r: r["speech"],
        "ced_speech_minus_singing": lambda r: r["speech"] - r["singing"],
    }
    out["auc_keep_vs_drop"] = {k: round(auc([f(r) for r in pos], [f(r) for r in neg]), 3) for k, f in scores.items()}
    for k, f in scores.items():
        negs = np.sort([f(r) for r in neg])
        for fpr in (0.02, 0.05, 0.10):
            thr = negs[int(np.ceil((1 - fpr) * len(negs))) - 1]
            out[f"{k}@false_keep<={fpr}"] = {"threshold": round(float(thr), 3),
                                              "speech_kept": round(float(np.mean([f(r) > thr for r in pos])), 3)}
    out["ced_ms_per_clip_median"] = round(float(np.median([r["ms"] for r in rows])), 1)
    print(json.dumps(out, indent=1))
