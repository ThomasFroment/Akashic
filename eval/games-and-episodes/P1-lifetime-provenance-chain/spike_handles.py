"""Spike: process-lifetime handle for C2 against real game processes.

    uv run python eval/games-and-episodes/P1-lifetime-provenance-chain/spike_handles.py [--no-ow]

For the harness QualityGame (renamed python, borderless on the right monitor) and Outer Wilds
(launched via steam://rungameid/753640, closed after): OpenProcess(SYNCHRONIZE |
PROCESS_QUERY_LIMITED_INFORMATION) result, microseconds per call (median of 101) for
OpenProcess+CloseHandle, WaitForSingleObject(h, 0) and GetProcessTimes, creation time against the
launch request and the first window, exit detection latency (ExitTime vs the wait being signalled),
the title chain (Steam appmanifest name, version-resource ProductName / OriginalFilename), and
Outer Wilds' window facts the harness lacks (class, style, QUNS). Writes spike_handles.json.
Exe names and game names only; no captured content.
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import statistics
import subprocess
import sys
import tempfile
import time
from ctypes import byref, wintypes
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "harness"))
import ge  # noqa: E402,F401  (puts eval/quality on sys.path)
import launchers  # noqa: E402

import winapi as w  # noqa: E402
from common import renamed_python  # noqa: E402

from akashic.platform.win.windows import Win32Windows  # noqa: E402

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
k32.OpenProcess.restype = wintypes.HANDLE
k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
k32.WaitForSingleObject.restype = wintypes.DWORD
k32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
k32.GetProcessTimes.restype = wintypes.BOOL
k32.GetProcessTimes.argtypes = (wintypes.HANDLE,) + (ctypes.POINTER(wintypes.FILETIME),) * 4
k32.CloseHandle.argtypes = (wintypes.HANDLE,)
k32.QueryFullProcessImageNameW.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
user32 = ctypes.windll.user32
version = ctypes.WinDLL("version")

SYNCHRONIZE, QUERY_LIMITED = 0x00100000, 0x1000
WAIT_OBJECT_0, WAIT_TIMEOUT, WAIT_FAILED = 0, 0x102, 0xFFFFFFFF
EPOCH_DIFF_100NS = 116444736000000000
N = 101
OW_APP = "753640"
OUT = HERE / "spike_handles.json"


def ft_ms(ft: wintypes.FILETIME) -> int:
    v = (ft.dwHighDateTime << 32) | ft.dwLowDateTime
    return (v - EPOCH_DIFF_100NS) // 10_000 if v else 0


def times(h) -> tuple[bool, int, int]:
    c, e, k, u = wintypes.FILETIME(), wintypes.FILETIME(), wintypes.FILETIME(), wintypes.FILETIME()
    ok = bool(k32.GetProcessTimes(h, byref(c), byref(e), byref(k), byref(u)))
    return ok, ft_ms(c), ft_ms(e)


def med_us(fn, n=N) -> float:
    fn()
    xs = []
    for _ in range(n):
        t = time.perf_counter()
        fn()
        xs.append((time.perf_counter() - t) * 1e6)
    return round(statistics.median(xs), 2)


def open_probe(pid: int) -> dict:
    h = k32.OpenProcess(SYNCHRONIZE | QUERY_LIMITED, False, pid)
    err = ctypes.get_last_error()
    out = {"open_ok": bool(h), "open_error": 0 if h else err}
    if not h:
        return out

    def open_close():
        hh = k32.OpenProcess(SYNCHRONIZE | QUERY_LIMITED, False, pid)
        k32.CloseHandle(hh)

    wait = k32.WaitForSingleObject(h, 0)
    ok, created, exited = times(h)
    out.update({"wait0": {WAIT_OBJECT_0: "signalled", WAIT_TIMEOUT: "timeout", WAIT_FAILED: "failed"}.get(wait, wait),
                "times_ok": ok, "creation_ms": created, "exit_ms_while_running": exited,
                "open_close_us": med_us(open_close), "wait0_us": med_us(lambda: k32.WaitForSingleObject(h, 0)),
                "get_process_times_us": med_us(lambda: times(h))})
    out["_h"] = h
    return out


def exit_probe(h, closed_at_ms: int, timeout_s: float) -> dict:
    """Waits in 50 ms slices (as a 10-20 Hz check would) and compares ExitTime with when it was seen."""
    end = time.monotonic() + timeout_s
    while time.monotonic() < end:
        if k32.WaitForSingleObject(h, 50) == WAIT_OBJECT_0:
            seen = int(time.time() * 1000)
            ok, _c, exited = times(h)
            return {"exit_seen": True, "exit_times_ok": ok, "exit_after_close_ms": exited - closed_at_ms,
                    "seen_after_exit_ms": seen - exited, "wait0_after_exit": k32.WaitForSingleObject(h, 0) == WAIT_OBJECT_0}
    return {"exit_seen": False}


def file_version(path: str) -> dict:
    size = version.GetFileVersionInfoSizeW(path, None)
    if not size:
        return {}
    buf = ctypes.create_string_buffer(size)
    if not version.GetFileVersionInfoW(path, 0, size, buf):
        return {}
    ptr, n = ctypes.c_void_p(), wintypes.UINT()
    version.VerQueryValueW(buf, "\\VarFileInfo\\Translation", byref(ptr), byref(n))
    langs = []
    if n.value >= 4:
        arr = ctypes.cast(ptr, ctypes.POINTER(wintypes.WORD * (n.value // 2))).contents
        langs = [f"{arr[i]:04x}{arr[i + 1]:04x}" for i in range(0, len(arr), 2)]
    out = {}
    for lang in [*langs, "040904b0", "040904e4"]:
        for key in ("ProductName", "OriginalFilename", "FileDescription"):
            if key in out:
                continue
            if version.VerQueryValueW(buf, f"\\StringFileInfo\\{lang}\\{key}", byref(ptr), byref(n)) and n.value:
                out[key] = ctypes.wstring_at(ptr, n.value - 1)
    return out


def steam_manifest_name(exe_path: str) -> str | None:
    p = exe_path.lower().replace("/", "\\")
    i = p.find("\\steamapps\\common\\")
    if i < 0:
        return None
    lib, installdir = p[:i], p[i + len("\\steamapps\\common\\"):].split("\\")[0]
    for acf in Path(lib, "steamapps").glob("appmanifest_*.acf"):
        text = acf.read_text(encoding="utf-8", errors="replace")
        m = re.search(r'"installdir"\s+"([^"]+)"', text)
        if m and m.group(1).lower() == installdir:
            n = re.search(r'"name"\s+"([^"]+)"', text)
            return n.group(1) if n else None
    return None


def window_facts(ww: Win32Windows, hwnd: int) -> dict:
    info = ww._info(hwnd)  # noqa: SLF001
    cls = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, cls, 256)
    style = user32.GetWindowLongW(hwnd, -16) & 0xFFFFFFFF
    s = ctypes.c_int()
    ctypes.windll.shell32.SHQueryUserNotificationState(byref(s))
    return {"class": cls.value, "caption": bool(style & 0x00C00000), "popup": bool(style & 0x80000000),
            "has_frame": info.has_frame, "exclusive_fullscreen": info.exclusive_fullscreen,
            "rect": [info.rect.left, info.rect.top, info.rect.right, info.rect.bottom],
            "monitor": w.monitor_of_rect((info.rect.left, info.rect.top, info.rect.right, info.rect.bottom)),
            "foreground": user32.GetForegroundWindow() == hwnd, "quns": s.value,
            "steam_running_appid_set": launchers.running_appid() != 0}


def pid_of(name: str) -> int | None:
    return next((pid for pid, (_pp, img) in w.process_table().items() if img.lower() == name), None)


def quality_game(ww: Win32Windows) -> dict:
    exe, env = renamed_python("QualityGame.exe", Path(tempfile.gettempdir()) / "akashic-p1-spike")
    mon = next(m for m in w.monitors() if m["name"] == "right")
    asked = int(time.time() * 1000)
    p = subprocess.Popen([str(exe), str(ge.QUALITY / "game.py"), "--mode", "borderless", "--rect", str(mon["x"]), str(mon["y"]),
                          str(mon["w"]), str(mon["h"]), "--seconds", "60", "--log", os.devnull],
                         env={**os.environ, **env}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        out = open_probe(p.pid)
        out["creation_after_request_ms"] = out.get("creation_ms", 0) - asked
        win_at = None
        for _ in range(80):
            if any(t.process == "qualitygame.exe" and t.rect.width > 200 for t in ww.windows() or []):
                win_at = int(time.time() * 1000)
                break
            time.sleep(0.1)
        out["window_after_creation_ms"] = (win_at - out["creation_ms"]) if win_at and "creation_ms" in out else None
        out["version"] = file_version(str(exe))
        h = out.pop("_h", None)
        closed = int(time.time() * 1000)
        subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True, check=False)
        if h:
            out.update(exit_probe(h, closed, 10))
            k32.CloseHandle(h)
        return out
    finally:
        p.kill()


def outer_wilds(ww: Win32Windows) -> dict:
    if pid_of("outerwilds.exe"):
        return {"error": "already running; not touched"}
    asked = int(time.time() * 1000)
    os.startfile(f"steam://rungameid/{OW_APP}")  # noqa: S606
    pid, deadline = None, time.monotonic() + 120
    while time.monotonic() < deadline and pid is None:
        pid = pid_of("outerwilds.exe")
        time.sleep(0.05)
    if pid is None:
        return {"error": "no outerwilds.exe within 120 s"}
    out = open_probe(pid)
    out["creation_after_request_ms"] = out.get("creation_ms", 0) - asked
    path = launchers_path(pid)
    out["steam_manifest_name"] = steam_manifest_name(path)
    out["version"] = file_version(path)
    out["launcher_facts"] = launchers.Facts().of(path)
    win, deadline = None, time.monotonic() + 90
    while time.monotonic() < deadline and win is None:
        win = w.outer_wilds_window()
        time.sleep(0.1)
    if win:
        out["window_after_creation_ms"] = int(time.time() * 1000) - out.get("creation_ms", 0)
        time.sleep(8)  # let it settle into its fullscreen mode
        out["window"] = window_facts(ww, win["hwnd"])
        if not out["window"]["foreground"]:
            w.focus(win["hwnd"])
            time.sleep(1)
            out["window_after_focus"] = window_facts(ww, win["hwnd"])
        # the per-check probe while the game is in front (same handle)
        h = out.get("_h")
        if h:
            out["wait0_us_in_front"] = med_us(lambda: k32.WaitForSingleObject(h, 0))
        closed = int(time.time() * 1000)
        w.close_window(win["hwnd"])
    else:
        closed = int(time.time() * 1000)
        subprocess.run(["taskkill", "/PID", str(pid), "/T"], capture_output=True, check=False)
    h = out.pop("_h", None)
    if h:
        out.update(exit_probe(h, closed, 60))
        k32.CloseHandle(h)
    if pid_of("outerwilds.exe"):
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, check=False)
        out["force_killed"] = True
    return out


def launchers_path(pid: int) -> str:
    h = k32.OpenProcess(QUERY_LIMITED, False, pid)
    buf, n = ctypes.create_unicode_buffer(1024), wintypes.DWORD(1024)
    ok = k32.QueryFullProcessImageNameW(h, 0, buf, byref(n)) if h else 0
    if h:
        k32.CloseHandle(h)
    return buf.value if ok else ""


def protected() -> dict:
    out = {}
    for name in ("msmpeng.exe", "lsass.exe", "csrss.exe"):
        pid = pid_of(name)
        if pid:
            r = open_probe(pid)
            if r.get("_h"):
                k32.CloseHandle(r.pop("_h"))
            out[name] = {k: v for k, v in r.items() if k in ("open_ok", "open_error", "wait0", "times_ok", "wait0_us", "get_process_times_us")}
    return out


def main() -> None:
    w.set_dpi_aware()
    ww = Win32Windows()
    res = {"when": time.strftime("%Y-%m-%d %H:%M"), "reps": N}
    res["quality_game"] = quality_game(ww)
    print("quality_game", json.dumps(res["quality_game"]), flush=True)
    res["protected"] = protected()
    print("protected", json.dumps(res["protected"]), flush=True)
    if "--no-ow" not in sys.argv:
        res["outer_wilds"] = outer_wilds(ww)
        print("outer_wilds", json.dumps(res["outer_wilds"]), flush=True)
    for k in ("quality_game", "outer_wilds"):
        res.get(k, {}).pop("creation_ms", None)
    OUT.write_text(json.dumps(res, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
