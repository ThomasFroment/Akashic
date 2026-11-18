"""Does a loopback capture client appear as an audible session on the endpoint it records?

Silent for the user: everything runs on VB-CABLE's "CABLE Input" render endpoint. A child process
plays a -15 dBFS tone into CABLE Input with winmm; this process lists CABLE Input's sessions
(pid, state, peak, system-sounds flag, own pid or not) before and while it holds Akashic's own
loopback capture (`_WasapiInput`, kind "system") on that endpoint. Numbers only.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from akashic.platform.win import audio as wa

TONE = r'''
import ctypes, math, struct, time
from ctypes import wintypes
w = ctypes.WinDLL("winmm")
class F(ctypes.Structure):
    _fields_ = [("a", wintypes.WORD), ("c", wintypes.WORD), ("r", wintypes.DWORD), ("b", wintypes.DWORD), ("al", wintypes.WORD), ("bits", wintypes.WORD), ("x", wintypes.WORD)]
class H(ctypes.Structure):
    _fields_ = [("d", ctypes.c_void_p), ("n", wintypes.DWORD), ("rec", wintypes.DWORD), ("u", ctypes.c_size_t), ("f", wintypes.DWORD), ("l", wintypes.DWORD), ("nx", ctypes.c_void_p), ("res", ctypes.c_size_t)]
class C(ctypes.Structure):
    _fields_ = [("m", wintypes.WORD), ("p", wintypes.WORD), ("v", wintypes.UINT), ("name", wintypes.WCHAR * 32), ("fmt", wintypes.DWORD), ("ch", wintypes.WORD), ("r1", wintypes.WORD), ("s", wintypes.DWORD)]
dev = next(i for i in range(w.waveOutGetNumDevs()) if (lambda c: (w.waveOutGetDevCapsW(i, ctypes.byref(c), ctypes.sizeof(c)), c.name.startswith("CABLE Input"))[1])(C()))
f = F(1, 1, 48000, 96000, 2, 16, 0)
h = wintypes.HANDLE()
assert w.waveOutOpen(ctypes.byref(h), dev, ctypes.byref(f), 0, 0, 0) == 0
amp = int(32767 * 10 ** (-12 / 20))
data = b"".join(struct.pack("<h", int(amp * math.sin(2 * math.pi * 440 * i / 48000))) for i in range(48000 * 12))
buf = ctypes.create_string_buffer(data)
hdr = H(ctypes.cast(buf, ctypes.c_void_p), len(data), 0, 0, 0, 0, None, 0)
w.waveOutPrepareHeader(h, ctypes.byref(hdr), ctypes.sizeof(hdr))
w.waveOutWrite(h, ctypes.byref(hdr), ctypes.sizeof(hdr))
time.sleep(12.5)
'''


def sessions(manager) -> list[dict]:
    out = []
    en = manager.GetSessionEnumerator()
    for i in range(en.GetCount()):
        ctl = en.GetSession(i)
        c2 = ctl.QueryInterface(wa.IAudioSessionControl2)
        try:
            system = c2.IsSystemSoundsSession() == 0
        except Exception:  # noqa: BLE001
            system = None
        pid = c2.GetProcessId()
        out.append({"pid": pid, "own": pid == os.getpid(), "state": ctl.GetState(),
                    "peak": round(ctl.QueryInterface(wa.IAudioMeterInformation).GetPeakValue(), 3), "system": system})
    return out


def main() -> None:
    wa._ensure_com()
    cable = wa.WindowsAudioDevices().find("system", "CABLE Input")
    assert cable is not None, "no CABLE Input endpoint"
    item = wa._enumerator().GetDevice(cable.device_id)
    manager = item.Activate(wa.IAudioSessionManager2._iid_, wa._CLSCTX_ALL, None).QueryInterface(wa.IAudioSessionManager2)
    player = subprocess.Popen([sys.executable, "-I", "-c", TONE])
    time.sleep(2.0)
    report = {"own_pid": os.getpid(), "player_pid": player.pid}
    report["tone_only"] = sessions(manager)
    capture = wa.WindowsAudioDevices().open("system", cable)
    got = 0
    t_end = time.monotonic() + 3.0
    while time.monotonic() < t_end:
        block = capture.read(0.2)
        got += 0 if block is None else len(block)
    report["captured_samples"] = got
    report["tone_and_loopback"] = sessions(manager)
    names = wa.WindowsAudioSessions()
    report["akashic_active_processes_cable"] = sorted(names.active_processes(refresh=True).get(cable.device_id, []))
    capture.close()
    player.wait(20)
    time.sleep(1.0)
    report["after_close"] = sessions(manager)
    out = Path(sys.argv[1])
    out.write_text(json.dumps(report, indent=1), encoding="utf-8")
    print(json.dumps(report, indent=1))


main()
