"""Does a PowerSetRequest display request show in CallNtPowerInformation(SystemExecutionState)? Also call costs."""
import ctypes, time
from ctypes import wintypes
powrprof = ctypes.WinDLL("powrprof")
k32 = ctypes.WinDLL("kernel32", use_last_error=True)
u32 = ctypes.WinDLL("user32")
ES_SYSTEM_REQUIRED, ES_DISPLAY_REQUIRED, ES_USER_PRESENT, ES_CONTINUOUS = 0x1, 0x2, 0x4, 0x80000000

def es():
    v = wintypes.ULONG()
    st = powrprof.CallNtPowerInformation(16, None, 0, ctypes.byref(v), ctypes.sizeof(v))
    return st, v.value

class REASON(ctypes.Structure):
    _fields_ = [("Version", wintypes.ULONG), ("Flags", wintypes.DWORD), ("Text", wintypes.LPWSTR)]

def show(tag):
    st, v = es()
    print(tag, "status", st, "hex", hex(v), "display", bool(v & ES_DISPLAY_REQUIRED), "system", bool(v & ES_SYSTEM_REQUIRED))

show("base")
k32.PowerCreateRequest.restype = wintypes.HANDLE
k32.PowerCreateRequest.argtypes = [ctypes.POINTER(REASON)]
k32.PowerSetRequest.argtypes = [wintypes.HANDLE, ctypes.c_int]
k32.PowerClearRequest.argtypes = [wintypes.HANDLE, ctypes.c_int]
r = REASON(0, 0x1, "refuter spike")  # POWER_REQUEST_CONTEXT_SIMPLE_STRING
h = k32.PowerCreateRequest(ctypes.byref(r))
print("set_display", k32.PowerSetRequest(h, 0))  # PowerRequestDisplayRequired
time.sleep(0.3)
show("after_PowerSetRequest_display")
print("set_system", k32.PowerSetRequest(h, 1))
time.sleep(0.3)
show("after_PowerSetRequest_display_system")
k32.PowerClearRequest(h, 0); k32.PowerClearRequest(h, 1); k32.CloseHandle(h)
time.sleep(0.3)
show("after_clear")
k32.SetThreadExecutionState.restype = wintypes.DWORD
k32.SetThreadExecutionState(ES_CONTINUOUS | ES_DISPLAY_REQUIRED)
time.sleep(0.3)
show("after_STES_display")
k32.SetThreadExecutionState(ES_CONTINUOUS)
time.sleep(0.3)
show("after_STES_clear")
# costs
n = 2000
t = time.perf_counter()
for _ in range(n): es()
print("us_per_CallNtPowerInformation", round((time.perf_counter() - t) / n * 1e6, 2))
class LII(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]
li = LII(ctypes.sizeof(LII), 0)
t = time.perf_counter()
for _ in range(n): u32.GetLastInputInfo(ctypes.byref(li)); k32.GetTickCount()
print("us_per_GetLastInputInfo_plus_tick", round((time.perf_counter() - t) / n * 1e6, 2))
