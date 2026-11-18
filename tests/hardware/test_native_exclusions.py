"""Native exclusion checks: real monitors and DPI, real UI Automation, hosted apps, audio, browsers.

Run on the target PC only:

    uv run pytest tests/hardware/test_native_exclusions.py -m hardware --no-cov -s

Only windows this test creates are ever examined: the real window source is wrapped so every
other window looks absent, and nothing here reads, stores or prints a user window's title, text,
URL or process name. Browser checks start a separate throwaway profile and load a synthetic page.
Each check skips (and records UNAVAILABLE) when its hardware or permission is missing; a skip is
never a pass. With ``AKASHIC_M01_EVIDENCE=<dir>`` the content-free outcomes are written as JSON.
"""

from __future__ import annotations

import contextlib
import ctypes
import http.server
import json
import os
import subprocess
import threading
import time
from ctypes import wintypes
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest
from capture_fakes import FakeOcr
from sink_spy import SinkSpy
from synthetic_window import bring_to_front, launch_window, right_monitor

from akashic.capture.browser_state import BrowserState
from akashic.capture.exclusions import Exclusions
from akashic.capture.ocr_gate import OcrGate
from akashic.capture.ports import Rect, WindowInfo
from akashic.capture.screen import ScreenCapture, Settings
from akashic.capture.screen_writer import ScreenWriter
from akashic.config import Exclusions as UserExclusions
from akashic.config import Paths
from akashic.platform.win.audio import WindowsAudioSessions, comtypes
from akashic.platform.win.displays import Win32Displays, set_dpi_aware
from akashic.platform.win.events import Win32WindowEvents
from akashic.platform.win.process import image_name
from akashic.platform.win.uia import WindowsUia
from akashic.platform.win.wgc import WgcCaptures
from akashic.platform.win.windows import Win32Windows
from akashic.storage import db
from akashic.storage.crypto import DataKey
from akashic.storage.frames import FrameStore
from akashic.storage.media import MediaStore
from akashic.storage.monitors import MonitorLog
from akashic.storage.sessions import GameLog

if TYPE_CHECKING:
    from collections.abc import Iterator

    from akashic.capture.ports import TopWindow

pytestmark = pytest.mark.hardware

EVIDENCE: dict[str, object] = {}
user32 = ctypes.WinDLL("user32", use_last_error=True)
shcore = ctypes.WinDLL("shcore")


def record(name: str, value: object) -> None:
    EVIDENCE[name] = value
    target = os.environ.get("AKASHIC_M01_EVIDENCE")
    if target:
        Path(target).mkdir(parents=True, exist_ok=True)
        (Path(target) / "native-m01.json").write_text(
            json.dumps(EVIDENCE, indent=2, sort_keys=True), encoding="utf-8"
        )


def monitor_facts() -> list[dict[str, int]]:
    """Rectangle and effective DPI of every monitor (hardware facts, no content)."""
    set_dpi_aware()
    found: list[dict[str, int]] = []
    proc_type = ctypes.WINFUNCTYPE(
        wintypes.BOOL,
        wintypes.HMONITOR,
        wintypes.HDC,
        ctypes.POINTER(wintypes.RECT),
        wintypes.LPARAM,
    )

    def visit(handle: int, hdc: int, rect: object, param: int) -> bool:
        del hdc, param
        dpi_x, dpi_y = ctypes.c_uint(), ctypes.c_uint()
        shcore.GetDpiForMonitor(handle, 0, ctypes.byref(dpi_x), ctypes.byref(dpi_y))
        r: Any = cast("Any", rect).contents
        found.append(
            {"left": r.left, "top": r.top, "right": r.right, "bottom": r.bottom, "dpi": dpi_x.value}
        )
        return True

    user32.EnumDisplayMonitors(None, None, proc_type(visit), 0)
    return found


class Owned:
    """The real window source and window facts, restricted to windows this test created."""

    def __init__(self, hwnds: set[int]) -> None:
        self.real = Win32Windows()
        self.hwnds = hwnds

    def focused_window(self) -> WindowInfo | None:
        window = self.real.focused_window()
        return window if window is not None and window.hwnd in self.hwnds else None

    def windows(self) -> list[TopWindow] | None:
        listed = self.real.windows()
        return None if listed is None else [w for w in listed if w.hwnd in self.hwnds]

    def cursor_pos(self) -> tuple[int, int]:
        return self.real.cursor_pos()

    def last_input_ms(self) -> int:
        return self.real.last_input_ms()

    def is_locked(self) -> bool:
        return self.real.is_locked()

    def awake_ms(self) -> int:
        return self.real.awake_ms()


@pytest.fixture
def synthetic() -> Iterator[list[subprocess.Popen[str]]]:
    processes: list[subprocess.Popen[str]] = []
    yield processes
    for process in processes:
        process.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=10)
        if process.stdout:
            process.stdout.close()


def test_focus_and_exclusion_transitions_on_three_mixed_dpi_monitors(  # noqa: PLR0915
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, synthetic: list[subprocess.Popen[str]]
) -> None:
    monitors = monitor_facts()
    dpis = sorted({m["dpi"] for m in monitors})
    record(
        "monitors",
        {
            "count": len(monitors),
            "dpi_values": dpis,
            "negative_x": any(m["left"] < 0 for m in monitors),
        },
    )
    if len(monitors) < 3 or len(dpis) < 2:
        record("monitor_transitions", "UNAVAILABLE: needs 3 or more monitors with mixed DPI")
        pytest.skip("needs 3 or more monitors with mixed DPI")
    first = next(m for m in monitors if m["left"] == 0)
    others = sorted((m for m in monitors if m is not first), key=lambda m: m["left"])
    left_one, right_one = others[0], others[-1]  # negative-x monitor and the right-hand one

    def box(m: dict[str, int], dx: int = 200, dy: int = 200, w: int = 900, h: int = 500) -> Rect:
        return Rect(m["left"] + dx, m["top"] + dy, m["left"] + dx + w, m["top"] + dy + h)

    # A sign-in window centred on the first monitor and wide enough to reach into the right-hand
    # monitor; a plain window on the left (negative-x) monitor; a manager-titled one.
    centre_x = (first["left"] + first["right"]) // 2
    reach = (right_one["left"] + right_one["right"]) // 2 + 300
    spanning = Rect(2 * centre_x - reach, first["top"] + 300, reach, first["top"] + 900)
    windows = {
        "password": launch_window("Akashic M01 synthetic sign in", spanning, "password"),
        "plain": launch_window("Akashic M01 synthetic notes", box(left_one), "plain"),
        "manager": launch_window("Akashic M01 synthetic Bitwarden", box(right_one), "plain"),
    }
    synthetic.extend(p for p, _ in windows.values())
    hwnds = {h for _, h in windows.values()}
    time.sleep(1.5)  # let the windows paint

    set_dpi_aware()
    key = DataKey.generate()
    paths = Paths(tmp_path / "data")
    paths.ensure()
    conn = db.connect(paths.db_file, key.subkey("db"))
    db.migrate(conn)
    spy = SinkSpy(paths)
    spy.watch(conn)
    spy.install(monkeypatch)
    writer = ScreenWriter(
        FrameStore(conn, MediaStore(paths.media_dir, key.subkey("media"))),
        MonitorLog(conn),
        OcrGate(FakeOcr(text="")),
    )
    owned = Owned(hwnds)
    capture = ScreenCapture(
        displays=Win32Displays(),
        windows=owned,
        facts=owned,
        events=Win32WindowEvents(),
        captures=WgcCaptures(),
        new_uia=WindowsUia,
        writer=writer,
        exclusions=Exclusions(UserExclusions(), "http://127.0.0.1:5123"),
        settings=Settings(min_gap_s=0.2),
        browsers=BrowserState(),
        games=GameLog(conn),
        on_own_address=lambda _own: None,
    )
    steps: dict[str, object] = {}
    clock = [1_000.0]

    def step(name: str, which: str) -> None:
        focused = bring_to_front(windows[which][1])
        before = conn.execute("SELECT count(*) FROM frames").fetchone()[0]
        clock[0] += 20.0
        for _ in range(10):  # the first passes start the window captures, later ones paint them
            capture.tick(clock[0])
            clock[0] += 0.3
            time.sleep(0.3)
        stop = threading.Event()
        stop.set()
        writer.run(stop)  # store what the loop queued
        total = conn.execute("SELECT count(*) FROM frames").fetchone()[0]
        steps[name] = {
            "focus_acquired": focused,
            "frames_stored": total - before,
            "monitors_with_the_window": conn.execute(
                "SELECT count(DISTINCT f.monitor_id) FROM frames f"
                " JOIN frame_windows w ON w.frame_id = f.id WHERE w.title = ? AND f.id > ?",
                (f"Akashic M01 synthetic {_TITLES[which]}", before),
            ).fetchone()[0],
            "files_total": len(spy.files),
        }

    step("1_plain_window_on_negative_x_monitor", "plain")
    step("2_password_window_spanning_monitors", "password")
    step("3_back_to_plain_window", "plain")
    step("4_manager_titled_window", "manager")
    step("5_password_window_again", "password")
    record("monitor_transitions", steps)
    capture.close()
    conn.close()

    # No title lists and no password-field outcome: these windows are judged by process only.
    outcome = cast("dict[str, dict[str, Any]]", steps)
    assert all(s["focus_acquired"] for s in outcome.values()), "could not focus a synthetic window"
    assert outcome["1_plain_window_on_negative_x_monitor"]["frames_stored"] >= 1
    password = outcome["2_password_window_spanning_monitors"]
    assert password["frames_stored"] >= 1
    # The window reaches into the right-hand monitor, so it is listed on at least two monitors.
    assert password["monitors_with_the_window"] >= 2
    assert outcome["4_manager_titled_window"]["frames_stored"] >= 1


_TITLES = {"plain": "notes", "password": "sign in", "manager": "Bitwarden"}


def test_hosted_app_resolves_to_its_own_process() -> None:
    """Calculator is a UWP app: its window is owned by applicationframehost.exe."""
    windows = Win32Windows()
    before = enumerate_frame_windows()
    subprocess.Popen(["calc.exe"])  # noqa: S607 - the Windows calculator, a hosted app
    deadline = time.monotonic() + 20
    opened: list[tuple[int, str]] = []
    while time.monotonic() < deadline and not opened:
        time.sleep(0.5)
        for hwnd in enumerate_frame_windows():
            if hwnd in before:
                continue
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            raw = image_name(pid.value)
            if raw == "applicationframehost.exe":
                opened.append((hwnd, raw))
    if not opened:
        record("hosted_app", "UNAVAILABLE: Calculator did not open a new hosted window")
        pytest.skip("no hosted app window appeared")
    resolved = {h: top_window(windows, h) for h, _ in opened}
    record(
        "hosted_app",
        {
            "new_frame_host_windows": len(opened),
            "frame_host_process": "applicationframehost.exe",
            "resolved_processes": sorted(w.process for w in resolved.values() if w is not None),
        },
    )
    for hwnd, info in resolved.items():
        if info is not None and "calc" in info.process:
            user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE: only the window this test opened
    assert any(w is not None and "calc" in w.process for w in resolved.values())
    assert all(w is not None and w.process != "applicationframehost.exe" for w in resolved.values())


def top_window(windows: Win32Windows, hwnd: int) -> TopWindow | None:
    return next((w for w in windows.windows() or [] if w.hwnd == hwnd), None)


def enumerate_frame_windows() -> list[int]:  # window handles only; no titles are read
    found: list[int] = []
    proc_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def visit(hwnd: int, param: int) -> bool:
        del param
        buffer = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(hwnd, buffer, 64)
        if buffer.value == "ApplicationFrameWindow" and user32.IsWindowVisible(hwnd):
            found.append(int(hwnd))
        return True

    user32.EnumWindows(proc_type(visit), 0)
    return found


def test_audio_endpoint_enumeration_and_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Real endpoint enumeration, then a real comtypes COMError from the enumerator."""
    from akashic.platform.win import audio as win_audio  # noqa: PLC0415

    sessions = WindowsAudioSessions()
    result = sessions.active_processes()
    endpoints = len(list(sessions._managers))  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
    facts: dict[str, object] = {
        "active_render_endpoints": endpoints,
        "enumeration_returned": "None" if result is None else "dict",
        "audible_process_count": None  # names are not recorded
        if result is None
        else sum(len(names) for names in result.values()),
    }

    def broken() -> None:
        raise comtypes.COMError(-2004287480, "AUDCLNT_E_DEVICE_INVALIDATED", (None,) * 5)

    monkeypatch.setattr(win_audio, "_enumerator", broken)
    failing = WindowsAudioSessions()
    facts["after_forced_com_error"] = "None" if failing.active_processes() is None else "not None"
    record("audio_endpoints", facts)
    assert result is not None
    assert facts["after_forced_com_error"] == "None"
    if endpoints == 0:
        pytest.skip("no active playback endpoint: enumeration succeeded with an empty list only")


class _Page(http.server.BaseHTTPRequestHandler):
    """The synthetic app page: an in-page input that imitates the browser's own address bar."""

    def do_GET(self) -> None:
        body = (
            b"<title>Synthetic app window</title><input id=i class=OmniboxViewViews>"
            b"<script>i.value='https://www.paypal.'+'com/x'</script>"
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        del format, args


def test_app_name_listing_is_retried_not_remembered(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real Start-menu app listing: it succeeds, a missing id is a miss that is retried
    only after the reload interval (not cached). Only counts are recorded."""
    from akashic.platform.win.media import NAMES_RELOAD_S, WindowsMedia  # noqa: PLC0415

    now = [0.0]
    media = WindowsMedia(clock=lambda: now[0])
    real_load_names = media._load_names
    listings: list[dict[str, str] | None] = []

    def counting_load_names() -> dict[str, str] | None:
        listings.append(real_load_names())
        return listings[-1]

    monkeypatch.setattr(media, "_load_names", counting_load_names)
    missing_id = "Akashic.Synthetic.Missing!App"
    facts: dict[str, object] = {}
    facts["missing_id_resolves"] = media.app_name(missing_id) is not None
    first_listing = listings[0]
    facts["listing_succeeded"] = first_listing is not None
    facts["installed_app_entries"] = len(first_listing or {})
    facts["listings_at_0s"] = len(listings)
    now[0] = 5
    media.app_name(missing_id)
    facts["listings_at_5s"] = len(listings)
    now[0] = NAMES_RELOAD_S + 1
    media.app_name(missing_id)
    facts["listings_after_reload_interval"] = len(listings)
    record("app_name_listing", facts)
    assert facts["listing_succeeded"] is True
    assert facts["missing_id_resolves"] is False
    assert (
        facts["listings_at_0s"],
        facts["listings_at_5s"],
        facts["listings_after_reload_interval"],
    ) == (1, 1, 2)


def find_browser_windows(root_pids: set[int]) -> list[tuple[int, str, str]]:
    """(hwnd, class, title) of visible top-level windows of the given processes."""
    found: list[tuple[int, str, str]] = []
    proc_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def visit(hwnd: int, param: int) -> bool:
        del param
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in root_pids and user32.IsWindowVisible(hwnd):
            cls = ctypes.create_unicode_buffer(128)
            user32.GetClassNameW(hwnd, cls, 128)
            title = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(hwnd, title, 512)
            if cls.value == "Chrome_WidgetWin_1" and title.value:
                found.append((int(hwnd), cls.value, title.value))
        return True

    user32.EnumWindows(proc_type(visit), 0)
    return found


def profile_pids(profile: Path) -> set[int]:
    script = (
        "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*"
        + str(profile).replace("'", "''")
        + "*' } | ForEach-Object { $_.ProcessId }"
    )
    out = subprocess.run(  # noqa: S603 - fixed program; the profile path is ours
        ["powershell", "-NoProfile", "-Command", script],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    return {int(line) for line in out.split() if line.isdigit()}


@pytest.mark.parametrize(
    ("exe", "label"),
    [
        (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe", "edge"),
        (r"C:\Program Files\Google\Chrome\Application\chrome.exe", "chrome"),
    ],
)
def test_real_browser_layouts_on_synthetic_pages(  # noqa: PLR0915, PLR0912, C901
    tmp_path: Path, exe: str, label: str
) -> None:
    """A throwaway profile shows synthetic pages: normal, private and app (no address bar).
    Records how each browser decorates its titles and what the omnibox reader returns."""
    if not Path(exe).exists():
        record(f"browser_{label}", "UNAVAILABLE: browser not installed")
        pytest.skip(f"{label} is not installed")
    set_dpi_aware()
    area = right_monitor()
    profile = tmp_path / f"{label}-profile"
    pages = {
        "normal": "data:text/html,<title>Bitwarden vs 1Password</title><p>synthetic",
        "named_incognito": "data:text/html,<title>Incognito</title><p>synthetic",
        "private": "data:text/html,<title>Plainpage</title><p>synthetic",
        "app": "",  # set below: served from a loopback server so no address bar is shown
    }
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Page)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    pages["app"] = f"http://127.0.0.1:{server.server_address[1]}/"
    base = [
        exe,
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-sync",
        f"--window-position={area.left + 100},{area.top + 100}",
        "--window-size=900,600",
    ]
    launches: dict[str, list[str]] = {
        "normal": [*base, "--new-window", pages["normal"]],
        "named_incognito": [*base, "--new-window", pages["named_incognito"]],
        "private": [*base, "--inprivate" if label == "edge" else "--incognito", pages["private"]],
        "app": [*base, f"--app={pages['app']}"],
    }
    uia = WindowsUia()
    windows = Win32Windows()
    exclusions = Exclusions(UserExclusions())
    results: dict[str, object] = {}
    try:
        for name, argv in launches.items():
            subprocess.Popen(argv)  # noqa: S603 - a throwaway profile, synthetic data: pages only
            seen: list[tuple[int, str, str]] = []
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline and not seen:
                time.sleep(1.0)
                pids = profile_pids(profile)
                titles = find_browser_windows(pids)
                wanted = {
                    "normal": "Bitwarden",
                    "named_incognito": "Incognito",
                    "private": "Plainpage",
                    "app": "Synthetic app",
                }[name]
                seen = [t for t in titles if wanted.casefold() in t[2].casefold()]
            if not seen:
                results[name] = "UNAVAILABLE: window did not appear"
                continue
            hwnd, _, title = seen[0]
            top = top_window(windows, hwnd)
            assert top is not None
            info = WindowInfo(top.hwnd, top.process, top.title, top.rect)
            url = uia.read_url(info, "chromium")
            caption = uia.browser_caption(info, "chromium")
            results[name] = {
                "process": info.process,
                "title": title,  # synthetic title of our own page, plus the browser's decoration
                "is_private_title": exclusions.is_private_title(title),
                "check_window": exclusions.check_window(info.process, title),
                "omnibox_readable": url is not None,
                "omnibox_is_page_text": bool(
                    url and "paypal.com" in url.casefold() and name == "app"
                ),
                "check_browser": exclusions.check_browser(url),
                "frame_caption": caption,  # UI Automation name of the browser frame
                "caption_is_private": caption is not None and exclusions.is_private_title(caption),
            }
    finally:
        server.shutdown()
        pids = profile_pids(profile)
        for pid in pids:
            subprocess.run(  # noqa: S603 - only the processes of this throwaway profile
                ["taskkill", "/PID", str(pid), "/T", "/F"],  # noqa: S607
                capture_output=True,
                check=False,
            )
    record(f"browser_{label}", results)
    normal = results.get("normal")
    if isinstance(normal, dict):
        assert normal["check_window"] is None, "a page titled Bitwarden vs 1Password must be kept"
        assert normal["is_private_title"] is False
    named = results.get("named_incognito")
    if isinstance(named, dict):
        assert named["check_window"] is None, "a page titled Incognito must be kept"
    private = results.get("private")
    if isinstance(private, dict):
        # Chrome 157 writes no private marker into its Win32 title, only into its UI Automation
        # frame name; Edge writes "[InPrivate]" into both. The caption must catch either.
        assert private["caption_is_private"] is True
        if label == "edge":
            assert private["is_private_title"] is True
    if isinstance(normal, dict):
        assert normal["caption_is_private"] is False
    if isinstance(named, dict):
        assert named["caption_is_private"] is False
    app = results.get("app")
    if isinstance(app, dict):
        assert app["omnibox_is_page_text"] is False, "an in-page input was read as the address"
