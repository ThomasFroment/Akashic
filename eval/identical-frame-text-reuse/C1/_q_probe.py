import sys, pathlib, shutil
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import harness, fixtures
here = pathlib.Path(__file__).parent; work = here / "_work_q"; work.mkdir(exist_ok=True)
m = harness.load(here / "_ref_nosnippet.py")
q = harness.phase_queries(m, work, fixtures.build(), False)
print(f"today_nosnippet p50={q['q_p50_ms']:.1f} p95={q['q_p95_ms']:.1f} | today p50={q['q_today_p50_ms']:.1f} p95={q['q_today_p95_ms']:.1f}")
shutil.rmtree(work, ignore_errors=True)
