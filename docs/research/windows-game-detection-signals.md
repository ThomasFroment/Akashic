# How can a desktop app tell a game from a fullscreen non-game on Windows?
Date: 2026-11-19 · Context: Python 3.14, Windows 11 build 26200, comtypes, winreg, PDH via ctypes; Akashic `capture/gaming.py` · Goal: stop excluded or non-game fullscreen windows being recorded as games and keep real games found (audit #188, #192) · Ruled out: none

## Answer
No OS list and no single runtime signal decides it. The cleanest cheap positives are where the
executable lives and what the launcher reports: an exe under an installed launcher game folder
(Steam `steamapps\common\<installdir>` from the app manifests, Epic manifests, GOG registry) [1],
Steam's `RunningAppID` set while the game runs, and Windows' GameConfigStore entries, which on the
target PC held 52 executables, 48 under the Steam library, no checked non-game, but missed an
installed game (Outer Wilds) [2]. `SHQueryUserNotificationState` state 3 is a precise but rare
positive: a real Steam game in the foreground read state 2 ("a full-screen application") for 10 of
10 samples [2][3]. A hidden cursor held in all samples, while the 3D-engine-only GPU share read 0 %
for the game and 10 % for Discord, so neither is a verdict alone [2][4]. Launcher folders and
GameConfigStore together cover Steam, Epic and GOG games; games from launchers they do not read
(Battle.net, Xbox, EA, Ubisoft, Riot, Minecraft Java under `javaw.exe`) still need a fallback such
as today's caption-less covering-window rule or the user's own list [5][6].

## Findings
### OS stores
- `Windows.Gaming.Preview.GamesEnumeration.GameList` needs the restricted `gameList` capability;
  calls fail at runtime unless the developer account is specially provisioned [7].
- GameConfigStore (`HKCU\System\GameConfigStore\Children`) holds one subkey per game Game Bar has
  seen, with `MatchedExeFullPath` on some entries and undocumented `Flags` [8]; a maintainer of
  another tool found it does not reliably contain installed games [9].
- On the target PC: 116 subkeys, 52 with an exe, 48 of those under the Steam library; Terraria and
  PAYDAY 3 present, Outer Wilds absent; none of discord.exe, code.exe, chrome.exe,
  windowsterminal.exe, steamwebhelper.exe, photos.exe, spotify.exe, zen.exe [2].
### Launcher manifests
- Steam: `SteamPath` in `HKCU\Software\Valve\Steam`; `steamapps\libraryfolders.vdf` lists the
  libraries; each library's `.acf` manifests give `appid`, `name`, `installdir` under
  `steamapps\common` [1]; on the target PC the manifests are `appmanifest_<appid>.acf` [2].
- Manifests do not name the exe, so the test is "the process's exe is under that folder" [1].
- Steam libraries also hold non-game apps (type Application or Tool in `appinfo.vdf`), and Steam
  itself, Big Picture included, lives outside `steamapps\common` [1][10].
- Epic `.item` JSON manifests carry `InstallLocation` and `LaunchExecutable`; GOG lists games under
  `HKLM\Software\GOG.com\Games` [11][12].
- `HKCU\Software\Valve\Steam` `RunningAppID` held the running game's app id on the target PC [2].
### Runtime signals
- QUNS 3 means an exclusive-mode Direct3D application; 2 means any full-screen application or
  presentation settings [3]; fullscreen optimizations run "exclusive" games as borderless windows
  under DWM, so state 3 is uncommon [13].
- Passive sample, Steam game in front: QUNS 2 in 10/10, cursor hidden 10/10 and clipped 3/10, game
  3D-engine share median 0.0 %, Discord 10.2 %, dwm.exe 18.7 % [2].
- Per-process GPU use comes from the GPU scheduler for any API; per-process utilization is the
  busiest engine, and video may not use the 3D engine at all [4].
- PresentMon's present mode (independent flip) needs an ETW session; 2.3.1 runs without
  Administrator privileges, and 2.5.0 added mitigation for certain titles that block ETW [14].
### Today's rule
- `is_game_window`: no process or no covered monitor is not a game; then the user's `games`, then
  `not_games`, then D3D exclusive fullscreen, then "no frame and not in `DEFAULT_NOT_GAMES`" [5].
  The borderless rule is a user decision of 2026-11-13 [6].

## Caveats
- The passive sample is one game, 10 s, possibly paused or in a menu; the 0 % comes from counting
  only `engtype_3D` (`eval/quality/winapi.py:366`), not all engines [2][4].
- The GameConfigStore count was read with PowerShell on 2026-11-19 and is not stored in the repo;
  its schema and `Flags` are undocumented and may change between builds [2][8].
- The Steam manifest layout rests on an open-source library's wiki and the target PC's files, not
  Valve's documentation (403) [1][2]; the Epic details come from one write-up [11].
- Cursor, module (engine DLL) and present-mode signals were not measured on non-games here [2][14].

## Sources
[1] GameFinder wiki, Steam — https://github.com/erri120/GameFinder/wiki/Steam — tier 2 — accessed 2026-11-19
[2] Local reads and passive sample, target PC — `eval/quality/spikes/game_signals_passive.py`, `eval/quality/spikes/game_signals_passive.json`; GameConfigStore and Steam registry read with PowerShell — tier 1 — 2026-11-19
[3] QUERY_USER_NOTIFICATION_STATE — https://learn.microsoft.com/en-us/windows/win32/api/shellapi/ne-shellapi-query_user_notification_state — tier 1 — updated 2024-02-22
[4] GPUs in the Task Manager — https://devblogs.microsoft.com/directx/gpus-in-the-task-manager/ — tier 1 — DirectX blog
[5] Akashic game detection — `src/akashic/capture/gaming.py:93-102`, `docs/CAPTURE.md:61-65` — tier 1 — 2026-11-18
[6] Akashic decisions, game detection row — `docs/DECISIONS.md:21` — tier 1 — 2026-11-13
[7] GameList class — https://learn.microsoft.com/en-us/uwp/api/windows.gaming.preview.gamesenumeration.gamelist — tier 1 — page dated 2017
[8] GameConfigStore registry dump — https://github.com/cs2kitchen/FSO-Reg-keys/blob/main/defaultRegKeys.reg — tier 2 — accessed 2026-11-19
[9] G-Helper issue 3935, maintainer reply — https://github.com/seerge/g-helper/issues/3935 — tier 2 — 2025-09-13
[10] SteamAppInfo parser (appinfo.vdf, `common.type`) — https://github.com/ValveResourceFormat/SteamAppInfo — tier 2 — accessed 2026-11-19
[11] Epic Games Store manifests write-up — https://jayd.ml/games/2020/05/16/epic-games-store-steam-libraries.html — tier 3 — 2020
[12] GameFinder wiki, GOG Galaxy — https://github.com/erri120/GameFinder/wiki/GOG-Galaxy — tier 2 — accessed 2026-11-19
[13] Demystifying full screen optimizations — https://devblogs.microsoft.com/directx/demystifying-full-screen-optimizations/ — tier 1 — DirectX blog
[14] PresentMon releases (2.3.1, 2.5.0) — https://github.com/GameTechDev/PresentMon/releases — tier 1 — accessed 2026-11-19
