"""Command line and Akashic host of the quality harness.

    uv run python eval\\quality\\run.py smoke|standard|day|attended [--seed N] [--machine LABEL]
    uv run python eval\\quality\\run.py score|coverage <run-id>
    uv run python eval\\quality\\run.py compare <run-a> <run-b>
    uv run python eval\\quality\\run.py restore

A run: preflight, fresh store, reference pass, start Akashic, warm-up, drive, stop capture, worker
drain, score. Akashic runs unmodified through akashic.supervisor, in a disposable store under local/.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import platform
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import tomllib
from pathlib import Path

import assets  # noqa: E402
import driver as drv  # noqa: E402
import observer  # noqa: E402
import scenario as sc  # noqa: E402
import score as scoring  # noqa: E402
import winapi as w  # noqa: E402
from common import (  # noqa: E402
    ASSETS, LENGTHS, LOCAL, MODELS_HOME, QUALITY, REPO, RESULTS, JsonlWriter, dpapi_wrap, free_loopback_port, load_local,
    load_toml, make_junction, now_ms, renamed_python, run_id, write_json,
)

WARMUP_S = 60
OW_WINDOW_S = 90
REFERENCE_S = {"smoke": 20, "standard": 60, "day": 60, "attended": 60}
OW_IDLE_S = 60
OW_EXIT_S = 60
ESTIMATE_GB_PER_HOUR = 3.0  # assumption until the first day run measures it
STORE_MARGIN = 2
MIN_TONE_S = 6
DRAIN_BASE_S = 30 * 60
DRAIN_FRACTION = 0.25
OW_APP_ID = "753640"
OW_PREFS_KEY = r"HKCU\Software\Mobius Digital\Outer Wilds"
LAUNCH_APPS = {"chrome": "Chrome", "edge": "Edge", "assistant": "Edge", "zen": "Zen", "discord": "Discord", "steam": "Steam", "outer_wilds": "Outer Wilds"}


class Refused(SystemExit):
    pass


# --- machine -------------------------------------------------------------------------------


def powershell(cmd: str) -> str:
    return subprocess.run(["powershell", "-NoProfile", "-Command", cmd], capture_output=True, text=True, timeout=60).stdout.strip()


def file_version(path: str) -> str | None:
    if not Path(path).is_file():
        return None
    return powershell(f"(Get-Item '{path}').VersionInfo.ProductVersion") or None


def machine_record(label: str) -> dict:
    mons = [{k: v for k, v in m.items() if k != "handle"} for m in w.monitors()]
    gpu = subprocess.run(["nvidia-smi", "--query-gpu=name,driver_version,memory.total", "--format=csv,noheader"], capture_output=True, text=True).stdout.strip()
    import winreg

    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as k:
        cpu = winreg.QueryValueEx(k, "ProcessorNameString")[0].strip()
    ram_gb = round(int(powershell("(Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory")) / 2**30)
    hardware = {"cpu": cpu, "gpu": gpu.split(",")[0], "ram_gb": ram_gb, "monitors": [(m["w"], m["h"], m["x"]) for m in mons]}
    git = lambda *a: subprocess.run(["git", "-C", str(REPO), *a], capture_output=True, text=True).stdout.strip()
    steam = drv.Driver.steam_exe() if _has_steam() else None
    return {
        "label": label, "fingerprint": hashlib.sha256(json.dumps(hardware, sort_keys=True).encode()).hexdigest()[:16], "monitors": mons,
        "gpu": gpu, "cpu": cpu, "ram_gb": ram_gb, "os_build": platform.version(),
        "render_endpoints": w.endpoints(w.E_RENDER), "capture_endpoints": w.endpoints(w.E_CAPTURE),
        "default_render": w.default_endpoint(w.E_RENDER)["name"], "default_capture": w.default_endpoint(w.E_CAPTURE)["name"],
        "versions": {**{k: file_version(v[0]) for k, v in drv.BROWSERS.items()}, "discord": _discord_version(), "steam": file_version(steam) if steam else None,
                     "outer_wilds": _ow_buildid()},
        "akashic_head": git("rev-parse", "HEAD"), "tree_dirty": bool(git("status", "--porcelain", "--", "src", "pyproject.toml", "uv.lock")),
    }


def _discord_exe() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", "")) / "Discord" / "Update.exe"


def _has_steam() -> bool:
    try:
        drv.Driver.steam_exe()
        return True
    except OSError:
        return False


def _discord_version() -> str | None:
    """Newest app-<version> folder of the Discord install (Update.exe's own version is the updater's)."""
    folders = (Path(os.environ.get("LOCALAPPDATA", "")) / "Discord").glob("app-*")
    versions = [p.name.removeprefix("app-") for p in folders]
    return max(versions, key=lambda v: [int(x) for x in v.split(".") if x.isdigit()], default=None)


def _ow_manifest() -> Path | None:
    if not _has_steam():
        return None
    steam = Path(drv.Driver.steam_exe()).parent
    libraries = [steam]
    vdf = steam / "steamapps" / "libraryfolders.vdf"
    if vdf.is_file():
        libraries += [Path(p.replace("\\\\", "\\")) for p in re.findall(r'"path"\s+"([^"]+)"', vdf.read_text(encoding="utf-8", errors="ignore"))]
    return next((m for lib in libraries if (m := lib / "steamapps" / f"appmanifest_{OW_APP_ID}.acf").is_file()), None)


def _ow_installed() -> bool:
    return _ow_manifest() is not None


def _ow_buildid() -> str | None:
    manifest = _ow_manifest()
    found = re.search(r'"buildid"\s+"(\d+)"', manifest.read_text(encoding="utf-8", errors="ignore")) if manifest else None
    return found.group(1) if found else None


def machine_label(arg: str | None) -> str:
    stored = LOCAL / "machine.toml"
    if arg:
        LOCAL.mkdir(parents=True, exist_ok=True)
        stored.write_text(f'label = "{arg}"\n', encoding="utf-8")
        return arg
    if not stored.is_file():
        raise Refused("--machine LABEL is required once on each PC")
    return tomllib.loads(stored.read_text(encoding="utf-8"))["label"]


# --- preflight -----------------------------------------------------------------------------


def preflight(length: str, scn: dict, store_root: Path) -> None:
    """Names the failed check and launches nothing."""
    from akashic.config import DEFAULT_CONFIG_TOML, Paths, load_config
    from akashic.pipeline import models

    fails: list[str] = []
    if assets.verify() != 0:
        fails.append("asset pack does not match its manifest")
    probe = Paths(MODELS_HOME)
    cfg_dir = LOCAL / "preflight-config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.toml").write_text(DEFAULT_CONFIG_TOML, encoding="utf-8")
    config = load_config(Paths(cfg_dir))
    for need in models.required(config):
        if not models.is_ready(probe.models_dir, probe.runtime_dir, need.spec):
            fails.append(f"required model or runtime missing in models-home: {need.spec.key}")
    if not any("CABLE Input" in e["name"] for e in w.endpoints(w.E_RENDER)):
        fails.append('render endpoint "CABLE Input" missing')
    if not any("CABLE Output" in e["name"] for e in w.endpoints(w.E_CAPTURE)):
        fails.append('capture endpoint "CABLE Output" missing')
    hours = scn["duration_ms"] / 3_600_000
    free = shutil.disk_usage(store_root.anchor).free
    if free < STORE_MARGIN * (ESTIMATE_GB_PER_HOUR * hours + 1) * 2**30:
        fails.append(f"free disk below {STORE_MARGIN}x the estimate for {hours:.1f} h")
    apps = {a["app"] for a in scn["windows"]}
    for app in sorted(apps & set(LAUNCH_APPS)):
        ok = {"Chrome": Path(drv.BROWSERS["chrome"][0]).is_file(), "Edge": Path(drv.BROWSERS["edge"][0]).is_file(), "Zen": Path(drv.BROWSERS["zen"][0]).is_file(),
              "Discord": _discord_exe().is_file(), "Steam": _has_steam(), "Outer Wilds": _ow_installed()}[LAUNCH_APPS[app]]
        if not ok:
            fails.append(f"app missing: {LAUNCH_APPS[app]}")
    if not tone_shows_as_session():
        fails.append("test tone from a renamed interpreter did not show as an audio session under its image name")
    timeout = w.screensaver_timeout_s()
    focus_starts = sorted(s["t0_ms"] for s in scn["steps"] if s["kind"] == "focus")
    longest = max((b - a for a, b in zip(focus_starts, focus_starts[1:], strict=False)), default=0) / 1000
    if timeout is not None and timeout <= longest + 5:
        fails.append(f"screensaver starts after {timeout} s, inside a {longest:.0f} s stretch without input")
    if fails:
        raise Refused("preflight failed:\n- " + "\n- ".join(fails))


def tone_shows_as_session() -> bool:
    manifest = json.loads((ASSETS / "manifest.json").read_text(encoding="utf-8"))
    clip = next(c for c in manifest["clips"] if c["kind"] == "music")
    tmp = LOCAL / "preflight-tone"
    tmp.mkdir(parents=True, exist_ok=True)
    schedule = tmp / "tone.json"
    schedule.write_text(json.dumps({"t0_wall_ms": now_ms() + 500, "end_ms": 4000, "items": [{"id": "tone", "t_ms": 0, "bank": clip["bank"], "off": 0, "n": 4 * assets.SR, "gain": 0.05}]}), encoding="utf-8")
    exe, env = renamed_python("PreflightTone.exe", tmp / "exe")
    proc = subprocess.Popen([str(exe), str(QUALITY / "player.py"), str(schedule), "--log", str(tmp / "tone.jsonl")], env={**os.environ, **env})
    seen = False
    import comtypes

    comtypes.CoInitialize()
    deadline = time.monotonic() + MIN_TONE_S
    while time.monotonic() < deadline and not seen:
        for _, dev in w.render_devices():
            seen |= any(w.image_name(s["pid"]).lower() == "preflighttone.exe" for s in w.sessions_of(dev))
        time.sleep(0.2)
    proc.wait(timeout=30)
    return seen


# --- store and config ------------------------------------------------------------------------


def make_store(local: Path, vault_port: int):
    from akashic.config import DEFAULT_CONFIG_TOML, Paths
    from akashic.storage import db
    from akashic.storage.crypto import DataKey, wrap_data_key

    paths = Paths(local / "store")
    paths.ensure()
    key = DataKey.generate()
    passphrase = secrets.token_urlsafe(32)
    paths.keyfile.write_bytes(wrap_data_key(key, passphrase))
    (local / "passphrase.dpapi").write_bytes(dpapi_wrap(passphrase.encode()))
    conn = db.connect(paths.db_file, key.subkey("db"))
    db.migrate(conn)
    toml = DEFAULT_CONFIG_TOML
    toml = re.sub(r'^mic_device = ""', 'mic_device = "CABLE Output"', toml, count=1, flags=re.M)
    toml = re.sub(r'^processes = \[\]', 'processes = ["qualityvault.exe"]', toml, count=1, flags=re.M)
    toml = re.sub(r'^urls = \[\]', f'urls = ["127.0.0.1:{vault_port}/*"]', toml, count=1, flags=re.M)
    paths.config_file.write_text(toml, encoding="utf-8")
    for link, target in ((paths.models_dir, MODELS_HOME / "models"), (paths.runtime_dir, MODELS_HOME / "runtime")):
        make_junction(link, target)
    return paths, key, conn


# --- desktop hygiene -------------------------------------------------------------------------


def save_and_minimize() -> None:
    w.kernel32.GetConsoleWindow.restype = ctypes.c_size_t
    own = {w.kernel32.GetConsoleWindow()}
    placements = [{"hwnd": x["hwnd"], "placement": w.get_placement(x["hwnd"]), "iconic": x["iconic"]} for x in w.user_windows()]
    LOCAL.mkdir(parents=True, exist_ok=True)
    (LOCAL / "placements.json").write_text(json.dumps(placements), encoding="utf-8")
    for p in placements:
        if not p["iconic"] or p["hwnd"] in own:
            w.minimize(p["hwnd"])


def restore_windows() -> None:
    f = LOCAL / "placements.json"
    if not f.is_file():
        return
    for p in json.loads(f.read_text(encoding="utf-8")):
        if not w.user32.IsWindow(p["hwnd"]) or p["iconic"]:
            continue
        w.set_placement(p["hwnd"], p["placement"])
    f.unlink()


LEFTOVER_APPS = {"discord": ("discord.exe",), "steam": ("steam.exe",), "calculator": ("calculatorapp.exe",)}


def save_running_apps() -> None:
    """Which apps the run may open were already running, so the cleanup closes only the run's own."""
    names = {exe.lower() for _ppid, exe in w.process_table().values()}
    running = {app: any(n in names for n in exes) for app, exes in LEFTOVER_APPS.items()}
    write_json(LOCAL / "apps-before.json", running)


def kill_run_processes(dirs: list[Path]) -> int:
    """Kills, with their trees, the processes whose command line names one of the run's folders: browsers keep
    background processes after their last window closes (Edge), Zen's launcher exits before its browser does."""
    if not dirs:
        return 0
    like = " -or ".join(f"$_.CommandLine -like '*{d}*'" for d in dirs)
    # Notepad and Explorer were handed a run file but hold the user's own tabs and folders
    keep = "$_.Name -notin @('notepad.exe', 'explorer.exe', 'powershell.exe')"
    ps = f"Get-CimInstance Win32_Process | Where-Object {{ ({like}) -and {keep} }} | ForEach-Object {{ $_.ProcessId }}"
    out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True, timeout=120).stdout
    pids = {int(x) for x in out.split() if x.isdigit()} - {os.getpid()}
    for pid in pids:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
    return len(pids)


def close_leftovers() -> None:
    """Closes the harness's Explorer folder windows and the apps the run started (Discord, Steam, Calculator)."""
    for x in w.top_windows():
        if "quality-folder" in x["title"] and w.image_name(x["pid"]).lower() == "explorer.exe":
            w.close_window(x["hwnd"])
    f = LOCAL / "apps-before.json"
    if not f.is_file():
        return
    before = json.loads(f.read_text(encoding="utf-8"))
    now = {exe.lower() for _ppid, exe in w.process_table().values()}
    if not before.get("steam") and "steam.exe" in now:  # -shutdown on a stopped Steam would start it
        subprocess.run([drv.Driver.steam_exe(), "-shutdown"], capture_output=True, timeout=60)
    for app in ("discord", "calculator"):
        if not before.get(app):
            for exe in LEFTOVER_APPS[app]:
                subprocess.run(["taskkill", "/IM", exe, "/F"], capture_output=True)
    f.unlink()


def dim_monitors() -> None:
    saved = w.brightness_read_all()
    write_json(LOCAL / "brightness.json", saved)
    for m in saved:
        if m["brightness"]:
            w.brightness_set(m["monitor"], m["brightness"]["min"])


def restore_brightness() -> None:
    f = LOCAL / "brightness.json"
    if f.is_file():
        for m in json.loads(f.read_text(encoding="utf-8")):
            if m["brightness"]:
                w.brightness_set(m["monitor"], m["brightness"]["cur"])
        f.unlink()


def ow_saves() -> list[Path]:
    root = Path(os.environ["USERPROFILE"]) / "AppData" / "LocalLow" / "Mobius Digital" / "Outer Wilds" / "SteamSaves"
    return list(root.glob("*/graphics.owsett"))


def ow_backup() -> None:
    dest = LOCAL / "ow-backup"
    dest.mkdir(parents=True, exist_ok=True)
    manifest = []
    for i, p in enumerate(ow_saves()):
        shutil.copy2(p, dest / f"{i}.owsett")
        manifest.append({"i": i, "path": str(p)})
    (dest / "files.json").write_text(json.dumps(manifest), encoding="utf-8")
    probe = subprocess.run(["reg", "export", OW_PREFS_KEY, str(dest / "prefs.reg"), "/y"], capture_output=True)
    (dest / "has_prefs").write_text("1" if probe.returncode == 0 else "0")


def ow_restore() -> None:
    dest = LOCAL / "ow-backup"
    if not (dest / "files.json").is_file():
        return
    for item in json.loads((dest / "files.json").read_text(encoding="utf-8")):
        shutil.copy2(dest / f"{item['i']}.owsett", item["path"])
    if (dest / "has_prefs").read_text() == "1":
        subprocess.run(["reg", "import", str(dest / "prefs.reg")], capture_output=True)
    shutil.rmtree(dest)


# --- host ------------------------------------------------------------------------------------


class Host:
    """The two Supervisors, spawned through spawn_module behind a pid-recording wrapper."""

    def __init__(self, paths, key, registry: observer.Observer | observer.Registry, api_port: int) -> None:
        from akashic import supervisor as sup

        self.registry = registry
        self.spawned = {"capture": 0, "worker": 0}
        self.sups: dict[str, object] = {}
        for label, spec in zip(("capture", "worker"), sup.CHILDREN, strict=True):
            def spawn(s, d, label=label):
                proc = sup.spawn_module(s, d)
                self.spawned[label] += 1
                self.registry.add_akashic(label, proc.pid)
                return proc

            supervisor = sup.Supervisor(paths.root, key, (spec,), spawn=spawn)
            supervisor.set_api_port(api_port)
            self.sups[label] = supervisor
        self.stop = threading.Event()
        threading.Thread(target=self._poll, daemon=True).start()

    def _poll(self) -> None:
        while not self.stop.is_set():
            for s in self.sups.values():
                s.poll()
            time.sleep(1)

    def start(self) -> None:
        for s in self.sups.values():
            s.start()

    def pause(self) -> None:
        for s in self.sups.values():
            s.send("pause")

    def resume(self) -> None:
        for s in self.sups.values():
            s.send("resume")

    def states(self) -> dict[str, str]:
        return {k: next(iter(s.states().values()), "stopped") for k, s in self.sups.items()}

    def restarts(self) -> dict[str, int]:
        return {k: max(0, v - 1) for k, v in self.spawned.items()}


# --- the run ---------------------------------------------------------------------------------


def reference_pass(length: str, truth: Path, obs: observer.Observer, run_dir: Path) -> None:
    """The synthetic game alone, borderless then exclusive, so G3 can compare frame times with and without Akashic."""
    exe, env = renamed_python("QualityGame.exe", run_dir / "exes")
    m = next(x for x in w.monitors() if x["primary"])
    for mode in ("borderless", "exclusive"):
        proc = subprocess.Popen([str(exe), str(QUALITY / "game.py"), "--mode", mode, "--rect", str(m["x"]), str(m["y"]), str(m["w"]), str(m["h"]),
                                 "--seconds", str(REFERENCE_S[length]), "--log", str(truth / "game-ref.jsonl")], env={**os.environ, **env})
        obs.reg.add_root("game", proc.pid)
        proc.wait(timeout=REFERENCE_S[length] + 60)


def launch_outer_wilds(registry: observer.Registry, run_log: JsonlWriter) -> None:
    """Backs up the game's settings, launches it, registers it as a game process and lets its menu idle."""
    ow_backup()
    subprocess.Popen([drv.Driver.steam_exe(), "-applaunch", OW_APP_ID, "-window-mode", "borderless", "-screen-fullscreen", "1", "-monitor", "1"])
    deadline = time.monotonic() + OW_WINDOW_S
    while time.monotonic() < deadline:
        win = w.outer_wilds_window()
        if win:
            registry.add_root("game", win["pid"])
            registry.add_window(win["hwnd"], "real")
            run_log.write(type="outer_wilds", state="window")
            time.sleep(OW_IDLE_S)
            return
        time.sleep(1)
    raise Refused("Outer Wilds window did not appear within 90 s of its launch (Steam launch arguments unconfirmed, or the game is not set up)")


def close_outer_wilds(run_log: JsonlWriter | None = None) -> None:
    """Closes the game and waits for its exit: it rewrites its settings on the way out."""
    win = w.outer_wilds_window()
    if not win:
        return
    w.close_window(win["hwnd"])
    for _ in range(OW_EXIT_S):
        if win["pid"] not in w.process_table():
            break
        time.sleep(1)
    else:
        subprocess.run(["taskkill", "/PID", str(win["pid"]), "/T", "/F"], capture_output=True)
        time.sleep(OW_EXIT_S // 4)
    if run_log:
        run_log.write(type="outer_wilds", state="closed")


def warm_up(host: Host, conn, n_monitors: int) -> list[dict]:
    deadline = time.monotonic() + WARMUP_S
    seen: list[dict] = []
    while time.monotonic() < deadline:
        states = host.states()
        seen.append({"t": now_ms(), **states})
        frames_ok = conn.execute("SELECT count(DISTINCT monitor_id) FROM frames").fetchone()[0] >= n_monitors
        if states["capture"] in ("ready", "gaming", "degraded") and frames_ok:  # gaming: Outer Wilds runs from before warm-up
            return seen
        time.sleep(1)
    raise Refused(f"warm-up failed within {WARMUP_S} s: {seen[-1] if seen else 'no state'}")


def drain(host: Host, conn, drive_end: int, drive_s: float, log: JsonlWriter) -> bool:
    from akashic.pipeline import episodes
    from akashic.pipeline.main import HOUSEKEEPING_INTERVAL_S

    deadline = time.monotonic() + DRAIN_BASE_S + DRAIN_FRACTION * drive_s
    def wait(cond) -> bool:
        while time.monotonic() < deadline:
            if cond():
                return True
            time.sleep(2)
        return False
    need = drive_end + episodes.CLOSE_GAP_MS + episodes.MIC_MARGIN_MS
    if not wait(lambda: episodes.horizon_ms(conn, now_ms()) >= need):
        return True
    time.sleep(HOUSEKEEPING_INTERVAL_S + 5)
    idle = lambda: conn.execute("SELECT count(*) FROM jobs WHERE state IN ('pending', 'running')").fetchone()[0] == 0
    while time.monotonic() < deadline:
        if wait(idle):
            time.sleep(HOUSEKEEPING_INTERVAL_S)
            if idle():
                log.write(type="drain", timed_out=False)
                return False
        else:
            break
    return True


def run(length: str, seed: int | None, machine_arg: str | None) -> int:
    label = machine_label(machine_arg)
    profile = load_toml(sc.PROFILE)
    seed = seed if seed is not None else sc.pv(profile, f"seed.{length}")
    rid = run_id(length, seed, label)
    results = RESULTS / rid
    truth = results / "truth"
    local = LOCAL / "runs" / rid
    truth.mkdir(parents=True)
    local.mkdir(parents=True)
    (results / "scenario.json").write_text(sc.generate(length, seed), encoding="utf-8", newline="\n")
    scn = json.loads((results / "scenario.json").read_text(encoding="utf-8"))
    preflight(length, scn, local)
    machine = machine_record(label)
    third = next((e["name"] for e in machine["render_endpoints"] if e["name"] != machine["default_render"] and not any(x in e["name"] for x in ("CABLE", "Elgato Virtual"))), None)
    machine["voice_elsewhere_endpoint"] = third[:25] if third else None
    write_json(results / "machine.json", machine)
    vault_port, api_port = free_loopback_port(), free_loopback_port()
    paths, key, conn = make_store(local, vault_port)
    (local / "temp-before.json").write_text(json.dumps([p.name for p in Path(os.environ.get("TEMP", "")).iterdir()]), encoding="utf-8")
    listener = socket.socket()
    listener.bind(("127.0.0.1", api_port))
    listener.listen()
    registry = observer.Registry()
    registry.store_dir = paths.root
    obs = observer.Observer(truth, registry)
    site_mod = load_local("site")
    site = site_mod.Site(truth, local, seed, vault_port)
    host = None
    drive = None
    run_log = JsonlWriter(truth / "run.jsonl")
    w.keep_awake(True)
    save_running_apps()
    try:
        save_and_minimize()
        if length == "day":
            dim_monitors()
        obs.start()
        obs.phase = "reference"
        reference_pass(length, truth, obs, local)
        if "outer_wilds@primary" in {x["id"] for x in scn["windows"]}:
            launch_outer_wilds(registry, run_log)
        obs.phase = "run"
        site.start()
        host = Host(paths, key, registry, api_port)
        host.start()
        run_log.write(type="warmup", states=warm_up(host, conn, len(machine["monitors"])))
        drive = drv.Driver(scn, site, obs, host, local, truth, machine, paths.root)
        drive.run()
        host.sups["capture"].stop()
        timed_out = drain(host, conn, now_ms(), scn["duration_ms"] / 1000, run_log)
        run_log.write(type="drain_result", timed_out=timed_out, restarts=host.restarts(), sup_states=host.states())
    finally:
        if host:
            host.stop.set()
            for s in host.sups.values():
                s.shutdown()
        obs.close()
        if drive:
            drive.close_all()
        close_outer_wilds(run_log)
        close_leftovers()
        time.sleep(5)  # windows just asked to close get to exit on their own first
        run_log.write(type="cleanup", killed=kill_run_processes([local, truth]))
        site.stop()
        listener.close()
        w.keep_awake(False)
        restore_brightness()
        ow_restore()
        restore_windows()
        run_log.close()
    # in a fresh process: the run's native threads (COM, WGC, audio) once crashed the scorer with exit 139
    rc = subprocess.run([sys.executable, "-X", "faulthandler", str(Path(__file__)), "score", rid]).returncode
    print(f"run {rid} written to {results} (score exit {rc})")
    return rc


def restore() -> int:
    close_outer_wilds()
    close_leftovers()
    print(f"killed {kill_run_processes([LOCAL / 'runs', RESULTS])} leftover run processes")
    restore_windows()
    restore_brightness()
    ow_restore()
    print("restored window placements, brightness and Outer Wilds settings")
    return 0


def main() -> int:
    w.set_dpi_aware()  # before any monitor rectangle is read: a DPI-unaware process sees virtualized ones
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("command", choices=(*LENGTHS, "score", "coverage", "compare", "restore"))
    ap.add_argument("args", nargs="*")
    ap.add_argument("--seed", type=int)
    ap.add_argument("--machine")
    a = ap.parse_args()
    if a.command in LENGTHS:
        return run(a.command, a.seed, a.machine)
    if a.command == "score":
        scoring.score(a.args[0])
        return 0
    if a.command == "coverage":
        return scoring.coverage_cmd(a.args[0])
    if a.command == "compare":
        return scoring.compare(a.args[0], a.args[1])
    return restore()


if __name__ == "__main__":
    sys.exit(main())
