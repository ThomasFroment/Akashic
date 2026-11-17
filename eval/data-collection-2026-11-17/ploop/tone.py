"""Child: render a sine tone on a playback endpoint with its own session muted (inaudible).

usage: tone.py FREQ SECONDS MODE [ENDPOINT_INDEX]
MODE: mute (session muted before Start) | vol0 (session volume 0) | quiet (session volume 0.001)
"""

import ctypes
import sys
import time
from ctypes import POINTER, byref, c_float

import numpy as np

sys.path.insert(0, __file__.rsplit("\\", 1)[0])
from wasapi import (  # noqa: E402
    IAudioClient, IAudioRenderClient, ISimpleAudioVolume, enumerator, fmt_float,
)

freq, seconds, mode = float(sys.argv[1]), float(sys.argv[2]), sys.argv[3]
index = int(sys.argv[4]) if len(sys.argv) > 4 else -1
en = enumerator()
if index < 0:
    dev = en.GetDefaultAudioEndpoint(0, 0)
else:
    dev = en.EnumAudioEndpoints(0, 1).Item(index)
client = dev.Activate(IAudioClient._iid_, 23, None).QueryInterface(IAudioClient)
rate = 48000
fmt = fmt_float(rate, 1)
import comtypes, uuid
session = comtypes.GUID(str(uuid.uuid4()).join("{}"))
# NOPERSIST: the mute never outlives this process; a private session GUID touches no other app.
client.Initialize(0, 0x80000000 | 0x08000000 | 0x00080000, 2_000_000, 0, byref(fmt), byref(session))
vol = client.GetService(byref(ISimpleAudioVolume._iid_)).QueryInterface(ISimpleAudioVolume)
if mode == "mute":
    vol.SetMute(True, None)
elif mode == "vol0":
    vol.SetMasterVolume(0.0, None)
elif mode == "quiet":
    vol.SetMasterVolume(0.002, None)
else:
    sys.exit("refusing an audible mode")
# Safety: never start unless the session is really silent.
if not (vol.GetMute() or vol.GetMasterVolume() <= 0.0025):
    sys.exit("session not muted; refusing to play")
render = client.GetService(byref(IAudioRenderClient._iid_)).QueryInterface(IAudioRenderClient)
size = client.GetBufferSize()
phase = 0
client.Start()
end = time.monotonic() + seconds
print("started", flush=True)
while time.monotonic() < end:
    pad = client.GetCurrentPadding()
    n = size - pad
    if n > 0:
        t = (np.arange(n) + phase) / rate
        phase += n
        data = (0.1 * np.sin(2 * np.pi * freq * t)).astype(np.float32)
        buf = render.GetBuffer(n)
        ctypes.memmove(buf, data.ctypes.data, n * 4)
        render.ReleaseBuffer(n, 0)
    time.sleep(0.01)
client.Stop()
