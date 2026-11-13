# OPEN

What a finished TODO.md item left open: checks not run, tests not written, deferred review
findings, rejected fixes. Grouped by the milestone the item belongs to. One line per point,
naming the item it came from.

When a point is done, delete its line in the commit that does it. When it turns into planned
work, move it to TODO.md as a task and delete it here. Drop a milestone heading once it is empty.

## A4. Screen coverage

- #21 Electron's `IsOffscreen` on a virtualized list (long Discord channel) not checked; Gecko (Zen) `IsOffscreen` on scrolled-away nodes not seen, the throwaway Zen page exposed no web content in the 2026-11-13 re-run
- #150 Not spiked on the real PC: `SHQueryUserNotificationState` while a borderless game, an exclusive-fullscreen game, fullscreen mpv/MPC-BE, browser F11 and RDP run; the cost of the call (cached 1 s); and whether QUNS reports D3D fullscreen while another window is in front (alt-tab, side monitor). Microsoft documents only that an exclusive D3D app is running, so tying it to the focused window is an assumption, bounded by the covers-monitor check. `kmplayer64.exe` comes from a third-party listing, not the vendor.
- #147 Cost not measured on the real PC with a fullscreen game (FPS/frame time, CPU, side-capture duration incl. up to 4 UIA reader calls per side browser, post-game `ocr_frame` backlog); the 15 s side interval is unmeasured
- #147 Not checked in the running app: Discord and a video on side monitors during a fullscreen game
- #147 No test asserts the `BrowserState` verdict a side browser publishes during a game
- #144 `window_at`'s `GA_ROOTOWNER` rule not run on real Windows (focused browser dropdown or autofill list)
- #144 Not checked in the running app: scroll a side Discord window, then the pane beside the focused one

## A3. Capture keeps running and keeps what it recorded

- #22 UIA timeout not checked against a really hung app
- #158 PrintWindow not checked on a really hung window
- #158 No check of a window over 16 MP spanning monitors
- #22 A hung focused browser still costs several UIA timeouts per tick
- #22 Timing spike used throwaway profiles; not timed on the user's real profiles or a focused Gecko password field
- #23 #160 Not checked on a real device unplug or default-device switch in the running app
- #162 Thresholds (3 failed grabs, 60 s without a device read) unmeasured; not checked with a real unplugged mic or a really hung window in the running app
- #161 Not checked against a real long retention or Delete-all lock in the running app
- #161 Frame and game-row writes are still not retried (one lost frame)
- #161 Play/pause events of a live session are lost on a busy database (matters for #167)
- #161 A database still busy at stop can exceed the supervisor's 10 s kill: queued audio is lost and an `.opus.enc` written before the kill may be orphaned; open media rows wait for close_stale

## A2. Capture stops when asked

- #60 #163 Not checked in the running app: Lock, Quit, pause then lock/unlock, pause then restart
- #163 No test sends a real `POST /api/pause` over HTTP; tests call `controls.pause()` directly
- #163 No test uses a really closed connection; the `ProgrammingError` is injected
- #163 The pause write holds `_restart_lock`, so a busy database delays `poll()` (up to 5 s; see #69)
