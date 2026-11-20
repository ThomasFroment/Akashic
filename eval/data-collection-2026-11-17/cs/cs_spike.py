"""Code-switching spike: one segment = a French FLEURS sentence followed by an English one.
Compares production decoding (detect, then force the likelier of fr/en) with alternatives.
CPU int8 at below-normal priority. Public CC-BY-4.0 audio only.

usage: cs_spike.py DATA_DIR N_PAIRS"""

from __future__ import annotations

import csv
import ctypes
import io
import json
import re
import sys
import tarfile
import time
import unicodedata
import urllib.request
import wave
from pathlib import Path

import numpy as np

ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x4000)
DATA = Path(sys.argv[1]); N = int(sys.argv[2])
HF = "https://huggingface.co/datasets/google/fleurs/resolve/main/data"
SYSTRAN = "https://huggingface.co/Systran/faster-whisper-large-v3/resolve/edaa852ec7e145841d8ffdb056a99866b5f0a478"
FILES = ["config.json", "preprocessor_config.json", "tokenizer.json", "vocabulary.json", "model.bin"]


def fetch(url, dest: Path):
    if dest.is_file() and dest.stat().st_size:
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".part")
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "spike"}), timeout=120) as r, tmp.open("wb") as o:
        while c := r.read(1 << 20):
            o.write(c)
    tmp.replace(dest)


def clips(lang, n):
    tsv = DATA / lang / "dev.tsv"
    fetch(f"{HF}/{lang}/dev.tsv", tsv)
    text = {row[1]: row[2] for row in csv.reader(tsv.open(encoding="utf-8"), delimiter="\t") if len(row) > 2}
    out = []
    with urllib.request.urlopen(urllib.request.Request(f"{HF}/{lang}/audio/dev.tar.gz", headers={"User-Agent": "spike"}), timeout=120) as r, \
            tarfile.open(fileobj=r, mode="r|gz") as tar:
        for m in tar:
            name = Path(m.name).name
            if not m.isfile() or name not in text:
                continue
            import av
            with av.open(io.BytesIO(tar.extractfile(m).read())) as c:
                rs = av.AudioResampler(format="flt", layout="mono", rate=16000)
                parts = [f.to_ndarray().reshape(-1) for fr_ in c.decode(audio=0) for f in rs.resample(fr_)]
                x = np.concatenate(parts).astype(np.float32)
            if 3 * 16000 < len(x) < 12 * 16000:
                out.append((x, text[name]))
            if len(out) >= n:
                break
    return out


def norm(s):
    s = unicodedata.normalize("NFKD", s.lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.findall(r"[a-z0-9]+", s)


def recall(ref, hyp):
    """Share of reference words found in the hypothesis (bag of words, clipped counts)."""
    r = norm(ref); h = norm(hyp)
    from collections import Counter
    cr, ch = Counter(r), Counter(h)
    return sum(min(c, ch[w]) for w, c in cr.items()) / max(1, len(r))


if __name__ == "__main__":
    model_dir = DATA / "faster-whisper-large-v3"
    for f in FILES:
        fetch(f"{SYSTRAN}/{f}", model_dir / f)
    fr, en = clips("fr_fr", N), clips("en_us", N)
    from faster_whisper import WhisperModel
    model = WhisperModel(str(model_dir), device="cpu", compute_type="int8", cpu_threads=4)
    gap = np.zeros(int(0.6 * 16000), np.float32)
    common = dict(task="transcribe", beam_size=5, condition_on_previous_text=False, no_speech_threshold=0.6,
                  log_prob_threshold=-2.0, compression_ratio_threshold=2.4, suppress_blank=True, vad_filter=False)

    def decode(x, language):
        segs, info = model.transcribe(x, language=language, **common)
        return " ".join(s.text.strip() for s in segs), info

    rows = []
    for i in range(min(len(fr), len(en))):
        for order in ("fr_en", "en_fr"):
            (a, ta), (b, tb) = (fr[i], en[i]) if order == "fr_en" else (en[i], fr[i])
            x = np.concatenate([a, gap, b])
            ref_fr = fr[i][1]; ref_en = en[i][1]
            t0 = time.perf_counter()
            _, _, probs = model.detect_language(x)
            scores = {l: p for l, p in probs if l in ("fr", "en")}
            forced = max(("fr", "en"), key=lambda l: scores.get(l, 0))
            prod, _ = decode(x, forced)
            free, info = decode(x, None)
            split = decode(a, max(("fr", "en"), key=lambda l: dict(model.detect_language(a)[2]).get(l, 0)))[0] + " " + \
                decode(b, max(("fr", "en"), key=lambda l: dict(model.detect_language(b)[2]).get(l, 0)))[0]
            row = {"pair": i, "order": order, "forced": forced, "free_lang": info.language,
                   "secs": round(len(x) / 16000, 1), "decode_s": round(time.perf_counter() - t0, 1)}
            for name, hyp in (("production", prod), ("unrestricted", free), ("split_at_pause", split)):
                row[f"{name}_fr_recall"] = round(recall(ref_fr, hyp), 3)
                row[f"{name}_en_recall"] = round(recall(ref_en, hyp), 3)
            rows.append(row)
            print(json.dumps(row), flush=True)
    (DATA / "cs_rows.json").write_text(json.dumps(rows))
    for name in ("production", "unrestricted", "split_at_pause"):
        f = np.mean([r[f"{name}_fr_recall"] for r in rows]); e = np.mean([r[f"{name}_en_recall"] for r in rows])
        lost = np.mean([min(r[f"{name}_fr_recall"], r[f"{name}_en_recall"]) < 0.5 for r in rows])
        print(name, "fr", round(float(f), 3), "en", round(float(e), 3), "segments losing a language (<50%)", round(float(lost), 3))
