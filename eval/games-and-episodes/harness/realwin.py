"""Real-window detection table: open each case fullscreen (right monitor where the app allows),
give it the foreground, record what gaming.py reads plus the extra signals, close it.

    uv run python eval/games-and-episodes/harness/realwin.py [out.json] [case ...]

Records per case: the production WindowInfo fields (process, rect, has_frame, exclusive_fullscreen
via Win32Windows), today's verdict, raw SHQueryUserNotificationState, window class and style bits,
cursor showing/clipped, launcher-folder and GameConfigStore facts of the exe (booleans only), Steam
RunningAppID set. Also per-signal call cost (median of 15 after a warm-up) on the first case. No
titles, URLs or paths are written: numbers, exe names and window classes only.

Every process started here is closed at the end of its case (process tree killed, or WM_CLOSE for
windows of shared processes); Steam Big Picture is closed with steam://close/bigpicture.
"""

from __future__ import annotations

import ctypes
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from ctypes import byref, wintypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ge  # noqa: E402  (puts eval/quality on sys.path)
import launchers  # noqa: E402

import winapi as w  # noqa: E402  (eval/quality/winapi.py)
from common import renamed_python  # noqa: E402  (eval/quality/common.py)

from akashic.capture import gaming  # noqa: E402
from akashic.capture.ports import Rect  # noqa: E402
from akashic.platform.win.windows import Win32Windows  # noqa: E402

user32 = ctypes.windll.user32
OUT = ge.HERE / "realwin.json"
WORK = Path(tempfile.gettempdir()) / "akashic-ge-realwin"
EXES = ge.HERE / "exes"
EXCLUDED = {"qualityvault.exe"}  # the quality runs' excluded process
VK_F11 = 0x7A
SW_MAXIMIZE, SW_RESTORE = 3, 9
GWL_STYLE, GWL_EXSTYLE = -16, -20
WS = {"caption": 0x00C00000, "popup": 0x80000000, "thickframe": 0x00040000, "maximize": 0x01000000, "sysmenu": 0x00080000}

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
ZEN = r"C:\Program Files\Zen Browser\zen.exe"
CODE = os.path.expandvars(r"%LOCALAPPDATA%\Programs\Microsoft VS Code\Code.exe")
WT = os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WindowsApps\wt.exe")


class CURSORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD), ("hCursor", wintypes.HANDLE), ("pt", wintypes.POINT)]


def quns() -> int:
    s = ctypes.c_int()
    ctypes.windll.shell32.SHQueryUserNotificationState(byref(s))
    return s.value


def cursor() -> tuple[bool, bool]:
    ci = CURSORINFO(ctypes.sizeof(CURSORINFO))
    user32.GetCursorInfo(byref(ci))
    clip = wintypes.RECT()
    user32.GetClipCursor(byref(clip))
    vx, vy, vw, vh = (user32.GetSystemMetrics(i) for i in (76, 77, 78, 79))
    return bool(ci.flags & 1), (clip.left, clip.top, clip.right, clip.bottom) != (vx, vy, vx + vw, vy + vh)


def exe_path(pid: int) -> str:
    h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
    if not h:
        return ""
    buf, n = ctypes.create_unicode_buffer(1024), wintypes.DWORD(1024)
    ok = ctypes.windll.kernel32.QueryFullProcessImageNameW(h, 0, buf, byref(n))
    ctypes.windll.kernel32.CloseHandle(h)
    return buf.value if ok else ""


def dir_kind(path: str) -> str:
    p = path.lower()
    if "\\steamapps\\common\\" in p:
        return "steam_library"
    if "\\windowsapps\\" in p:
        return "windowsapps"
    if str(ge.ROOT).lower() in p:
        return "harness"
    if p.startswith(("c:\\program files", "c:\\windows")):
        return "program_files_or_windows"
    if "\\appdata\\" in p:
        return "user_appdata"
    return "other"


def monitors() -> list[Rect]:
    return [Rect(m["x"], m["y"], m["x"] + m["w"], m["y"] + m["h"]) for m in w.monitors()]


def right_monitor() -> dict:
    return next(m for m in w.monitors() if m["name"] == "right")


def on_primary(fn, name: str):
    """The same case on the primary monitor: SHQueryUserNotificationState reads only that one."""
    def run(ww, facts, _mon):
        got = fn(ww, facts, next(m for m in w.monitors() if m["primary"]))
        for r in got if isinstance(got, list) else [got]:
            r["name"] = name
        return got
    return run


def wait_window(ww: Win32Windows, procs: set[str], before: set[int], timeout: float, covering: bool = False) -> int | None:
    end = time.monotonic() + timeout
    mons = monitors()
    while time.monotonic() < end:
        for t in ww.windows() or []:
            if t.hwnd in before or t.process not in procs or t.minimized or t.cloaked:
                continue
            if t.rect.width < 200 or t.rect.height < 200:
                continue
            if covering and gaming.covers_monitor(t.rect, mons) is None:
                continue
            return t.hwnd
        time.sleep(0.25)
    return None


def wait_covering(ww: Win32Windows, hwnd: int, timeout: float = 8.0) -> bool:
    end = time.monotonic() + timeout
    mons = monitors()
    while time.monotonic() < end:
        info = ww._info(hwnd)  # noqa: SLF001 - the production reader
        if info is not None and gaming.covers_monitor(info.rect, mons) is not None:
            return True
        time.sleep(0.25)
    return False


def kill_tree(pid: int) -> None:
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, check=False)
    kill_ours()


def kill_ours() -> None:
    """Every process whose command line names this spike's work folder (relaunched browser children)."""
    ps = ("Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'akashic-ge-realwin' -and "
          "$_.Name -notmatch '^(bash|powershell|python|uv)' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }")
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, check=False)


def press(vk: int) -> None:
    user32.keybd_event(vk, 0, 0, 0)
    user32.keybd_event(vk, 0, 2, 0)


def record(ww: Win32Windows, hwnd: int, facts: launchers.Facts, case: dict) -> dict:
    focused = w.focus(hwnd)
    time.sleep(1.0)
    w.focus(hwnd)
    time.sleep(0.5)
    fg = user32.GetForegroundWindow() == hwnd
    info = ww.focused_window() if fg else None
    if info is None or info.hwnd != hwnd:
        info = ww._info(hwnd)  # noqa: SLF001
    style = user32.GetWindowLongW(hwnd, GWL_STYLE) & 0xFFFFFFFF
    exstyle = user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & 0xFFFFFFFF
    cls = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, cls, 256)
    showing, clipped = cursor()
    mons = monitors()
    cov = gaming.covers_monitor(info.rect, mons)
    path = exe_path(info.pid)
    row = {**case, "source": "real", "foreground": fg, "focus_ok": focused, "process": info.process,
           "rect": [info.rect.left, info.rect.top, info.rect.right, info.rect.bottom],
           "monitor": [cov.left, cov.top, cov.right, cov.bottom] if cov else None,
           "has_frame": info.has_frame, "exclusive_fullscreen": info.exclusive_fullscreen, "quns": quns(),
           "class": cls.value, "style": {k: bool(style & v) for k, v in WS.items()}, "ex_layered": bool(exstyle & 0x80000),
           "ex_topmost": bool(exstyle & 0x8), "zoomed": bool(user32.IsZoomed(hwnd)), "cursor_showing": showing,
           "cursor_clipped": clipped, "exe_dir_kind": dir_kind(path), **facts.of(path),
           "steam_running_appid_set": launchers.running_appid() != 0,
           "today_is_game": gaming.is_game_window(info, mons, gaming.GameSettings())}
    print(f"  {case['name']}: process {row['process']} fg {fg} covers {cov is not None} frame {row['has_frame']}"
          f" excl {row['exclusive_fullscreen']} quns {row['quns']} class {row['class']} cursor {showing}/{clipped}"
          f" dir {row['exe_dir_kind']} launcher {row['under_launcher_game_dir']} gcs {row['in_gameconfigstore']}"
          f" -> today {row['today_is_game']}", flush=True)
    return row


def timings(ww: Win32Windows, hwnd: int, facts: launchers.Facts) -> dict:
    pid = ww._info(hwnd).pid  # noqa: SLF001
    path = exe_path(pid)

    def med(fn, n=15):
        fn()
        xs = []
        for _ in range(n):
            t = time.perf_counter()
            fn()
            xs.append((time.perf_counter() - t) * 1e6)
        return round(statistics.median(xs), 1)

    return {"focused_window_us": med(ww.focused_window), "quns_us": med(quns), "cursor_us": med(cursor),
            "exe_path_us": med(lambda: exe_path(pid)), "facts_lookup_us": med(lambda: facts.of(path)),
            "running_appid_us": med(launchers.running_appid), "class_name_us": med(lambda: user32.GetClassNameW(hwnd, ctypes.create_unicode_buffer(256), 256)),
            "launcher_refresh_ms": round(med(facts.refresh, 9) / 1000, 2)}


# --- cases ----------------------------------------------------------------------------------


def synthetic_game(name: str, exe_name: str, mode: str, truth: bool, excluded: bool):
    def run(ww, facts, mon):
        exe, env = renamed_python(exe_name, EXES)
        log = WORK / f"{name}.jsonl"
        before = {t.hwnd for t in ww.windows() or []}
        p = subprocess.Popen([str(exe), str(ge.QUALITY / "game.py"), "--mode", mode, "--rect", str(mon["x"]), str(mon["y"]),
                              str(mon["w"]), str(mon["h"]), "--seconds", "40", "--log", str(log)],
                             env={**os.environ, **env}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            hwnd = wait_window(ww, {exe_name.lower()}, before, 20)
            if hwnd is None:
                return {"name": name, "error": "no window"}
            row = record(ww, hwnd, facts, {"name": name, "truth_game": truth, "excluded": excluded})
            if name in ("game_borderless", "game_borderless_primary"):
                row["timings"] = timings(ww, hwnd, facts)
            return row
        finally:
            kill_tree(p.pid)
    return run


def chromium(name: str, path: str, proc: str):
    def run(ww, facts, mon):
        prof = WORK / f"{name}-profile"
        before = {t.hwnd for t in ww.windows() or []}
        p = subprocess.Popen([path, f"--user-data-dir={prof}", "--no-first-run", "--no-default-browser-check", "--new-window",
                              f"--window-position={mon['x'] + 100},{mon['y'] + 100}", "--start-fullscreen", "about:blank"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            hwnd = wait_window(ww, {proc}, before, 20, covering=True)
            if hwnd is None:
                return {"name": name, "error": "no fullscreen window"}
            return record(ww, hwnd, facts, {"name": name, "truth_game": False, "excluded": False})
        finally:
            kill_tree(p.pid)
    return run


def maximized_app(name: str, argv_fn, proc: str):
    """Launch, move to the right monitor (no taskbar there: its work area is the whole monitor, as
    with an auto-hidden taskbar) and maximize."""
    def run(ww, facts, mon):
        before = {t.hwnd for t in ww.windows() or []}
        p = subprocess.Popen(argv_fn(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        hwnd = None
        try:
            hwnd = wait_window(ww, {proc}, before, 40)
            if hwnd is None:
                return {"name": name, "error": "no window"}
            time.sleep(3.0)
            w.place(hwnd, mon["x"] + 100, mon["y"] + 100, 1600, 1000)
            time.sleep(0.5)
            user32.ShowWindow(hwnd, SW_MAXIMIZE)
            if not wait_covering(ww, hwnd):
                return {"name": name, "error": "maximized window does not cover"}
            return record(ww, hwnd, facts, {"name": name, "truth_game": False, "excluded": False})
        finally:
            kill_tree(p.pid)
    return run


def passive(name: str, proc: str):
    """An already open window of the user's, read without focusing or moving it."""
    def run(ww, facts, _mon):
        mons = monitors()
        for t in ww.windows() or []:
            if t.process == proc and not t.minimized and gaming.covers_monitor(t.rect, mons) is not None:
                info = ww._info(t.hwnd)  # noqa: SLF001
                cls = ctypes.create_unicode_buffer(256)
                user32.GetClassNameW(t.hwnd, cls, 256)
                return {"name": name, "truth_game": False, "excluded": False, "source": "passive", "foreground": False,
                        "process": info.process, "rect": [info.rect.left, info.rect.top, info.rect.right, info.rect.bottom],
                        "has_frame": info.has_frame, "exclusive_fullscreen": False, "quns": None, "class": cls.value,
                        "exe_dir_kind": dir_kind(exe_path(info.pid)), **facts.of(exe_path(info.pid)),
                        "today_is_game": gaming.is_game_window(info, mons, gaming.GameSettings())}
        return {"name": name, "error": "no covering window"}
    return run


def f11_app(name: str, argv_fn, proc: str, *, maximize_case: str | None = None, kill: bool = True):
    """Launch, move to the right monitor, F11; optionally also record the window maximized (not F11)."""
    def run(ww, facts, mon):
        before = {t.hwnd for t in ww.windows() or []}
        p = subprocess.Popen(argv_fn(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        rows, hwnd = [], None
        try:
            hwnd = wait_window(ww, {proc}, before, 40)
            if hwnd is None:
                return {"name": name, "error": "no window"}
            time.sleep(3.0)  # let the app finish its first paint before the key
            w.place(hwnd, mon["x"] + 100, mon["y"] + 100, 1600, 1000)
            time.sleep(0.5)
            if not w.focus(hwnd):
                w.focus(hwnd)
            time.sleep(0.3)
            for _ in range(3):
                w.focus(hwnd)
                time.sleep(0.3)
                if user32.GetForegroundWindow() == hwnd:
                    press(VK_F11)
                if wait_covering(ww, hwnd, 4.0):
                    break
            if not wait_covering(ww, hwnd, 1.0):
                print(f"  fg match {user32.GetForegroundWindow() == hwnd}", flush=True)
                rows.append({"name": name, "error": "F11 did not cover a monitor"})
            else:
                rows.append(record(ww, hwnd, facts, {"name": name, "truth_game": False, "excluded": False}))
            if maximize_case:
                w.focus(hwnd)
                time.sleep(0.2)
                if user32.GetForegroundWindow() == hwnd:
                    w.press_key(VK_F11)  # leave fullscreen
                time.sleep(1.0)
                user32.ShowWindow(hwnd, SW_MAXIMIZE)
                time.sleep(1.0)
                rows.append(record(ww, hwnd, facts, {"name": maximize_case, "truth_game": False, "excluded": False}))
            return rows
        finally:
            if kill:
                kill_tree(p.pid)
            if hwnd and user32.IsWindow(hwnd):
                w.close_window(hwnd)
    return run


def terminal(ww, facts, mon):
    before = {t.hwnd for t in ww.windows() or []}
    subprocess.Popen([WT, "-w", "new", "--pos", f"{mon['x'] + 100},{mon['y'] + 100}", "--fullscreen"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    hwnd = wait_window(ww, {"windowsterminal.exe"}, before, 20)
    try:
        if hwnd is None:
            return {"name": "terminal_f11", "error": "no window"}
        if not wait_covering(ww, hwnd):
            return {"name": "terminal_f11", "error": "not covering"}
        return record(ww, hwnd, facts, {"name": "terminal_f11", "truth_game": False, "excluded": False})
    finally:
        if hwnd:
            w.close_window(hwnd)


def photos(ww, facts, mon):
    from PIL import Image

    img = WORK / "ge-photo.png"
    Image.new("RGB", (1920, 1080), (40, 90, 140)).save(img)
    before = {t.hwnd for t in ww.windows() or []}
    os.startfile(f"ms-photos:viewer?fileName={img}")  # noqa: S606
    hwnd = wait_window(ww, {"photos.exe", "microsoft.photos.exe"}, before, 30)
    try:
        if hwnd is None:
            return {"name": "photos_fullscreen", "error": "no window"}
        time.sleep(2.0)
        w.place(hwnd, mon["x"] + 100, mon["y"] + 100, 1600, 1000)
        time.sleep(0.5)
        w.focus(hwnd)
        time.sleep(0.3)
        if user32.GetForegroundWindow() == hwnd:
            w.press_key(VK_F11)
        if not wait_covering(ww, hwnd):
            return {"name": "photos_fullscreen", "error": "F11 did not cover a monitor"}
        return record(ww, hwnd, facts, {"name": "photos_fullscreen", "truth_game": False, "excluded": False})
    finally:
        if hwnd:
            w.close_window(hwnd)


def big_picture(ww, facts, mon):
    before = {t.hwnd for t in ww.windows() or []}
    os.startfile("steam://open/bigpicture")  # noqa: S606
    hwnd = wait_window(ww, {"steamwebhelper.exe"}, before, 30, covering=True)
    try:
        if hwnd is None:
            return {"name": "steam_big_picture", "error": "no covering window"}
        time.sleep(2.0)
        return record(ww, hwnd, facts, {"name": "steam_big_picture", "truth_game": False, "excluded": False})
    finally:
        os.startfile("steam://close/bigpicture")  # noqa: S606
        time.sleep(3.0)


CASES = {
    "game_borderless": synthetic_game("game_borderless", "QualityGame.exe", "borderless", True, False),
    "game_exclusive": synthetic_game("game_exclusive", "QualityGame.exe", "exclusive", True, False),
    "excluded_borderless": synthetic_game("excluded_borderless", "QualityVault.exe", "borderless", False, True),
    "edge_f11": chromium("edge_f11", EDGE, "msedge.exe"),
    "chrome_f11": chromium("chrome_f11", CHROME, "chrome.exe"),
    "zen_f11": f11_app("zen_f11", lambda: [ZEN, "-no-remote", "-profile", str(WORK / "zen-profile"), "about:blank"], "zen.exe"),
    "code_f11": f11_app("code_f11", lambda: [CODE, "--user-data-dir", str(WORK / "code-data"), "--extensions-dir",
                                               str(WORK / "code-ext"), "--new-window", "--disable-workspace-trust"],
                        "code.exe"),
    "electron_frameless_maximized": maximized_app("electron_frameless_maximized", lambda: [
        CODE, "--user-data-dir", str(WORK / "code-data"), "--extensions-dir", str(WORK / "code-ext"), "--new-window",
        "--disable-workspace-trust"], "code.exe"),
    "discord_maximized_passive": passive("discord_maximized_passive", "discord.exe"),
    "terminal_f11": terminal,
    "photos_fullscreen": photos,
    "steam_big_picture": big_picture,
}
CASES["game_borderless_primary"] = on_primary(synthetic_game("game_borderless_primary", "QualityGame.exe", "borderless", True, False), "game_borderless_primary")
CASES["game_exclusive_primary"] = on_primary(synthetic_game("game_exclusive_primary", "QualityGame.exe", "exclusive", True, False), "game_exclusive_primary")
CASES["edge_f11_primary"] = on_primary(chromium("edge_f11_primary", EDGE, "msedge.exe"), "edge_f11_primary")


def observed_outer_wilds(facts: launchers.Facts) -> dict:
    """Outer Wilds as the quality runs saw it (not launched here): borderless over the primary
    monitor, judged a game by today's rule in every run; exe facts from the Steam manifests."""
    in_dirs = any(d.rstrip("\\").endswith("\\outer wilds") for d in facts.game_dirs)
    return {"name": "outer_wilds_observed", "truth_game": True, "excluded": False, "source": "runs", "process": "outerwilds.exe",
            "rect": [0, 0, 3840, 2160], "monitor": [0, 0, 3840, 2160], "has_frame": False, "exclusive_fullscreen": False,
            "quns": None, "class": None, "exe_dir_kind": "steam_library", "under_launcher_game_dir": in_dirs,
            "in_gameconfigstore": any(p.endswith("\\outerwilds.exe") for p in facts.gcs), "under_steam_root": False,
            "today_is_game": True}


def main() -> None:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].endswith(".json") else OUT
    names = [a for a in sys.argv[1:] if not a.endswith(".json")] or list(CASES)
    w.set_dpi_aware()
    WORK.mkdir(parents=True, exist_ok=True)
    ww, facts, mon = Win32Windows(), launchers.Facts(), right_monitor()
    prev = json.loads(out.read_text(encoding="utf-8")) if out.is_file() else {"cases": []}
    rows = {r["name"]: r for r in prev["cases"]}
    for name in names:
        print(name, flush=True)
        try:
            got = CASES[name](ww, facts, mon)
        except Exception as exc:  # noqa: BLE001 - one case must not stop the table
            got = {"name": name, "error": type(exc).__name__}
        for r in got if isinstance(got, list) else [got]:
            if "error" in r:
                print(f"  {r['name']}: {r['error']}", flush=True)
            rows[r["name"]] = r
        time.sleep(1.0)
    rows["outer_wilds_observed"] = observed_outer_wilds(facts)
    out.write_text(json.dumps({"right_monitor": [mon["x"], mon["y"], mon["w"], mon["h"]],
                               "right_work_equals_monitor": mon["work"] == [mon["x"], mon["y"], mon["w"], mon["h"]],
                               "cases": list(rows.values())}, indent=1), encoding="utf-8")
    shutil.rmtree(WORK, ignore_errors=True)


if __name__ == "__main__":
    main()
