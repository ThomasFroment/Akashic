"""Live check of #190: capture the corpus pages in real browser windows with Akashic's WGC capture.

    uv run python eval/small-text-ocr/live/capture.py --browser edge|chrome|zen --monitor left|primary
                  [--chain full|zen] [--limit N]

Pages: the harness corpus pages (harness/corpus.py page_specs / page_html, same text, layout and
themes) copied under live/pages/<chain>/ with one extra script: the page writes its id, which truth
lines fit the real viewport, the viewport size and devicePixelRatio into document.title, then
moves to the next page after DWELL_MS. A start page waits START_MS so the window can be placed.
The script reads the window title (GetWindowTextW) to know which page is on screen, waits for the
frames to settle, and grabs one frame with akashic.platform.win.wgc.WgcCaptures (raw pixels in
memory, as the capture does); frames are saved losslessly under live/captures/<browser>-<monitor>/
(gitignored) with live/captures/<browser>-<monitor>.json (numbers and ids only).

Browsers run windowed at a fixed size (1440 x 900 CSS px, i.e. x the monitor scale in device px),
not maximised, from a fresh profile under live/_profiles/ (removed afterwards); the window is made
topmost and focused so Chromium never sees it occluded, then closed. Only this script's own
windows are captured.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

LIVE = Path(__file__).resolve().parent
ROOT = LIVE.parent
REPO = ROOT.parents[1]
sys.path.insert(0, str(ROOT / "harness"))
sys.path.insert(0, str(REPO / "eval" / "quality"))
import corpus as corpus_mod  # noqa: E402
import winapi  # noqa: E402

from akashic.platform.win.wgc import WgcCaptures  # noqa: E402

BROWSERS = {
    "edge": Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    "chrome": Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    "zen": Path(r"C:\Program Files\Zen Browser\zen.exe"),
}
WINDOW_CSS = (1440, 900)
DWELL_MS = {"full": 2400, "zen": 4500}
START_MS = 9000
SETTLE_S = 1.0  # after the title changes
QUIET_S = 0.3  # no new frame for this long before grabbing
TITLE_RE = re.compile(r"^AKL\|([^|]+)\|([01]*)\|(\d+)x(\d+)\|([\d.]+)")
ZEN_PREFS = {
    "zen.welcome-screen.seen": True, "browser.shell.checkDefaultBrowser": False, "browser.aboutwelcome.enabled": False,
    "datareporting.policy.dataSubmissionEnabled": False, "datareporting.healthreport.uploadEnabled": False,
    "toolkit.telemetry.enabled": False, "app.update.auto": False, "browser.startup.homepage_override.mstone": "ignore",
    "startup.homepage_welcome_url": "", "browser.translations.enable": False, "extensions.update.enabled": False,
    "browser.sessionstore.resume_from_crash": False, "browser.tabs.warnOnClose": False,
}


def chain_specs(chain: str) -> list[dict]:
    specs = corpus_mod.page_specs()
    if chain == "zen":
        specs = [s for s in specs if s["lang"] == "mixed" and s["theme"] in ("dark", "light")]
    return specs


def build_pages(chain: str, limit: int | None) -> Path:
    specs = chain_specs(chain)[:limit]
    out = LIVE / "pages" / chain
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    ids = [s["id"] for s in specs] + ["done"]
    for spec, nxt in zip(specs, ids[1:], strict=True):
        js = ("<script>(function(){const b=JSON.parse(document.getElementById('boxes').textContent);let f='';"
              "for(const e of b.els){if(e.k==='line')f+=e.fits?'1':'0';}"
              f"document.title='AKL|{spec['id']}|'+f+'|'+innerWidth+'x'+innerHeight+'|'+devicePixelRatio;"
              f"setTimeout(function(){{location.href='{nxt}.html';}},{DWELL_MS[chain]});}})();</script>")
        (out / f"{spec['id']}.html").write_text(corpus_mod.page_html(spec).replace("</body>", js + "</body>"), encoding="utf-8")
    simple = "<!doctype html><meta charset='utf-8'><title>{t}</title><body style='background:#808080'>{b}</body>"
    (out / "start.html").write_text(simple.format(t="AKL|start|0|0x0|0", b=f"<script>setTimeout(function(){{location.href='{ids[0]}.html';}},{START_MS});</script>"), encoding="utf-8")
    (out / "done.html").write_text(simple.format(t="AKL|done|0|0x0|0", b=""), encoding="utf-8")
    return out


def title(hwnd: int) -> str:
    import ctypes

    buf = ctypes.create_unicode_buffer(512)
    winapi.user32.GetWindowTextW(hwnd, buf, 512)
    return buf.value


def window_rect(hwnd: int) -> tuple[int, int, int, int]:
    import ctypes
    from ctypes import wintypes

    rc = wintypes.RECT()
    winapi.user32.GetWindowRect(hwnd, ctypes.byref(rc))
    return rc.left, rc.top, rc.right, rc.bottom


def launch(browser: str, profile: Path, url: str, x: int, y: int, w: int, h: int) -> subprocess.Popen:
    exe = BROWSERS[browser]
    if browser == "zen":
        profile.mkdir(parents=True, exist_ok=True)
        prefs = "".join(f'user_pref("{k}", {json.dumps(v)});\n' for k, v in ZEN_PREFS.items())
        (profile / "user.js").write_text(prefs, encoding="utf-8")
        args = [str(exe), "-profile", str(profile), "-no-remote", "-new-instance", url]
    else:
        args = [str(exe), f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check",
                "--disable-features=Translate", "--disable-sync", "--disable-background-networking",
                "--disable-component-update", "--disable-extensions", f"--window-position={x},{y}",
                f"--window-size={w},{h}", "--new-window"]
        if browser == "edge":
            args.append("--guest")
        args.append(url)
    return subprocess.Popen(args)


def find_window(pid: int, exe: str, timeout: float = 40.0) -> int:
    """Our window: an AKL| page title in a process of this browser (Zen's launcher process exits, so
    the window's process is not always a descendant of the one we started)."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        pids = winapi.descendants({pid}) | {pid}
        for wnd in winapi.top_windows():
            if wnd["title"].startswith("AKL|") and not wnd["iconic"] and (
                    wnd["pid"] in pids or winapi.image_name(wnd["pid"]).lower() == exe.lower()):
                return wnd["hwnd"]
        time.sleep(0.2)
    raise RuntimeError("browser window not found")


def place_exact(hwnd: int, x: int, y: int, w: int, h: int) -> tuple[int, int, int, int]:
    for _ in range(8):
        winapi.place(hwnd, x, y, w, h, topmost=True)
        time.sleep(0.6)
        rc = window_rect(hwnd)
        if max(abs(rc[0] - x), abs(rc[1] - y), abs(rc[2] - x - w), abs(rc[3] - y - h)) <= 2:
            return rc
    return window_rect(hwnd)


def kill_tree(proc: subprocess.Popen, profile: Path) -> None:
    """Kill the started process tree (if still alive) and any process of ours left running with this profile."""
    if proc.poll() is None:
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, check=False)
    ps = (f"Get-CimInstance Win32_Process | Where-Object {{ $_.CommandLine -like '*{profile}*' }} | "
          "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }")
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, check=False)


def run(browser: str, monitor: str, chain: str, limit: int | None) -> None:
    winapi.set_dpi_aware()
    mon = next(m for m in winapi.monitors() if m["name"] == monitor)
    assert monitor in ("left", "primary"), "the right monitor belongs to another agent"
    scale = mon["scale"]
    w, h = round(WINDOW_CSS[0] * scale), round(WINDOW_CSS[1] * scale)
    x, y = mon["x"] + round(120 * scale), mon["y"] + round(100 * scale)
    pages = build_pages(chain, limit)
    tag = f"{browser}-{monitor}"
    out = LIVE / "captures" / tag
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    profile = LIVE / "_profiles" / tag
    shutil.rmtree(profile, ignore_errors=True)
    before = winapi.foreground()
    proc = launch(browser, profile, (pages / "start.html").as_uri(), x, y, w, h)
    caps = WgcCaptures()
    saver = ThreadPoolExecutor(2)
    meta: dict = {"browser": browser, "monitor": monitor, "scale": scale, "chain": chain, "window_css": WINDOW_CSS,
                  "pages": []}
    hwnd = 0
    try:
        hwnd = find_window(proc.pid, BROWSERS[browser].name)
        rc = place_exact(hwnd, x, y, w, h)
        winapi.focus(hwnd)
        meta["window_rect"] = rc
        meta["client_rect"] = winapi.client_rect(hwnd)
        meta["placed_on"] = winapi.monitor_of_rect(rc)
        if not caps.start(hwnd, 0):
            raise RuntimeError(f"WGC start failed: {caps.fault_counts()}")
        seen: set[str] = set()
        t_end = time.monotonic() + START_MS / 1000 + 40 + len(chain_specs(chain)[:limit]) * (DWELL_MS[chain] / 1000 + 2)
        while time.monotonic() < t_end:
            m = TITLE_RE.match(title(hwnd))
            if not m or m.group(1) in seen or m.group(1) == "start":
                time.sleep(0.03)
                continue
            pid_ = m.group(1)
            seen.add(pid_)
            if pid_ == "done":
                break
            t_change = time.monotonic()
            time.sleep(SETTLE_S)
            seq = caps.latest_seq(hwnd)
            quiet = False
            for _ in range(5):
                time.sleep(QUIET_S)
                now = caps.latest_seq(hwnd)
                if now == seq:
                    quiet = True
                    break
                seq = now
            frame = caps.grab(hwnd)
            m2 = TITLE_RE.match(title(hwnd))
            if frame is None or not m2 or m2.group(1) != pid_:
                print(f"  {pid_}: missed (frame {frame is not None}, title now {title(hwnd)[:40]!r})", flush=True)
                continue
            img = frame.image
            saver.submit(img.save, out / f"{pid_}.png", compress_level=1)
            meta["pages"].append({"id": pid_, "fits": m.group(2), "viewport_css": [int(m.group(3)), int(m.group(4))],
                                  "dpr": float(m.group(5)), "size": list(img.size), "frame_after_change_s": round(frame.at - t_change, 3), "quiet": quiet,
                                  "foreground": winapi.foreground() == hwnd})
            print(f"  {pid_}: {img.size} vp {m.group(3)}x{m.group(4)} dpr {m.group(5)}", flush=True)
    finally:
        caps.close()
        saver.shutdown(wait=True)
        if hwnd:
            winapi.close_window(hwnd)
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            pass
        time.sleep(3.0)
        kill_tree(proc, profile)
        time.sleep(1.0)
        shutil.rmtree(profile, ignore_errors=True)
        if before:
            winapi.focus(before)
    (LIVE / "captures" / f"{tag}.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
    print(f"{tag}: {len(meta['pages'])} pages captured, window {meta.get('window_rect')} on {meta.get('placed_on')}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--browser", required=True, choices=sorted(BROWSERS))
    ap.add_argument("--monitor", required=True, choices=("left", "primary"))
    ap.add_argument("--chain", default="full", choices=("full", "zen"))
    ap.add_argument("--limit", type=int)
    a = ap.parse_args()
    run(a.browser, a.monitor, a.chain, a.limit)
