"""Live capture for the #208 check: a synthetic chat page (chat.html) in real Edge or Chrome, or a
synthetic Windows Terminal (term_child.py), on the RIGHT monitor, captured like production.

    .venv\\Scripts\\python.exe live\\run_live.py edge|chrome|term <outdir> [--plan ...] [--seed N]

Per check (every 5 s): the foreground is recorded and, when it is not our window, our window is
re-focused (the check is flagged; the analysis drops it). The latest frame of
akashic.platform.win.wgc.WgcCaptures (min interval 1000 ms, as desk.py) is composed onto a black
2560x1440 canvas at the window's place on the monitor (compose.py), reduced exactly as
imaging.signature (Image.reduce(4) -> L) and as C3's 4x2 reduction, and the reduced frames are
appended (zlib) to <outdir>/frames.bin. A second WGC session on the same window runs with
dirty_region_mode REPORT_ONLY and logs, per check, the dirty rects of the frames since the
previous check (numbers only). The page posts its own event log to a local HTTP server on
127.0.0.1 (events.jsonl). Only windows this script opened are captured; all content is synthetic.
"""

from __future__ import annotations

import argparse
import ctypes
import http.server
import json
import shutil
import subprocess
import sys
import threading
import time
import zlib
from ctypes import byref, wintypes
from datetime import timedelta
from pathlib import Path

import numpy as np
from PIL import Image

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "eval" / "quality"))

import winapi  # noqa: E402
from akashic.capture import imaging  # noqa: E402
from akashic.platform.win.wgc import WgcCaptures  # noqa: E402
from winrt.windows.graphics.capture import (  # noqa: E402
    Direct3D11CaptureFramePool,
    GraphicsCaptureDirtyRegionMode,
    GraphicsCaptureItem,
)
from winrt.windows.graphics.directx import DirectXPixelFormat  # noqa: E402
from winrt.windows.graphics.directx.direct3d11.interop import create_direct3d11_device  # noqa: E402

user32, kernel32, dwmapi = winapi.user32, winapi.kernel32, winapi.dwmapi
user32.AttachThreadInput.argtypes = (wintypes.DWORD, wintypes.DWORD, wintypes.BOOL)
user32.BringWindowToTop.argtypes = (wintypes.HWND,)
user32.IsWindow.argtypes = (wintypes.HWND,)

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
PY = sys.executable
INTERVAL = 5.0
FRAME_INTERVAL_MS = 1000  # desk.py
BROWSER_PLAN = "idle:60,caret:420,ecaret:420,clock:420,spinner:420,quiet:360,events:1800"
# Phases where the window must be foreground (a caret only blinks in the focused window). In the
# other phases the script does not take focus back, so it does not fight other agents for it;
# the analysis drops the checks around every focus change either way.
FOCUS_PHASES = {"caret", "events", "bar", "block"}
TERM_PLAN = "idle:60,bar:480,block:480"


def efb(hwnd: int) -> tuple[int, int, int, int]:
    rc = wintypes.RECT()
    dwmapi.DwmGetWindowAttribute(hwnd, 9, byref(rc), ctypes.sizeof(rc))  # DWMWA_EXTENDED_FRAME_BOUNDS
    return rc.left, rc.top, rc.right, rc.bottom


def bring(hwnd: int) -> bool:
    """Foreground our window without keystrokes (thread-input attach); Alt fallback."""
    fg = winapi.foreground()
    me = kernel32.GetCurrentThreadId()
    other = user32.GetWindowThreadProcessId(fg, None) if fg else 0
    if other and other != me:
        user32.AttachThreadInput(me, other, True)
    user32.BringWindowToTop(hwnd)
    user32.SetForegroundWindow(hwnd)
    if other and other != me:
        user32.AttachThreadInput(me, other, False)
    time.sleep(0.05)
    if winapi.foreground() == hwnd:
        return True
    return winapi.focus(hwnd)


def reactivate(hwnd: int, log: Log) -> bool:
    """Foreground our window AND get the page's own focus event back. On 2026-11-20 Edge stayed
    foreground per GetForegroundWindow after a refocus but never got its window focus back (no
    caret for 50 min), so when the page still reports blur, minimize and restore it."""
    ok = bring(hwnd)
    if log.page_focus is None:
        return ok
    time.sleep(0.3)
    if log.page_focus:
        return ok
    log.restores += 1
    user32.ShowWindow(hwnd, 6)  # SW_MINIMIZE
    time.sleep(0.3)
    user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    ok = bring(hwnd)
    time.sleep(0.3)
    return ok and bool(log.page_focus)


class Log:
    def __init__(self, path: Path) -> None:
        self.f = path.open("a", encoding="utf-8")
        self.lock = threading.Lock()
        self.n = 0
        self.phase = None
        self.page_focus = None  # browsers: the page's own window focus/blur events
        self.restores = 0  # minimize/restore cycles done by reactivate()

    def add(self, ev: dict) -> None:
        ev["srv"] = time.time()
        with self.lock:
            if ev.get("kind") == "phase":
                self.phase = ev.get("name")
            # Browsers only (their load event carries the page size "w"). term_child.py's load has
            # no focus signal; treating it as blur made every bar/block check of the term and term2
            # runs minimize and restore the terminal (2026-11-20), so page_focus stays None there.
            if ev.get("kind") in ("win_focus", "win_blur") or (ev.get("kind") == "load" and "w" in ev):
                self.page_focus = ev.get("focus", ev.get("kind") == "win_focus")
            self.f.write(json.dumps(ev) + "\n")
            self.f.flush()
            self.n += 1


def serve(log: Log) -> tuple[http.server.ThreadingHTTPServer, int]:
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            name = self.path.split("?")[0].lstrip("/")
            if name != "chat.html":
                self.send_error(404)
                return
            body = (HERE / "chat.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):  # noqa: N802
            n = int(self.headers.get("Content-Length", 0))
            try:
                log.add(json.loads(self.rfile.read(n)))
            except ValueError:
                pass
            self.send_response(204)
            self.end_headers()

        def log_message(self, *a):  # quiet
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


class Dirty:
    """Second WGC session on the same window, REPORT_ONLY: dirty rects per frame, no pixels kept."""

    def __init__(self, hwnd: int) -> None:
        self.lock = threading.Lock()
        self.rects: list[tuple[int, int, int, int]] = []
        self.frames = 0
        self.modes: set[int] = set()
        self.device = create_direct3d11_device()
        self.item = GraphicsCaptureItem.try_create_from_window_id((hwnd,))
        size = self.item.size
        self.pool = Direct3D11CaptureFramePool.create_free_threaded(
            self.device, DirectXPixelFormat.B8_G8_R8_A8_UINT_NORMALIZED, 1, size)
        self.session = self.pool.create_capture_session(self.item)
        self.session.is_border_required = False
        self.session.is_cursor_capture_enabled = False
        self.session.min_update_interval = timedelta(milliseconds=0)
        self.session.dirty_region_mode = GraphicsCaptureDirtyRegionMode.REPORT_ONLY
        self.pool.add_frame_arrived(self._on)
        self.session.start_capture()

    def _on(self, pool, _a) -> None:
        f = pool.try_get_next_frame()
        if f is None:
            return
        try:
            rects = [(r.x, r.y, r.width, r.height) for r in (f.dirty_regions or [])]
            mode = int(f.dirty_region_mode)
        finally:
            f.close()
        with self.lock:
            self.rects.extend(rects)
            self.frames += 1
            self.modes.add(mode)

    def take(self) -> dict:
        with self.lock:
            rects, frames, self.rects, self.frames = self.rects, self.frames, [], 0
            modes = sorted(self.modes)
        if not rects:
            return {"frames": frames, "rects": 0, "union": None, "area": 0, "modes": modes, "uniq": []}
        uniq = sorted(set(rects))
        x0, y0 = min(r[0] for r in rects), min(r[1] for r in rects)
        x1, y1 = max(r[0] + r[2] for r in rects), max(r[1] + r[3] for r in rects)
        return {"frames": frames, "rects": len(rects), "union": [x0, y0, x1 - x0, y1 - y0],
                "area": sum(r[2] * r[3] for r in rects), "modes": modes, "uniq": uniq[:40], "n_uniq": len(uniq)}

    def close(self) -> None:
        for step in (self.pool.close, self.session.close, self.device.close):
            try:
                step()
            except Exception:  # noqa: BLE001
                pass


def c3_reduce(img: Image.Image) -> np.ndarray:
    g = img.reduce((4, 2)).convert("L")
    return np.frombuffer(g.tobytes(), dtype=np.uint8).reshape(g.size[1], g.size[0])


def find_window(pred, timeout: float = 30.0) -> int:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        for w in winapi.top_windows():
            if pred(w):
                return w["hwnd"]
        time.sleep(0.25)
    return 0


def class_name(hwnd: int) -> str:
    b = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, b, 256)
    return b.value


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("app", choices=("edge", "chrome", "term"))
    ap.add_argument("out", type=Path)
    ap.add_argument("--plan")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--dx", type=int, default=300)
    ap.add_argument("--no-dirty", action="store_true", help="no second (REPORT_ONLY) WGC session")
    ap.add_argument("--sample", action="store_true", help="save one composed sample PNG (own window)")
    a = ap.parse_args()
    plan = a.plan or (TERM_PLAN if a.app == "term" else BROWSER_PLAN)
    total = sum(int(p.split(":")[1]) for p in plan.split(","))
    out = a.out
    out.mkdir(parents=True, exist_ok=True)
    winapi.set_dpi_aware()
    mon = next(m for m in winapi.monitors() if m["name"] == "right")
    assert mon["x"] > 0 and (mon["w"], mon["h"]) == (2560, 1440) and mon["scale"] == 1.0, mon
    log = Log(out / "events.jsonl")
    srv, port = serve(log)
    win_x, win_y, win_w, win_h = mon["x"] + a.dx, mon["y"] + 80, 1600, 1200
    delay = 8
    proc = None
    profile = HERE / "profiles" / f"{a.app}-{a.seed}"
    t_launch = time.time()
    if a.app in ("edge", "chrome"):
        if profile.exists():
            shutil.rmtree(profile, ignore_errors=True)
        url = f"http://127.0.0.1:{port}/chat.html?plan={plan}&seed={a.seed}&delay={delay}"
        exe = EDGE if a.app == "edge" else CHROME
        args = [exe, f"--user-data-dir={profile}", "--guest", "--no-first-run", "--no-default-browser-check",
                "--new-window", f"--window-position={win_x},{win_y}", f"--window-size={win_w},{win_h}",
                "--disable-features=msEdgeSidebarV2,msUndersideButton,Translate,msEdgeCopilot", url]
        proc = subprocess.Popen(args)  # noqa: S603
        pids = lambda: winapi.descendants({proc.pid}, keep_exited=True)  # noqa: E731
        hwnd = find_window(lambda w: w["pid"] in pids() and "Akashic live chat test" in w["title"]
                           and not w["iconic"])
    else:
        before = {w["hwnd"] for w in winapi.top_windows() if class_name(w["hwnd"]) == "CASCADIA_HOSTING_WINDOW_CLASS"}
        args = ["wt.exe", "-w", "new", "--pos", f"{win_x},{win_y}", "--size", "150,45", "new-tab", "--title",
                "Akashic live term", "--suppressApplicationTitle", PY, "-I", str(HERE / "term_child.py"),
                str(port), plan, str(delay)]
        subprocess.Popen(args)  # noqa: S603
        hwnd = find_window(lambda w: w["hwnd"] not in before and class_name(w["hwnd"]) == "CASCADIA_HOSTING_WINDOW_CLASS")
    if not hwnd:
        print("window not found", flush=True)
        if proc:
            proc.kill()
        return
    time.sleep(1.0)
    winapi.place(hwnd, win_x, win_y, win_w, win_h)
    stole = winapi.foreground() == hwnd
    bring(hwnd)
    caps = WgcCaptures()
    assert caps.start(hwnd, FRAME_INTERVAL_MS)
    dirty = None if a.no_dirty else Dirty(hwnd)
    meta_f = (out / "checks.jsonl").open("w", encoding="utf-8")
    frames_f = (out / "frames.bin").open("wb")
    wall_off = time.time() - time.monotonic()
    info = {"app": a.app, "plan": plan, "seed": a.seed, "port": port, "monitor": [mon["x"], mon["y"], mon["w"], mon["h"]],
            "window_req": [win_x, win_y, win_w, win_h], "launch_wall": t_launch, "focus_on_launch": stole,
            "interval_s": INTERVAL, "frame_interval_ms": FRAME_INTERVAL_MS, "python": sys.version}
    print(json.dumps(info), flush=True)
    end = time.monotonic() + delay + total + 15
    tick = time.monotonic() + 2.0
    i = 0
    sampled = False
    try:
        while time.monotonic() < end:
            time.sleep(max(0.0, tick - time.monotonic()))
            tick += INTERVAL
            if not user32.IsWindow(hwnd):
                print("window gone", flush=True)
                break
            fg = winapi.foreground()
            fg_ok = fg == hwnd
            refocused = False
            phase = log.phase
            page_ok = log.page_focus is not False
            if (not fg_ok or not page_ok) and (phase in FOCUS_PHASES or phase is None):
                refocused = reactivate(hwnd, log)
            t_check = time.time()
            fr = caps.grab(hwnd)
            d = dirty.take() if dirty else {"frames": 0, "rects": 0, "union": None, "area": 0, "modes": [], "uniq": []}
            rec = {"i": i, "t": t_check, "phase": phase, "fg_ok": fg_ok, "page_focus": log.page_focus, "fg_cls": class_name(fg) if fg and not fg_ok else None,
                   "refocused": refocused, "restores": log.restores, "dirty": d}
            if fr is None:
                rec["frame"] = None
            else:
                l, t, r, b = efb(hwnd)
                canvas = Image.new("RGB", (mon["w"], mon["h"]), (0, 0, 0))
                canvas.paste(fr.image, (l - mon["x"], t - mon["y"]))
                t0 = time.perf_counter_ns()
                sig = imaging.signature(canvas)
                t1 = time.perf_counter_ns()
                g3 = c3_reduce(canvas)
                t2 = time.perf_counter_ns()
                if a.sample and not sampled and i >= 6:
                    canvas.crop((l - mon["x"], t - mon["y"], l - mon["x"] + fr.image.width,
                                 t - mon["y"] + fr.image.height)).save(out / "sample.png")
                    sampled = True
                blobs = [zlib.compress(sig.gray.tobytes(), 1), zlib.compress(g3.tobytes(), 1)]
                pos = frames_f.tell()
                for blob in blobs:
                    frames_f.write(blob)
                rec.update({"frame": {"seq": fr.seq, "at": fr.at + wall_off, "size": list(fr.image.size),
                                      "pos": [l - mon["x"], t - mon["y"]]},
                            "blob": [pos, len(blobs[0]), len(blobs[1])], "shape": [list(sig.gray.shape), list(g3.shape)],
                            "t_sig_ms": (t1 - t0) / 1e6, "t_c3_ms": (t2 - t1) / 1e6})
            meta_f.write(json.dumps(rec) + "\n")
            meta_f.flush()
            if i % 12 == 0:
                print(f"check {i} fg_ok={fg_ok} events={log.n} frames={d['frames']}", flush=True)
            i += 1
    finally:
        frames_f.close()
        meta_f.close()
        if dirty:
            dirty.close()
        caps.close()
        if user32.IsWindow(hwnd):
            winapi.close_window(hwnd)
        if proc is not None:
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                pass
            for pid in winapi.descendants({proc.pid}):
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)  # noqa: S603, S607
        time.sleep(1.0)
        info["window_closed"] = not user32.IsWindow(hwnd)
        (out / "info.json").write_text(json.dumps(info, indent=1), encoding="utf-8")
        srv.shutdown()
        if profile.exists():
            shutil.rmtree(profile, ignore_errors=True)
        print("done", json.dumps(info), flush=True)


if __name__ == "__main__":
    main()
