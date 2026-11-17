"""Read-only: which processes hold an active capture (microphone) session, and the ConsentStore
'in use' entries. Executable names only."""
import json, sys, os, winreg
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
from passive import IAudioSessionManager2, IAudioSessionControl2, IAudioMeterInformation, name_of
from wasapi import enumerator
en = enumerator(); devs = en.EnumAudioEndpoints(1, 1)  # eCapture, active
caps = []
for i in range(devs.GetCount()):
    mgr = devs.Item(i).Activate(IAudioSessionManager2._iid_, 23, None).QueryInterface(IAudioSessionManager2)
    se = mgr.GetSessionEnumerator()
    for j in range(se.GetCount()):
        s = se.GetSession(j); c2 = s.QueryInterface(IAudioSessionControl2)
        pid = c2.GetProcessId()
        caps.append({"endpoint": i, "exe": name_of(pid)[0] if pid else "", "state": c2.GetState()})
inuse = []
base = r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone\NonPackaged"
with winreg.OpenKey(winreg.HKEY_CURRENT_USER, base) as k:
    for i in range(winreg.QueryInfoKey(k)[0]):
        sub = winreg.EnumKey(k, i)
        with winreg.OpenKey(k, sub) as kk:
            try:
                start = winreg.QueryValueEx(kk, "LastUsedTimeStart")[0]; stop = winreg.QueryValueEx(kk, "LastUsedTimeStop")[0]
            except OSError:
                continue
        if start and stop == 0:
            inuse.append(sub.split("#")[-1].lower())
print(json.dumps({"capture_sessions": caps, "consentstore_in_use_now": inuse}, indent=1))
