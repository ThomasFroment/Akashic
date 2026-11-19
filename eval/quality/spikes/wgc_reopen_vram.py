"""Does opening and closing Windows.Graphics.Capture captures leak VRAM in Akashic's capture code?

In the day run the capture process's VRAM rose from 0.7 to 4.5 GB, about 10 times faster within
30 s of a game-mode switch (when per-window captures are stopped and restarted) than elsewhere.
This spike drives `akashic.platform.win.wgc.WgcCaptures` directly on an existing window that
changes nothing on screen (the desktop's `Progman` window, as wide as all monitors): start, wait,
stop; N times per variant, each variant in a fresh process. It samples this process's dedicated
VRAM (PDH, as the harness's G2) after the cycles and after `close()`. Nothing is shown, focused
or stored.

Variants: `grab` (wait for a frame and grab it, as capture does), `nowait` (stop before any frame
arrives), `nobitmap` (frames arrive but are only closed, never copied to a SoftwareBitmap),
`session_first` (close the session before the pool), `gc` (gc.collect() after each stop).

    uv run python eval/quality/spikes/wgc_reopen_vram.py <out.json> [cycles]
"""

import contextlib
import gc
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import winapi  # noqa: E402

VARIANTS = ("grab", "nowait", "nobitmap", "session_first", "gc")


def one(mode: str, cycles: int) -> dict:
    from akashic.platform.win import wgc

    winapi.set_dpi_aware()
    hwnd = winapi.user32.FindWindowW("Progman", None)
    gpu = winapi.GpuCounters()
    pid = os.getpid()
    vram = lambda: round(gpu.sample()[0].get(pid, 0.0) / 2**20)
    if mode == "nobitmap":
        def store(self, cap, arrived):  # noqa: ARG001
            f = cap.pool.try_get_next_frame()
            if f is not None:
                f.close()
        wgc.WgcCaptures._store_frame = store
    if mode == "session_first":
        def close_session_first(cap):
            for step in (lambda: cap.item.remove_closed(cap.closed_token), lambda: cap.pool.remove_frame_arrived(cap.frame_token),
                         lambda: cap.session.close(), lambda: cap.pool.close()):
                with contextlib.suppress(Exception):
                    step()
        wgc._close_native = close_session_first
    caps = wgc.WgcCaptures()
    base = vram()
    size = None
    for _ in range(cycles):
        caps.start(hwnd, 0)
        if mode == "nowait":
            pass
        elif mode == "grab":
            deadline = time.monotonic() + 3
            frame = None
            while frame is None and time.monotonic() < deadline:
                time.sleep(0.05)
                frame = caps.grab(hwnd)
            size = frame.image.size if frame is not None else size
        else:
            time.sleep(1.0)
        caps.stop(hwnd)
        if mode == "gc":
            gc.collect()
        time.sleep(0.2)
    after = vram()
    caps.close()
    time.sleep(1)
    return {"mode": mode, "cycles": cycles, "frame": size, "base_mib": base, "after_cycles_mib": after,
            "after_close_mib": vram(), "per_cycle_mib": round((after - base) / cycles, 1)}


def main() -> None:
    if sys.argv[1] == "--one":
        print(json.dumps(one(sys.argv[2], int(sys.argv[3]))))
        return
    out = Path(sys.argv[1])
    cycles = int(sys.argv[2]) if len(sys.argv) > 2 else 10
    rows = []
    for mode in VARIANTS:
        res = subprocess.run([sys.executable, __file__, "--one", mode, str(cycles)], capture_output=True, text=True, timeout=600)
        line = next((ln for ln in res.stdout.splitlines() if ln.startswith("{")), None)
        rows.append(json.loads(line) if line else {"mode": mode, "error": res.stderr[-500:]})
        print(rows[-1], flush=True)
    out.write_text(json.dumps({"window": "Progman (all monitors)", "variants": rows}, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
