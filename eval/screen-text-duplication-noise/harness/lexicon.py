"""Build lexicon/words.txt: the offline FR/EN word list behind the noise metric.

Sources (all public, nothing captured):
  1. FLEURS en_us + fr_fr dev transcripts (ref_text), in the repo at
     eval/transcript-hallucination-rejection/data/fixture/clips.jsonl (CC-BY-4.0).
  2. Whisper large-v3 byte-level BPE vocabulary (MIT): word-initial tokens (leading "G-dot" space
     marker) of >= 3 letters, from the model folder the quality runs ship with. A frequency-ranked
     list from web text; it also holds other languages' words, which only makes the metric lenient.
Harness-rendered text (sentences.json, site.py constants, run truth) is a separate class, loaded at
run time from the repo, not written here.

    uv run python eval/screen-text-duplication-noise/harness/lexicon.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from shared import REPO, fold_token, tokens  # noqa: E402

FLEURS = REPO / "eval/transcript-hallucination-rejection/data/fixture/clips.jsonl"
WHISPER = REPO / "eval/quality/local/runs/20261119-standard-s21-target-1818/store/models/faster-whisper-large-v3/vocabulary.json"
OUT = HERE / "lexicon" / "words.txt"


def bytes_to_unicode() -> dict[str, int]:
    """GPT-2 byte <-> printable unicode map, inverted: printable char -> byte."""
    bs = list(range(ord("!"), ord("~") + 1)) + list(range(ord("¡"), ord("¬") + 1)) + list(range(ord("®"), ord("ÿ") + 1))
    cs = bs[:]
    n = 0
    for b in range(256):
        if b not in bs:
            bs.append(b)
            cs.append(256 + n)
            n += 1
    return {chr(c): b for b, c in zip(bs, cs, strict=True)}


def main() -> None:
    words: dict[str, set[str]] = {}
    n_sent = 0
    for line in FLEURS.read_text(encoding="utf-8").splitlines():
        e = json.loads(line)
        if e.get("lang") in ("en", "fr") and e.get("ref_text"):
            n_sent += 1
            for t in tokens(e["ref_text"]):
                if not t.isdigit():
                    words.setdefault(fold_token(t), set()).add("fleurs")
    dec = bytes_to_unicode()
    n_tok = 0
    for tok in json.loads(WHISPER.read_text(encoding="utf-8")):
        if not tok.startswith("Ġ"):
            continue
        try:
            text = bytes(dec[c] for c in tok[1:]).decode("utf-8")
        except (KeyError, UnicodeDecodeError):
            continue
        if len(text) >= 3 and text.isalpha():
            n_tok += 1
            words.setdefault(fold_token(text), set()).add("whisper")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("".join(f"{w}\t{','.join(sorted(s))}\n" for w, s in sorted(words.items())), encoding="utf-8")
    both = sum(1 for s in words.values() if len(s) == 2)
    print(f"fleurs_sentences={n_sent} whisper_word_tokens={n_tok} words={len(words)} in_both={both} -> {OUT}")


if __name__ == "__main__":
    main()
