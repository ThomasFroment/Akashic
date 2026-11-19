"""Do Windows.Graphics.Capture dirty regions report a one-word change, and how big is a caret's?

A child Tk window (WS_EX_NOACTIVATE, right monitor, never focused) shows a page of text and an
empty chat line. The parent captures it like `platform/win/wgc.py` (free-threaded pool, one
buffer, no cursor) with `dirty_region_mode = REPORT_ONLY`, then asks the child for one change at
a time: a one-word reply ("ok", 14 px), a short sentence, a red badge, a 2 px caret on and off,
and nothing (idle). Per step: frames that arrived, the union of their dirty regions, and their
area as a share of the window. Synthetic strings only.

    uv run python eval/quality/spikes/wgc_dirty_regions.py <out.json>
"""

import ctypes
import json
import subprocess
import sys
import threading
import time
from datetime import timedelta
from pathlib import Path

from winrt.windows.graphics.capture import (
    Direct3D11CaptureFramePool,
    GraphicsCaptureDirtyRegionMode,
    GraphicsCaptureItem,
)
from winrt.windows.graphics.directx import DirectXPixelFormat
from winrt.windows.graphics.directx.direct3d11.interop import create_direct3d11_device

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import winapi  # noqa: E402

CHILD = r"""
import ctypes, sys, tkinter as t
ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
u = ctypes.windll.user32
u.GetAncestor.restype = ctypes.c_void_p; u.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
u.GetWindowLongPtrW.restype = ctypes.c_ssize_t; u.GetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int]
u.SetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t]
u.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
x, y = int(sys.argv[1]), int(sys.argv[2])
r = t.Tk(); r.withdraw(); r.title('Akashic dirty-region spike'); r.geometry(f'900x420+{x}+{y}')
bg, fg = '#202124', '#dcddde'
r.configure(bg=bg)
for i in range(12):
    t.Label(r, text='lorem ipsum dolor sit amet ' * 3, bg=bg, fg=fg, font=('Segoe UI', 11), anchor='w').place(x=20, y=10 + i * 22)
chat = t.Label(r, text='', bg=bg, fg=fg, font=('Segoe UI', 14), anchor='w'); chat.place(x=20, y=300, width=500, height=30)
badge = t.Canvas(r, width=20, height=20, bg=bg, highlightthickness=0); badge.place(x=600, y=305)
caret = t.Frame(r, width=2, height=22, bg=bg); caret.place(x=20, y=350)
r.update()
hwnd = u.GetAncestor(r.winfo_id(), 2)
u.SetWindowLongPtrW(hwnd, -20, u.GetWindowLongPtrW(hwnd, -20) | 0x08000000 | 0x80)  # NOACTIVATE, TOOLWINDOW
u.ShowWindow(hwnd, 4)  # SW_SHOWNOACTIVATE
r.update()
print(hwnd, flush=True)
def poll():
    line = sys.stdin.readline().strip()
    if line == 'word': chat.config(text='ok')
    elif line == 'line': chat.config(text='on se retrouve ce soir ?')
    elif line == 'clear': chat.config(text=''); badge.delete('all')
    elif line == 'badge': badge.create_oval(2, 2, 18, 18, fill='#ed4245', outline='')
    elif line == 'caret_on': caret.config(bg=fg)
    elif line == 'caret_off': caret.config(bg=bg)
    elif line == 'quit': r.destroy(); return
    r.update(); print('ok', flush=True); r.after(1, poll)
r.after(1, poll); r.mainloop()
"""

STEPS = ["idle", "word", "clear", "line", "clear", "badge", "clear", "caret_on", "caret_off", "caret_on", "caret_off", "idle"]


def main() -> None:
    winapi.set_dpi_aware()
    mons = winapi.monitors()
    right = next(m for m in mons if m["name"] == "right")
    left, top = right["x"] + 200, right["y"] + 200
    before_fg = winapi.foreground()
    child = subprocess.Popen([sys.executable, "-c", CHILD, str(left), str(top)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)  # noqa: S603
    hwnd = int(child.stdout.readline())
    stole_focus = winapi.foreground() == hwnd

    device = create_direct3d11_device()
    item = GraphicsCaptureItem.try_create_from_window_id((hwnd,))
    size = item.size
    pool = Direct3D11CaptureFramePool.create_free_threaded(device, DirectXPixelFormat.B8_G8_R8_A8_UINT_NORMALIZED, 1, size)
    session = pool.create_capture_session(item)
    session.is_border_required = False
    session.is_cursor_capture_enabled = False
    session.min_update_interval = timedelta(milliseconds=0)
    session.dirty_region_mode = GraphicsCaptureDirtyRegionMode.REPORT_ONLY
    lock = threading.Lock()
    frames: list[list[tuple[int, int, int, int]]] = []

    def on_frame(p, _a) -> None:
        f = p.try_get_next_frame()
        if f is None:
            return
        try:
            rects = [(r.x, r.y, r.width, r.height) for r in (f.dirty_regions or [])]
            mode = f.dirty_region_mode
        finally:
            f.close()
        with lock:
            frames.append((rects, int(mode)))

    pool.add_frame_arrived(on_frame)
    session.start_capture()
    time.sleep(1.0)
    with lock:
        frames.clear()
    rows = []
    for step in STEPS:
        if step != "idle":
            child.stdin.write(step + "\n")
            child.stdin.flush()
            child.stdout.readline()
        time.sleep(0.6)
        with lock:
            got, frames[:] = list(frames), []
        rects = [r for fr, _m in got for r in fr]
        area = sum(w * h for _x, _y, w, h in rects)
        if rects:
            x0, y0 = min(r[0] for r in rects), min(r[1] for r in rects)
            x1, y1 = max(r[0] + r[2] for r in rects), max(r[1] + r[3] for r in rects)
            union = (x0, y0, x1 - x0, y1 - y0)
        else:
            union = None
        rows.append({"step": step, "frames": len(got), "rect_count": len(rects), "union": union, "area_px": area,
                     "area_share": round(area / (size.width * size.height), 5), "modes": sorted({m for _f, m in got}),
                     "rects": rects[:8]})
    child.stdin.write("quit\n")
    child.stdin.flush()
    session.close()
    pool.close()
    child.wait(5)
    out = {"window": [size.width, size.height], "stole_focus": stole_focus, "foreground_restored": winapi.foreground() == before_fg, "rows": rows}
    Path(sys.argv[1]).write_text(json.dumps(out, indent=1), encoding="utf-8")
    print("window", size.width, size.height, "stole_focus", stole_focus)
    for r in rows:
        print(f"{r['step']:<10} frames={r['frames']:<3} rects={r['rect_count']:<3} union={r['union']} area={r['area_px']} share={r['area_share']}")


main()
