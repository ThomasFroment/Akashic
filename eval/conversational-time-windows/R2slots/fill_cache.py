"""Run the real local model on every question of a phrases file and cache its plan JSON.

    python R2slots/fill_cache.py <phrases_file> [--baseline]

Needs a running llama-server (start_server.sh: 127.0.0.1, port.txt, key.txt). The request is built
by the app's own ``akashic.pipeline.llm.LlamaChat.complete_json`` (temperature 0, json_schema
strict, thinking off, max_tokens 400), against this prototype's prompt (plan_prompt.py). With
``--baseline`` it sends the app's unchanged planner prompt/schema instead (to measure the added
output tokens and latency). Output, appended, one line per question:

    R2slots/model_cache_<stem>.jsonl     {question, prompt_sha, raw, ms, completion_tokens, ...}
    R2slots/baseline_cache_<stem>.jsonl  (--baseline)

Questions already cached under the same prompt_sha are skipped.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
SRC = HERE.parents[2] / "src"
for p in (str(SRC), str(HERE)):
    if p not in sys.path:
        sys.path.insert(0, p)

import plan_prompt as pp  # noqa: E402

NAMES = ("Minlo", "Sacha", "Léa")
MODEL = "gemma-4-12b-qat-q4"


prompt_sha = pp.prompt_sha
PROMPT_SHA = pp.PROMPT_SHA


class _Server:
    """Stands in for akashic.pipeline.llm.LlamaServer against an already-running server, so the
    app's own LlamaChat.complete_json builds the request. Records usage and latency."""

    def __init__(self, base_url: str, key: str) -> None:
        self.base_url, self.key = base_url, key
        self.last: dict[str, Any] = {}

    def available(self) -> bool:
        return True

    def request(self, path: str, body: dict[str, object] | None = None) -> object:
        from akashic.pipeline.llm import http_transport  # noqa: PLC0415

        t = time.perf_counter()
        answer = http_transport("POST", f"{self.base_url}{path}", self.key, body)
        usage = answer.get("usage") if isinstance(answer, dict) else None
        self.last = {
            "ms": round((time.perf_counter() - t) * 1000, 1),
            "completion_tokens": (usage or {}).get("completion_tokens"),
            "prompt_tokens": (usage or {}).get("prompt_tokens"),
        }
        return answer


def main() -> None:
    from akashic.pipeline.llm import LlamaChat  # noqa: PLC0415

    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    baseline = "--baseline" in sys.argv
    phrases = Path(args[0])
    if not phrases.exists():
        phrases = HERE.parent / args[0]
    if baseline:
        from akashic.search import planner  # noqa: PLC0415

        system_of = lambda q: planner.plan_prompt(q, NAMES)  # noqa: E731
        schema, sha = planner.PLAN_SCHEMA, prompt_sha(planner.PLAN_SYSTEM, planner.PLAN_SCHEMA)
        out = HERE / f"baseline_cache_{phrases.stem}.jsonl"
    else:
        system_of = lambda q: pp.plan_prompt(q, NAMES)  # noqa: E731
        schema, sha = pp.PLAN_SCHEMA, PROMPT_SHA
        out = HERE / f"model_cache_{phrases.stem}.jsonl"
    done: set[str] = set()
    if out.exists():
        for line in out.read_text(encoding="utf-8").splitlines():
            rec = json.loads(line)
            if rec["prompt_sha"] == sha:
                done.add(rec["question"])
    questions = list(dict.fromkeys(
        json.loads(line)["question"] for line in phrases.read_text(encoding="utf-8").splitlines()
    ))  # fmt: skip
    port = (HERE / "port.txt").read_text().strip()
    server = _Server(f"http://127.0.0.1:{port}", (HERE / "key.txt").read_text().strip())
    chat = LlamaChat(server, MODEL)  # type: ignore[arg-type]
    chat.complete_json(*system_of("warm up"), schema)  # load + warm the prompt cache
    todo = [q for q in questions if q not in done]
    print(f"{len(todo)} to run ({len(done)} cached) -> {out.name}", flush=True)
    with out.open("a", encoding="utf-8") as fh:
        for i, q in enumerate(todo):
            try:
                raw, err = chat.complete_json(*system_of(q), schema), None
            except Exception as exc:  # noqa: BLE001 - recorded; the merge falls back to rules
                raw, err = "", f"{type(exc).__name__}: {exc}"
            rec = {"question": q, "prompt_sha": sha, "raw": raw, "error": err, **server.last}
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            fh.flush()
            if i % 25 == 0:
                print(i, rec["ms"], rec["completion_tokens"], flush=True)


if __name__ == "__main__":
    main()
