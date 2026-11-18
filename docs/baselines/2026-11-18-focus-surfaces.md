# Focus-taking surfaces spike 2026-11-18

Measurements behind TODO F21 and the input of F22 (popup rules): which top-level windows
appear when a browser, the shell or Windows shows a popup, a prompt or an overlay; what the
unchanged production path (`Desk` with `Verdicts`, `WgcCaptures`) does with each; and whether
their content can carry a captured secret. It also reruns the A4 occlusion measurement
([CAPTURE.md](../CAPTURE.md), "UI Automation timeouts") unlocked and in the foreground, and
measures `SHQueryUserNotificationState` (QUNS) and game detection with browser F11.
The test windows were disposable processes of the spike in one Job object
(`KILL_ON_JOB_CLOSE`), throwaway Chrome and Zen profiles on a dead proxy and a local page
server on 127.0.0.1; the user's profiles were not used (one file, the Proton Pass package, was
copied out of the user's Zen profile, see Limits). This report and the raw result file hold
numbers, booleans, Win32 and UIA class names, UIA control types, exe names of browser, shell
and system processes, monitor labels and verdict reason categories only: no title, page
text, address, OCR text, AutomationId or user-app name. The scripts were throwaway and are not
kept (shared helpers `lib.py`, `lib2.py`, `lib3.py`); each ran these cases: `a4.py` the A4 rerun,
`chrome_run.py` the Chrome cases, `zen_run.py` the Zen cases, `webview_run.py` (host
`webview_host.py`) the WebView2 cases, `shell_run.py` the CredUI (child `credui_child.py`), taskbar,
snipping overlay, toast, Command Palette and Task Manager cases, `f11.py` the QUNS and F11
states, `facts.py` the machine facts. Four debug scripts (emoji panel, taskbar hover, two toast
variants) were superseded. No file under `src/` or `tests/` changed by this work. The F22
decision (every browser popup stays blank) is recorded under "Decision input for F22"; it changes
no code.

## Machine

Same PC as [2026-11-17-large-page-text.md](2026-11-17-large-page-text.md): Windows 11 build
26200, Python 3.14.10. Monitors, left to right: `left` 2560x1440 at 100% (left = -2560), `primary`
3840x2160 at 125%, `right` 2560x1440 at 100% (left = 3840). Every spike window sat on `right`
unless a case says otherwise.

| Fact | Value |
| --- | --- |
| Chrome / Zen | 157.0.8103.79 / 1.24.1b |
| Idle QUNS (`SHQueryUserNotificationState`) | HRESULT 0, state 5 (accepts notifications) |
| Keyboard layouts (`GetKeyboardLayoutList`) | 1, layout id `0xf0010409`, no IME (high nibble not `0xE`) |
| UAC | `EnableLUA` 1, `ConsentPromptBehaviorAdmin` 5, `PromptOnSecureDesktop` 1; token elevation type 3 (limited, split admin token) |
| Command Palette | running; `SummonOn` = `ToPrimary`; hotkey fields not present in its settings file (Win+F12 was pressed and opened it) |
| Task Manager before the run | not running |
| `Shell_SecondaryTrayWnd` on the right (or any other) monitor | none: the taskbar exists on the primary only |

## Method

- **Production path.** `Desk(WgcCaptures(), Verdicts(Exclusions(UserExclusions(processes=("powershell.exe",)),
  "http://127.0.0.1:5123"), BudgetedUia(ThreadedUia(WindowsUia, FaultLog, clock)), BrowserState()))`,
  as wired in `ScreenCapture.__init__`, and a second `WgcCaptures` as the probe, in memory.
  Per case: a baseline snapshot of the visible window list (`Win32Windows.windows()`), the action,
  then polling every 0.2 s for up to 15 to 25 s for a *surface*: a window new in the visible list, or
  going from cloaked or minimized to shown. After 1.0 to 1.5 s the surfaces are re-collected: the
  taskbar and Task Manager cases by a fresh window scan; the Chrome, Zen, WebView2, CredUI,
  snipping overlay and Command Palette cases ran before that change and kept the surfaces found at
  first sight. `Desk.check` runs 10 times 0.3 s apart on the windows of the job's processes plus the surfaces
  (user windows are never captured, see Limits). Verdict and reason are read from the `Verdicts` memo
  after `judge`; role and `painted` from the `Desk` snapshot entries; marker pixels (R > 180, G < 80,
  B > 180) from the composite of every monitor and from a probe frame of the surface window.
- **Credentials.** Random lower-case letters generated in memory per run (username 9, password 11
  letters). "Visible" = the Windows OCR text of the probe frame (in memory, not stored) holds the token
  allowing one wrong letter.
- **Input safety.** Every keystroke or hover goes through one guard that first checks the foreground
  window: it must belong to a job process, or be a shell surface the spike just opened. Refused sends are
  counted. Final runs: 73 guarded sends, 0 refused, 0 sent anywhere else (the Task Manager case and the A4 rerun sent none,
  because the spike windows took the foreground on their own). Earlier,
  superseded runs of the same scripts (debugging the emoji panel, the taskbar hover and the toast)
  sent more guarded keystrokes; they were not counted and whether any was refused was not recorded. Input is `SendInput` (keys, absolute mouse
  move); the only unguarded calls are `SetCursorPos` to park the cursor and `SetForegroundWindow`
  with a thread-input attach.
- **Processes.** Children started suspended, assigned to the Job, then resumed. Chrome and Zen with a
  dead proxy (`--proxy-server=http://127.0.0.1:9`; Zen `network.proxy.*` 127.0.0.1:9, `network.trr.mode=5`).
  Zen was closed with `WM_CLOSE` and had exited before the job closed. Only job processes were killed.
  Task Manager was started with `ShellExecuteExW` and is outside the job.

## A4 rerun: flagless Chrome 157, unlocked, window in front

Five launches of Chrome with a page of 30 synthetic marker lines on 127.0.0.1, unchanged
`WindowsUia.read_text` (`max_nodes` 3000) every 0.5 s from the moment the window was in front,
cap 15 s. The window had been placed on the right monitor (1600x1000) and was in front in all 5.

| Measure | Result (5 of 5 launches alike) |
| --- | --- |
| Seconds from foreground to the first read holding a marker | 0.55 (2 reads; reads every 0.5 s, so that resolution; page of 30 marker lines; timed from the window in front) vs 2.66 in the 2026-11-17 run (page of 30k elements; timed from window placement): different runs, not one comparison |
| Visible, not in front (a spike window in front on the same monitor, 3 reads) | 27 markers per read |
| Fully covered by a spike window of exactly its rectangle, 10 reads over 10 s | 27 markers in every read |
| Uncovered again (spike window killed) | markers in the first read, 0.03 s, 1 read |
| Page `visibilityState` / `hasFocus()` in all three states | `visible` / `false`; the page's own report was still arriving at the end of each state |

Chrome did not treat the covered window as occluded (the page stayed `visible`), so no blank page
tree appeared and blanking under occlusion was not tested unlocked. The cover was a plain spike
window (`synthetic_window.py`, kind `plain`: framed overlapped window, not TOOLWINDOW, not LAYERED,
not WS_POPUP) in front, of exactly the page window's rectangle. Why Chrome did not count it as
occluding was not examined. The page never reported `hidden`.

## Chrome (throwaway profile, window on the right monitor)

Surface columns: class / process; owner kind (same pid as owner); flags; size; monitor; UIA root
class and control type; verdict (reason); owner verdict; Desk role and `painted`; marker pixels in
the composite and in the surface's probe frame; synthetic username and password visible.

| Case | Appeared | Surface |
| --- | --- | --- |
| Save-password bubble (typed synthetic login, Enter) | yes, 0.2 s | `Chrome_WidgetWin_1` / chrome.exe; browser main window (same pid); WS_POPUP, TOOLWINDOW, not TOPMOST, not NOACTIVATE, no frame, alpha none, not cloaked, not minimized, not ghost; 1 foreign child; 462x488; right; UIA `Chrome_WidgetWin_1`, Pane; **exclude** (`browser_popup`); owner keep; role hidden, `painted` false; marker 0 / 0; **username visible: true, password visible: false** |
| Password autofill dropdown (Down in the username field after saving) | yes, 0.2 s | `Chrome_WidgetWin_1` / chrome.exe; browser main window (same pid); WS_POPUP, TOOLWINDOW, TOPMOST, NOACTIVATE, no frame; 1 foreign child; 216x119; right; UIA `Chrome_WidgetWin_1`, Pane; **exclude** (`browser_popup`); owner keep; hidden, not painted; marker 0 / 0; username visible: true, password visible: false |
| HTTP Basic auth prompt | no new top-level window seen | the page was requested (the 401 was served); that a prompt showed on screen, and where, was not checked (no screenshot, no UIA read): not measured |
| WebAuthn / passkey prompt from a 127.0.0.1 page | no new top-level window seen | the page was requested; same: the prompt itself was not confirmed on screen (Esc was sent): not measured |
| Win+. emoji panel over a page input | not run: no surface within 15 s | Win+. sent twice (once by `keybd_event`, once by `SendInput` with scan codes) while Chrome was in front; the foreground never changed, so the panel did not open |
| Password-manager extension popup | not run: no extension installed, branded Chrome 137+ ignores `--load-extension` | |

The save bubble was then saved through UIA `Invoke` on its Save button (a control of the throwaway
profile) so the autofill case had a stored login. The Win32 and verdict facts above are of the
window found at first sight only; whether a second new window appeared was not checked.

## Zen (throwaway profile on the right monitor, window 1600x1000)

The window opened on the right monitor from the preset `xulstore.json`. The Proton Pass package
was loaded with `extensions.autoDisableScopes=0`; the popup opened with the manifest's
`_execute_action` shortcut (Ctrl+Shift+Y); nothing was clicked or typed inside it.

| Case | Appeared | Surface |
| --- | --- | --- |
| Proton Pass extension popup | yes, at once | `MozillaDropShadowWindowClass` / zen.exe; browser main window (same pid); WS_POPUP, TOOLWINDOW, not TOPMOST, not NOACTIVATE, no frame; 1 foreign child; 602x432; right; UIA `cui-widget-panel panel-no-padding`, Group; **exclude** (`window_unreadable`, from the blank-title rule, not the popup rule: `_rules_reason` runs before the popup branch, `src/akashic/capture/verdicts.py` about 308-314); owner keep; hidden, not painted; marker 0 / 0; probe frame delivered; username visible: false, password visible: false (no credentials were typed into it) |
| Save-login doorhanger (typed synthetic login, Enter) | yes, at once | `MozillaDropShadowWindowClass` / zen.exe; browser main window (same pid); WS_POPUP, TOOLWINDOW, not TOPMOST, not NOACTIVATE, no frame; 1 foreign child; 374x213; right; UIA `popup-notification-panel panel-no-padding`, Group; **exclude** (`window_unreadable`, from the blank-title rule, not the popup rule, as for the extension popup); owner keep; hidden, not painted; marker 0 / 0; username visible: true, password visible: false |
| Login autocomplete (Down in the username field) | not run: no surface within 15 s | the login was saved through UIA `Invoke` on the doorhanger button; no popup window followed the key |
| HTTP Basic auth prompt | no new top-level window seen | the page was requested; the prompt was not confirmed on screen: not measured |
| Emoji panel (Win+.) | not run: no surface within 15 s | same as Chrome |

The Proton Pass popup's content was not read: signed in: not checked (new profile, expected signed
out; see Limits). Zen exited
after `WM_CLOSE` before the job closed.

## WebView2 (pywebview 6.0 in an isolated uv environment, Python 3.13, dead proxy)

The host window was a job child on the right monitor; both popups belong to `msedgewebview2.exe`.

| Case | Appeared | Surface |
| --- | --- | --- |
| `<select>` dropdown (Alt+Down) | yes | `Chrome_WidgetWin_1` / msedgewebview2.exe; owner: spike window (not the same pid); WS_POPUP, not TOOLWINDOW, not TOPMOST, NOACTIVATE, no frame; 1 foreign child; 36x74; right; UIA `Chrome_WidgetWin_1`, Pane; **keep**; owner keep; role app, painted; marker 0 / 0 |
| Datalist suggestions (typed two letters) | yes | `Chrome_WidgetWin_1` / msedgewebview2.exe; owner: spike window (not the same pid); WS_POPUP, TOOLWINDOW, TOPMOST, NOACTIVATE, no frame; 1 foreign child; 342x186; right; UIA `Chrome_WidgetWin_1`, Pane; **keep**; owner keep; app, painted; marker 0 / 0 |

## Windows CredUI prompt

`CredUIPromptForWindowsCredentialsW` from a job child; synthetic credentials typed into it with the
dialog allowed by the guard. One surface: `Credential Dialog Xaml Host` / credentialuibroker.exe;
no owner; WS_POPUP, not TOOLWINDOW, not TOPMOST, not NOACTIVATE, has frame; 0 foreign children;
570x689; primary; UIA `Credential Dialog Xaml Host`, Window; **exclude** (`system_secret`); no owner
verdict; hidden, not painted; marker 0 / 0; probe frame delivered, non-blank not recorded;
username visible: false, password visible: false (so the two false may mean a blank frame).
Esc dismissed it.

## Shell surfaces

**Taskbar thumbnail of a minimized, excluded marker window.** The marker window (magenta
PowerShell form) was minimized; the cursor hovered its taskbar button on the primary taskbar
(there is none on the right monitor) by `SendInput` while a job window was in front. One new
window appeared after 0.41 s: `Xaml_WindowedPopupClass` / explorer.exe; owner another popup (same
pid); WS_POPUP, not TOOLWINDOW, TOPMOST, NOACTIVATE, no frame; 0 foreign children; **0x0**; primary;
UIA `Xaml_WindowedPopupClass`, Pane. It has no verdict or Desk role: a zero-size window has no
visible part. A WGC probe could not start on it (HRESULT 0x80070057). Marker pixels reaching the
composite: 0, with no capture of the thumbnail to judge: **not measured**. No existing explorer
window was in the `Desk` list, and the thumbnail picture is not a window of its own in the list.
2026-11-15 measured 25,070 marker pixels with a desktop grab
([window composition](2026-11-15-window-composition.md)); the case is open in OPEN.md.

**Win+Shift+S overlay that outlives an excluded marker window closed under it.** The marker window was
visible on the right monitor; the overlay was opened; the marker window was closed with `WM_CLOSE`
(gone) while the overlay was up. Four new windows, all `snippingtool.exe`, no owner, TOPMOST, TOOLWINDOW:

| Class | Size | Monitor | Verdict (reason) | Role | Marker px, probe |
| --- | --- | --- | --- | --- | --- |
| `XamlWindow` (UIA Window) | 2560x1440 | right | exclude (`shell_copy_surface`) | hidden, not painted | 0 |
| `XamlWindow` | 2560x1440 | left | exclude (`shell_copy_surface`) | hidden, not painted | 0 |
| `XamlWindow` | 3840x2160 | primary | exclude (`shell_copy_surface`) | hidden, not painted | 0 |
| `SnipOverlayRootWindow` | 8960x2160 | primary (spans all) | none: cloaked, so not visible to `Desk` | none | 0 |

Marker pixels in the composite: 0. The probe frames of these overlays showed 0 although the overlay
held a frozen copy of the marker, so they are blank (likely excluded from capture) and prove nothing.
The overlay classes are already in `SHELL_COPY_SURFACES`; 2026-11-15 measured 3.71 M marker pixels
with a desktop grab and 0 composed. The spike did not test removing the marker's exclusion.

**Toast on the primary.** Not run: no window changed within 20 s of showing a toast with the
PowerShell app id and a local magenta hero image (QUNS before: HRESULT 0, state 5, so toasts were
accepted by the shell's own check; the toast did not appear, likely because notifications for that
app id or Focus assist suppress it; no user setting was changed). Nothing to remove from history; the
removal by tag and group ran (not checked beyond its exit).

**PowerToys Command Palette (Win+F12).** Over the excluded magenta marker (primary monitor, in front)
and in a control run over a neutral window with no marker: identical in both. `WinUIDesktopWin32WindowClass` /
microsoft.cmdpal.ui.exe; owner "other" (an owner exists that is not in the list); WS_POPUP not set,
TOOLWINDOW, TOPMOST, no frame; 0 foreign children; 1000x600; primary; UIA same class, Pane;
**keep**; role app, **painted**; marker pixels 0 in the composite and 0 in the probe frame in both runs (2026-11-15 saw 1,292
unexplained marker pixels with it; see Limits).
The marker window stayed hidden under it, so nothing of it reached the composite; the Command
Palette's own content is kept and painted.

**Task Manager (auto-elevating).** Started with `ShellExecuteExW` ("open"); no UAC prompt appeared. Window
`TaskManagerWindow` / taskmgr.exe; no owner; frame, 1758x946, on the right monitor already. Process name
readable: yes; foreign children readable: yes (0); **keep**, role app, painted; WGC started and delivered a frame:
yes; `WindowsUia.read_text` returned 0 blocks; `SetWindowPos` returned false (the window was already on the
right monitor); `PostMessage(WM_CLOSE)` did not close it: **left open, needs user to close**. It runs
elevated (it ignored the posted close from a non-elevated process); this was not verified by a token read.
Not terminated.

## QUNS and F11 (Chrome and Zen)

QUNS read every 0.5 s for 5 s per state: all 10 samples HRESULT 0, state 5 in all 8 browser states.
`focused_window()` is the window in front at that moment; "browser" is the browser's own window facts
(`has_frame` and `covers_monitor` from the window list).

| State | Chrome: focused has_frame / covers; browser has_frame / covers | Zen: same | `exclusive_fullscreen` | `is_game_window` with defaults; would be a game without the deny-list | `GameMode.observe` 6 s started a game |
| --- | --- | --- | --- | --- | --- |
| In front, not fullscreen | true / no; true / no | true / no; true / no | false | no; no | no (both) |
| F11, in front, right monitor | false / yes; false / yes | false / yes; false / yes | false | no; **yes** | no (both) |
| F11, spike window focused over it | true / no (the spike window); false / **no** | true / no (the spike window); false / **yes** | false | no; Chrome no, Zen **yes** (browser window) | no (both) |
| After leaving F11 | true / no; true / no | true / no; true / no | false | no; no | no (both) |

QUNS reported "accepts notifications" (state 5) and never D3D or presentation mode in any browser F11
state, including with F11 in front. In "F11 with a spike window focused over it", Chrome's own window no longer covered the
monitor (cause not examined; Zen's still covered it).
Without the deny-list, both browsers in F11 would be a game by the frame test (no frame and covering
the monitor); the deny-list is what keeps them out.

## Decision input for F22

One row per popup kind. Recommended verdicts were inputs; F22 decided (user, 2026-11-18, "Keep them blank"): every browser
popup, owned or not, stays blank permanently, because the credential bubbles share class and owner
with harmless popups such as select dropdowns; WebView2 popups follow their owner. The 2026-11-15
rows are from [2026-11-15-window-composition.md](2026-11-15-window-composition.md) (popups clicked
by the user, without the popup rule); the others are this spike's.

| Kind | Owned? | What identifies it | Can show captured secrets | Today | Recommended |
| --- | --- | --- | --- | --- | --- |
| Chrome save-password bubble | owned by the main window (same pid) | `Chrome_WidgetWin_1`, UIA `Chrome_WidgetWin_1` Pane, chrome.exe, 462x488, WS_POPUP+TOOLWINDOW, not TOPMOST, activatable | username: measured visible; password: masked here; the bubble has a reveal button (known Chrome capability, not clicked) | exclude (`browser_popup`) | **always exclude** |
| Chrome password autofill dropdown | owned (same pid) | same class, 216x119, WS_POPUP+TOOLWINDOW+TOPMOST+NOACTIVATE | username: measured visible; also account names and a "manage" row | exclude (`browser_popup`) | **always exclude** |
| Chrome HTTP Basic auth, WebAuthn prompt | no top-level window seen | not confirmed on screen | not measured | main window verdict | **not measured** |
| Chrome select dropdown (2026-11-15) | owned by the browser window | `Chrome_WidgetWin_1`, 307x146 | page option text: 27,344 marker pixels, 4 SECRET words | blank (`browser_popup`) | **always exclude** |
| Chrome suggestions and tooltip (2026-11-15) | owned | same class, 209x139 and 147x25 | 2 SECRET words each | blank | **always exclude** |
| Chrome context menu (2026-11-15) | owned | same class, 431x510 | no page content | blank | **always exclude** (same class and owner as the rest) |
| Zen popups (2026-11-15) | owned | `MozillaDropShadowWindowClass`, four popups | 2 SECRET words | blank (`window_unreadable` today, see the Zen rows) | **always exclude** |
| Zen extension popup (Proton Pass) | owned (same pid) | `MozillaDropShadowWindowClass`, UIA root `cui-widget-panel panel-no-padding` Group, zen.exe, 602x432, WS_POPUP+TOOLWINDOW | a signed-in password-manager popup lists vault items and can reveal passwords (known capability; nothing read here) | exclude (`window_unreadable`) | **always exclude** |
| Zen save-login doorhanger | owned (same pid) | same class, UIA `popup-notification-panel panel-no-padding` Group, 374x213 | username: measured visible; password masked here, a show-password control exists (known) | exclude (`window_unreadable`) | **always exclude** |
| Zen login autocomplete | not seen | | | | not measured |
| Zen HTTP Basic auth | no top-level window seen | not confirmed on screen | not measured | main window verdict | **not measured** |
| Emoji panel (Win+.) | not seen (did not open) | | clipboard-history tab is a known capability of the panel | | not measured |
| WebView2 select dropdown | owner is the host window of another process (not the same pid) | `Chrome_WidgetWin_1`, msedgewebview2.exe, 36x74, WS_POPUP+NOACTIVATE | its own page's option text | keep | **follow owner** (as 2026-11-15) |
| WebView2 datalist suggestions | owner is the host (other pid) | same class, 342x186, TOOLWINDOW+TOPMOST+NOACTIVATE | typed values from the page's datalist | keep | **follow owner** |
| Windows CredUI prompt | ownerless | `Credential Dialog Xaml Host`, credentialuibroker.exe, 570x689, frame | typed username and password (not seen here: non-blank probe frame not recorded) | exclude (`system_secret`) | **always exclude** (keep the process rule) |
| Taskbar thumbnail | owned by another popup | `Xaml_WindowedPopupClass`, explorer.exe, 0x0, TOPMOST+NOACTIVATE | previews of minimized windows (the picture is not in the list) | not judged (no visible part) | **not measured** (2026-11-15: 25,070 marker pixels with a desktop grab, inconclusive; open in OPEN.md) |
| Snipping overlay | ownerless, one window per monitor | `XamlWindow` and `SnipOverlayRootWindow`, snippingtool.exe, monitor-sized, TOPMOST+TOOLWINDOW | a frozen copy of the screen (2026-11-15: 3.71 M marker pixels with a desktop grab, 0 composed) | exclude (`shell_copy_surface`) | **always exclude** (already listed) |
| PowerToys Command Palette | ownerless in the list | `WinUIDesktopWin32WindowClass`, microsoft.cmdpal.ui.exe, 1000x600, TOPMOST+TOOLWINDOW, no frame | clipboard history and search results by extension (known capability); it did not show the window under it; 2026-11-15: 1,292 unexplained marker pixels | keep, painted | **always exclude** by process (judgement, not measured content) |
| Task Manager | ownerless | `TaskManagerWindow`, taskmgr.exe, frame | process names and command lines | keep, painted | **keep** |
| Toast | not seen | | | | not measured |

## Not run

- Chrome password-manager extension popup: none installed, branded Chrome 137+ ignores `--load-extension`.
- Chrome emoji panel and Zen emoji panel: Win+. did not open the panel within 15 s.
- Zen login autocomplete: no popup window appeared within 15 s.
- Toast: no toast window appeared within 20 s.
- IME candidate window: `GetKeyboardLayoutList` shows no IME.
- Edge, Brave, Firefox, Chrome for Testing: out of scope.
- A UAC prompt or secure desktop: none appeared (Task Manager launched without one).
- Taskbar thumbnail: the only new window was 0x0 and not capturable; not measured.
- Chrome and Zen HTTP Basic auth and Chrome WebAuthn prompts: only the page request was checked.
- The unexplained 1,292 marker pixels of Command Palette (2026-11-15) were not reproduced.

## Limits

- One PC, one run per case after the harness settled; one sample per surface size. The cases that ran
  before the surface re-collection was changed to a fresh window scan (Chrome, Zen, WebView2, CredUI, the
  snipping overlay and Command Palette cases) took the surface set found at first sight; each of them
  reported one or more surfaces as listed, and whether a second new window appeared was not checked.
- User windows were never captured: the `Desk` list held only job-process windows and the new
  surfaces, so a user window covering a spike window was not modelled. Occlusion among the spike's
  windows only.
- The dead proxy means no page outside 127.0.0.1 loaded; the browsers' prompts were the ones reached
  from local pages.
- Marker pixels were 0 everywhere. The marker window was in the list (excluded) in the shell cases, so a
  non-zero count would have meant a leak; but 0 in a probe frame is weak evidence of anything. The
  marker test (R > 180, G < 80, B > 180) misses tinted or blurred markers (acrylic), so the Command
  Palette 0 does not explain the 1,292 of 2026-11-15. The snipping overlay probe frames showed 0
  although the overlay held a frozen copy of the marker, so those frames are blank (likely excluded
  from capture).
- OCR was Windows OCR with the `en-US` recognizer; the system language may differ, so a "password" word
  or a token could be missed: the visible booleans are lower bounds. The Chrome save bubble and the
  Zen doorhanger showed the username; the Chrome dropdown showed it too in the final run.
- The Proton Pass package was copied read-only from the user's Zen profile `extensions\` folder into
  the throwaway profile (no other profile file was touched); its popup was not read, so "signed in" was
  not checked in its UIA tree (the profile is new, so it is expected to be signed out).
- The foreground and the cursor were taken and handed back at the end of each script, which touches
  a user window; Task Manager is the one process outside the job and was left open for the user to
  close (it ignored `WM_CLOSE`; it was not terminated).
- The Chrome A4 pages were readable at once because the window was in the foreground and never
  occluded for Chromium; this does not contradict the 2026-11-13 locked-PC result, only extends
  the 2026-11-17 one-launch result to five launches.
- QUNS was only sampled with browsers; games, mpv/MPC-BE and RDP (#150) remain unspiked.
