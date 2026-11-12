"""Errors per run, routing share, call latency (warm) and token usage."""
import json, statistics, sys
from pathlib import Path
tag = sys.argv[1]
for name, sub, calls in [("pairs", "llm", f"calls_{tag}_pairs.jsonl"), ("heldout2", "heldout2_llm", f"calls_{tag}_heldout2.jsonl")]:
    d = json.loads(Path(sub, "details.json").read_text("utf-8"))
    rows = d["candidate"]["rows"]
    cl = [json.loads(l) for l in Path(calls).read_text("utf-8").splitlines() if l.strip()]
    routed = {(c["prev"], c["question"]) for c in cl}
    print(f"== {name}: {d['metrics'][:160]}")
    print(f"   routed {len(routed)}/{len(rows)} = {len(routed)/len(rows):.3f}; calls {len(cl)}; fallbacks {sum(c['result'] is None for c in cl)}")
    ms = sorted(c["ms"] for c in cl[1:])
    if ms:
        q = lambda p: ms[min(len(ms)-1, int(p*len(ms)))]
        print(f"   latency ms (warm, excl. first) p50={statistics.median(ms):.0f} p95={q(0.95):.0f} max={ms[-1]:.0f}")
        pt = [c["usage"]["prompt_tokens"] for c in cl if c.get("usage")]
        ct = [c["usage"]["completion_tokens"] for c in cl if c.get("usage")]
        cached = [c["timings"].get("cache_n") for c in cl if c.get("timings")]
        print(f"   prompt_tokens p50={statistics.median(pt)} completion p50={statistics.median(ct)} cache_n p50={statistics.median([x for x in cached if x is not None] or [0])}")
    byq = {(c["prev"], c["question"]): c for c in cl}
    for e in d["candidate"]["errors"]:
        c = byq.get((e["prev"], e["question"]))
        print(f"  {e['id']} [{'LLM '+str(c['raw']) if c else 'R0'}] {e['prev']} -> {e['question']}")
        for f, v in e["wrong"].items():
            print(f"      {f}: gold {v['gold']} got {v['got']} S={v['standalone']} E={v['earlier']} C={v['candidate']}")
