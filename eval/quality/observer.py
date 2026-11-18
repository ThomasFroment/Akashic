"""OS observer and guardrail sampler, independent of Akashic (no akashic import).

Writes truth/os.jsonl (changes only) and truth/guard.jsonl (sampled at 1 Hz, one row per 10 s). Window
titles are written only for windows whose content the harness generated; launched real apps get their
exe only; every other window is "foreign" (hwnd and pid only).
"""

from __future__ import annotations

import ctypes
import os
import statistics
import threading
import time
from ctypes import byref, wintypes
from pathlib import Path

import comtypes

import winapi as w
from common import JsonlWriter

AUDIBLE_ON, AUDIBLE_OFF_POLLS = 0.001, 5
EVENT_FOREGROUND, EVENT_NAMECHANGE = 0x3, 0x800C
STORE_SIZE_EVERY_S = 60
GUARD_ROW_S = 10  # samples are taken at 1 Hz and written as one row per this many seconds, so a day stays small
CONNECTION_POLL_S = 0.5


class OsLog(JsonlWriter):
    """The OS log, plus a count per event type so the driver can wait for a lock, resume or default change."""

    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.counts: dict[str, int] = {}

    def write(self, **fields: object) -> None:
        kind = str(fields.get("t"))
        self.counts[kind] = self.counts.get(kind, 0) + 1
        super().write(**fields)


class Registry:
    """What the harness started, so the observer can say which windows it may name."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.roots: dict[str, set[int]] = {"harness": set(), "real": set(), "game": set()}  # "game": the synthetic game and Outer Wilds
        self.akashic: dict[str, set[int]] = {}  # supervisor label -> spawn pids
        self.hwnds: dict[int, str] = {}
        self.store_dir: Path | None = None

    def add_root(self, kind: str, pid: int) -> None:
        with self.lock:
            self.roots[kind].add(pid)

    def add_akashic(self, label: str, pid: int) -> None:
        with self.lock:
            self.akashic.setdefault(label, set()).add(pid)

    def add_window(self, hwnd: int, kind: str) -> None:
        with self.lock:
            self.hwnds[hwnd] = kind

    def classify(self, hwnd: int, pid: int, table) -> str:
        with self.lock:
            if hwnd in self.hwnds:
                return self.hwnds[hwnd]
            roots = {kind: set(pids) for kind, pids in self.roots.items()}
        for kind in ("harness", "real", "game"):
            if pid in w.descendants(roots[kind], table):
                return kind
        return "foreign"


class Observer:
    def __init__(self, truth_dir: Path, registry: Registry) -> None:
        self.os_log = OsLog(truth_dir / "os.jsonl")
        self.guard_log = JsonlWriter(truth_dir / "guard.jsonl")
        self.reg = registry
        self.stop = threading.Event()
        self.threads: list[threading.Thread] = []
        self.last_fg = 0
        self.name_counts: dict[int, int] = {}
        self.lock = threading.Lock()
        self.phase = "run"  # "reference" while the game runs without Akashic

    def start(self) -> None:
        for target in (self._hooks, self._poll_fg, self._outer_wilds, self._audio, self._notifications, self._guard, self._connection_loop):
            t = threading.Thread(target=target, daemon=True)
            t.start()
            self.threads.append(t)
        w.watch_session_and_power(lambda name: self.os_log.write(t=name), self.stop)

    def close(self) -> None:
        self.stop.set()
        for t in self.threads:
            t.join(timeout=5)
        self.os_log.close()
        self.guard_log.close()

    def _describe(self, hwnd: int, table: dict | None = None) -> dict:
        pid = wintypes.DWORD()
        w.user32.GetWindowThreadProcessId(hwnd, byref(pid))
        kind = self.reg.classify(hwnd, pid.value, table or w.process_table())
        info: dict = {"hwnd": hwnd, "pid": pid.value, "kind": kind}
        if kind != "foreign":
            info["exe"] = w.image_name(pid.value)
        if kind in ("harness", "game"):
            buf = ctypes.create_unicode_buffer(256)
            w.user32.GetWindowTextW(hwnd, buf, 256)
            info["title"] = buf.value
        return info

    def _foreground(self, src: str) -> None:
        hwnd = w.foreground()
        with self.lock:
            if hwnd == self.last_fg:
                return
            self.last_fg = hwnd
        self.os_log.write(t="fg", src=src, **self._describe(hwnd))

    def _hooks(self) -> None:
        proto = ctypes.WINFUNCTYPE(None, wintypes.HANDLE, wintypes.DWORD, wintypes.HWND, wintypes.LONG, wintypes.LONG, wintypes.DWORD, wintypes.DWORD)

        def on_event(_h, event, hwnd, id_object, _child, _thread, _time):
            if event == EVENT_FOREGROUND:
                self._foreground("hook")
            elif event == EVENT_NAMECHANGE and id_object == 0 and hwnd:
                top = w.user32.GetAncestor(hwnd, w.GA_ROOT) or hwnd
                with self.lock:
                    self.name_counts[top] = self.name_counts.get(top, 0) + 1

        keep = proto(on_event)
        w.user32.SetWinEventHook.restype = wintypes.HANDLE
        for ev in (EVENT_FOREGROUND, EVENT_NAMECHANGE):
            w.user32.SetWinEventHook(ev, ev, None, keep, 0, 0, 0)
        msg = wintypes.MSG()
        last_flush = time.monotonic()
        while not self.stop.is_set():
            if w.user32.PeekMessageW(byref(msg), None, 0, 0, 1):
                w.user32.DispatchMessageW(byref(msg))
            else:
                time.sleep(0.01)
            if time.monotonic() - last_flush >= 1.0:
                last_flush = time.monotonic()
                with self.lock:
                    counts, self.name_counts = self.name_counts, {}
                table = w.process_table() if counts else None
                for hwnd, n in counts.items():
                    self.os_log.write(t="names", n=n, **self._describe(hwnd, table))

    def _poll_fg(self) -> None:
        while not self.stop.is_set():
            self._foreground("poll")
            time.sleep(0.05)

    def _outer_wilds(self) -> None:
        """Outer Wilds' window rectangle, monitor and whether it covers it, each time it appears or changes."""
        last = None
        while not self.stop.wait(1.0):
            win = w.outer_wilds_window()
            rect = win["rect"] if win else None
            if rect and rect != last:
                self.os_log.write(t="ow_mode", rect=rect, **w.monitor_of_rect(rect))
            last = rect

    def _audio(self) -> None:
        comtypes.CoInitialize()
        state: dict[tuple[str, int], dict] = {}
        devices: list = []
        refreshed = 0.0
        while not self.stop.is_set():
            if time.monotonic() - refreshed > 2.0:
                devices, refreshed = w.render_devices(), time.monotonic()
            seen: set[tuple[str, int]] = set()
            polled = {info["id"] for info, _ in devices}
            for info, dev in devices:
                try:
                    sessions = w.sessions_of(dev)
                except Exception:  # noqa: BLE001 - an endpoint disappearing mid-poll
                    polled.discard(info["id"])
                    continue
                for s in sessions:
                    key = (info["id"], s["pid"])
                    seen.add(key)
                    st = state.setdefault(key, {"on": False, "quiet": 0, "endpoint": info["name"], "exe": None})
                    if s["peak"] > AUDIBLE_ON:
                        st["quiet"] = 0
                        if not st["on"]:
                            st["on"], st["exe"] = True, self._exe_name(s["pid"])
                            self._audio_event(st, s["pid"], s["peak"], True)
                    elif st["on"]:
                        st["quiet"] += 1
                        if st["quiet"] >= AUDIBLE_OFF_POLLS:
                            st["on"] = False
                            self._audio_event(st, s["pid"], s["peak"], False)
            for key in [k for k in state if k not in seen and k[0] in polled]:  # the session is gone with its process
                if state[key]["on"]:
                    self._audio_event(state[key], key[1], 0.0, False)
                del state[key]
            time.sleep(0.1)

    def _exe_name(self, pid: int) -> str:
        return w.image_name(pid) if self.reg.classify(0, pid, w.process_table()) != "foreign" else "foreign"

    def _audio_event(self, st: dict, pid: int, peak: float, audible: bool) -> None:
        self.os_log.write(t="audio", endpoint=st["endpoint"], pid=pid, exe=st["exe"], audible=audible, peak=round(peak, 4))

    def _notifications(self) -> None:
        comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
        keep = w.register_endpoint_listener(lambda name, **f: self.os_log.write(t=name, **f))
        while not self.stop.is_set():
            time.sleep(0.2)
        del keep

    # --- guardrails ---

    def _trees(self, table) -> dict[str, tuple[set[int], set[int]]]:
        """label -> (every pid of the tree, the spawn pids still alive)."""
        with self.reg.lock:
            roots = {label: set(pids) for label, pids in self.reg.akashic.items()}
            roots["game"] = set(self.reg.roots["game"])
        return {label: (w.descendants(pids, table), pids & table.keys()) for label, pids in roots.items()}

    @staticmethod
    def _new_acc(exe: str, root: bool) -> dict:
        return {"exe": exe, "root": root, "cpu": [], "vram": 0, "gpu3d": [], "io_w": None}

    def _guard(self) -> None:
        comtypes.CoInitialize()
        gpu = w.GpuCounters()
        prev: dict[int, float] = {}
        acc: dict[str, dict[int, dict]] = {}
        window_start, last_store = 0.0, 0.0
        last = time.monotonic()
        while not self.stop.is_set():
            time.sleep(max(0.0, 1.0 - (time.monotonic() - last)))
            now = time.monotonic()
            dt, last = now - last, now
            window_start = window_start or now
            table = w.process_table()
            mem, eng = gpu.sample()
            for label, (pids, roots) in self._trees(table).items():
                for pid in pids:
                    a = acc.setdefault(label, {}).setdefault(pid, self._new_acc(table[pid][1], pid in roots))
                    cpu = w.cpu_seconds(pid)
                    if cpu is not None:
                        if pid in prev and dt > 0:
                            a["cpu"].append(100 * (cpu - prev[pid]) / dt)
                        prev[pid] = cpu
                    a["vram"] = max(a["vram"], int(mem.get(pid, 0)))
                    if label == "game":
                        a["gpu3d"].append(eng.get(pid, 0))
                    a["io_w"] = w.io_written_bytes(pid)
            if now - window_start >= GUARD_ROW_S:
                rec: dict = {"t": "guard", "phase": self.phase, "window_s": round(now - window_start), "trees": {
                    label: {str(pid): {"exe": a["exe"], "root": a["root"], "cpu_pct_1hz": [round(x, 1) for x in a["cpu"]],
                                       "vram_b": a["vram"], "gpu3d_pct": round(statistics.fmean(a["gpu3d"]), 1) if a["gpu3d"] else None, "io_w": a["io_w"]}
                            for pid, a in procs.items()} for label, procs in acc.items()}}
                if now - last_store >= STORE_SIZE_EVERY_S and self.reg.store_dir:
                    last_store = now
                    rec["store_bytes"] = store_size(self.reg.store_dir)
                self.guard_log.write(**rec)
                acc, window_start = {}, now

    def _connection_loop(self) -> None:
        seen: set[tuple] = set()
        while not self.stop.is_set():
            started = time.monotonic()
            trees = self._trees(w.process_table())
            self._connections(set().union(*(pids for label, (pids, _) in trees.items() if label != "game")), seen)
            time.sleep(max(0.0, CONNECTION_POLL_S - (time.monotonic() - started)))

    def _connections(self, akashic: set[int], seen: set[tuple]) -> None:
        for c in w.connections():
            if c["pid"] not in akashic:
                continue
            udp_any = c["proto"] == "udp" and not w.is_loopback(c["local"])
            if not (w.is_remote(c["remote"]) or udp_any):
                continue
            key = (c["proto"], c["pid"], c["local"], c["remote"], c["rport"])
            if key not in seen:
                seen.add(key)
                self.guard_log.write(t="conn", proto=c["proto"], pid=c["pid"], local=c["local"], remote=c["remote"], rport=c["rport"],
                                     exe=w.image_name(c["pid"]))


def store_size(root: Path) -> int:
    total = 0
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if not (Path(dirpath) / d).is_junction()]
        for f in files:
            try:
                total += (Path(dirpath) / f).stat().st_size
            except OSError:
                pass
    return total
