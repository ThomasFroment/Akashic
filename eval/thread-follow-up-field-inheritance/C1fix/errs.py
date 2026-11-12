import json
import sys
from pathlib import Path

d = json.loads((Path(__file__).parent / "details.json").read_text("utf-8"))
print(d["metrics"])
for e in d["candidate"]["errors"]:
    print(f"[{e['id']}] {e['prev']!r} -> {e['question']!r}")
    for f, v in e["wrong"].items():
        print(f"    {f:8} gold={v['gold']:7} got={v['got']:7} S={v['standalone']} E={v['earlier']} C={v['candidate']}")
