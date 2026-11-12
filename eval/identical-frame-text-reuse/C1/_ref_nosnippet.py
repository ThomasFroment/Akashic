"""Attribution probe: today's query_frames with snippet() removed (FTS side otherwise unchanged)."""
import inspect, sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import ref_today
from ref_today import *  # noqa: F403
src = inspect.getsource(ref_today.query_frames).replace(
    '"SELECT f.id, snippet(frames_fts, -1, :open, :close, \'…\', 24)"', '"SELECT f.id, \'\'"')
assert "snippet(frames_fts" not in src
ns = dict(vars(ref_today)); exec(src, ns); query_frames = ns["query_frames"]
