"""Latency and output tokens of the plan call: this prototype's prompt vs the app's unchanged one.

    python R2slots/latency.py <phrases_file> [<phrases_file> ...]

Reads model_cache_<stem>.jsonl and baseline_cache_<stem>.jsonl (fill_cache.py, --baseline), only
for the current prompt sha, and prints p50/p95 of call latency and completion tokens, plus the
per-question added output tokens (paired by question).
"""

from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import plan_prompt as pp  # noqa: E402


def pct(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(p * len(xs)))]


def load(path: Path, sha: str | None) -> dict[str, dict]:
    out = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            r = json.loads(line)
            if (sha is None or r["prompt_sha"] == sha) and not r.get("error"):
                out[r["question"]] = r
    return out


def main() -> None:
    new: dict[str, dict] = {}
    base: dict[str, dict] = {}
    for a in sys.argv[1:]:
        stem = Path(a).stem
        new |= load(HERE / f"model_cache_{stem}.jsonl", pp.PROMPT_SHA)
        base |= load(HERE / f"baseline_cache_{stem}.jsonl", None)
    for name, d in (("r2slots", new), ("app_baseline", base)):
        if not d:
            continue
        ms = [r["ms"] for r in d.values()]
        tok = [r["completion_tokens"] for r in d.values()]
        print(f"{name}: n={len(d)} latency_ms p50={statistics.median(ms):.0f} p95={pct(ms, .95):.0f}"
              f" out_tokens p50={statistics.median(tok):.0f} p95={pct(tok, .95):.0f}"
              f" prompt_tokens p50={statistics.median(r['prompt_tokens'] for r in d.values()):.0f}")  # fmt: skip
    both = [q for q in new if q in base]
    if both:
        dt = [new[q]["completion_tokens"] - base[q]["completion_tokens"] for q in both]
        dms = [new[q]["ms"] - base[q]["ms"] for q in both]
        print(f"paired n={len(both)} added_out_tokens p50={statistics.median(dt):.0f} "
              f"p95={pct(dt, .95):.0f} mean={statistics.mean(dt):.1f} added_ms p50="
              f"{statistics.median(dms):.0f} p95={pct(dms, .95):.0f}")  # fmt: skip


if __name__ == "__main__":
    main()
