"""Download the public data for the hallucination-rejection fixture into data/raw/.

Run:  .venv\\Scripts\\python.exe -I fetch_data.py [part ...]
Parts: model fleurs mswc esc50 fsd50k demand oga  (default: all). Idempotent: finished parts
leave a DONE marker. Nothing here touches the Akashic data directory.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import random
import sys
import tarfile
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
RAW = HERE / "data" / "raw"
MODELS = HERE / "data" / "models"
HF = "https://huggingface.co/datasets"
UA = {"User-Agent": "akashic-invent-fixture/1.0"}


def get(url: str, timeout: float = 60.0):
    for attempt in range(5):
        try:
            return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            print(f"  retry {attempt + 1} {url}: {type(exc).__name__} {exc}", flush=True)
            time.sleep(3 * (attempt + 1))
    raise RuntimeError(url)


def download(url: str, dest: Path, sha256: str | None = None) -> Path:
    if dest.is_file() and dest.stat().st_size > 0:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    h = hashlib.sha256()
    with get(url) as resp, tmp.open("wb") as out:
        while chunk := resp.read(1 << 20):
            out.write(chunk)
            h.update(chunk)
    if sha256 and h.hexdigest() != sha256:
        tmp.unlink()
        raise RuntimeError(f"checksum mismatch for {url}")
    tmp.replace(dest)
    print(f"  {dest.name}: {dest.stat().st_size / 1e6:.1f} MB", flush=True)
    return dest


def done(part: str) -> Path:
    return RAW / part / "DONE"


# --- Whisper large-v3 (the shipped default; same URL, commit and checksums as pipeline/models.py)
SYSTRAN = "https://huggingface.co/Systran/faster-whisper-large-v3/resolve/edaa852ec7e145841d8ffdb056a99866b5f0a478"
MODEL_FILES = {
    "config.json": "a9306624f5ec14270a014b647e5c316b6e03a662c369758d1b90697a7b0655b9",
    "preprocessor_config.json": "7ccc62c6f2765af1f3b46c00c9b5894426835a05021c8b9c01eecb6dfb542711",
    "tokenizer.json": "6d8cbd7cd0d8d5815e478dac67b85a26bbe77c1f5e0c6d76d1ce2abc0e5f21ca",
    "vocabulary.json": "c69260f2ab26d659b7c398f9a2b2b48ed0df16c3b47d7326782fd9cba71690c1",
    "model.bin": "69f74147e3334731bc3a76048724833325d2ec74642fb52620eda87352e3d4f1",
}


def fetch_model() -> None:
    folder = MODELS / "faster-whisper-large-v3"
    for name, digest in MODEL_FILES.items():
        download(f"{SYSTRAN}/{name}", folder / name, digest)


# --- FLEURS (google/fleurs, CC-BY-4.0): dev split of fr_fr and en_us, read Wikipedia sentences
def fetch_fleurs() -> None:
    if done("fleurs").exists():
        return
    for lang in ("fr_fr", "en_us"):
        for split in ("dev",):
            download(f"{HF}/google/fleurs/resolve/main/data/{lang}/{split}.tsv",
                     RAW / "fleurs" / lang / f"{split}.tsv")
            download(f"{HF}/google/fleurs/resolve/main/data/{lang}/audio/{split}.tar.gz",
                     RAW / "fleurs" / lang / f"{split}.tar.gz")
    done("fleurs").write_text("ok")


# --- Multilingual Spoken Words (MLCommons/ml_spoken_words, CC-BY-4.0): 1 s single-word clips cut
# from Common Voice sentences. Shards are alphabetical by word; only the shards holding the reply
# words are streamed, and only those words are kept.
MSWC_WORDS = {
    "fr": {"oui", "non", "ouais", "merci", "ok", "okay", "nan", "bon", "bah", "ah", "allez",
           "alors", "bien", "voilà", "vrai", "pardon", "parfait", "salut", "super", "bonjour",
           "exactement", "carrément"},
    "en": {"yes", "yeah", "yep", "no", "nope", "okay", "ok", "thanks", "sure", "sorry", "right",
           "hello", "hey", "hi", "nice", "please", "really", "wow", "oh", "well", "cool",
           "great", "fine"},
}
MSWC_SHARDS = {
    "fr": [("test", 0), ("test", 2), ("test", 3), ("test", 4), ("dev", 2)],
    "en": [("test", 1), ("test", 3), ("test", 4), ("test", 6), ("test", 7), ("test", 8),
           ("test", 11), ("dev", 11)],
}
MSWC_CAP = 70  # clips kept per word and language


def fetch_mswc() -> None:
    if done("mswc").exists():
        return
    for lang, shards in MSWC_SHARDS.items():
        out = RAW / "mswc" / lang
        out.mkdir(parents=True, exist_ok=True)
        counts: dict[str, int] = {}
        for p in out.glob("*.opus"):
            w = p.name.split("_common_voice_")[0]
            counts[w] = counts.get(w, 0) + 1
        for split, shard in shards:
            marker = out / f".{split}{shard}"
            if marker.exists():
                continue
            url = f"{HF}/MLCommons/ml_spoken_words/resolve/main/data/opus/{lang}/{split}/audio/{shard}.tar.gz"
            print(f"  streaming {url}", flush=True)
            with get(url, timeout=120) as resp, tarfile.open(fileobj=resp, mode="r|gz") as tar:
                for m in tar:
                    if not m.isfile() or "_common_voice_" not in m.name:
                        continue
                    name = Path(m.name).name
                    word = name.split("_common_voice_")[0]
                    if word in MSWC_WORDS[lang] and counts.get(word, 0) < MSWC_CAP:
                        f = tar.extractfile(m)
                        if f is None:
                            continue
                        (out / f"{split}__{name}").write_bytes(f.read())
                        counts[word] = counts.get(word, 0) + 1
            marker.write_text("ok")
            print(f"  {lang} {split}/{shard}: {dict(sorted(counts.items()))}", flush=True)
    done("mswc").write_text("ok")


# --- ESC-50 (CC BY-NC 3.0; personal, non-commercial use only): 2000 x 5 s environmental clips
def fetch_esc50() -> None:
    if done("esc50").exists():
        return
    download("https://codeload.github.com/karolpiczak/ESC-50/zip/refs/heads/master",
             RAW / "esc50" / "ESC-50-master.zip")
    done("esc50").write_text("ok")


# --- FSD50K eval set (Fhrozen/FSD50k mirror of Zenodo 4060432; per-clip CC0/CC-BY/CC-BY-NC/
# Sampling+). Clips with any human-voice label are excluded.
VOICE = ("speech", "singing", "voice", "conversation", "chatter", "crowd", "whispering",
         "laughter", "giggle", "chuckle", "screaming", "shout", "yell", "choir", "rap",
         "cheering", "applause", "children", "babbl", "human_group", "speech_synth", "humming",
         "male", "female", "child", "sigh", "burping", "cough", "sneeze", "breathing", "gasp",
         "crying", "baby")
FSD_GROUPS = {
    "music": ("music", "guitar", "piano", "drum", "bass", "synthesizer", "keyboard_(musical)",
              "organ", "brass", "bowed_string", "percussion", "cymbal", "plucked", "harp",
              "trumpet", "violin", "marimba", "glockenspiel", "mallet", "wind_instrument",
              "accordion", "tabla", "rattle_(instrument)", "gong", "chime", "bell", "harmonica"),
    "game": ("gunshot", "explosion", "fireworks", "boom", "engine", "vehicle", "motorcycle",
             "race_car", "aircraft", "car", "truck", "shatter", "glass", "crushing", "hammer",
             "alarm", "siren", "beep", "power_tool", "drill", "thunder", "slam", "crack",
             "knock", "tick", "zipper", "whoosh", "skateboard", "bicycle", "train"),
    "desk": ("computer_keyboard", "typing", "mechanical_fan", "printer", "scissors", "writing",
             "keys_jangling", "cupboard", "drawer", "door", "packing_tape", "microwave",
             "dishes", "cutlery", "frying", "boiling", "water_tap", "toilet", "pour"),
}
FSD_CAP = {"music": 260, "game": 220, "desk": 120, "other": 120}


def fetch_fsd50k() -> None:
    if done("fsd50k").exists():
        return
    base = f"{HF}/Fhrozen/FSD50k/resolve/main"
    out = RAW / "fsd50k"
    download(f"{base}/labels/eval.csv", out / "eval.csv")
    download(f"{base}/metadata/eval_clips_info_FSD50K.json", out / "eval_clips_info_FSD50K.json")
    download(f"{base}/LICENSE-DATASET.md", out / "LICENSE-DATASET.md")
    rows = list(csv.DictReader((out / "eval.csv").open(encoding="utf-8")))
    rng = random.Random(1234)
    rng.shuffle(rows)
    chosen: dict[str, list[str]] = {g: [] for g in FSD_CAP}
    for row in rows:
        labels = row["labels"].lower().split(",")
        if any(v in lab for lab in labels for v in VOICE):
            continue
        group = next((g for g, keys in FSD_GROUPS.items()
                      if any(k in lab for lab in labels for k in keys)), "other")
        if len(chosen[group]) < FSD_CAP[group]:
            chosen[group].append(row["fname"])
    (out / "chosen.json").write_text(json.dumps(chosen, indent=1))
    for group, names in chosen.items():
        for name in names:
            download(f"{base}/clips/eval/{name}.wav", out / "clips" / f"{name}.wav")
    done("fsd50k").write_text("ok")


# --- DEMAND (Zenodo 1227121, CC-BY-4.0): 5 min multichannel recordings of non-speech environments
DEMAND_ENVS = ("DKITCHEN", "DWASHING", "NFIELD", "STRAFFIC", "TCAR")


def fetch_demand() -> None:
    if done("demand").exists():
        return
    for env in DEMAND_ENVS:
        download(f"https://zenodo.org/records/1227121/files/{env}_16k.zip?download=1",
                 RAW / "demand" / f"{env}_16k.zip")
    done("demand").write_text("ok")


# --- OpenGameArt CC0 packs: retro/game SFX and chiptune game music
OGA = {
    "512_retro_sfx.zip": "https://opengameart.org/sites/default/files/The%20Essential%20Retro%20Video%20Game%20Sound%20Effects%20Collection%20%5B512%20sounds%5D.zip",
    "100_cc0_sfx.zip": "https://opengameart.org/sites/default/files/100-CC0-SFX_0.zip",
    "80_cc0_creature_sfx.zip": "https://opengameart.org/sites/default/files/80-CC0-creature-SFX_0.zip",
    "50_cc0_retro_synth_sfx.zip": "https://opengameart.org/sites/default/files/50-CC0-retro-synth-SFX.zip",
    "25_cc0_bang_sfx.zip": "https://opengameart.org/sites/default/files/25-CC0-bang-sfx.zip",
    "100_cc0_wood_metal_sfx.zip": "https://opengameart.org/sites/default/files/100-CC0-wood-metal-SFX.zip",
    "5_action_chiptunes.zip": "https://opengameart.org/sites/default/files/5%20Action%20Chiptunes%20By%20Juhani%20Junkala.zip",
    "chiptune_adventures_ogg.zip": "https://opengameart.org/sites/default/files/Juhani%20Junkala%20%5BChiptune%20Adventures%5D%20OGG.zip",
}


def fetch_oga() -> None:
    if done("oga").exists():
        return
    for name, url in OGA.items():
        download(url, RAW / "oga" / name)
    done("oga").write_text("ok")


# --- Google Speech Commands v0.02 test set (CC-BY-4.0): standalone 1 s "yes"/"no" recordings.
# MSWC drops words shorter than three letters, so English "no" comes from here.
SC_WORDS = ("no", "yes")
SC_CAP = 70


def fetch_speechcommands() -> None:
    if done("speechcommands").exists():
        return
    out = RAW / "speechcommands"
    out.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}
    url = "http://download.tensorflow.org/data/speech_commands_test_set_v0.02.tar.gz"
    with get(url, timeout=120) as resp, tarfile.open(fileobj=resp, mode="r|gz") as tar:
        for m in tar:
            parts = Path(m.name).parts
            if m.isfile() and len(parts) >= 2 and parts[-2] in SC_WORDS and m.name.endswith(".wav"):
                word = parts[-2]
                if counts.get(word, 0) < SC_CAP:
                    (out / f"{word}__{parts[-1]}").write_bytes(tar.extractfile(m).read())
                    counts[word] = counts.get(word, 0) + 1
    print(f"  speech commands: {counts}", flush=True)
    done("speechcommands").write_text("ok")


# --- FSD50K dev, VAD-prefiltered: only Silero + the production segmenter decide which non-speech
# reaches Whisper, and few clips do (about 3%), so dev clips are streamed, levelled exactly as the
# fixture levels them, run through the production capture, and kept (16 kHz int16) only when they
# open a segment. Raw wav files are not stored.
FSD_DEV_KEEP = 330
FSD_DEV_TRY = 9000


def fetch_fsd50k_dev() -> None:
    if done("fsd50k_dev").exists():
        return
    from concurrent.futures import ThreadPoolExecutor

    import numpy as np

    sys.path[:0] = [str(HERE), str(HERE.parents[1] / "src")]
    import build_fixture as bf
    import prod

    base = f"{HF}/Fhrozen/FSD50k/resolve/main"
    out = RAW / "fsd50k_dev"
    out.mkdir(parents=True, exist_ok=True)
    download(f"{base}/labels/dev.csv", out / "dev.csv")
    download(f"{base}/metadata/dev_clips_info_FSD50K.json", out / "dev_clips_info_FSD50K.json")
    info = json.loads((out / "dev_clips_info_FSD50K.json").read_text(encoding="utf-8"))
    rows = list(csv.DictReader((out / "dev.csv").open(encoding="utf-8")))
    random.Random(4321).shuffle(rows)
    todo = []
    for row in rows:
        labels = row["labels"].lower().split(",")
        if any(v in lab for lab in labels for v in VOICE):
            continue
        group = next((g for g, keys in FSD_GROUPS.items()
                      if any(k in lab for lab in labels for k in keys)), "other")
        todo.append((row["fname"], group))
    todo = todo[:FSD_DEV_TRY]
    capture = prod.Capture()
    kept: dict[str, dict] = {}

    def fetch(item):
        name, _group = item
        try:
            with get(f"{base}/clips/dev/{name}.wav") as resp:
                return item, resp.read()
        except Exception:  # noqa: BLE001
            return item, None

    tried = 0
    def batches():
        with ThreadPoolExecutor(12) as pool:  # 240 at a time, so stopping early stops downloading
            for i in range(0, len(todo), 240):
                if len(kept) >= FSD_DEV_KEEP:
                    return
                yield from pool.map(fetch, todo[i : i + 240])

    if True:
        for (name, group), data in batches():
            tried += 1
            if data is None or len(kept) >= FSD_DEV_KEEP:
                continue
            audio = bf.decode(data)[: int(bf.MAX_NONSPEECH_S * bf.SR)]
            if len(audio) < bf.SR:
                continue
            levelled = bf.set_level(audio, f"fsd50k:{name}fsd50k/dev/{name}.wav", bf.NONSPEECH_LEVEL)
            _p, utts, _d = capture.run(bf.pad(levelled))
            if utts:
                np.save(out / f"{name}.npy", np.round(np.clip(audio, -1, 1) * 32767).astype(np.int16))
                kept[name] = {"group": group, "license": info.get(name, {}).get("license", "?")}
            if tried % 500 == 0:
                print(f"  tried {tried}, kept {len(kept)}", flush=True)
            if len(kept) >= FSD_DEV_KEEP:
                break
    (out / "kept.json").write_text(json.dumps(kept, indent=1))
    print(f"  fsd50k dev: tried {tried}, kept {len(kept)}", flush=True)
    done("fsd50k_dev").write_text("ok")


PARTS = {"model": fetch_model, "speechcommands": fetch_speechcommands, "fleurs": fetch_fleurs, "mswc": fetch_mswc, "esc50": fetch_esc50,
         "fsd50k": fetch_fsd50k, "fsd50k_dev": fetch_fsd50k_dev, "demand": fetch_demand, "oga": fetch_oga}

if __name__ == "__main__":
    for part in sys.argv[1:] or list(PARTS):
        print(f"[{part}]", flush=True)
        PARTS[part]()
    print("finished", flush=True)
