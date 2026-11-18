"""Driver: plays scenario.json on a monotonic schedule and logs every action to truth/driver.jsonl.

Input (clicks, keys) goes only to windows the harness owns. Real apps (Discord, Steam, Outer Wilds,
Calculator, Notepad) are only launched, moved, focused and closed. Browsers get throwaway profiles
and only the allowed flags (profile, no-first-run, default-browser-check, position, size, --app=,
private modes).
"""

from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import assets
import observer
import winapi as w
from common import QUALITY, REPO, JsonlWriter, load_local, now_ms, renamed_python

site_module = load_local("site")  # the harness's site.py, not the stdlib module of that name
Site, nonce = site_module.Site, site_module.nonce

LATE_MS = 1000
LAUNCH_WAIT_S = 25
CLICK_DELAY_S = 1.5  # after the step starts, so the focus and the page are settled
CLICK_WAIT_S = 20  # for the page to report its geometry for this step
POPUP_OPEN_S = 2
PROMPT_WAIT_S = 180
VK_ESCAPE = 0x1B
BROWSERS = {
    "chrome": (r"C:\Program Files\Google\Chrome\Application\chrome.exe", "--incognito"),
    "edge": (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe", "--inprivate"),
    "zen": (r"C:\Program Files\Zen Browser\zen.exe", "-private-window"),
}
VSCODE_SETTINGS = {
    "workbench.startupEditor": "none", "workbench.welcomePage.walkthroughs.openOnInstall": False,
    "workbench.tips.enabled": False, "update.showReleaseNotes": False, "update.mode": "none",
    "telemetry.telemetryLevel": "off", "chat.disableAIFeatures": True, "chat.commandCenter.enabled": False,
    "workbench.secondarySideBar.defaultVisibility": "hidden", "extensions.ignoreRecommendations": True,
    "security.workspace.trust.enabled": False, "git.openRepositoryInParentFolders": "never",
}
PRIVATE_APP = {"chrome_incognito": "chrome", "edge_inprivate": "edge", "zen_private": "zen"}
TITLE_RE = {"calculator": r"Calculator|Calculatrice", "steam": r"^Steam", "notepad": r"Notepad|Bloc-notes", "explorer": r"quality-folder"}
# one window per user: an already open or minimized one is taken, so `before` cannot exclude it
SINGLE_INSTANCE_EXES = {"calculator": ("applicationframehost.exe", "calculatorapp.exe"), "steam": ("steam.exe",)}
LEFT_OPEN = ("discord", "outer_wilds")  # run.py closes Outer Wilds; Discord was the user's
STEP_ACTIONS = ("focus", "video", "pip", "title_change", "excluded_pages", "pause", "busy_disk", "canary", "lock", "sleep", "device_change")


class Prompt:
    """Harness-owned topmost window on the primary monitor for attended instructions."""

    def __init__(self, rect: tuple[int, int, int, int]) -> None:
        self.q: queue.Queue = queue.Queue()
        self.rect = rect
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        import tkinter as tk

        root = tk.Tk()
        root.title("Quality harness prompt")
        x, y, wd, ht = self.rect
        root.geometry(f"700x160+{x + (wd - 700) // 2}+{y + 60}")
        root.attributes("-topmost", True)
        label = tk.Label(root, text="", font=("Segoe UI", 18), wraplength=660)
        label.pack(expand=True)

        def poll() -> None:
            while not self.q.empty():
                text = self.q.get()
                label.config(text=text or "")
                root.deiconify() if text else root.withdraw()
            root.after(100, poll)

        root.withdraw()
        poll()
        root.mainloop()

    def show(self, text: str) -> None:
        self.q.put(text)

    def hide(self) -> None:
        self.q.put(None)


class Driver:
    def __init__(self, scn: dict, site: Site, obs: observer.Observer, host, run_dir: Path, truth_dir: Path, machine: dict, store_dir: Path) -> None:
        self.scn, self.site, self.obs, self.host = scn, site, obs, host
        self.run_dir, self.store_dir = run_dir, store_dir
        self.log = JsonlWriter(truth_dir / "driver.jsonl")
        self.truth_dir = truth_dir
        self.monitors = {m["name"]: m for m in machine["monitors"]}
        self.windows: dict[str, dict] = {}  # harness window id -> {hwnd, pid, proc, exe}
        self.page_step: dict[str, str] = {}  # harness window id -> step id of the page it shows
        self.popups: dict[str, subprocess.Popen] = {}  # step id -> synthetic owned popup
        self.game_proc: subprocess.Popen | None = None  # the running game session
        self.procs: list[subprocess.Popen] = []
        self.steps = {s["id"]: s for s in scn["steps"]}
        self.window_app = {x["id"]: x["app"] for x in scn["windows"]}
        self.window_mon = {x["id"]: x["monitor"] for x in scn["windows"]}
        self.window_look = {x["id"]: x.get("look", {}) for x in scn["windows"]}
        self.late = self.misses = 0
        self.shift_ms = 0
        self.exes_dir = run_dir / "exes"
        self.prompt = Prompt(self._rect("primary"))
        self.third_endpoint = machine.get("voice_elsewhere_endpoint")
        self.start_mono = 0.0
        self.start_wall = 0

    # --- logging and geometry ---

    def act(self, action: str, wid: str = "", lateness: int = 0, **extra: object) -> None:
        win = self.windows.get(wid, {})
        proc = win.get("proc")
        self.log.write(action=action, window=wid, hwnd=win.get("hwnd"), pid=proc.pid if proc else win.get("pid"), exe=win.get("exe"), lateness_ms=lateness, **extra)

    def _rect(self, mon: str) -> tuple[int, int, int, int]:
        """The monitor's work area: windows placed on the whole monitor slide under the taskbar."""
        m = self.monitors[mon]
        return tuple(m["work"]) if m.get("work") else (m["x"], m["y"], m["w"], m["h"])

    def _full(self, mon: str) -> tuple[int, int, int, int]:
        m = self.monitors[mon]
        return m["x"], m["y"], m["w"], m["h"]

    def slot_rect(self, mon: str, index: int, count: int, full: bool) -> tuple[int, int, int, int]:
        if full:
            return self._full(mon)
        x, y, wd, ht = self._rect(mon)
        if count < 2:
            return x, y, wd, ht
        return x + index * wd // 2, y, wd // 2, ht

    # --- launching ---

    def _spawn(self, args: list[str], kind: str, env: dict | None = None) -> subprocess.Popen:
        proc = subprocess.Popen(args, env={**os.environ, **(env or {})}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.procs.append(proc)
        self.obs.reg.add_root(kind, proc.pid)
        return proc

    def _find(self, before: set[int], match, allow_iconic: bool = False) -> dict | None:
        deadline = time.monotonic() + LAUNCH_WAIT_S
        while time.monotonic() < deadline:
            for win in w.top_windows():
                if win["hwnd"] not in before and (allow_iconic or not win["iconic"]) and match(win):
                    return win
            time.sleep(0.25)
        return None

    def _register(self, wid: str, win: dict | None, proc: subprocess.Popen | None, kind: str) -> None:
        if not win:
            self.act("launch_failed", wid)
            return
        self.obs.reg.add_window(win["hwnd"], kind)
        self.windows[wid] = {"hwnd": win["hwnd"], "pid": win["pid"], "proc": proc, "exe": w.image_name(win["pid"])}

    def prepare(self) -> None:
        """Before the drive: build every video, then start every window the scenario shows, so no step waits for a launch."""
        built: set[str] = set()
        for s in self.scn["steps"]:
            video = s["params"].get("video") if s["kind"] == "focus" else None
            if video and video["title_id"] not in built:
                built.add(video["title_id"])
                self.site.prepare_video(s)
        first_use: dict[str, dict] = {}
        for s in self.scn["steps"]:
            if s["kind"] != "focus":
                continue
            for k, wid in enumerate(s["windows"]):
                first_use.setdefault(wid, s if k == 0 else self.fallback_step(wid))
        for wid, step in first_use.items():
            if self.window_app[wid] not in ("hung", "game", "outer_wilds", "elevated"):
                self.ensure(wid, step)

    def ensure(self, wid: str, step: dict) -> None:
        if wid in self.windows and w.user32.IsWindowVisible(self.windows[wid]["hwnd"]):
            return
        app = self.window_app[wid]
        mon = self.window_mon[wid]
        before = {x["hwnd"] for x in w.top_windows()}
        if app in BROWSERS or app == "assistant":
            self._launch_browser(wid, app, mon, step, before)
        elif app == "discord":
            update = Path(os.environ["LOCALAPPDATA"]) / "Discord" / "Update.exe"
            self._spawn([str(update), "--processStart", "Discord.exe"], "real")
            # an already running Discord keeps its window, which was in `before`: take any top window of Discord.exe
            self._register(wid, self._find(set(), lambda x: w.image_name(x["pid"]).lower() == "discord.exe", allow_iconic=True), None, "real")
        elif app == "vscode":
            project = self.run_dir / f"quality-{nonce(self.scn['seed'], wid)}-rust-project"
            project.mkdir(exist_ok=True)
            (project / "main.rs").write_text('fn main() { println!("quality harness"); }\n', encoding="utf-8")
            code = Path(os.environ["LOCALAPPDATA"]) / "Programs" / "Microsoft VS Code" / "Code.exe"
            # a fresh profile opens a welcome page and sign-in prompts the user never sees day to day
            user = self.run_dir / "vscode-data" / "User"
            user.mkdir(parents=True, exist_ok=True)
            (user / "settings.json").write_text(json.dumps(VSCODE_SETTINGS, indent=2), encoding="utf-8")
            proc = self._spawn([str(code), "--user-data-dir", str(self.run_dir / "vscode-data"), "--extensions-dir", str(self.run_dir / "vscode-ext"), "--new-window", str(project)], "harness")
            self._register(wid, self._find(before, lambda x: project.name in x["title"] or "Visual Studio Code" in x["title"]), proc, "harness")
        elif app == "terminal":
            feed = self._spawn(["wt.exe", "-w", "new", sys.executable, str(QUALITY / "termfeed.py"), str(self.truth_dir / "terminal.jsonl"), str(self.scn["seed"])], "harness")
            self._register(wid, self._find(before, lambda x: "termfeed" in x["title"].lower() or "quality" in x["title"].lower()), feed, "harness")
        elif app in ("notepad", "explorer"):
            target = self.run_dir / ("quality-folder" if app == "explorer" else f"quality-{nonce(self.scn['seed'], wid)}.txt")
            target.mkdir(exist_ok=True) if app == "explorer" else target.write_text("quality harness text\n" * 20, encoding="utf-8")
            self._spawn([f"{app}.exe", str(target)], "real")
            self._register(wid, self._find(before, lambda x: re.search(TITLE_RE[app], x["title"]) is not None or target.stem in x["title"]), None, "real")
        elif app in SINGLE_INSTANCE_EXES:
            self._spawn(["calc.exe"] if app == "calculator" else [self.steam_exe()], "real")
            is_app = lambda x: re.search(TITLE_RE[app], x["title"]) is not None and w.image_name(x["pid"]).lower() in SINGLE_INSTANCE_EXES[app]
            self._register(wid, self._find(set(), is_app, allow_iconic=True), None, "real")
        elif app == "outer_wilds":
            self._register(wid, w.outer_wilds_window(), None, "real")
        elif app == "hung":
            self._launch_hung(wid, mon, before)
        elif app == "game":
            # the window may exist already: match on the session's process only
            self._register(wid, self._find(set(), lambda v: v["pid"] in w.descendants({self.game_proc.pid})), None, "game")
        elif app == "elevated":
            self.prompt.show("Approve the UAC prompt")
            self._spawn(["powershell", "-NoProfile", "-Command", "Start-Process notepad -Verb RunAs"], "real")
            self._register(wid, self._find(before, lambda x: re.search(TITLE_RE["notepad"], x["title"]) is not None), None, "real")
            self.prompt.hide()
        self.act("ensure", wid)

    @staticmethod
    def steam_exe() -> str:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
            return winreg.QueryValueEx(k, "SteamExe")[0]

    def _launch_browser(self, wid: str, app: str, mon: str, step: dict, before: set[int], private: bool = False, url: str | None = None) -> None:
        browser = "edge" if app == "assistant" else app
        exe, private_flag = BROWSERS[browser]
        x, y, wd, ht = self._rect(mon)
        url = url or self.site.url(wid, {**step, "params": {**step["params"], "app": app}})
        profile = self.run_dir / "profiles" / f"{re.sub(r'[^A-Za-z0-9]+', '-', wid)}-{len(self.procs)}"
        if browser == "zen":
            args = [exe, "-profile", str(profile)] + ([private_flag] if private else []) + [url]
        else:
            args = [exe, f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check", f"--window-position={x},{y}", f"--window-size={wd // 2},{ht}"]
            if browser == "edge" and not private:
                args.append("--guest")  # a fresh Edge profile signs in with the PC's Microsoft account and syncs; a guest window does not
            args += [f"--app={url}"] if app == "assistant" else ([private_flag] if private else []) + [url]
        profile.mkdir(parents=True, exist_ok=True)
        proc = self._spawn(args, "harness")
        self.page_step[wid] = step["id"]
        self.act("page_load", wid, step=step["id"], url=url)
        self._register(wid, self._find(before, lambda v: v["pid"] in w.descendants({proc.pid})), proc, "harness")

    def fallback_step(self, wid: str) -> dict:
        """A page for a window the scenario only shows beside the focused one."""
        look = self.window_look.get(wid) or {"lang": "en"}
        return {"id": "f" + re.sub(r"[^A-Za-z0-9]+", "-", wid), "t0_ms": 0, "t1_ms": 1000, "kind": "focus", "params": {"app": self.window_app.get(wid, "chrome"), "page": "article", **look}}

    def _launch_hung(self, wid: str, mon: str, before: set[int]) -> None:
        x, y, wd, ht = self.slot_rect(mon, 0, 2, False)
        script = REPO / "tests" / "hardware" / "synthetic_window.py"
        proc = self._spawn([sys.executable, str(script), f"Quality hung {wid}", str(x), str(y), str(wd), str(ht), "hang"], "harness")
        self._register(wid, self._find(before, lambda v: v["pid"] in w.descendants({proc.pid})), proc, "harness")

    # --- steps ---

    def layout(self, step: dict) -> None:
        """Show the step's windows on their monitors, minimize the rest. A hung window is never placed or
        minimized: those calls would block this thread until its sleep ends."""
        by_mon: dict[str, list[str]] = {}
        for wid in step["windows"]:
            by_mon.setdefault(self.window_mon[wid], []).append(wid)
        keep = set(step["windows"])
        for wid, win in self.windows.items():
            if wid not in keep and not wid.startswith("hung#"):
                w.minimize(win["hwnd"])
        for mon, wids in by_mon.items():
            for i, wid in enumerate(wids):
                self.ensure(wid, step if wid == step["windows"][0] else self.fallback_step(wid))
                win = self.windows.get(wid)
                if win and not wid.startswith("hung#"):
                    full = self.window_app[wid] in ("game", "outer_wilds")
                    w.place(win["hwnd"], *self.slot_rect(mon, i, len(wids), full))

    def do_focus(self, step: dict, lateness: int) -> None:
        wid = step["windows"][0]
        self.layout(step)
        win = self.windows.get(wid)
        if not win:
            self.act("focus_miss", wid, lateness, step=step["id"], reason="no window")
            self.misses += 1
            return
        app = step["params"]["app"]
        if (app in BROWSERS or app == "assistant") and not step["params"].get("keep_page") and self.page_step.get(wid) != step["id"]:
            url = self.site.url(wid, step)
            self.page_step[wid] = step["id"]
            self.site.command(wid, cmd="goto", url=url)
            self.act("page_load", wid, step=step["id"], url=url)
        ok = w.focus(win["hwnd"])
        self.misses += not ok
        self.act("focus" if ok else "focus_miss", wid, lateness, step=step["id"], tags=step["tags"])
        if step["params"].get("popups"):
            threading.Thread(target=self.open_popups, args=(step,), daemon=True).start()

    def control_point(self, wid: str, control: str) -> tuple[int, int] | None:
        """Screen pixel of a page control: the page reports it in viewport CSS px, the viewport sits in the
        bottom right of the window's client area."""
        rect, win = self.site.rects.get(wid), self.windows.get(wid)
        if not rect or not win or control not in rect["controls"]:
            return None
        _, _, right, bottom = w.client_rect(win["hwnd"])
        cx, cy = rect["controls"][control]
        return round(right - rect["iw"] * rect["dpr"] + cx * rect["dpr"]), round(bottom - rect["ih"] * rect["dpr"] + cy * rect["dpr"])

    def click_control(self, wid: str, control: str, step_id: str) -> bool:
        """Click a control of the page this step loaded, once the page has reported where it is and only when
        that window is what the OS finds under the point."""
        deadline = time.monotonic() + CLICK_WAIT_S
        while time.monotonic() < deadline:
            rect = self.site.rects.get(wid)
            if rect and rect.get("s") == self.page_step.get(wid) and control in rect["controls"]:
                break
            time.sleep(0.25)
        point = self.control_point(wid, control) if wid in self.windows else None
        reason = "no geometry report for this page" if not point else None
        if point and w.root_window_at(*point) != self.windows[wid]["hwnd"]:
            reason = "another window is under the point"
        if reason:
            self.act("click_miss", wid, control=control, step=step_id, reason=reason)
            self.misses += 1
            return False
        w.click(*point)
        self.act("click", wid, control=control, step=step_id)
        return True

    def do_video(self, step: dict, lateness: int) -> None:
        self.act("video", step["windows"][0], lateness, step=step["id"])
        threading.Thread(target=self._click_after_delay, args=(step, "play"), daemon=True).start()

    def do_pip(self, step: dict, lateness: int) -> None:
        self.act("pip", step["windows"][0], lateness, step=step["id"])
        threading.Thread(target=self._click_after_delay, args=(step, "pip"), daemon=True).start()

    def _click_after_delay(self, step: dict, control: str) -> None:
        time.sleep(CLICK_DELAY_S)
        self.click_control(step["windows"][0], control, step["id"])

    def open_popups(self, step: dict) -> None:
        """A page <select> dropdown, and the synthetic window with an owned popup beside the page."""
        wid = step["windows"][0]
        time.sleep(CLICK_DELAY_S)
        if self.click_control(wid, "sel", step["id"]):
            self.act("popup_select", wid, step=step["id"])
        x, y, wd, ht = self.slot_rect(self.window_mon[wid], 1, 2, False)
        script = REPO / "tests" / "hardware" / "synthetic_window.py"
        self.popups[step["id"]] = self._spawn([sys.executable, str(script), f"Quality popup {step['id']}", str(x), str(y), str(wd // 2), str(ht // 2), "popup"], "harness")
        self.act("popup_open", wid, step=step["id"])

    def end_popups(self, step: dict) -> None:
        win = self.windows.get(step["windows"][0])
        if win and w.foreground() == win["hwnd"]:
            w.press_key(VK_ESCAPE)  # closes the page's open dropdown
        proc = self.popups.pop(step["id"], None)
        if proc:
            proc.terminate()
            self.act("popup_closed", step["windows"][0], step=step["id"])

    def do_title_change(self, step: dict, lateness: int) -> None:
        wid = step["windows"][0]
        self.site.command(wid, cmd="title", title=f"{self.site.unique_nonce(self.scn['seed'], step['id'], 'title')} update")
        self.act("title_change", wid, lateness, step=step["id"])

    def do_excluded_pages(self, step: dict, lateness: int) -> None:
        wid = step["windows"][0]
        kind = step["params"]["private"]
        vault = kind == "vault"  # a normal window of its own: the harness page's window keeps polling its commands
        mon = self.window_mon[wid]
        before = {x["hwnd"] for x in w.top_windows()}
        private_id = f"{wid}#private-{step['id']}"
        app = self.window_app[wid] if vault else PRIVATE_APP[kind]
        self.window_app[private_id], self.window_mon[private_id] = app, mon
        self._launch_browser(private_id, app, mon, self.fallback_step(private_id), before, private=not vault,
                             url=f"http://127.0.0.1:{self.site.vault_port}/" if vault else None)
        win = self.windows.get(private_id)
        if win:
            w.place(win["hwnd"], *self.slot_rect(mon, 1, 2, False))
            w.focus(win["hwnd"])
        self.act("vault_page" if vault else "private_page", private_id, lateness, step=step["id"], private=kind)

    def end_excluded_pages(self, step: dict) -> None:
        for wid in [k for k in self.windows if "#private" in k and k.endswith(step["id"])]:
            w.close_window(self.windows.pop(wid)["hwnd"])
            self.act("private_closed", wid, step=step["id"])

    def do_pause(self, step: dict, lateness: int) -> None:
        self.host.pause()
        self.act("pause", lateness=lateness, step=step["id"])

    def end_pause(self, step: dict) -> None:
        self.host.resume()
        self.act("resume", step=step["id"])

    def do_busy_disk(self, step: dict, lateness: int) -> None:
        def run() -> None:
            path = self.store_dir.parent / "busy.tmp"
            block = os.urandom(4 << 20)
            end = time.monotonic() + (step["t1_ms"] - step["t0_ms"]) / 1000
            with path.open("wb", buffering=0) as f:
                while time.monotonic() < end:
                    f.write(block)
                    os.fsync(f.fileno())
            path.unlink()

        threading.Thread(target=run, daemon=True).start()
        self.act("busy_disk", lateness=lateness, step=step["id"], seconds=(step["t1_ms"] - step["t0_ms"]) / 1000)

    def do_canary(self, step: dict, lateness: int) -> None:
        secs = (step["t1_ms"] - step["t0_ms"]) / 1000
        manifest = json.loads((assets.ASSETS / "manifest.json").read_text(encoding="utf-8"))
        clips = [c for c in manifest["clips"] if c["speaker"] == "spare_en1"]
        items, t = [], 500
        for c in clips:
            if t + c["dur_s"] * 1000 > secs * 1000:
                break
            items.append({"id": f"{step['id']}-{c['id']}", "t_ms": t, "bank": c["bank"], "off": c["off"], "n": c["n"], "gain": 1.0, "stream": "excluded"})
            t += round(c["dur_s"] * 1000) + 500
        mon = self._rect(step["monitor"])
        self.spawn_player("QualityVault.exe", items, secs, "", f"canary-{step['id']}", canary=(mon[0] + 200, mon[1] + 200, 640, 480), kind="harness")
        self.act("canary", lateness=lateness, step=step["id"])

    # --- players ---

    def spawn_player(self, exe_name: str, items: list[dict], seconds: float, device: str, label: str, canary=None, kind: str = "harness", start_wall: int | None = None) -> None:
        schedule = self.run_dir / f"schedule-{label}.json"
        schedule.write_text(json.dumps({"t0_wall_ms": start_wall or now_ms() + 1500, "end_ms": round(seconds * 1000), "items": items}), encoding="utf-8")
        exe, env = renamed_python(exe_name, self.exes_dir)
        args = [str(exe), str(QUALITY / "player.py"), str(schedule), "--device", device, "--log", str(self.truth_dir / f"player-{label}.jsonl")]
        if canary:
            args += ["--canary", *map(str, canary)]
        self._spawn(args, kind, env)
        self.act("player", label=label, exe_name=exe_name, items=len(items))

    def start_players(self) -> None:
        """One process per audio role, scheduled across the whole drive from the scenario."""
        manifest = json.loads((assets.ASSETS / "manifest.json").read_text(encoding="utf-8"))
        clips = {c["id"]: c for c in manifest["clips"]}
        total = self.scn["duration_ms"] / 1000
        call, own, elsewhere, music = [], [], [], []
        for s in self.scn["steps"]:
            for u in s["params"].get("utterances", []):
                c = clips[u["clip"]]
                item = {"id": f"{s['id']}-{u['t_ms']}", "t_ms": u["t_ms"], "bank": c["bank"], "off": c["off"], "n": c["n"], "gain": 1.0, "stream": u["stream"], "speaker": u["speaker"]}
                {"call": call, "own": own, "elsewhere": elsewhere}[u["stream"]].append(item)
            if s["kind"] == "music":
                music += tile(s, clips["music-000"], "music")
        self.spawn_player("Discord.exe", call, total, "", "call", start_wall=self.start_wall)
        self.spawn_player("Mic.exe", own, total, "CABLE Input", "own", start_wall=self.start_wall)
        self.spawn_player("Spotify.exe", music, total, "", "music", start_wall=self.start_wall)
        if self.third_endpoint:
            self.spawn_player("Discord.exe", elsewhere, total, self.third_endpoint, "elsewhere", start_wall=self.start_wall)
        else:
            self.act("voice_elsewhere_impossible", reason="no render endpoint that is neither the default, CABLE Input nor a Wave Link virtual endpoint")

    def start_game(self, session: dict) -> None:
        manifest = json.loads((assets.ASSETS / "manifest.json").read_text(encoding="utf-8"))
        clip = next(c for c in manifest["clips"] if c["kind"] == "game")
        items = [i for s in self.scn["steps"] if s["kind"] == "game_sound" for i in tile(s, clip, "game", session["t0_ms"]) if session["t0_ms"] <= s["t0_ms"] < session["t1_ms"]]
        seconds = (session["t1_ms"] - session["t0_ms"]) / 1000
        sched = self.run_dir / f"schedule-game-{session['t0_ms']}.json"
        sched.write_text(json.dumps({"t0_wall_ms": now_ms() + 1500, "end_ms": round(seconds * 1000), "items": items}), encoding="utf-8")
        exe, env = renamed_python("QualityGame.exe", self.exes_dir)
        x, y, wd, ht = self._full("primary")
        for wid in [k for k, app in self.window_app.items() if app == "game"]:
            self.windows.pop(wid, None)  # the previous session's window is closing
        self.game_proc = self._spawn([str(exe), str(QUALITY / "game.py"), "--mode", session["mode"], "--rect", str(x), str(y), str(wd), str(ht), "--seconds", str(seconds),
                     "--log", str(self.truth_dir / "game.jsonl"), "--sound", str(sched)], "game", env)
        self.act("game_session", mode=session["mode"], seconds=seconds)

    # --- attended events ---

    def wait_event(self, name: str, before: int, since: int) -> int:
        """Wall ms from `since` until the event fires; wall time, because the clock the loop schedules by may stop in sleep."""
        while self.obs.os_log.counts.get(name, 0) <= before and now_ms() - since < PROMPT_WAIT_S * 1000:
            time.sleep(0.2)
        return now_ms() - since

    def do_attended(self, step: dict, lateness: int) -> None:
        kind = step["kind"]
        planned = step["t1_ms"] - step["t0_ms"]
        counts = self.obs.os_log.counts
        if kind == "lock":
            before = counts.get("unlock", 0)
            self.act("lock", lateness=lateness, step=step["id"])
            since = now_ms()
            w.user32.LockWorkStation()
            waited = self.wait_event("unlock", before, since)
        elif kind == "sleep":
            before = counts.get("resume", 0)
            self.prompt.show("Going to sleep. Wake the PC after it sleeps.")
            time.sleep(3)
            self.act("sleep", lateness=lateness, step=step["id"])
            since = now_ms()
            ctypes_powrprof().SetSuspendState(False, False, False)
            waited = self.wait_event("resume", before, since)
        elif kind == "device_change":
            before = counts.get("default_changed", 0)
            self.prompt.show("Change the default output device now (any other device)")
            self.act("prompt_device_change", lateness=lateness, step=step["id"])
            waited = self.wait_event("default_changed", before, now_ms())
        else:
            return
        self.prompt.hide()
        self.shift_ms += max(0, waited - planned)
        self.act(f"{kind}_done", waited_ms=waited, step=step["id"], shift_ms=self.shift_ms)

    # --- main loop ---

    def run(self) -> None:
        self.prepare()
        self.start_wall = now_ms() + 3000
        self.start_mono = time.monotonic() + 3.0
        self.act("drive_start", start_wall_ms=self.start_wall)
        self.start_players()
        actions: list[tuple[int, int, str, dict]] = []
        for s in self.scn["steps"]:
            kind = s["kind"]
            if kind in STEP_ACTIONS:
                actions.append((s["t0_ms"], 1, kind, s))
            if kind in ("excluded_pages", "pause"):
                actions.append((s["t1_ms"], 0, "end_" + kind, s))
            if kind == "focus" and s["params"].get("popups"):
                actions.append((s["t1_ms"], 0, "end_popups", s))
            if kind == "focus" and s["params"].get("launch_ms") is not None:
                actions.append((s["params"]["launch_ms"], 0, "prelaunch", s))
            if kind == "focus" and s["params"]["app"] == "hung":
                actions.append((s["t1_ms"], 0, "close_hung", s))
        for sess in self.scn["game_sessions"]:
            actions.append((sess["t0_ms"], 0, "game", sess))
        actions.sort(key=lambda a: (a[0], a[1]))
        for t, _, kind, payload in actions:
            target = self.start_mono + (t + self.shift_ms) / 1000
            delay = target - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            lateness = round(-delay * 1000) if delay < 0 else 0
            self.late += lateness > LATE_MS
            self.dispatch(kind, payload, lateness)
        remaining = (self.scn["duration_ms"] + self.shift_ms) / 1000 - (time.monotonic() - self.start_mono)
        time.sleep(max(0, remaining))
        self.act("drive_end")

    def dispatch(self, kind: str, payload: dict, lateness: int) -> None:
        try:
            if kind in ("lock", "sleep", "device_change"):
                self.do_attended(payload, lateness)
            elif kind == "game":
                self.start_game(payload)
            elif kind == "prelaunch":
                self.ensure(payload["windows"][0], payload)
            elif kind == "close_hung":
                win = self.windows.pop(payload["windows"][0], None)
                if win:
                    w.close_window(win["hwnd"])
                    if win.get("proc"):
                        win["proc"].terminate()
                    self.act("hung_closed", payload["windows"][0], step=payload["id"])
            elif kind.startswith("end_"):
                getattr(self, kind)(payload)
            else:
                getattr(self, "do_" + kind)(payload, lateness)
        except Exception as exc:  # noqa: BLE001 - one failed action must not end the run
            self.act("error", kind=kind, error=type(exc).__name__)

    def close_all(self) -> None:
        for wid, win in self.windows.items():
            if self.window_app.get(wid) not in LEFT_OPEN:
                w.close_window(win["hwnd"])
        for proc in self.procs:
            if proc.poll() is None and proc.pid not in self.obs.reg.roots["real"]:
                proc.terminate()
        self.log.close()


def tile(step: dict, clip: dict, stream: str, base_ms: int = 0) -> list[dict]:
    """Loop a bank clip across a step's span as back-to-back items."""
    out, t = [], step["t0_ms"]
    while t < step["t1_ms"]:
        n = min(clip["n"], round((step["t1_ms"] - t) * assets.SR / 1000))
        out.append({"id": f"{step['id']}-{t}", "t_ms": t - base_ms, "bank": clip["bank"], "off": clip["off"], "n": n, "gain": 0.5, "stream": stream})
        t += round(n * 1000 / assets.SR)
    return out


def ctypes_powrprof():
    import ctypes

    return ctypes.WinDLL("powrprof")
