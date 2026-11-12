# pyright: basic
"""Start with Windows: the user's own ``Run`` registry value (no admin rights, no service).

Reports and writes facts only; Settings decides when. Manual check: tests/manual/CHECKLIST.md.
"""

from __future__ import annotations

import winreg
from typing import Final

RUN_KEY: Final = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE: Final = "Akashic"


def is_enabled() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, VALUE)
    except OSError:
        return False
    return True


def enable(command: str) -> None:
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
        winreg.SetValueEx(key, VALUE, 0, winreg.REG_SZ, command)


def disable() -> None:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, VALUE)
    except FileNotFoundError:
        return
