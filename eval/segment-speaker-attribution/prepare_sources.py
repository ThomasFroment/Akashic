"""Decode the downloaded corpora into per-speaker 16 kHz int16 utterance banks.

Reads the archives in place (no extraction, nothing executed from them):
- data/librispeech/{dev-clean,test-clean}.tar.gz  (LibriSpeech, OpenSLR 12, CC BY 4.0)
- data/mls_french/{dev,test,9_hours}.parquet      (MLS French, HF facebook/multilingual_librispeech, CC BY 4.0)

Writes data/sources/<spk>.npz (audio int16 concatenated + utterance offsets) and
data/sources/speakers.json (lang, gender, corpus, utterances, seconds). At most MAX_S seconds per
speaker are kept (utterances in corpus order).

    .venv/Scripts/python.exe prepare_sources.py
"""

from __future__ import annotations

import io
import json
import tarfile
from collections import defaultdict

import av
import numpy as np

from common import DATA, SR

MAX_S = 900.0
OUT = DATA / "sources"


def decode(data: bytes) -> np.ndarray:
    resampler = av.AudioResampler(format="flt", layout="mono", rate=SR)
    parts = []
    with av.open(io.BytesIO(data)) as c:
        for frame in c.decode(audio=0):
            parts.extend(o.to_ndarray().reshape(-1) for o in resampler.resample(frame))
    parts.extend(o.to_ndarray().reshape(-1) for o in resampler.resample(None))
    return np.concatenate(parts).astype(np.float32)


def librispeech(banks: dict, meta: dict) -> None:
    genders: dict[str, str] = {}
    for split in ("dev-clean", "test-clean"):
        path = DATA / "librispeech" / f"{split}.tar.gz"
        with tarfile.open(path, "r:gz") as tar:
            for member in tar:
                if not member.isfile():
                    continue
                name = member.name
                if name.endswith("SPEAKERS.TXT"):
                    for line in tar.extractfile(member).read().decode("utf-8").splitlines():
                        if line.startswith(";"):
                            continue
                        cols = [c.strip() for c in line.split("|")]
                        if len(cols) >= 2:
                            genders[cols[0]] = cols[1]
                    continue
                if not name.endswith(".flac"):
                    continue
                spk = "en" + name.split("/")[-1].split("-")[0]
                seconds = sum(len(u) for u in banks[spk]) / SR
                if seconds >= MAX_S:
                    continue
                banks[spk].append(decode(tar.extractfile(member).read()))
                meta[spk] = {"lang": "en", "corpus": "librispeech", "split": split}
    for spk, m in meta.items():
        if m["corpus"] == "librispeech":
            m["gender"] = genders.get(spk[2:], "?")


def mls(banks: dict, meta: dict) -> None:
    import pyarrow.parquet as pq

    for split in ("dev", "test", "9_hours"):
        table = pq.ParquetFile(DATA / "mls_french" / f"{split}.parquet")
        for batch in table.iter_batches(batch_size=64, columns=["audio", "speaker_id"]):
            for audio, spk_id in zip(
                batch.column("audio").to_pylist(), batch.column("speaker_id").to_pylist(), strict=True
            ):
                spk = f"fr{spk_id}"
                if spk in meta and meta[spk]["split"] != split:
                    continue  # keep one split per speaker
                if sum(len(u) for u in banks[spk]) / SR >= MAX_S:
                    continue
                banks[spk].append(decode(audio["bytes"]))
                meta[spk] = {"lang": "fr", "corpus": "mls_french", "split": split, "gender": "?"}


def pitch_gender(utts: list[np.ndarray]) -> tuple[str, float]:
    """MLS parquet has no gender: median F0 by autocorrelation over voiced 40 ms frames
    (>= 165 Hz -> F). Only used to form same-gender similar-voice pairs."""
    x = np.concatenate(utts)[: SR * 120]
    frame = 640
    f0s = []
    for i in range(0, len(x) - frame, frame):
        f = x[i : i + frame] - x[i : i + frame].mean()
        if np.sqrt(np.mean(f * f)) < 0.02:
            continue
        ac = np.correlate(f, f, "full")[frame - 1 :]
        lo, hi = SR // 400, SR // 70
        lag = lo + int(np.argmax(ac[lo:hi]))
        if ac[lag] > 0.4 * ac[0]:
            f0s.append(SR / lag)
    f0 = float(np.median(f0s)) if f0s else 0.0
    return ("F" if f0 >= 165 else "M"), f0


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    banks: dict[str, list[np.ndarray]] = defaultdict(list)
    meta: dict[str, dict] = {}
    librispeech(banks, meta)
    mls(banks, meta)
    for spk, utts in banks.items():
        audio = np.concatenate(utts)
        offsets = np.cumsum([0] + [len(u) for u in utts])
        np.savez(OUT / f"{spk}.npz", audio=(np.clip(audio, -1, 1) * 32767).astype(np.int16), offsets=offsets)
        m = meta[spk]
        m["utterances"] = len(utts)
        m["seconds"] = round(len(audio) / SR, 1)
        g, f0 = pitch_gender(utts)
        m["f0"] = round(f0, 1)
        if m["gender"] == "?":
            m["gender"] = g
            m["gender_from"] = "f0"
    (OUT / "speakers.json").write_text(json.dumps(meta, indent=1, sort_keys=True), encoding="utf-8")
    by = defaultdict(int)
    for m in meta.values():
        by[(m["lang"], m["gender"])] += 1
    print(len(meta), "speakers", dict(by))


if __name__ == "__main__":
    main()
