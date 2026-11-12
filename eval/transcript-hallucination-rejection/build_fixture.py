"""Assemble the labelled clips from data/raw/ into data/fixture/.

Run:  .venv\\Scripts\\python.exe -I build_fixture.py
Writes data/fixture/clips/<id>.npy (16 kHz mono float32, padded with 0.5 s of digital silence
before and 1.5 s after, as a capture stream would hear it) and data/fixture/clips.jsonl.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import random
import re
import sys
import tarfile
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path[:0] = [str(HERE), str(REPO / "src")]

import av  # noqa: E402
import numpy as np  # noqa: E402

from akashic.pipeline.transcribe import build_prompt  # noqa: E402

RAW = HERE / "data" / "raw"
OUT = HERE / "data" / "fixture"
SR = 16_000
LEAD_S, TRAIL_S = 0.5, 1.5
MAX_SPEECH_S = 18.0
MAX_NONSPEECH_S = 25.0
MINING_PCT = 40
SHORT_PER_WORD = 45  # clips kept per (language, word) for the short-reply subset


def save_clip(path: Path, samples: np.ndarray) -> None:
    """16-bit PCM, like a capture device delivers; load with load_clip."""
    np.save(path, np.round(np.clip(samples, -1, 1) * 32767).astype(np.int16))


def load_clip(path: Path) -> np.ndarray:
    return np.load(path).astype(np.float32) / np.float32(32767)
DISCORD_BITRATE = 32_000


# ---------------------------------------------------------------- audio
def decode(data: bytes) -> np.ndarray:
    """Any container PyAV reads, to 16 kHz mono float32 (first audio stream)."""
    resampler = av.AudioResampler(format="flt", layout="mono", rate=SR)
    parts = []
    with av.open(io.BytesIO(data)) as container:
        for frame in container.decode(audio=0):
            parts.extend(out.to_ndarray().reshape(-1) for out in resampler.resample(frame))
    parts.extend(out.to_ndarray().reshape(-1) for out in resampler.resample(None))
    return np.concatenate(parts).astype(np.float32) if parts else np.zeros(0, np.float32)


def discord_opus(samples: np.ndarray) -> np.ndarray:
    """Discord-like voice path: 48 kHz Opus at 32 kbps, decoded back to 16 kHz."""
    up = av.AudioResampler(format="flt", layout="mono", rate=48_000)
    buf = io.BytesIO()
    with av.open(buf, "w", format="ogg") as container:
        stream = container.add_stream("libopus", rate=48_000)
        stream.bit_rate = DISCORD_BITRATE
        stream.layout = "mono"
        frame = av.AudioFrame.from_ndarray(samples[None, :].astype(np.float32), format="flt", layout="mono")
        frame.sample_rate = SR
        for f48 in [*up.resample(frame), *up.resample(None)]:
            container.mux(stream.encode(f48))
        container.mux(stream.encode(None))
    out = decode(buf.getvalue())
    if len(out) >= len(samples):
        return out[: len(samples)]
    return np.pad(out, (0, len(samples) - len(out)))


def pad(samples: np.ndarray) -> np.ndarray:
    return np.concatenate([np.zeros(int(LEAD_S * SR), np.float32), np.clip(samples, -1, 1),
                           np.zeros(int(TRAIL_S * SR), np.float32)]).astype(np.float32)


def rms(x: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.square(x, dtype=np.float64)))) if x.size else 0.0


# Playback-like levels. Dataset recording levels are arbitrary (FLEURS sits mostly below the
# 0.015 energy floor); a Discord voice or game heard through loopback is at listening volume.
# The "active" level is the RMS of the louder half of the 32 ms windows.
SPEECH_LEVEL = (0.03, 0.15)
NONSPEECH_LEVEL = (0.02, 0.15)


def active_rms(x: np.ndarray) -> float:
    n = len(x) // 512
    if n == 0:
        return rms(x)
    e = np.sort(np.mean(np.square(x[: n * 512].reshape(n, 512), dtype=np.float64), axis=1))
    return float(np.sqrt(np.mean(e[n // 2 :])))


def set_level(x: np.ndarray, key: str, band: tuple[float, float]) -> np.ndarray:
    """Scale to a seeded log-uniform active level in ``band``; peaks kept below 0.99."""
    import math
    target = math.exp(random.Random(f"level:{key}").uniform(math.log(band[0]), math.log(band[1])))
    level = active_rms(x)
    if level <= 0:
        return x.astype(np.float32)
    y = x.astype(np.float64) * (target / level)
    peak = float(np.max(np.abs(y)))
    if peak > 0.99:
        y *= 0.99 / peak
    return y.astype(np.float32)


def split_of(group: str) -> str:
    h = int(hashlib.sha1(group.encode()).hexdigest(), 16) % 100
    return "mining" if h < MINING_PCT else "test"


# ---------------------------------------------------------------- names
STOP_CAPS = {
    "i", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    "january", "february", "march", "april", "may", "june", "july", "august", "september",
    "october", "november", "december", "mr", "mrs", "dr", "st", "the", "a", "an", "le", "la",
    "les", "un", "une", "des", "il", "elle", "on", "nous", "vous", "ils", "elles", "ce", "cette",
    "in", "on", "at", "it", "this", "that", "en", "au", "aux", "de", "du", "mais", "et", "or",
}
_TOKEN = re.compile(r"[\w'’\-\.]+|[^\w\s]")
_ELISION = re.compile(r"^(?:[lLdDjJmMnNsStTcC]|[qQ]u|[jJ]usqu|[lL]orsqu|[pP]uisqu)['’]")


def proper_names(text: str) -> list[str]:
    """Runs of capitalised words that are not sentence-initial (simple, conservative)."""
    names: list[str] = []
    run: list[str] = []
    sentence_start = True
    for tok in _TOKEN.findall(text):
        word = re.sub(r"['’]s$", "", _ELISION.sub("", tok)).strip(".'’-")
        is_word = bool(re.match(r"\w", word))
        cap = is_word and word[:1].isupper() and len(word) >= 3 and word.lower() not in STOP_CAPS
        if cap and not sentence_start and not word.isdigit():
            run.append(word)
        else:
            if run:
                names.append(" ".join(run))
            run = []
        if is_word:
            sentence_start = False
        if tok in {".", "!", "?", ":", "«", '"'} or tok.endswith("."):
            sentence_start = True
    if run:
        names.append(" ".join(run))
    seen, out = set(), []
    for n in names:
        if n.lower() not in seen:
            seen.add(n.lower())
            out.append(n)
    return out


# A plausible personal vocabulary (friends, games, tools): invented strings, not captured data.
PERSONAL_VOCAB = [
    "Minlo", "Akashic", "Discord", "Valorant", "Elden Ring", "Baldur's Gate 3", "Helldivers 2",
    "Factorio", "Deep Rock Galactic", "Satisfactory", "Lethal Company", "Kévin", "Mathis", "Léa",
    "Théo", "Clément", "Noémie", "Hollow Knight", "Path of Exile", "Rocket League", "Overwatch",
    "Twitch", "OBS", "Python", "ShadowPlay", "Gaëtan", "Yanis", "Maëlys", "Jérémy", "Sacha",
    "Final Fantasy XIV", "Monster Hunter", "Stardew Valley", "Minecraft", "Terraria", "Steam",
    "Zelda", "Hades", "Balatro", "Palworld", "VS Code", "PowerShell", "Froggy", "Nyx", "Kaz",
]


# ---------------------------------------------------------------- sources
def fleurs() -> list[dict]:
    items = []
    for lang, code in (("fr_fr", "fr"), ("en_us", "en")):
        folder = RAW / "fleurs" / lang
        rows = {}
        for line in (folder / "dev.tsv").read_text(encoding="utf-8").splitlines():
            cols = line.split("\t")
            rows[cols[1]] = cols
        with tarfile.open(folder / "dev.tar.gz", "r:gz") as tar:
            for m in tar:
                name = Path(m.name).name
                if not m.isfile() or name not in rows:
                    continue
                cols = rows[name]
                audio = decode(tar.extractfile(m).read())
                if len(audio) / SR > MAX_SPEECH_S or len(audio) / SR < 1.0:
                    continue
                items.append({"lang": code, "sentence_id": cols[0], "file": f"fleurs/{lang}/dev/{name}",
                              "text": cols[2], "names": proper_names(cols[2]), "audio": audio})
    return items


def mswc() -> list[dict]:
    items = []
    for lang in ("fr", "en"):
        for p in sorted((RAW / "mswc" / lang).glob("*.opus")):
            split, name = p.name.split("__", 1)
            word = name.split("_common_voice_")[0]
            source = name.split("_common_voice_")[1].split(".")[0].split("__")[0]
            items.append({"lang": lang, "word": word, "source": f"cv_{lang}_{source}",
                          "file": f"mswc/{lang}/{split}/{name}", "audio": decode(p.read_bytes())})
    for p in sorted((RAW / "speechcommands").glob("*.wav")):  # standalone English yes/no
        word, name = p.name.split("__", 1)
        items.append({"lang": "en", "word": word, "source": f"sc_{name.split('_nohash_')[0]}",
                      "file": f"speechcommands/test/{word}/{name}", "audio": decode(p.read_bytes())})
    return items


def esc50() -> list[dict]:
    items = []
    with zipfile.ZipFile(RAW / "esc50" / "ESC-50-master.zip") as z:
        meta = {r["filename"]: r for r in csv.DictReader(io.TextIOWrapper(
            z.open("ESC-50-master/meta/esc50.csv"), encoding="utf-8"))}
        for name, r in meta.items():
            audio = decode(z.read(f"ESC-50-master/audio/{name}"))
            items.append({"source": "esc50", "category": f"esc50/{r['category']}",
                          "group": f"esc50:{r['src_file']}", "file": f"esc50/{name}",
                          "licence": "CC BY-NC 3.0", "audio": audio})
    return items


def fsd50k() -> list[dict]:
    folder = RAW / "fsd50k"
    chosen = json.loads((folder / "chosen.json").read_text())
    info = json.loads((folder / "eval_clips_info_FSD50K.json").read_text(encoding="utf-8"))
    items = []
    for group, names in chosen.items():
        for name in names:
            p = folder / "clips" / f"{name}.wav"
            if not p.is_file():
                continue
            audio = decode(p.read_bytes())[: int(MAX_NONSPEECH_S * SR)]
            if len(audio) < SR:
                continue
            lic = info.get(name, {}).get("license", "?")
            items.append({"source": "fsd50k", "category": f"fsd50k/{group}", "group": f"fsd50k:{name}",
                          "file": f"fsd50k/eval/{name}.wav", "licence": lic, "audio": audio})
    return items


DEMAND_MINING = ("DKITCHEN", "NFIELD")


def fsd50k_dev() -> list[dict]:
    """FSD50K dev clips kept by fetch_data.py because they open a production segment at their
    fixture level (VAD prefilter: only such audio is ever transcribed)."""
    folder = RAW / "fsd50k_dev"
    meta = json.loads((folder / "kept.json").read_text(encoding="utf-8"))
    items = []
    for name, m in meta.items():
        audio = np.load(folder / f"{name}.npy").astype(np.float32) / np.float32(32767)
        items.append({"source": "fsd50k", "category": f"fsd50k_dev/{m['group']}", "group": f"fsd50k:{name}",
                      "file": f"fsd50k/dev/{name}.wav", "licence": m["license"], "audio": audio,
                      "prefiltered": True})
    return items


def demand() -> list[dict]:
    items = []
    for p in sorted((RAW / "demand").glob("*_16k.zip")):
        env = p.name.split("_")[0]
        with zipfile.ZipFile(p) as z:
            member = next(n for n in z.namelist() if n.endswith("ch01.wav"))
            audio = decode(z.read(member))
        for k, start in enumerate(range(0, len(audio) - 15 * SR, 15 * SR)):
            items.append({"source": "demand", "category": f"demand/{env}", "group": f"demand:{env}",
                          # five recordings only: fixed env-level split so both splits get some
                          "split": "mining" if env in DEMAND_MINING else "test",
                          "file": f"demand/{env}/ch01.wav@{k * 15}s", "licence": "CC-BY-4.0",
                          "audio": audio[start : start + 15 * SR]})
    return items


def oga(rng: random.Random) -> list[dict]:
    """CC0 game audio: chiptune tracks cut into 20 s pieces; short SFX composed into 3-12 s
    sequences from files of one split only (so splits stay disjoint by source file)."""
    items = []
    sfx: dict[str, list[tuple[str, np.ndarray]]] = {"mining": [], "test": []}
    for p in sorted((RAW / "oga").glob("*.zip")):
        with zipfile.ZipFile(p) as z:
            for n in sorted(z.namelist()):
                if not n.lower().endswith((".wav", ".ogg", ".mp3")) or "__MACOSX" in n:
                    continue
                try:
                    audio = decode(z.read(n))
                except Exception:  # noqa: BLE001
                    continue
                key = f"oga:{p.stem}/{n}"
                if "chiptune" in p.stem:
                    for k, start in enumerate(range(0, len(audio) - 20 * SR, 20 * SR)):
                        items.append({"source": "oga", "category": "oga/chiptune_music", "group": key,
                                      "file": f"oga/{p.name}/{n}@{k * 20}s", "licence": "CC0",
                                      "audio": audio[start : start + 20 * SR]})
                elif len(audio) > 0:
                    sfx[split_of(key)].append((key, audio[: 6 * SR]))
    for split, pool in sfx.items():
        for k in range(len(pool) // 6):
            parts, keys, total = [], [], 0.0
            target = rng.uniform(3, 12)
            while total < target:
                key, audio = rng.choice(pool)
                gap = np.zeros(int(rng.uniform(0.05, 0.8) * SR), np.float32)
                parts += [audio, gap]
                keys.append(key)
                total += (len(audio) + len(gap)) / SR
            items.append({"source": "oga", "category": "oga/game_sfx", "group": f"oga-seq:{split}:{k}",
                          "split": split, "file": "+".join(keys[:6]) + ("..." if len(keys) > 6 else ""),
                          "licence": "CC0", "audio": np.concatenate(parts)[: int(MAX_NONSPEECH_S * SR)]})
    return items


# ---------------------------------------------------------------- VAD onset (production capture)
_CAPTURE = None


def vad_onset(audio: np.ndarray) -> int | None:
    """Start (ms, in the unpadded audio) of the first production segment, or None."""
    global _CAPTURE  # noqa: PLW0603
    if _CAPTURE is None:
        import prod
        _CAPTURE = prod.Capture()
    _probs, utts, _drops = _CAPTURE.run(pad(audio))
    return max(0, utts[0].start_ms - int(LEAD_S * 1000)) if utts else None


# ---------------------------------------------------------------- assembly
def main() -> None:
    rng = random.Random(20261110)
    (OUT / "clips").mkdir(parents=True, exist_ok=True)
    print("loading FLEURS", flush=True)
    fl = fleurs()
    print(f"  {len(fl)} utterances", flush=True)
    print("loading MSWC", flush=True)
    ms = mswc()
    by_word: dict[tuple[str, str], list[dict]] = {}
    for u in ms:
        by_word.setdefault((u["lang"], u["word"]), []).append(u)
    for key, group in by_word.items():  # a seeded shuffle, then a prefix: raising the cap only adds clips
        random.Random(str(key)).shuffle(group)
    ms = [u for key in sorted(by_word) for u in by_word[key][:SHORT_PER_WORD]]
    print(f"  {len(ms)} words", flush=True)
    print("loading non-speech", flush=True)
    noise = esc50() + fsd50k() + fsd50k_dev() + demand() + oga(rng)
    for n in noise:
        n["audio"] = set_level(n["audio"], n["group"] + n["file"], NONSPEECH_LEVEL)
    for u in [*fl, *ms]:
        u["audio"] = set_level(u["audio"], u["file"], SPEECH_LEVEL)
    print(f"  {len(noise)} clips", flush=True)

    name_pool = sorted({n for u in fl for n in u["names"]})
    texts = {"fr": [u["text"] for u in fl if u["lang"] == "fr"],
             "en": [u["text"] for u in fl if u["lang"] == "en"]}

    def prompt_for(cid: str, lang: str | None, own_text: str | None, include: list[str]) -> dict:
        r = random.Random(cid)
        size = r.randint(3, 30)
        pool = [*PERSONAL_VOCAB, *name_pool]
        own = {n.lower() for n in include}
        vocab = [v for v in r.sample(pool, size) if v.lower() not in own]
        if own_text:  # never put the clip's own names in by accident
            low = own_text.lower()
            vocab = [v for v in vocab if v.lower() not in low]
        for n in include:
            vocab.insert(r.randint(0, min(len(vocab), 8)), n)
        plang = lang if lang and r.random() < 0.7 else r.choice(["fr", "en"])
        previous = r.choice([t for t in texts[plang] if t != own_text])
        return {"vocab": vocab, "previous": previous, "prompt": build_prompt(tuple(vocab), previous)}

    records: list[dict] = []

    def emit(rec: dict, audio: np.ndarray, *, opus_copy: bool = False) -> None:
        clip = pad(audio)
        save_clip(OUT / "clips" / f"{rec['id']}.npy", clip)
        rec["duration_s"] = round(len(clip) / SR, 3)
        records.append(rec)
        if opus_copy:
            o = dict(rec, id=rec["id"] + "_opus", opus=True, orig_id=rec["id"])
            save_clip(OUT / "clips" / f"{o['id']}.npy", discord_opus(clip))
            records.append(o)

    lead = int(LEAD_S * 1000)

    # names / normal / concat from FLEURS
    rng.shuffle(fl)
    with_names = [u for u in fl if u["names"]]
    names_set = with_names[:150]
    rest = [u for u in fl if u not in names_set]
    normal_set, concat_src = rest[:400], rest[400:]
    for subset, pool in (("names", names_set), ("normal", normal_set)):
        for u in pool:
            cid = f"{subset}_{u['lang']}_{Path(u['file']).stem}"
            r = random.Random(cid)
            include = u["names"][: r.randint(1, 3)] if subset == "names" and r.random() < 0.8 else []
            group = f"fleurs:{u['lang']}:{u['sentence_id']}"
            dur = int(len(u["audio"]) / SR * 1000)
            rec = {"id": cid, "kind": "speech", "subset": subset, "lang": u["lang"], "ref_text": u["text"],
                   "names": u["names"], "names_in_vocab": include, "opus": False, "orig_id": cid,
                   "group": group, "split": split_of(group), "source": "fleurs", "file": u["file"],
                   "licence": "CC-BY-4.0", "speech_ms": [lead, lead + dur], "tail_ms": None,
                   **prompt_for(cid, u["lang"], u["text"], include)}
            emit(rec, u["audio"], opus_copy=True)

    # short replies from MSWC
    for u in ms:
        cid = f"short_{u['lang']}_{Path(u['file']).stem}"
        group = f"short:{u['source']}"
        dur = int(len(u["audio"]) / SR * 1000)
        rec = {"id": cid, "kind": "speech", "subset": "short", "lang": u["lang"], "ref_text": u["word"],
               "names": [], "names_in_vocab": [], "opus": False, "orig_id": cid, "group": group,
               "split": split_of(group), "source": u["file"].split("/")[0], "file": u["file"], "licence": "CC-BY-4.0",
               "speech_ms": [lead, lead + dur], "tail_ms": None, **prompt_for(cid, u["lang"], u["word"], [])}
        emit(rec, u["audio"], opus_copy=True)

    # non-speech
    for k, n in enumerate(noise):
        cid = f"noise_{n['category'].replace('/', '_')}_{k}"
        split = n.get("split") or split_of(n["group"])
        rec = {"id": cid, "kind": "nonspeech", "subset": n["category"], "lang": None, "ref_text": "",
               "names": [], "names_in_vocab": [], "opus": False, "orig_id": cid, "group": n["group"],
               "split": split, "source": n["source"], "file": n["file"], "licence": n["licence"],
               "speech_ms": None, "tail_ms": None, "prefiltered": n.get("prefiltered", False),
               **prompt_for(cid, None, None, [])}
        emit(rec, n["audio"])
        n["active_ms"] = vad_onset(n["audio"])

    # Speech followed by 3-10 s of non-speech (trailing-credit tests). Tails come from non-speech
    # clips that open a production segment, starting just before their first speech-like window
    # (otherwise the segmenter closes after the speech and the tail is never in the same segment).
    # Half are "beds": the tail starts 1-3 s before the speech ends, 6-12 dB under it.
    tails = {sp: [n for n in noise if n.get("active_ms") is not None
                  and (n.get("split") or split_of(n["group"])) == sp] for sp in ("mining", "test")}
    concat_specs = [("concat_fleurs", u) for u in concat_src[:180]]
    short_pool = list(ms)
    rng.shuffle(short_pool)
    concat_specs += [("concat_short", u) for u in short_pool[:90]]
    for k, (subset, u) in enumerate(concat_specs):
        if subset == "concat_fleurs":
            group, ref, names = f"fleurs:{u['lang']}:{u['sentence_id']}", u["text"], u["names"]
        else:
            group, ref, names = f"short:{u['source']}", u["word"], []
        split = split_of(group)
        n = rng.choice(tails[split])
        tail_len = int(rng.uniform(3, 10) * SR)
        onset = max(0, int((n["active_ms"] / 1000 - rng.uniform(0.0, 0.5)) * SR))
        tail = n["audio"][onset:]
        if len(tail) < tail_len:
            tail = np.tile(tail, tail_len // max(len(tail), 1) + 1)
        tail = tail[:tail_len].astype(np.float32).copy()
        bed = rng.random() < 0.5
        speech = u["audio"]
        ratio = 10 ** (-rng.uniform(6, 12) / 20) if bed else rng.uniform(0.3, 1.0)
        if active_rms(tail) > 0:
            tail *= np.float32(active_rms(speech) * ratio / active_rms(tail))
        if bed:
            overlap = min(int(rng.uniform(1.0, 3.0) * SR), len(speech))
            audio = np.concatenate([speech, np.zeros(tail_len - overlap, np.float32)])
            audio[len(speech) - overlap :] += tail
            t_start_s = (len(speech) - overlap) / SR
        else:
            gap = np.zeros(int(rng.uniform(0.0, 0.3) * SR), np.float32)
            audio = np.concatenate([speech, gap, tail]).astype(np.float32)
            t_start_s = (len(speech) + len(gap)) / SR
        peak = float(np.max(np.abs(audio)))
        if peak > 0.99:
            audio = audio * np.float32(0.99 / peak)
        s_end = lead + int(len(speech) / SR * 1000)
        t_start = lead + int(t_start_s * 1000)
        cid = f"{subset}_{u['lang']}_{k}"
        rec = {"id": cid, "kind": "concat", "subset": subset, "lang": u["lang"], "ref_text": ref,
               "names": names, "names_in_vocab": [], "opus": False, "orig_id": cid,
               "group": f"{group}|{n['group']}", "split": split, "source": f"{u['file']} + {n['file']}",
               "file": u["file"], "tail_source": n["file"], "tail_category": n["category"], "bed": bed,
               "licence": f"CC-BY-4.0 + {n['licence']}", "speech_ms": [lead, s_end],
               "tail_ms": [t_start, t_start + int(tail_len / SR * 1000)],
               **prompt_for(cid, u["lang"], ref, [])}
        emit(rec, audio)

    with (OUT / "clips.jsonl").open("w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    from collections import Counter
    print(Counter((r["kind"], r["subset"].split("/")[0], r["split"]) for r in records))
    print(f"{len(records)} clips written")


if __name__ == "__main__":
    main()
