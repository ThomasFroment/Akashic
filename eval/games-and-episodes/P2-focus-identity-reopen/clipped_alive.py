"""Score P2 (END=alive) on the clipped+split truth of P1's post.py (truth clipped to the recorded
span, split at stored Pause gaps). Prints numbers only and writes nothing.

    uv run python eval/games-and-episodes/P2-focus-identity-reopen/clipped_alive.py
"""

import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace

os.environ["P2_END"] = "alive"
HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("post_mod", HERE.parent / "P1-lifetime-provenance-chain" / "post.py")
post = importlib.util.module_from_spec(spec)
spec.loader.exec_module(post)
ge = post.ge
mod = post.harness.load_module(HERE / "candidate.py")
mod.LAST = {}
for rid in ge.RUNS:
    run = post.load(rid)
    out = []
    for r in mod.game_rows(ge.fresh(run.shadow), run.ctx):
        r = tuple(r)
        if len(r) == 3:
            r = (r[0], ge.titles(run.ctx).get(r[0]), r[1], r[2])
        out.append(SimpleNamespace(process=r[0], title=r[1], start=r[2], end=r[3]))
    mod.LAST[rid] = out
cs = post.clipped_scoring(mod)
print(json.dumps(cs["aggregate"]))
for rid, v in cs["runs"].items():
    print(rid[-4:], v["rows_per_truth"], [round(x, 1) for x in v["m6_start_err_s"]], [round(x, 1) for x in v["m6_end_err_s"]],
          round(v["f1_video"], 2), round(v["f1_game"], 2), round(v["title_acc"], 2))
