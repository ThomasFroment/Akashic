"""Runs inside the Windows Terminal window opened by run_live.py: prints a synthetic build log and a
prompt, then holds still while the cursor blinks (phases: idle = cursor hidden, bar = blinking bar
DECSCUSR 5, block = blinking block DECSCUSR 1). Posts phase changes to the local log server."""

import json
import sys
import time
import urllib.request

port, plan, delay = int(sys.argv[1]), sys.argv[2], float(sys.argv[3])


def log(ev: dict) -> None:
    ev["t"] = int(time.time() * 1000)
    req = urllib.request.Request(f"http://127.0.0.1:{port}/log", data=json.dumps(ev).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        urllib.request.urlopen(req, timeout=2).read()  # noqa: S310 - 127.0.0.1 only
    except OSError:
        pass


w = sys.stdout.write
w("\x1b[2J\x1b[H\x1b[?25l")
for i in range(18):
    w(f"   Compiling demo-crate-{i:02d} v0.{i}.3 (C:\\demo\\crates\\demo-{i:02d})\n")
w("    Finished `dev` profile [unoptimized + debuginfo] target(s) in 12.34s\n")
w("     Running `target\\debug\\demo.exe`\n\nPS C:\\demo> ")
sys.stdout.flush()
log({"kind": "load"})
time.sleep(delay)
SHAPE = {"idle": None, "bar": "\x1b[5 q", "block": "\x1b[1 q"}
for item in plan.split(","):
    name, dur = item.split(":")
    shape = SHAPE[name]
    w("\x1b[?25l" if shape is None else shape + "\x1b[?25h")
    sys.stdout.flush()
    log({"kind": "phase", "name": name, "dur": int(dur)})
    time.sleep(int(dur))
log({"kind": "done"})
