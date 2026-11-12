"""Interleaved A/B latency of the plan call: app prompt (A) vs R2slots prompt (B), same questions.

    python R2slots/latency_ab.py <phrases_file> [n]

Every 4th question of the file up to n (default 40); per question both calls, order alternating,
so GPU contention (other processes) hits both alike. Writes latency_ab_<stem>.jsonl and prints
p50/p95 of each and of the paired difference.
"""

from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import fill_cache as fc  # noqa: E402
import plan_prompt as pp  # noqa: E402


def pct(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(p * len(xs)))]


def main() -> None:
    from akashic.pipeline.llm import LlamaChat  # noqa: PLC0415
    from akashic.search import planner  # noqa: PLC0415

    path = Path(sys.argv[1])
    if not path.exists():
        path = HERE.parent / sys.argv[1]
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 40
    qs = [json.loads(x)["question"] for x in path.read_text(encoding="utf-8").splitlines()][::4][:n]
    port = (HERE / "port.txt").read_text().strip()
    server = fc._Server(f"http://127.0.0.1:{port}", (HERE / "key.txt").read_text().strip())  # noqa: SLF001
    chat = LlamaChat(server, fc.MODEL)  # type: ignore[arg-type]
    calls = {
        "A": lambda q: chat.complete_json(*planner.plan_prompt(q, fc.NAMES), planner.PLAN_SCHEMA),
        "B": lambda q: chat.complete_json(*pp.plan_prompt(q, fc.NAMES), pp.PLAN_SCHEMA),
    }
    for k in "AB":
        calls[k]("warm up")
    rows = []
    for i, q in enumerate(qs):
        rec: dict = {"question": q}
        for k in ("AB" if i % 2 == 0 else "BA"):
            calls[k](q)
            rec[k] = dict(server.last)
        rows.append(rec)
    out = HERE / f"latency_ab_{path.stem}.jsonl"
    out.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), "utf-8")
    for k in "AB":
        ms = [r[k]["ms"] for r in rows]
        tok = [r[k]["completion_tokens"] for r in rows]
        print(f"{k}: n={len(rows)} ms p50={statistics.median(ms):.0f} p95={pct(ms, .95):.0f} "
              f"tokens p50={statistics.median(tok):.0f}")  # fmt: skip
    d = [r["B"]["ms"] - r["A"]["ms"] for r in rows]
    dt = [r["B"]["completion_tokens"] - r["A"]["completion_tokens"] for r in rows]
    print(f"B-A: ms p50={statistics.median(d):.0f} p95={pct(d, .95):.0f} "
          f"tokens p50={statistics.median(dt):.0f} p95={pct(dt, .95):.0f}")  # fmt: skip


if __name__ == "__main__":
    main()
