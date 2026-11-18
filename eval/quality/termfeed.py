"""Terminal line feed for the Terminal case: prints a new line every few seconds and logs it.

    python termfeed.py LOG.jsonl SEED
"""

from __future__ import annotations

import json
import random
import sys
import time

from common import ASSETS, JsonlWriter

PERIOD_S = 4


def main() -> None:
    log, seed = JsonlWriter(sys.argv[1]), sys.argv[2]
    sentences = json.loads((ASSETS / "sentences.json").read_text(encoding="utf-8"))
    rng = random.Random(f"{seed}/terminal")
    sys.stdout.write("\x1b]0;quality termfeed\x07")
    n = 0
    while True:
        n += 1
        text = f"[{n:04d}] {rng.choice(sentences[rng.choice(('en', 'fr', 'mixed'))])}"
        print(text, flush=True)
        log.write(type="line", id=f"T{n}", text=text)
        time.sleep(PERIOD_S)


if __name__ == "__main__":
    main()
