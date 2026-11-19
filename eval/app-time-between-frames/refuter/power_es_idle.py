"""Sample the SystemExecutionState bits next to keyboard/mouse idle seconds. Numbers only."""
import ctypes, time, sys
from ctypes import wintypes
powrprof = ctypes.WinDLL("powrprof"); k32 = ctypes.WinDLL("kernel32"); u32 = ctypes.WinDLL("user32")
class LII(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]
def sample():
    v = wintypes.ULONG(); powrprof.CallNtPowerInformation(16, None, 0, ctypes.byref(v), 4)
    li = LII(ctypes.sizeof(LII), 0); u32.GetLastInputInfo(ctypes.byref(li))
    idle = ((k32.GetTickCount() - li.dwTime) & 0xFFFFFFFF) / 1000
    return hex(v.value), round(idle, 1)
for i in range(int(sys.argv[1])):
    print(*sample()); time.sleep(float(sys.argv[2]))
