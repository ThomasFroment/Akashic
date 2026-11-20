"""Launcher-folder and OS-store facts about an executable path (read-only registry and file reads).

Used by the real-window spike (facts per case) and by detection candidates (cached, refreshed at
most once a minute per the brief). Paths are compared lower-case and never printed.
"""

from __future__ import annotations

import glob
import json
import os
import re
import winreg
from pathlib import Path


def _reg(root, path: str, name: str):
    try:
        with winreg.OpenKey(root, path) as k:
            return winreg.QueryValueEx(k, name)[0]
    except OSError:
        return None


def steam_root() -> Path | None:
    p = _reg(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath")
    return Path(p) if p else None


def steam_game_dirs() -> list[str]:
    """<library>/steamapps/common/<installdir> of every installed app manifest."""
    root = steam_root()
    if root is None:
        return []
    libs = {str(root)}
    vdf = root / "steamapps" / "libraryfolders.vdf"
    if vdf.is_file():
        libs |= {m.replace("\\\\", "\\") for m in re.findall(r'"path"\s+"([^"]+)"', vdf.read_text(encoding="utf-8", errors="replace"))}
    out = []
    for lib in libs:
        for acf in glob.glob(os.path.join(lib, "steamapps", "appmanifest_*.acf")):
            m = re.search(r'"installdir"\s+"([^"]+)"', Path(acf).read_text(encoding="utf-8", errors="replace"))
            if m:
                out.append(os.path.join(lib, "steamapps", "common", m.group(1)).lower().replace("/", "\\"))
    return out


def epic_game_dirs() -> list[str]:
    base = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "Epic" / "EpicGamesLauncher" / "Data" / "Manifests"
    out = []
    for item in base.glob("*.item") if base.is_dir() else []:
        try:
            loc = json.loads(item.read_text(encoding="utf-8")).get("InstallLocation")
        except (OSError, ValueError):
            continue
        if loc:
            out.append(loc.lower().replace("/", "\\"))
    return out


def gog_game_dirs() -> list[str]:
    out = []
    for base in (r"Software\WOW6432Node\GOG.com\Games", r"Software\GOG.com\Games"):
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base) as k:
                for i in range(winreg.QueryInfoKey(k)[0]):
                    p = _reg(winreg.HKEY_LOCAL_MACHINE, base + "\\" + winreg.EnumKey(k, i), "path")
                    if p:
                        out.append(p.lower().replace("/", "\\"))
        except OSError:
            continue
    return out


def gameconfigstore_exes() -> set[str]:
    out = set()
    base = r"System\GameConfigStore\Children"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, base) as k:
            for i in range(winreg.QueryInfoKey(k)[0]):
                p = _reg(winreg.HKEY_CURRENT_USER, base + "\\" + winreg.EnumKey(k, i), "MatchedExeFullPath")
                if p:
                    out.add(p.lower().replace("/", "\\"))
    except OSError:
        pass
    return out


def running_appid() -> int:
    v = _reg(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "RunningAppID")
    return int(v or 0)


class Facts:
    """Every launcher list read once (the candidates' cache, refreshed by the caller)."""

    def __init__(self) -> None:
        self.refresh()

    def refresh(self) -> None:
        dirs = [*steam_game_dirs(), *epic_game_dirs(), *gog_game_dirs()]
        self.game_dirs = tuple(d.rstrip("\\") + "\\" for d in dirs)
        self.gcs = gameconfigstore_exes()
        root = steam_root()
        self.steam_root = (str(root).lower().replace("/", "\\").rstrip("\\") + "\\") if root else None

    def of(self, path: str) -> dict:
        p = (path or "").lower().replace("/", "\\")
        return {"under_launcher_game_dir": bool(p) and p.startswith(self.game_dirs),
                "in_gameconfigstore": p in self.gcs,
                "under_steam_root": bool(p and self.steam_root and p.startswith(self.steam_root))}
