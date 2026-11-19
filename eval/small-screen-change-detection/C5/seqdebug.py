"""C5 fix-round helper: replay chosen harness check streams with a candidate and print the
store indices (and the draft events missed). Usage: uv run python seqdebug.py <cand.py> name..."""
import sys
from pathlib import Path

HARNESS = Path(__file__).resolve().parents[1] / "harness"
sys.path.insert(0, str(HARNESS))
from hcommon import Candidate, readonly, today_reduce  # noqa: E402
from scenes import N_CHECKS, Scene, now_at, page, streams, _hms  # noqa: E402

cand = Candidate(sys.argv[1])
FROM = int(dict(a.split("=") for a in sys.argv if a.startswith("from=")).get("from", 0))
for st in streams():
    if st.name not in sys.argv[2:]:
        continue
    scene = Scene(page("4k", "dark"), {"t": today_reduce})
    state, ref, prev, stores = cand.make_state(), None, None, []
    for i in range(N_CHECKS):
        for key, item in st.ops[i]:
            scene.set(key, item)
        g = readonly(scene.render()["t"])
        if ref is None:
            ref = g
            stores.append(i)
        elif cand.decide(state, ref, g, prev, "4k", now=now_at(i)):
            ref = g
            stores.append(i)
        prev = g
    print(st.name, len(stores) - 1, "stores:", [(s, _hms(now_at(s))) for s in stores if s >= FROM])
    s = set(stores)
    for ev in st.events:
        hit = [t for t in range(ev["t"], min(ev["end"], ev["t"] + 7)) if t in s]
        if not hit:
            print("  missed", ev)
