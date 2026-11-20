"""Muted tone rendered by the base interpreter itself (no venv launcher), captured by its PID;
also a two-tone separation with endpoint independence. Numbers only."""
import json, subprocess, sys, time, ctypes, os
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
from spike import Cap, tone_stats, run_caps, cpu_s
BASE = sys._base_executable
ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x4000)
env = dict(os.environ, PYTHONPATH=os.path.join(sys.prefix, "Lib", "site-packages"))
def child(freq, secs, mode, ep=-1):
    return subprocess.Popen([BASE, os.path.join(HERE, "tone.py"), str(freq), str(secs), mode, str(ep)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, creationflags=0x4000)
out = {}
for mode in ("quiet", "mute"):
    p = child(440, 3, mode); line = p.stdout.readline()
    if not line:
        out[mode] = {"child_failed": p.stderr.read().decode()[-200:]}; continue
    c = Cap(p.pid); run_caps([c], 2.0); out[mode] = tone_stats(c.signal()); c.close(); p.wait()
print(json.dumps(out))
