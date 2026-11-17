"""Passive: per audible session, compare its process-loopback level with its session meter.
Numbers and executable names only; nothing is written to disk."""

import ctypes
import json
import subprocess
import sys
import time
from ctypes import POINTER, byref, c_float, c_int, c_void_p, wintypes

import numpy as np

HERE = __file__.rsplit("\\", 1)[0]
sys.path.insert(0, HERE)
import comtypes  # noqa: E402
from comtypes import COMMETHOD, GUID, IUnknown  # noqa: E402

from spike import Cap, tone_stats  # noqa: E402
from wasapi import enumerator  # noqa: E402

HRESULT = ctypes.HRESULT


class IAudioMeterInformation(IUnknown):
    _iid_ = GUID("{C02216F6-8C67-4B5B-9D00-D008E73E0064}")
    _methods_ = (COMMETHOD([], HRESULT, "GetPeakValue", (["out"], POINTER(c_float), "p")),)


class IAudioSessionControl2(IUnknown):
    _iid_ = GUID("{BFB7FF88-7239-4FC9-8FA2-07C950BE9C6D}")
    _methods_ = (
        COMMETHOD([], HRESULT, "GetState", (["out"], POINTER(c_int), "s")),
        COMMETHOD([], HRESULT, "GetDisplayName", (["out"], POINTER(c_void_p), "a")),
        COMMETHOD([], HRESULT, "SetDisplayName", (["in"], c_void_p, "a"), (["in"], c_void_p, "b")),
        COMMETHOD([], HRESULT, "GetIconPath", (["out"], POINTER(c_void_p), "a")),
        COMMETHOD([], HRESULT, "SetIconPath", (["in"], c_void_p, "a"), (["in"], c_void_p, "b")),
        COMMETHOD([], HRESULT, "GetGroupingParam", (["out"], POINTER(GUID), "a")),
        COMMETHOD([], HRESULT, "SetGroupingParam", (["in"], c_void_p, "a"), (["in"], c_void_p, "b")),
        COMMETHOD([], HRESULT, "R", (["in"], c_void_p, "p")),
        COMMETHOD([], HRESULT, "U", (["in"], c_void_p, "p")),
        COMMETHOD([], HRESULT, "GetSessionIdentifier", (["out"], POINTER(c_void_p), "a")),
        COMMETHOD([], HRESULT, "GetSessionInstanceIdentifier", (["out"], POINTER(c_void_p), "a")),
        COMMETHOD([], HRESULT, "GetProcessId", (["out"], POINTER(wintypes.DWORD), "p")),
    )


class IAudioSessionEnumerator(IUnknown):
    _iid_ = GUID("{E2F5BB11-0570-40CA-ACDD-3AA01277DEE8}")
    _methods_ = (
        COMMETHOD([], HRESULT, "GetCount", (["out"], POINTER(c_int), "n")),
        COMMETHOD([], HRESULT, "GetSession", (["in"], c_int, "i"), (["out"], POINTER(POINTER(IUnknown)), "s")),
    )


class IAudioSessionManager2(IUnknown):
    _iid_ = GUID("{77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F}")
    _methods_ = (
        COMMETHOD([], HRESULT, "A", (["in"], c_void_p, "a")),
        COMMETHOD([], HRESULT, "B", (["in"], c_void_p, "a")),
        COMMETHOD([], HRESULT, "GetSessionEnumerator", (["out"], POINTER(POINTER(IAudioSessionEnumerator)), "e")),
    )


def name_of(pid: int) -> tuple[str, int]:
    out = subprocess.run(["powershell", "-NoProfile", "-Command",
                          f"$p=Get-CimInstance Win32_Process -Filter 'ProcessId={pid}'; \"$($p.Name)|$($p.ParentProcessId)\""],
                         capture_output=True, text=True).stdout.strip()
    n, _, pp = out.partition("|")
    return n.lower(), int(pp or 0)


def sessions():
    en = enumerator()
    devs = en.EnumAudioEndpoints(0, 1)
    found = []
    for i in range(devs.GetCount()):
        mgr = devs.Item(i).Activate(IAudioSessionManager2._iid_, 23, None).QueryInterface(IAudioSessionManager2)
        se = mgr.GetSessionEnumerator()
        for j in range(se.GetCount()):
            s = se.GetSession(j)
            c2 = s.QueryInterface(IAudioSessionControl2)
            if c2.GetState() != 1:
                continue
            peak = s.QueryInterface(IAudioMeterInformation).GetPeakValue()
            pid = c2.GetProcessId()
            if pid:
                found.append((i, pid, peak))
    return found


if __name__ == "__main__":
    ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x4000)
    seen = {}
    for _ in range(3):
        for ep, pid, peak in sessions():
            seen.setdefault(pid, {"endpoint": ep, "peaks": []})["peaks"].append(round(peak, 4))
        time.sleep(0.3)
    report = []
    for pid, info in seen.items():
        name, parent = name_of(pid)
        pname, _ = name_of(parent) if parent else ("", 0)
        row = {"exe": name, "parent_exe": pname, "endpoint": info["endpoint"], "session_peak_max": max(info["peaks"])}
        try:
            c = Cap(pid)
            t0 = time.monotonic()
            while time.monotonic() - t0 < 2.0:
                c.poll(); time.sleep(0.01)
            x = c.signal()
            c.close()
            row["loopback_rms"] = round(float(np.sqrt(np.mean(x**2))) if len(x) else 0.0, 5)
            row["loopback_peak"] = round(float(np.max(np.abs(x))) if len(x) else 0.0, 4)
            row["silent_flag_packets"] = c.silent_flags
        except Exception as exc:  # noqa: BLE001
            row["error"] = repr(exc)[:120]
        if parent:
            try:
                c = Cap(parent)
                t0 = time.monotonic()
                while time.monotonic() - t0 < 2.0:
                    c.poll(); time.sleep(0.01)
                x = c.signal(); c.close()
                row["parent_tree_rms"] = round(float(np.sqrt(np.mean(x**2))) if len(x) else 0.0, 5)
            except Exception as exc:  # noqa: BLE001
                row["parent_error"] = repr(exc)[:120]
        report.append(row)
    print(json.dumps(report, indent=1))
