"""Asset pack of the quality harness: fetch public corpora, build the committed pack, verify it.

    uv run --with pyarrow==26.0.1 python eval\\quality\\assets.py fetch|build|verify

Sources (eval only, CC BY 4.0): LibriSpeech dev-clean (OpenSLR 12) and the MLS French test split
(facebook/multilingual_librispeech, pinned to a dataset revision). Videos and game HUD images are
built at run time from the pack.
"""

from __future__ import annotations

import io
import json
import random
import sys
import tarfile
import tempfile
import wave
from collections import defaultdict
from pathlib import Path

import av
import numpy as np

from common import ASSETS, RAW, download_pinned, sha256_file

SR = 16_000
FRAME = 512  # 32 ms
BANKS = ASSETS / "banks"
LIBRI_URL = "https://www.openslr.org/resources/12/dev-clean.tar.gz"
LIBRI_SHA = "76f87d090650617fca0cac8f88b9416e0ebf80350acb97b343a85fa903728ab3"
MLS_REV = "2e83e61823b4c47dcbcb1980bb88601274127609"
MLS_URL = f"https://huggingface.co/datasets/facebook/multilingual_librispeech/resolve/{MLS_REV}/french/test-00000-of-00001.parquet"
MLS_SHA = "118dad8c119d13564464cc2c5d2d4ffaea4f5752f4cb50a589f502e905ede736"
LIBRI_FILE, MLS_FILE = RAW / "dev-clean.tar.gz", RAW / "mls-test.parquet"
MAX_PACK_BYTES = 50_000_000
SEED = 20261118
CLIP_S = (3.0, 16.0)
TARGET_DBFS = -26.0
# (role, per-language count, seconds per speaker)
ROLES = (("call", 2, 600), ("user", 1, 600), ("video", 2, 300), ("spare", 3, 300))
MIN_UTTERANCES = 30
MAX_WORDS = 20
MIXED_CLIPS = 8
TTS_TEXTS = (
    "Minlo", "Akashic", "Outer Wilds", "Steam", "Discord", "the Minecraft Rust project", "quality harness",
    "Quelle vidéo as-tu regardée hier après-midi", "Minlo a parlé d'un jeu ce matin", "Which solo game did you play",
    "Season one episode three", "Saison deux épisode quatre", "Hollow Knight", "Rust and Minecraft together",
    "Le projet s'appelle Moonlight", "Check the build before the stream", "Rendez-vous demain à neuf heures",
)


# --- speech activity (the speaker study's rule, make_fixture.py:47-54) -------------------------


def activity(x: np.ndarray) -> np.ndarray:
    n = len(x) // FRAME
    rms = np.sqrt(np.mean(x[: n * FRAME].reshape(n, FRAME) ** 2, axis=1) + 1e-12)
    ref = np.percentile(rms, 95) if n else 0.0
    act = rms > max(0.003, ref * 10 ** (-30 / 20))
    return act | np.r_[act[1:], False] | np.r_[False, act[:-1]]


def speech_spans(x: np.ndarray) -> list[list[float]]:
    act, out, i = activity(x), [], 0
    while i < len(act):
        if act[i]:
            j = i
            while j < len(act) and act[j]:
                j += 1
            out.append([round(i * FRAME / SR, 3), round(j * FRAME / SR, 3)])
            i = j
        else:
            i += 1
    return out


# --- audio io ----------------------------------------------------------------------------------


def decode(data: bytes | Path) -> np.ndarray:
    resampler = av.AudioResampler(format="flt", layout="mono", rate=SR)
    parts = []
    with av.open(io.BytesIO(data) if isinstance(data, bytes) else str(data)) as c:
        for frame in c.decode(audio=0):
            parts.extend(o.to_ndarray().reshape(-1) for o in resampler.resample(frame))
    parts.extend(o.to_ndarray().reshape(-1) for o in resampler.resample(None))
    return np.concatenate(parts).astype(np.float32)


def encode_opus(x: np.ndarray, path: Path) -> None:
    """Opus in Ogg, voip application, 32 kbps."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with av.open(str(path), "w", format="ogg") as out:
        stream = out.add_stream("libopus", rate=SR, layout="mono")
        stream.bit_rate = 32_000
        stream.codec_context.options = {"application": "voip"}
        resampler = av.AudioResampler(format="s16", layout="mono", rate=SR, frame_size=320)
        pcm = (np.clip(x, -1, 1) * 32767).astype(np.int16)
        for i in range(0, len(pcm), 320 * 50):
            frame = av.AudioFrame.from_ndarray(pcm[i:i + 320 * 50][None, :], format="s16", layout="mono")
            frame.sample_rate = SR
            for f in resampler.resample(frame):
                out.mux(stream.encode(f))
        for f in resampler.resample(None):
            out.mux(stream.encode(f))
        out.mux(stream.encode(None))


def load_bank(name: str) -> np.ndarray:
    """float32 samples of a bank in the pack."""
    return decode(BANKS / f"{name}.opus")


def clip_samples(clip: dict, banks: dict[str, np.ndarray]) -> np.ndarray:
    return banks[clip["bank"]][clip["off"]:clip["off"] + clip["n"]]


def normalized(x: np.ndarray) -> np.ndarray:
    act = np.repeat(activity(x), FRAME)[: len(x)]
    ref = x[: len(act)][act[: len(x)]] if act.any() else x
    gain = 10 ** (TARGET_DBFS / 20) / max(float(np.sqrt(np.mean(ref ** 2))), 1e-6)
    return np.clip(x * gain, -0.95, 0.95).astype(np.float32)


# --- fetch -------------------------------------------------------------------------------------


def fetch() -> None:
    for url, dest, sha in ((LIBRI_URL, LIBRI_FILE, LIBRI_SHA), (MLS_URL, MLS_FILE, MLS_SHA)):
        print(f"{dest.name}: ", end="", flush=True)
        download_pinned(url, dest, sha)
        print("ok")


# --- build -------------------------------------------------------------------------------------


def librispeech_utterances() -> tuple[dict[str, list[tuple[bytes, str]]], list[str]]:
    """Per speaker: (flac bytes, transcript) of 3-16 s utterances, and every transcript."""
    texts: dict[str, str] = {}
    flacs: dict[str, bytes] = {}
    with tarfile.open(LIBRI_FILE, "r:gz") as tar:
        for m in tar:
            if not m.isfile():
                continue
            if m.name.endswith(".trans.txt"):
                for line in tar.extractfile(m).read().decode().splitlines():
                    key, _, text = line.partition(" ")
                    texts[key] = text
            elif m.name.endswith(".flac"):
                flacs[m.name.split("/")[-1][:-5]] = tar.extractfile(m).read()
    by_spk: dict[str, list[tuple[bytes, str]]] = defaultdict(list)
    for key in sorted(flacs):
        by_spk["en" + key.split("-")[0]].append((flacs[key], texts[key]))
    return by_spk, [texts[k] for k in sorted(texts)]


def mls_utterances() -> tuple[dict[str, list[tuple[bytes, str]]], list[str]]:
    import pyarrow.parquet as pq

    pf = pq.ParquetFile(MLS_FILE)
    seconds: dict[str, float] = defaultdict(float)
    texts: list[str] = []
    for batch in pf.iter_batches(batch_size=256, columns=["speaker_id", "audio_duration", "transcript"]):
        for spk, dur, text in zip(*(batch.column(c).to_pylist() for c in ("speaker_id", "audio_duration", "transcript")), strict=True):
            if CLIP_S[0] <= dur <= CLIP_S[1]:
                seconds[spk] += dur
            texts.append(text)
    chosen = set(sorted(s for s in seconds if seconds[s] >= 900)[:12])
    by_spk: dict[str, list[tuple[bytes, str]]] = defaultdict(list)
    for batch in pf.iter_batches(batch_size=64, columns=["audio", "speaker_id", "transcript", "audio_duration"]):
        for audio, spk, text, dur in zip(*(batch.column(c).to_pylist() for c in ("audio", "speaker_id", "transcript", "audio_duration")), strict=True):
            if spk in chosen and CLIP_S[0] <= dur <= CLIP_S[1]:
                by_spk["fr" + spk].append((audio["bytes"], text))
    return by_spk, sorted(set(texts))


def pick_clips(utts: list[tuple[bytes, str]], seconds: float, rng: random.Random) -> list[tuple[np.ndarray, str]]:
    order = list(range(len(utts)))
    rng.shuffle(order)
    out, total = [], 0.0
    for i in order:
        x = decode(utts[i][0])
        if CLIP_S[0] <= len(x) / SR <= CLIP_S[1]:
            out.append((x, utts[i][1]))
            total += len(x) / SR
        if total >= seconds:
            break
    return out


def write_bank(name: str, clips: list[tuple[np.ndarray, str]], meta: dict, manifest: dict, per_clip: list[dict] | None = None) -> None:
    """Concatenate clips into one Opus bank, then record offsets and spans of the decoded result."""
    audio = np.concatenate([normalized(x) for x, _ in clips])
    encode_opus(audio, BANKS / f"{name}.opus")
    heard = load_bank(name)
    off = 0
    for k, (x, text) in enumerate(clips):
        n = len(x)
        manifest["clips"].append({"id": f"{name}-{k:03d}", "bank": name, "off": off, "n": n, "dur_s": round(n / SR, 3),
                                  "text": text, "spans": speech_spans(heard[off:off + n]), **meta, **(per_clip[k] if per_clip else {})})
        off += n


def sapi_clips() -> list[tuple[str, list[tuple[np.ndarray, str]]]]:
    import comtypes.client

    voice = comtypes.client.CreateObject("SAPI.SpVoice")
    stream = comtypes.client.CreateObject("SAPI.SpFileStream")
    out = []
    voices = voice.GetVoices()
    for v in range(voices.Count):
        voice.Voice = voices.Item(v)
        label = "".join(ch for ch in voices.Item(v).GetDescription().split(" - ")[0] if ch.isalnum()).lower()
        clips = []
        for text in TTS_TEXTS:
            path = Path(tempfile.gettempdir()) / "quality-sapi.wav"
            stream.Format.Type = 18  # SAFT16kHz16BitMono
            stream.Open(str(path), 3)
            voice.AudioOutputStream = stream
            voice.Speak(text)
            stream.Close()
            with wave.open(str(path)) as w:
                x = np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32768
            path.unlink()
            clips.append((np.concatenate([np.zeros(SR // 4, np.float32), x, np.zeros(SR // 4, np.float32)]), text))
        out.append((label, clips))
    return out


def synth_music(seconds: int, rng: np.random.Generator) -> np.ndarray:
    t = np.arange(seconds * SR) / SR
    x = np.zeros_like(t)
    chords = ((220, 277, 330), (196, 247, 294), (174, 220, 262), (196, 247, 330))
    for k, chord in enumerate(chords * (seconds // 8)):
        seg = slice(k * 2 * SR, (k + 1) * 2 * SR)
        tt = t[seg] - t[seg][0]
        env = np.minimum(1, tt * 8) * np.exp(-tt * 0.8)
        for f in chord:
            x[seg] += env * (np.sin(2 * np.pi * f * tt) + 0.3 * np.sin(2 * np.pi * 2 * f * tt)) / len(chord)
    return (0.5 * x / max(1e-6, np.abs(x).max())).astype(np.float32)


def synth_game(seconds: int, rng: np.random.Generator) -> np.ndarray:
    x = np.zeros(seconds * SR, np.float32)
    for start in np.sort(rng.integers(0, (seconds - 2) * SR, seconds // 2)):
        n = int(rng.integers(SR // 8, SR))
        env = np.exp(-np.arange(n) / (n / 4))
        if rng.random() < 0.5:
            burst = rng.standard_normal(n) * env
        else:
            f = np.linspace(rng.integers(200, 900), rng.integers(900, 2400), n)
            burst = np.sin(2 * np.pi * np.cumsum(f) / SR) * env
        x[start:start + n] += burst.astype(np.float32)
    return (0.6 * x / max(1e-6, np.abs(x).max())).astype(np.float32)


def build() -> None:
    rng = random.Random(SEED)
    manifest: dict = {"version": 1, "sources": {"librispeech": [LIBRI_URL, LIBRI_SHA], "mls_french": [MLS_URL, MLS_SHA]},
                      "speakers": {}, "clips": [], "files": {}}
    en, en_texts = librispeech_utterances()
    fr, fr_texts = mls_utterances()
    pools = {"en": sorted(en), "fr": sorted(fr)}
    used: set[str] = set()
    corpora = {"en": (en, "librispeech"), "fr": (fr, "mls_french")}
    for role, per_lang, seconds in ROLES:
        for lang in ("fr", "en"):
            for k in range(per_lang):
                corpus_spk = next(s for s in pools[lang] if s not in used and len(corpora[lang][0][s]) >= MIN_UTTERANCES)
                used.add(corpus_spk)
                sid = f"{role}_{lang}{k + 1}"
                clips = pick_clips(corpora[lang][0][corpus_spk], seconds, rng)
                manifest["speakers"][sid] = {"role": role, "lang": lang, "corpus": corpora[lang][1], "corpus_speaker": corpus_spk}
                write_bank(sid, clips, {"kind": "speech", "speaker": sid, "lang": lang}, manifest)
                print(f"{sid}: {len(clips)} clips", flush=True)
    # mixed FR/EN: a whole English clip then a whole French one, so the transcript is exactly the audio.
    # The halves come from voices no other role uses; each half keeps its own speaker and text.
    mixed, halves = [], []
    banks = {n: load_bank(n) for n in ("spare_en2", "spare_fr2")}
    shortest = lambda speaker: sorted((c for c in manifest["clips"] if c["speaker"] == speaker), key=lambda c: (c["dur_s"], c["id"]))[:MIXED_CLIPS]
    gap = SR // 6
    for a, b in zip(shortest("spare_en2"), shortest("spare_fr2"), strict=True):
        mixed.append((np.concatenate([clip_samples(a, banks), np.zeros(gap, np.float32), clip_samples(b, banks)]), a["text"] + " " + b["text"]))
        halves.append({"split_s": round((a["n"] + gap // 2) / SR, 3),
                       "parts": [{"speaker": a["speaker"], "lang": "en", "text": a["text"]}, {"speaker": b["speaker"], "lang": "fr", "text": b["text"]}]})
    manifest["speakers"]["mixed1"] = {"role": "extra", "lang": "mixed", "corpus": "splice", "corpus_speaker": "spare_en2+spare_fr2"}
    write_bank("mixed1", mixed, {"kind": "speech", "speaker": "mixed1", "lang": "mixed"}, manifest, halves)
    for label, clips in sapi_clips():
        sid = f"tts_{label}"
        manifest["speakers"][sid] = {"role": "tts", "lang": "en", "corpus": "sapi", "corpus_speaker": label}
        write_bank(sid, clips, {"kind": "tts", "speaker": sid, "lang": "en"}, manifest)
    nrng = np.random.default_rng(SEED)
    for name, audio in (("music", synth_music(60, nrng)), ("game", synth_game(30, nrng))):
        encode_opus(audio, BANKS / f"{name}.opus")
        manifest["clips"].append({"id": f"{name}-000", "bank": name, "off": 0, "n": len(audio), "dur_s": len(audio) / SR,
                                  "text": "", "spans": [], "kind": name, "speaker": name, "lang": ""})
    sentences = build_sentences(en_texts, fr_texts, rng)
    (ASSETS / "sentences.json").write_text(json.dumps(sentences, ensure_ascii=False, indent=0, sort_keys=True), encoding="utf-8", newline="\n")
    manifest["files"] = {p.relative_to(ASSETS).as_posix(): {"sha256": sha256_file(p), "bytes": p.stat().st_size}
                         for p in sorted(ASSETS.rglob("*")) if p.is_file() and p.name != "manifest.json"}
    (ASSETS / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8", newline="\n")
    total = sum(v["bytes"] for v in manifest["files"].values())
    print(f"pack: {total / 1e6:.1f} MB, {len(manifest['clips'])} clips")
    if total > MAX_PACK_BYTES:
        raise SystemExit("pack exceeds 50 MB")


def build_sentences(en: list[str], fr: list[str], rng: random.Random) -> dict[str, list[str]]:
    def clean(s: str) -> str:
        s = " ".join(s.strip().lower().split()[:MAX_WORDS])
        return s[:1].upper() + s[1:] + "."

    def ok(s: str) -> bool:
        return len(s.split()) >= 6

    en_pool = [clean(s) for s in en if ok(s)]
    fr_pool = [clean(s) for s in fr if ok(s)]
    en_pick, fr_pick = rng.sample(en_pool, 400), rng.sample(fr_pool, 400)
    mixed = [f"{rng.choice(en_pick).rstrip('.')} {rng.choice(('and then', 'mais aussi', 'puis', 'but'))} {rng.choice(fr_pick)[0].lower()}{rng.choice(fr_pick)[1:]}"
             for _ in range(200)]
    return {"en": en_pick, "fr": fr_pick, "mixed": mixed}


def verify() -> int:
    manifest_path = ASSETS / "manifest.json"
    if not manifest_path.is_file():
        print("no manifest")
        return 1
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    bad = []
    for rel, meta in manifest["files"].items():
        p = ASSETS / rel
        if not p.is_file() or sha256_file(p) != meta["sha256"]:
            bad.append(rel)
    extra = {p.relative_to(ASSETS).as_posix() for p in ASSETS.rglob("*") if p.is_file()} - set(manifest["files"]) - {"manifest.json"}
    for rel in sorted(extra):
        bad.append(f"{rel} (not in manifest)")
    print("pack matches its manifest" if not bad else "MISMATCH: " + ", ".join(bad))
    return 1 if bad else 0


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "fetch":
        fetch()
    elif cmd == "build":
        build()
    elif cmd == "verify":
        sys.exit(verify())
    else:
        sys.exit("usage: assets.py fetch|build|verify")
