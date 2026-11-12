import json, sys
for f, n in (("calls_pairs.jsonl", 170), ("calls_heldout2.jsonl", 110)):
    rows = [json.loads(l) for l in open(f, encoding="utf-8")]
    ms = sorted(r["ms"] for r in rows)
    bad = sum(r["result"] is None for r in rows)
    ct = [r["usage"]["completion_tokens"] for r in rows]; pt = [r["usage"]["prompt_tokens"] for r in rows]
    cached = [r["usage"].get("prompt_tokens_details", {}).get("cached_tokens", 0) for r in rows]
    print(f, f"routed={len(rows)}/{n}={len(rows)/n:.2f} invalid={bad} p50={ms[len(ms)//2]:.0f} p95={ms[int(.95*len(ms))]:.0f} max={ms[-1]:.0f} out_tok={min(ct)}-{max(ct)} prompt_tok~{sorted(pt)[len(pt)//2]} cached_med={sorted(cached)[len(cached)//2]}")
