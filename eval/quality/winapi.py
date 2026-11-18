"""Windows glue of the harness: ctypes for windows, input, processes, counters, sockets; comtypes
for audio endpoints and sessions. Declares its own COM interfaces and imports nothing from akashic.
"""

from __future__ import annotations

import ctypes
import ipaddress
import socket
import struct
import threading
import time
from ctypes import POINTER, byref, c_float, c_int, c_uint, c_ulong, c_void_p, c_wchar_p, wintypes
from pathlib import Path

import comtypes
from comtypes import COMMETHOD, GUID, HRESULT, IUnknown

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
shcore = ctypes.WinDLL("shcore")
dxva2 = ctypes.WinDLL("dxva2")
winmm = ctypes.WinDLL("winmm")
iphlpapi = ctypes.WinDLL("iphlpapi")
pdh = ctypes.WinDLL("pdh")
wtsapi = ctypes.WinDLL("wtsapi32")
ole32 = ctypes.WinDLL("ole32")
dwmapi = ctypes.WinDLL("dwmapi")


class _Placement(ctypes.Structure):  # WINDOWPLACEMENT
    _fields_ = (("length", wintypes.UINT), ("flags", wintypes.UINT), ("show", wintypes.UINT), ("min_pos", wintypes.POINT),
                ("max_pos", wintypes.POINT), ("normal", wintypes.RECT))


for fn, res, args in (
    (user32.GetForegroundWindow, wintypes.HWND, ()),
    (user32.SetForegroundWindow, wintypes.BOOL, (wintypes.HWND,)),
    (user32.GetWindowTextW, c_int, (wintypes.HWND, c_wchar_p, c_int)),
    (user32.GetWindowRect, wintypes.BOOL, (wintypes.HWND, POINTER(wintypes.RECT))),
    (user32.IsWindowVisible, wintypes.BOOL, (wintypes.HWND,)),
    (user32.ShowWindow, wintypes.BOOL, (wintypes.HWND, c_int)),
    (user32.SetWindowPos, wintypes.BOOL, (wintypes.HWND, wintypes.HWND, c_int, c_int, c_int, c_int, c_uint)),
    (user32.GetWindowThreadProcessId, wintypes.DWORD, (wintypes.HWND, POINTER(wintypes.DWORD))),
    (user32.PostMessageW, wintypes.BOOL, (wintypes.HWND, c_uint, wintypes.WPARAM, wintypes.LPARAM)),
    (user32.GetWindow, wintypes.HWND, (wintypes.HWND, c_uint)),
    (user32.IsIconic, wintypes.BOOL, (wintypes.HWND,)),
    (user32.GetClientRect, wintypes.BOOL, (wintypes.HWND, POINTER(wintypes.RECT))),
    (user32.ClientToScreen, wintypes.BOOL, (wintypes.HWND, POINTER(wintypes.POINT))),
    (user32.WindowFromPoint, wintypes.HWND, (wintypes.POINT,)),
    (user32.GetAncestor, wintypes.HWND, (wintypes.HWND, c_uint)),
    (user32.GetWindowPlacement, wintypes.BOOL, (wintypes.HWND, POINTER(_Placement))),
    (user32.SetWindowPlacement, wintypes.BOOL, (wintypes.HWND, POINTER(_Placement))),
    (user32.GetClassNameW, c_int, (wintypes.HWND, c_wchar_p, c_int)),
    (dwmapi.DwmGetWindowAttribute, wintypes.LONG, (wintypes.HWND, wintypes.DWORD, c_void_p, wintypes.DWORD)),
    (kernel32.OpenProcess, wintypes.HANDLE, (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)),
    (kernel32.CloseHandle, wintypes.BOOL, (wintypes.HANDLE,)),
):
    fn.restype, fn.argtypes = res, args

SW_MINIMIZE, SW_RESTORE, SW_SHOWNOACTIVATE = 6, 9, 4
ENUM_CURRENT_SETTINGS = -1
SWP_NOZORDER, SWP_NOACTIVATE = 0x4, 0x10
WM_CLOSE = 0x10
PROCESS_QUERY_LIMITED = 0x1000
HWND_TOPMOST = -1
GA_ROOT = 2
DWMWA_CLOAKED = 14
SHELL_CLASSES = ("Shell_TrayWnd", "Shell_SecondaryTrayWnd", "Progman", "WorkerW")


def set_dpi_aware() -> None:
    user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # per-monitor v2


# --- monitors, windows, focus, input ---------------------------------------------------------


def monitors() -> list[dict]:
    """Physical-pixel rectangles with scale and refresh; names primary, right and left."""
    out: list[dict] = []

    class MI(ctypes.Structure):  # MONITORINFOEXW
        _fields_ = (("cbSize", wintypes.DWORD), ("rc", wintypes.RECT), ("work", wintypes.RECT), ("flags", wintypes.DWORD),
                    ("device", wintypes.WCHAR * 32))

    class DM(ctypes.Structure):  # DEVMODEW
        _fields_ = (("name", wintypes.WCHAR * 32), ("spec", wintypes.WORD), ("driver", wintypes.WORD), ("size", wintypes.WORD), ("extra", wintypes.WORD),
                    ("fields", wintypes.DWORD), ("pos", ctypes.c_long * 2), ("orient", wintypes.DWORD), ("fo", wintypes.DWORD),
                    ("color", wintypes.SHORT), ("duplex", wintypes.SHORT), ("yres", wintypes.SHORT), ("tt", wintypes.SHORT),
                    ("collate", wintypes.SHORT), ("form", wintypes.WCHAR * 32), ("log", wintypes.WORD), ("bpp", wintypes.DWORD),
                    ("w", wintypes.DWORD), ("h", wintypes.DWORD), ("dfl", wintypes.DWORD), ("freq", wintypes.DWORD),
                    ("rest", ctypes.c_byte * 32))

    cb = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC, POINTER(wintypes.RECT), wintypes.LPARAM)

    def visit(handle, _dc, _rc, _lp):
        info = MI(cbSize=ctypes.sizeof(MI))
        user32.GetMonitorInfoW(handle, byref(info))
        dpi_x, dpi_y = c_uint(), c_uint()
        shcore.GetDpiForMonitor(handle, 0, byref(dpi_x), byref(dpi_y))
        mode = DM(size=ctypes.sizeof(DM))
        user32.EnumDisplaySettingsW(info.device, ENUM_CURRENT_SETTINGS, byref(mode))
        out.append({"handle": handle, "x": info.rc.left, "y": info.rc.top, "w": info.rc.right - info.rc.left,
                    "h": info.rc.bottom - info.rc.top, "scale": dpi_x.value / 96, "primary": bool(info.flags & 1), "refresh": mode.freq})
        return True

    user32.EnumDisplayMonitors(None, None, cb(visit), 0)
    out.sort(key=lambda m: m["x"])
    primary = next(m for m in out if m["primary"])
    for m in out:
        m["name"] = "primary" if m is primary else ("right" if m["x"] > primary["x"] else "left")
    return out


def top_windows() -> list[dict]:
    out: list[dict] = []
    cb = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def visit(hwnd, _lp):
        if user32.IsWindowVisible(hwnd) and not user32.GetWindow(hwnd, 4):  # GW_OWNER
            buf = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(hwnd, buf, 256)
            rc, pid = wintypes.RECT(), wintypes.DWORD()
            user32.GetWindowRect(hwnd, byref(rc))
            user32.GetWindowThreadProcessId(hwnd, byref(pid))
            out.append({"hwnd": hwnd or 0, "pid": pid.value, "title": buf.value, "iconic": bool(user32.IsIconic(hwnd)),
                        "rect": (rc.left, rc.top, rc.right, rc.bottom)})
        return True

    user32.EnumWindows(cb(visit), 0)
    return out


def user_windows() -> list[dict]:
    """top_windows without the shell's own windows and the cloaked frames of hidden UWP apps."""
    out = []
    for x in top_windows():
        name, cloaked = ctypes.create_unicode_buffer(256), wintypes.DWORD()
        user32.GetClassNameW(x["hwnd"], name, 256)
        dwmapi.DwmGetWindowAttribute(x["hwnd"], DWMWA_CLOAKED, byref(cloaked), ctypes.sizeof(cloaked))
        if name.value not in SHELL_CLASSES and not cloaked.value:
            out.append(x)
    return out


def get_placement(hwnd: int) -> str:
    """The window's whole placement (normal rectangle, maximized or not) as hex, for set_placement."""
    placement = _Placement(length=ctypes.sizeof(_Placement))
    user32.GetWindowPlacement(hwnd, byref(placement))
    return bytes(placement).hex()


def set_placement(hwnd: int, packed: str) -> None:
    user32.SetWindowPlacement(hwnd, byref(_Placement.from_buffer_copy(bytes.fromhex(packed))))


def outer_wilds_window() -> dict | None:
    return next((x for x in top_windows() if "Outer Wilds" in x["title"] and image_name(x["pid"]).lower() == "outerwilds.exe"), None)


def monitor_of_rect(rect: tuple[int, int, int, int]) -> dict:
    """The monitor holding a window's centre, and whether the window covers exactly that monitor (borderless or fullscreen)."""
    cx, cy = (rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2
    mon = next((m for m in monitors() if m["x"] <= cx < m["x"] + m["w"] and m["y"] <= cy < m["y"] + m["h"]), None)
    covers = mon is not None and rect == (mon["x"], mon["y"], mon["x"] + mon["w"], mon["y"] + mon["h"])
    return {"monitor": mon["name"] if mon else None, "covers": covers}


def place(hwnd: int, x: int, y: int, w: int, h: int, *, topmost: bool = False) -> None:
    user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE if not user32.IsIconic(hwnd) else SW_RESTORE)
    user32.SetWindowPos(hwnd, HWND_TOPMOST if topmost else None, x, y, w, h, SWP_NOACTIVATE | (0 if topmost else SWP_NOZORDER))


def minimize(hwnd: int) -> None:
    user32.ShowWindow(hwnd, SW_MINIMIZE)


def close_window(hwnd: int) -> None:
    user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)


def foreground() -> int:
    return user32.GetForegroundWindow() or 0


def focus(hwnd: int) -> bool:
    """Synthetic Alt press, then SetForegroundWindow. False when the OS did not honour it."""
    user32.keybd_event(0x12, 0, 0, 0)
    user32.keybd_event(0x12, 0, 2, 0)
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.05)
    return foreground() == hwnd


class _MouseInput(ctypes.Structure):
    _fields_ = (("dx", ctypes.c_long), ("dy", ctypes.c_long), ("data", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("extra", c_void_p))


class _KeyInput(ctypes.Structure):
    _fields_ = (("vk", wintypes.WORD), ("scan", wintypes.WORD), ("flags", wintypes.DWORD), ("time", wintypes.DWORD), ("extra", c_void_p))


class _InputUnion(ctypes.Union):
    _fields_ = (("mi", _MouseInput), ("ki", _KeyInput))


class _Input(ctypes.Structure):
    _fields_ = (("type", wintypes.DWORD), ("u", _InputUnion))


def _send(*inputs: _Input) -> None:
    arr = (_Input * len(inputs))(*inputs)
    user32.SendInput(len(inputs), arr, ctypes.sizeof(_Input))


def client_rect(hwnd: int) -> tuple[int, int, int, int]:
    """Client area of a window in virtual-screen physical pixels: left, top, right, bottom."""
    rc, origin = wintypes.RECT(), wintypes.POINT(0, 0)
    user32.GetClientRect(hwnd, byref(rc))
    user32.ClientToScreen(hwnd, byref(origin))
    return origin.x, origin.y, origin.x + rc.right, origin.y + rc.bottom


def root_window_at(x: int, y: int) -> int:
    """Top-level window under a screen point (0 when none)."""
    child = user32.WindowFromPoint(wintypes.POINT(x, y))
    return user32.GetAncestor(child, GA_ROOT) or 0 if child else 0


def click(x: int, y: int) -> None:
    """Left click at virtual-screen physical pixels (SendInput, absolute, virtual desktop)."""
    vx, vy = user32.GetSystemMetrics(76), user32.GetSystemMetrics(77)
    vw, vh = user32.GetSystemMetrics(78), user32.GetSystemMetrics(79)
    ax, ay = round((x - vx) * 65535 / (vw - 1)), round((y - vy) * 65535 / (vh - 1))
    flags = 0x8000 | 0x4000 | 0x1  # absolute | virtualdesk | move
    mouse = lambda f: _Input(0, _InputUnion(mi=_MouseInput(ax, ay, 0, f, 0, None)))
    _send(mouse(flags), mouse(flags | 0x2), mouse(flags | 0x4))


def press_key(vk: int) -> None:
    key = lambda f: _Input(1, _InputUnion(ki=_KeyInput(vk, 0, f, 0, None)))
    _send(key(0), key(2))


# --- processes -------------------------------------------------------------------------------


class _ProcEntry(ctypes.Structure):
    _fields_ = (("size", wintypes.DWORD), ("usage", wintypes.DWORD), ("pid", wintypes.DWORD), ("heap", c_void_p),
                ("module", wintypes.DWORD), ("threads", wintypes.DWORD), ("ppid", wintypes.DWORD), ("pri", ctypes.c_long),
                ("flags", wintypes.DWORD), ("exe", wintypes.WCHAR * 260))


def process_table() -> dict[int, tuple[int, str]]:
    """pid -> (parent pid, exe name), from one Toolhelp snapshot."""
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    snap = kernel32.CreateToolhelp32Snapshot(2, 0)
    entry = _ProcEntry(size=ctypes.sizeof(_ProcEntry))
    out: dict[int, tuple[int, str]] = {}
    ok = kernel32.Process32FirstW(snap, byref(entry))
    while ok:
        out[entry.pid] = (entry.ppid, entry.exe)
        ok = kernel32.Process32NextW(snap, byref(entry))
    kernel32.CloseHandle(snap)
    return out


def descendants(roots: set[int], table: dict[int, tuple[int, str]] | None = None) -> set[int]:
    """The roots that still exist plus their whole descendant tree, re-resolved from `table`."""
    table = table or process_table()
    tree = {p for p in roots if p in table}
    grew = True
    while grew:
        grew = False
        for pid, (ppid, _) in table.items():
            if ppid in tree and pid not in tree:
                tree.add(pid)
                grew = True
    return tree


def _open(pid: int):
    return kernel32.OpenProcess(PROCESS_QUERY_LIMITED, False, pid)


def cpu_seconds(pid: int) -> float | None:
    h = _open(pid)
    if not h:
        return None
    c, e, k, u = (wintypes.FILETIME() for _ in range(4))
    ok = kernel32.GetProcessTimes(h, byref(c), byref(e), byref(k), byref(u))
    kernel32.CloseHandle(h)
    if not ok:
        return None
    ticks = lambda f: (f.dwHighDateTime << 32 | f.dwLowDateTime)
    return (ticks(k) + ticks(u)) / 1e7


class _IoCounters(ctypes.Structure):
    _fields_ = tuple((n, ctypes.c_ulonglong) for n in ("rops", "wops", "oops", "rbytes", "wbytes", "obytes"))


def io_written_bytes(pid: int) -> int | None:
    h = _open(pid)
    if not h:
        return None
    io = _IoCounters()
    ok = kernel32.GetProcessIoCounters(h, byref(io))
    kernel32.CloseHandle(h)
    return io.wbytes if ok else None


def image_name(pid: int) -> str:
    h = _open(pid)
    if not h:
        return ""
    buf, n = ctypes.create_unicode_buffer(520), wintypes.DWORD(520)
    kernel32.QueryFullProcessImageNameW(h, 0, buf, byref(n))
    kernel32.CloseHandle(h)
    return Path(buf.value).name


# --- GPU counters (PDH) ----------------------------------------------------------------------


class _PdhItem(ctypes.Structure):
    _fields_ = (("name", c_wchar_p), ("status", wintypes.DWORD), ("pad", wintypes.DWORD), ("value", ctypes.c_double))


class GpuCounters:
    """Per-pid dedicated VRAM bytes and 3D-engine utilization from the 'GPU ...' PDH objects."""

    def __init__(self) -> None:
        self.query = c_void_p()
        pdh.PdhOpenQueryW(None, 0, byref(self.query))
        self.mem, self.eng = c_void_p(), c_void_p()
        pdh.PdhAddEnglishCounterW(self.query, r"\GPU Process Memory(*)\Dedicated Usage", 0, byref(self.mem))
        pdh.PdhAddEnglishCounterW(self.query, r"\GPU Engine(*)\Utilization Percentage", 0, byref(self.eng))
        pdh.PdhCollectQueryData(self.query)

    @staticmethod
    def _array(counter) -> dict[str, float]:
        size, count = wintypes.DWORD(0), wintypes.DWORD(0)
        fmt = 0x200 | 0x8000  # PDH_FMT_DOUBLE | PDH_FMT_NOCAP100
        pdh.PdhGetFormattedCounterArrayW(counter, fmt, byref(size), byref(count), None)
        if not size.value:
            return {}
        buf = ctypes.create_string_buffer(size.value)
        if pdh.PdhGetFormattedCounterArrayW(counter, fmt, byref(size), byref(count), buf) != 0:
            return {}
        items = ctypes.cast(buf, POINTER(_PdhItem))
        return {items[i].name: items[i].value for i in range(count.value)}

    def sample(self) -> tuple[dict[int, float], dict[int, float]]:
        """({pid: dedicated bytes}, {pid: 3D utilization %})."""
        pdh.PdhCollectQueryData(self.query)
        mem: dict[int, float] = {}
        for name, v in self._array(self.mem).items():
            pid = int(name.split("_")[1])
            mem[pid] = mem.get(pid, 0.0) + v
        eng: dict[int, float] = {}
        for name, v in self._array(self.eng).items():
            if "engtype_3D" in name:
                pid = int(name.split("_")[1])
                eng[pid] = eng.get(pid, 0.0) + v
        return mem, eng


# --- sockets ---------------------------------------------------------------------------------


AF_INET, AF_INET6 = 2, 23
TCP_TABLE_OWNER_PID_ALL, UDP_TABLE_OWNER_PID = 5, 1


def _addr(raw: bytes | int, family: int) -> str:
    return socket.inet_ntoa(struct.pack("<L", raw)) if family == AF_INET else socket.inet_ntop(AF_INET6, raw)


def _table(fn, family: int, table_class: int, row_fmt: str) -> list[tuple]:
    size = wintypes.DWORD(0)
    fn(None, byref(size), False, family, table_class, 0)
    buf = ctypes.create_string_buffer(size.value)
    if fn(buf, byref(size), False, family, table_class, 0) != 0:
        return []
    n = struct.unpack_from("<L", buf, 0)[0]
    row_size = struct.calcsize(row_fmt)
    return [struct.unpack_from(row_fmt, buf, 4 + i * row_size) for i in range(n)]


def connections() -> list[dict]:
    """IPv4 and IPv6 TCP and UDP endpoints with their owner pid; remote kept only for TCP."""
    out = []
    for family, tcp_fmt, udp_fmt in ((AF_INET, "<LLLLLL", "<LLL"), (AF_INET6, "<16sLL16sLLLL", "<16sLLL")):
        for row in _table(iphlpapi.GetExtendedTcpTable, family, TCP_TABLE_OWNER_PID_ALL, tcp_fmt):
            if family == AF_INET:
                state, laddr, _lport, raddr, rport, pid = row
            else:
                laddr, _scope, _lport, raddr, _rscope, rport, state, pid = row
            out.append({"proto": "tcp", "state": state, "local": _addr(laddr, family), "remote": _addr(raddr, family), "rport": socket.ntohs(rport & 0xFFFF), "pid": pid})
        for row in _table(iphlpapi.GetExtendedUdpTable, family, UDP_TABLE_OWNER_PID, udp_fmt):
            out.append({"proto": "udp", "local": _addr(row[0], family), "remote": "", "rport": 0, "pid": row[-1]})
    return out


def is_loopback(addr: str) -> bool:
    ip = ipaddress.ip_address(addr)
    return (getattr(ip, "ipv4_mapped", None) or ip).is_loopback


def is_remote(addr: str) -> bool:
    """A real peer: not empty, not loopback, not the unspecified address."""
    return bool(addr) and not is_loopback(addr) and not ipaddress.ip_address(addr).is_unspecified


# --- COM audio: our own declarations ---------------------------------------------------------

CLSID_MMDeviceEnumerator = GUID("{BCDE0395-E52F-467C-8E3D-C4579291692E}")
IID_IAudioSessionManager2 = GUID("{77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F}")
E_RENDER, E_CAPTURE = 0, 1
STATE_ACTIVE = 1
ROLE_CONSOLE = 0


class PROPERTYKEY(ctypes.Structure):
    _fields_ = (("fmtid", GUID), ("pid", wintypes.DWORD))


class PROPVARIANT(ctypes.Structure):
    _fields_ = (("vt", wintypes.WORD), ("r1", wintypes.WORD), ("r2", wintypes.WORD), ("r3", wintypes.WORD),
                ("ptr", c_void_p), ("pad", ctypes.c_ulonglong))


PKEY_FRIENDLY_NAME = PROPERTYKEY(GUID("{A45C254E-DF1C-4EFD-8020-67D146A850E0}"), 14)


class IPropertyStore(IUnknown):
    _iid_ = GUID("{886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99}")
    _methods_ = (
        COMMETHOD([], HRESULT, "GetCount", (["out"], POINTER(c_uint), "c")),
        COMMETHOD([], HRESULT, "GetAt", (["in"], c_uint, "i"), (["out"], POINTER(PROPERTYKEY), "k")),
        COMMETHOD([], HRESULT, "GetValue", (["in"], POINTER(PROPERTYKEY), "key"), (["in", "out"], POINTER(PROPVARIANT), "v")),
    )


class IMMDevice(IUnknown):
    _iid_ = GUID("{D666063F-1587-4E43-81F1-B948E807363F}")
    _methods_ = (
        COMMETHOD([], HRESULT, "Activate", (["in"], POINTER(GUID), "iid"), (["in"], c_ulong, "ctx"), (["in"], c_void_p, "params"),
                  (["out"], POINTER(POINTER(IUnknown)), "iface")),
        COMMETHOD([], HRESULT, "OpenPropertyStore", (["in"], c_ulong, "access"), (["out"], POINTER(POINTER(IPropertyStore)), "props")),
        COMMETHOD([], HRESULT, "GetId", (["out"], POINTER(c_wchar_p), "id")),
        COMMETHOD([], HRESULT, "GetState", (["out"], POINTER(c_ulong), "state")),
    )


class IMMDeviceCollection(IUnknown):
    _iid_ = GUID("{0BD7A1BE-7A1A-44DB-8397-CC5392387B5E}")
    _methods_ = (
        COMMETHOD([], HRESULT, "GetCount", (["out"], POINTER(c_uint), "n")),
        COMMETHOD([], HRESULT, "Item", (["in"], c_uint, "i"), (["out"], POINTER(POINTER(IMMDevice)), "dev")),
    )


class IMMNotificationClient(IUnknown):
    _iid_ = GUID("{7991EEC9-7E89-4D85-8390-6C703CEC60C0}")
    _methods_ = (
        COMMETHOD([], HRESULT, "OnDeviceStateChanged", (["in"], c_wchar_p, "id"), (["in"], c_ulong, "state")),
        COMMETHOD([], HRESULT, "OnDeviceAdded", (["in"], c_wchar_p, "id")),
        COMMETHOD([], HRESULT, "OnDeviceRemoved", (["in"], c_wchar_p, "id")),
        COMMETHOD([], HRESULT, "OnDefaultDeviceChanged", (["in"], c_int, "flow"), (["in"], c_int, "role"), (["in"], c_wchar_p, "id")),
        COMMETHOD([], HRESULT, "OnPropertyValueChanged", (["in"], c_wchar_p, "id"), (["in"], PROPERTYKEY, "key")),
    )


class IMMDeviceEnumerator(IUnknown):
    _iid_ = GUID("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
    _methods_ = (
        COMMETHOD([], HRESULT, "EnumAudioEndpoints", (["in"], c_int, "flow"), (["in"], c_ulong, "mask"),
                  (["out"], POINTER(POINTER(IMMDeviceCollection)), "devices")),
        COMMETHOD([], HRESULT, "GetDefaultAudioEndpoint", (["in"], c_int, "flow"), (["in"], c_int, "role"),
                  (["out"], POINTER(POINTER(IMMDevice)), "dev")),
        COMMETHOD([], HRESULT, "GetDevice", (["in"], c_wchar_p, "id"), (["out"], POINTER(POINTER(IMMDevice)), "dev")),
        COMMETHOD([], HRESULT, "RegisterEndpointNotificationCallback", (["in"], POINTER(IMMNotificationClient), "client")),
        COMMETHOD([], HRESULT, "UnregisterEndpointNotificationCallback", (["in"], POINTER(IMMNotificationClient), "client")),
    )


class IAudioMeterInformation(IUnknown):
    _iid_ = GUID("{C02216F6-8C67-4B5B-9D00-D008E73E0064}")
    _methods_ = (COMMETHOD([], HRESULT, "GetPeakValue", (["out"], POINTER(c_float), "peak")),)


class IAudioSessionControl(IUnknown):
    _iid_ = GUID("{F4B1A599-7266-4319-A8CA-E70ACB11E8CD}")
    _methods_ = (
        COMMETHOD([], HRESULT, "GetState", (["out"], POINTER(c_int), "state")),
        COMMETHOD([], HRESULT, "GetDisplayName", (["out"], POINTER(c_wchar_p), "n")),
        COMMETHOD([], HRESULT, "SetDisplayName", (["in"], c_wchar_p, "n"), (["in"], c_void_p, "ctx")),
        COMMETHOD([], HRESULT, "GetIconPath", (["out"], POINTER(c_wchar_p), "p")),
        COMMETHOD([], HRESULT, "SetIconPath", (["in"], c_wchar_p, "p"), (["in"], c_void_p, "ctx")),
        COMMETHOD([], HRESULT, "GetGroupingParam", (["out"], POINTER(GUID), "g")),
        COMMETHOD([], HRESULT, "SetGroupingParam", (["in"], POINTER(GUID), "g"), (["in"], c_void_p, "ctx")),
        COMMETHOD([], HRESULT, "RegisterAudioSessionNotification", (["in"], c_void_p, "n")),
        COMMETHOD([], HRESULT, "UnregisterAudioSessionNotification", (["in"], c_void_p, "n")),
    )


class IAudioSessionControl2(IAudioSessionControl):
    _iid_ = GUID("{BFB7FF88-7239-4FC9-8FA2-07C950BE9C6D}")
    _methods_ = (
        COMMETHOD([], HRESULT, "GetSessionIdentifier", (["out"], POINTER(c_wchar_p), "i")),
        COMMETHOD([], HRESULT, "GetSessionInstanceIdentifier", (["out"], POINTER(c_wchar_p), "i")),
        COMMETHOD([], HRESULT, "GetProcessId", (["out"], POINTER(wintypes.DWORD), "pid")),
        COMMETHOD([], HRESULT, "IsSystemSoundsSession"),
        COMMETHOD([], HRESULT, "SetDuckingPreference", (["in"], wintypes.BOOL, "b")),
    )


class IAudioSessionEnumerator(IUnknown):
    _iid_ = GUID("{E2F5BB11-0570-40CA-ACDD-3AA01277DEE8}")
    _methods_ = (
        COMMETHOD([], HRESULT, "GetCount", (["out"], POINTER(c_int), "n")),
        COMMETHOD([], HRESULT, "GetSession", (["in"], c_int, "i"), (["out"], POINTER(POINTER(IAudioSessionControl)), "s")),
    )


class IAudioSessionManager2(IUnknown):
    _iid_ = IID_IAudioSessionManager2
    _methods_ = (
        COMMETHOD([], HRESULT, "GetAudioSessionControl", (["in"], c_void_p, "g"), (["in"], c_ulong, "f"), (["out"], POINTER(POINTER(IAudioSessionControl)), "s")),
        COMMETHOD([], HRESULT, "GetSimpleAudioVolume", (["in"], c_void_p, "g"), (["in"], c_ulong, "f"), (["out"], POINTER(c_void_p), "v")),
        COMMETHOD([], HRESULT, "GetSessionEnumerator", (["out"], POINTER(POINTER(IAudioSessionEnumerator)), "e")),
    )


def enumerator() -> IMMDeviceEnumerator:
    return comtypes.CoCreateInstance(CLSID_MMDeviceEnumerator, IMMDeviceEnumerator, comtypes.CLSCTX_ALL)


def device_name(dev: IMMDevice) -> str:
    props = dev.OpenPropertyStore(0)
    pv = props.GetValue(byref(PKEY_FRIENDLY_NAME), PROPVARIANT())
    name = ctypes.wstring_at(pv.ptr) if pv.ptr else ""
    ole32.PropVariantClear(byref(pv))
    return name


def endpoints(flow: int) -> list[dict]:
    """Active endpoints of one flow: id and friendly name."""
    coll = enumerator().EnumAudioEndpoints(flow, STATE_ACTIVE)
    return [{"id": d.GetId(), "name": device_name(d)} for d in (coll.Item(i) for i in range(coll.GetCount()))]


def default_endpoint(flow: int) -> dict:
    d = enumerator().GetDefaultAudioEndpoint(flow, ROLE_CONSOLE)
    return {"id": d.GetId(), "name": device_name(d)}


def sessions_of(dev: IMMDevice) -> list[dict]:
    """Audio sessions of a render endpoint: pid, state, peak. Meters are cheap to poll at 10 Hz."""
    mgr = dev.Activate(byref(IID_IAudioSessionManager2), 23, None).QueryInterface(IAudioSessionManager2)
    enum = mgr.GetSessionEnumerator()
    out = []
    for i in range(enum.GetCount()):
        ctl = enum.GetSession(i)
        ctl2 = ctl.QueryInterface(IAudioSessionControl2)
        meter = ctl.QueryInterface(IAudioMeterInformation)
        out.append({"pid": ctl2.GetProcessId(), "state": ctl.GetState(), "peak": meter.GetPeakValue()})
    return out


def render_devices() -> list[tuple[dict, IMMDevice]]:
    coll = enumerator().EnumAudioEndpoints(E_RENDER, STATE_ACTIVE)
    devs = [coll.Item(i) for i in range(coll.GetCount())]
    return [({"id": d.GetId(), "name": device_name(d)}, d) for d in devs]


class EndpointListener(comtypes.COMObject):
    """Our own IMMNotificationClient: endpoint added, removed, state changed, default changed."""

    _com_interfaces_ = [IMMNotificationClient]

    def __init__(self, emit) -> None:
        super().__init__()
        self.emit = emit

    def OnDeviceStateChanged(self, id, state):  # noqa: A002
        self.emit("endpoint_state", id=id, state=state)
        return 0

    def OnDeviceAdded(self, id):  # noqa: A002
        self.emit("endpoint_added", id=id)
        return 0

    def OnDeviceRemoved(self, id):  # noqa: A002
        self.emit("endpoint_removed", id=id)
        return 0

    def OnDefaultDeviceChanged(self, flow, role, id):  # noqa: A002
        self.emit("default_changed", flow=flow, role=role, id=id or "")
        return 0

    def OnPropertyValueChanged(self, id, key):  # noqa: A002
        return 0


def register_endpoint_listener(emit):
    enum = enumerator()
    client = EndpointListener(emit)
    enum.RegisterEndpointNotificationCallback(client)
    return enum, client


# --- session and power notifications (message-only window) -----------------------------------

WM_WTSSESSION_CHANGE, WM_POWERBROADCAST = 0x2B1, 0x218
WTS_LOCK, WTS_UNLOCK = 7, 8
PBT_SUSPEND, PBT_RESUME_AUTO, PBT_RESUME_SUSPEND = 0x4, 0x12, 0x7
WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, c_uint, wintypes.WPARAM, wintypes.LPARAM)


class _WndClass(ctypes.Structure):
    _fields_ = (("style", c_uint), ("proc", WNDPROC), ("cls", c_int), ("wnd", c_int), ("inst", wintypes.HINSTANCE),
                ("icon", c_void_p), ("cursor", c_void_p), ("bg", c_void_p), ("menu", c_wchar_p), ("name", c_wchar_p))


class _Msg(ctypes.Structure):
    _fields_ = (("hwnd", wintypes.HWND), ("msg", c_uint), ("w", wintypes.WPARAM), ("l", wintypes.LPARAM),
                ("time", wintypes.DWORD), ("pt", wintypes.POINT))


def watch_session_and_power(emit, stop: threading.Event) -> threading.Thread:
    """Lock, unlock, suspend, resume via WTSRegisterSessionNotification and
    RegisterSuspendResumeNotification on a message-only window. Runs until `stop` is set."""

    def run() -> None:
        events = {(WM_WTSSESSION_CHANGE, WTS_LOCK): "lock", (WM_WTSSESSION_CHANGE, WTS_UNLOCK): "unlock",
                  (WM_POWERBROADCAST, PBT_SUSPEND): "suspend", (WM_POWERBROADCAST, PBT_RESUME_AUTO): "resume",
                  (WM_POWERBROADCAST, PBT_RESUME_SUSPEND): "resume"}

        def proc(hwnd, msg, w, l):
            name = events.get((msg, w))
            if name:
                emit(name)
            return user32.DefWindowProcW(hwnd, msg, w, l)

        keep = WNDPROC(proc)
        kernel32.GetModuleHandleW.restype = wintypes.HMODULE
        user32.DefWindowProcW.argtypes = (wintypes.HWND, c_uint, wintypes.WPARAM, wintypes.LPARAM)
        user32.DefWindowProcW.restype = ctypes.c_ssize_t
        cls = _WndClass(proc=keep, inst=kernel32.GetModuleHandleW(None), name="QualityObserverSink")
        user32.RegisterClassW(byref(cls))
        user32.CreateWindowExW.restype = wintypes.HWND
        user32.CreateWindowExW.argtypes = (wintypes.DWORD, c_wchar_p, c_wchar_p, wintypes.DWORD, c_int, c_int, c_int, c_int, wintypes.HWND, c_void_p, wintypes.HINSTANCE, c_void_p)
        hwnd = user32.CreateWindowExW(0, "QualityObserverSink", "sink", 0, 0, 0, 0, 0, wintypes.HWND(-3), None, cls.inst, None)  # HWND_MESSAGE
        wtsapi.WTSRegisterSessionNotification(hwnd, 0)
        user32.RegisterSuspendResumeNotification.restype = c_void_p
        user32.RegisterSuspendResumeNotification(hwnd, 0)  # DEVICE_NOTIFY_WINDOW_HANDLE
        msg = _Msg()
        while not stop.is_set():
            if user32.PeekMessageW(byref(msg), None, 0, 0, 1):
                user32.DispatchMessageW(byref(msg))
            else:
                time.sleep(0.02)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


# --- brightness, execution state, screensaver, waveOut ---------------------------------------


class _PhysMon(ctypes.Structure):
    _pack_ = 1
    _fields_ = (("h", wintypes.HANDLE), ("desc", wintypes.WCHAR * 128))


def brightness_read_all() -> list[dict]:
    """DDC/CI brightness per monitor (min, current, max), or None where unsupported."""
    out = []
    for m in monitors():
        n = wintypes.DWORD()
        entry = {"monitor": m["name"], "brightness": None}
        if dxva2.GetNumberOfPhysicalMonitorsFromHMONITOR(m["handle"], byref(n)) and n.value:
            arr = (_PhysMon * n.value)()
            dxva2.GetPhysicalMonitorsFromHMONITOR(m["handle"], n.value, arr)
            lo, cur, hi = wintypes.DWORD(), wintypes.DWORD(), wintypes.DWORD()
            if dxva2.GetMonitorBrightness(arr[0].h, byref(lo), byref(cur), byref(hi)):
                entry["brightness"] = {"min": lo.value, "cur": cur.value, "max": hi.value}
            dxva2.DestroyPhysicalMonitors(n.value, arr)
        out.append(entry)
    return out


def brightness_set(name: str, value: int) -> bool:
    for m in monitors():
        if m["name"] != name:
            continue
        n = wintypes.DWORD()
        if not (dxva2.GetNumberOfPhysicalMonitorsFromHMONITOR(m["handle"], byref(n)) and n.value):
            return False
        arr = (_PhysMon * n.value)()
        dxva2.GetPhysicalMonitorsFromHMONITOR(m["handle"], n.value, arr)
        ok = bool(dxva2.SetMonitorBrightness(arr[0].h, value))
        dxva2.DestroyPhysicalMonitors(n.value, arr)
        return ok
    return False


ES_CONTINUOUS, ES_SYSTEM_REQUIRED, ES_DISPLAY_REQUIRED = 0x80000000, 0x1, 0x2


def keep_awake(on: bool) -> None:
    kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED if on else ES_CONTINUOUS)


def screensaver_timeout_s() -> int | None:
    """Seconds of idle before the screensaver starts, None when it is off."""
    active, timeout = wintypes.BOOL(), c_uint()
    user32.SystemParametersInfoW(0x10, 0, byref(active), 0)
    user32.SystemParametersInfoW(0x0E, 0, byref(timeout), 0)
    return timeout.value if active.value else None


class _WaveFormat(ctypes.Structure):
    _fields_ = (("tag", wintypes.WORD), ("ch", wintypes.WORD), ("rate", wintypes.DWORD), ("bps", wintypes.DWORD),
                ("align", wintypes.WORD), ("bits", wintypes.WORD), ("cb", wintypes.WORD))


class _WaveHdr(ctypes.Structure):
    _fields_ = (("data", c_void_p), ("len", wintypes.DWORD), ("rec", wintypes.DWORD), ("user", c_void_p), ("flags", wintypes.DWORD),
                ("loops", wintypes.DWORD), ("next", c_void_p), ("reserved", c_void_p))


class _MmTime(ctypes.Structure):
    _pack_ = 1
    _fields_ = (("type", c_uint), ("samples", c_uint), ("unused", c_uint))


TIME_SAMPLES = 2


class _WaveOutCaps(ctypes.Structure):
    _fields_ = (("mid", wintypes.WORD), ("pid", wintypes.WORD), ("ver", wintypes.DWORD), ("name", wintypes.WCHAR * 32),
                ("formats", wintypes.DWORD), ("ch", wintypes.WORD), ("res", wintypes.WORD), ("support", wintypes.DWORD))


def wave_out_names() -> list[str]:
    names = []
    for i in range(winmm.waveOutGetNumDevs()):
        caps = _WaveOutCaps()
        winmm.waveOutGetDevCapsW(c_uint(i), byref(caps), ctypes.sizeof(caps))
        names.append(caps.name)
    return names


class WavePlayer:
    """Plays mono 16-bit PCM on the waveOut device whose name contains `device` ('' = default)."""

    def __init__(self, device: str, rate: int) -> None:
        names = wave_out_names()
        self.index = next((i for i, n in enumerate(names) if device.lower() in n.lower()), None) if device else 0xFFFFFFFF
        if self.index is None:
            raise RuntimeError(f"no waveOut device named {device!r}")
        self.fmt = _WaveFormat(1, 1, rate, rate * 2, 2, 16, 0)
        self.handle = c_void_p()
        if winmm.waveOutOpen(byref(self.handle), c_uint(self.index), byref(self.fmt), 0, 0, 0):
            raise RuntimeError("waveOutOpen failed")
        self._live: list = []

    def start(self, pcm: bytes) -> None:
        buf = ctypes.create_string_buffer(pcm, len(pcm))
        hdr = _WaveHdr(ctypes.cast(buf, c_void_p), len(pcm))
        winmm.waveOutPrepareHeader(self.handle, byref(hdr), ctypes.sizeof(hdr))
        winmm.waveOutWrite(self.handle, byref(hdr), ctypes.sizeof(hdr))
        self._live.append((hdr, buf))

    def played_samples(self) -> int:
        """Samples the device has played since it was opened."""
        pos = _MmTime(TIME_SAMPLES)
        winmm.waveOutGetPosition(self.handle, byref(pos), ctypes.sizeof(pos))
        return pos.samples

    def wait_oldest(self) -> None:
        hdr, _ = self._live[0]
        while not hdr.flags & 1:
            time.sleep(0.005)
        winmm.waveOutUnprepareHeader(self.handle, byref(hdr), ctypes.sizeof(hdr))
        self._live.pop(0)

    def wait(self) -> None:
        for hdr, _ in self._live:
            while not hdr.flags & 1:  # WHDR_DONE
                time.sleep(0.01)
            winmm.waveOutUnprepareHeader(self.handle, byref(hdr), ctypes.sizeof(hdr))
        self._live.clear()

    def close(self) -> None:
        winmm.waveOutReset(self.handle)
        winmm.waveOutClose(self.handle)
