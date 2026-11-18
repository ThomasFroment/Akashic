"""Passive game-signal sample (no window, no focus change): for 10 s at 1 Hz, record the QUNS state,
whether the foreground window belongs to a process running from the Steam library, Steam's
RunningAppID, cursor showing/clipped, and the 3D-engine share of each process tree by image name.
Writes numbers and exe names of a fixed set only.

    uv run python eval/quality/spikes/game_signals_passive.py <out.json>
"""

import ctypes
import json
import statistics
import sys
import time
import winreg
from ctypes import byref, wintypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import winapi as w  # noqa: E402

LIB = r"C:\Program Files (x86)\Steam\steamapps\common".lower()
WATCH = ("discord.exe", "zen.exe", "chrome.exe", "msedge.exe", "code.exe", "windowsterminal.exe", "steamwebhelper.exe", "dwm.exe")


class CURSORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD), ("hCursor", wintypes.HANDLE), ("pt", wintypes.POINT)]


def quns() -> int:
    state = ctypes.c_int()
    ctypes.windll.shell32.SHQueryUserNotificationState(byref(state))
    return state.value


def running_appid() -> int:
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
        return int(winreg.QueryValueEx(k, "RunningAppID")[0])


def exe_path(pid: int) -> str:
    h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
    if not h:
        return ""
    buf = ctypes.create_unicode_buffer(1024)
    n = wintypes.DWORD(1024)
    ok = ctypes.windll.kernel32.QueryFullProcessImageNameW(h, 0, buf, byref(n))
    ctypes.windll.kernel32.CloseHandle(h)
    return buf.value.lower() if ok else ""


def main() -> None:
    w.set_dpi_aware()
    gpu = w.GpuCounters()
    time.sleep(1.0)
    rows = []
    for _ in range(10):
        time.sleep(1.0)
        _, eng = gpu.sample()
        table = w.process_table()
        fg_hwnd = ctypes.windll.user32.GetForegroundWindow()
        pid = wintypes.DWORD()
        ctypes.windll.user32.GetWindowThreadProcessId(fg_hwnd, byref(pid))
        fg_path = exe_path(pid.value)
        game_pids = {p for p in table if exe_path(p).startswith(LIB)}
        ci = CURSORINFO(ctypes.sizeof(CURSORINFO))
        ctypes.windll.user32.GetCursorInfo(byref(ci))
        clip, screen = wintypes.RECT(), wintypes.RECT()
        ctypes.windll.user32.GetClipCursor(byref(clip))
        vx, vy = ctypes.windll.user32.GetSystemMetrics(76), ctypes.windll.user32.GetSystemMetrics(77)
        vw, vh = ctypes.windll.user32.GetSystemMetrics(78), ctypes.windll.user32.GetSystemMetrics(79)
        clipped = (clip.left, clip.top, clip.right, clip.bottom) != (vx, vy, vx + vw, vy + vh)
        by_name: dict[str, float] = {}
        for p, share in eng.items():
            name = table.get(p, (0, ""))[1].lower()
            if p in game_pids:
                name = "steam-library-game"
            if name in WATCH or name == "steam-library-game":
                by_name[name] = by_name.get(name, 0.0) + share
        rows.append({"quns": quns(), "fg_is_steam_library": fg_path.startswith(LIB), "running_appid_set": running_appid() != 0,
                     "cursor_showing": bool(ci.flags & 1), "cursor_clipped": clipped, "share_3d": {k: round(v, 1) for k, v in by_name.items()}})
    names = sorted({k for r in rows for k in r["share_3d"]})
    summary = {
        "quns_states": sorted({r["quns"] for r in rows}),
        "fg_is_steam_library": sum(r["fg_is_steam_library"] for r in rows),
        "cursor_showing": sum(r["cursor_showing"] for r in rows),
        "cursor_clipped": sum(r["cursor_clipped"] for r in rows),
        "share_3d_median": {n: statistics.median(r["share_3d"].get(n, 0.0) for r in rows) for n in names},
        "samples": len(rows),
    }
    Path(sys.argv[1]).write_text(json.dumps({"summary": summary, "rows": rows}, indent=1), encoding="utf-8")
    print(json.dumps(summary, indent=1))


main()
