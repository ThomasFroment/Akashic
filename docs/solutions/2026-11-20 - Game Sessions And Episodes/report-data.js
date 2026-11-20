window.REPORT = {
  title: "Game Sessions And Episodes",
  date: "2026-11-20",
  depth: "full",
  context: "Python 3.14, ctypes Win32 (OpenProcess, WaitForSingleObject, GetProcessTimes, IsWindow), Windows 11 26200, Ryzen 7 7800X3D; harness replays of quality runs 0315, 1818 (standard) and 0547 (day)",
  problem: "Akashic splits one play session into many stored game rows (7 per real session in the standard runs, up to 25 in the day run), opens game rows for fullscreen non-games and for excluded processes (#188, #192), and builds video and game episodes that miss what happened (M11.1 F1 video 0.12 on the day run, game 0.32-0.33; M11.2 0.53-0.75).",
  goals: ["One stored game session per real play session", "Tell games from fullscreen non-games and keep excluded apps out (#188, #192)", "Video and game episodes that match what happened (boundaries and titles)"],
  contract: {
    must: ["At most 1.5 stored game rows per real game session on the standard and day runs", "M6.1 game start and end error within 15 s", "No excluded process in a game row or the log (#188)", "No game row for a fullscreen non-game of the harness (Big Picture, F11 browser/terminal/editor, Photos, frameless Electron)", "M11.1 F1 for video and game episodes at least 0.8", "M11.2 title accuracy at least 0.8"],
    fake: ["Games from launchers not installed here detected by today's covering-window rule only", "Episode boundaries within 30 s"]
  },
  budget: ["≤ 1 ms extra per foreground check", "No new polling thread above 1 Hz", "Registry and launcher reads cached, refreshed at most once a minute", "Episode derivation ≤ 100 ms per day of data on one core", "No new dependency; schema may change"],
  constraints: ["Win32 through ctypes only", "GamesEnumeration needs a restricted capability", "Protected processes refuse process handles"],
  nonGoals: ["Game frame-rate guardrails (G3)", "Game-mode capture cadence and OCR of game HUDs", "Conversation and work/browsing episodes", "Media session timing (#189)", "Ask quality"],

  answer: {
    text: "**C1**: one game row per process instance, from process creation to exit through a held `SYNCHRONIZE | QUERY_LIMITED` handle, time in front kept as a separate ledger, a provenance verdict that checks the exclusion first, and video episodes built from chains of media rows; a Pause splits game rows, game episodes and video chains [1]. On truth clipped to the recorded span and split at stored Pauses it meets every must: rows per session 1.25 (max 2 only where a harness restart overlaps by 0.2-0.5 s), M6.1 start and end 0.0 s, detection 0/0/0, F1 video 1.00 and game 1.00, M11.2 0.86 [1]. Cost: +21.8 µs per check at first sight of a process, about 2 µs steady, against 1 ms; changed derivation 71.1 ms/day against 100; game-mode time unchanged (2.64 h, 6 s apart) [1][2]. Today's scorer reads rows 1.54 and M6.1 end 698.9 s for C1: it does not clamp the truth end to capture stop and counts every overlapping row [1]. Prior art: Playnite folder tracking, Steam play time, ActivityWatch heartbeat merge [6][7]; none found for a lifetime span with an in-front ledger. When the handle is refused, **C2** (focus rows, identity reopen, `IsWindow` liveness end) gets start ≤ 6.4 s, end ≤ 0.9 s, video F1 and titles 0.80 at the limit [1]."
  },
  bestFit: "C1",

  metrics: [
    { key: "check", label: "Extra per foreground check (worst)", unit: "µs", limit: 1000, budget: true, log: true },
    { key: "derive", label: "Derivation of changed parts", unit: "ms/day", limit: 100, budget: true },
    { key: "rows_c", label: "Rows per session, clipped truth (mean)", limit: 1.5 },
    { key: "start_c", label: "M6.1 start error max, clipped truth", unit: "s", limit: 15 },
    { key: "end_c", label: "M6.1 end error max, clipped truth", unit: "s", limit: 15 },
    { key: "f1v_c", label: "Video F1 min, clipped truth", limit: 0.8, better: "higher" },
    { key: "f1g_c", label: "Game F1 min, clipped truth", limit: 0.8, better: "higher" },
    { key: "titles_c", label: "M11.2 titles min, clipped truth", limit: 0.8, better: "higher" },
    { key: "det", label: "False games + excluded names written", limit: 0 },
    { key: "rows", label: "Rows per session, today's scorer (mean)", limit: 1.5, log: true },
    { key: "end", label: "M6.1 end error max, today's scorer", unit: "s", limit: 15, log: true },
    { key: "f1v", label: "Video F1 min, today's scorer", limit: 0.8, better: "higher" },
    { key: "f1g", label: "Game F1 min, today's scorer", limit: 0.8, better: "higher" },
    { key: "titles", label: "M11.2 titles min, today's scorer", limit: 0.8, better: "higher" }
  ],
  chart: { x: "check", y: "rows" },

  captureColumns: ["Per run: truth game, game rows, game episodes, truth video, video episodes", "Real-window detection table", "Harness metrics line (today's scorer)"],
  figures: [
    { src: "baseline.png", title: "Today on the same harness", caption: "Rows per session 5.38 (25 rows for one Outer Wilds session on the day run); 4 false games and 1 excluded name written (red and amber squares)." },
    { src: "fig-game-timeline.png", title: "C1 game rows against process lifetimes", caption: "Rows end at capture stop (white tick) while Outer Wilds' truth runs past it, the source of today's scorer's 698.9 s. The stored Pause (red) splits the Outer Wilds row. gaming_now follows the front ledger like today's rows." },
    { src: "fig-video-chains.png", title: "C1 viewing chains (P = 180 s, Pause splits)", caption: "0315: the short piece before the Pause is dropped by MIN_PLAYED_MS, so the first chain starts after the Pause. 1818: that piece is its own chain. Day run: 16 media rows become 4 chains." },
    { src: "fig-video-0547-v3.png", title: "One 70.9 min viewing, production against chains", caption: "Production makes 8 fragments below IoU 0.5 (red); every chain gap P covers it as one episode." },
    { src: "fig-video-1818-v1.png", title: "A viewing across a recording gap", caption: "Production keeps a short fragment before the gap (red); the chains in this P3 figure bridge the gap. C1 splits at a Pause instead." },
    { src: "fig-p2-end-error.png", title: "C2 end error against pulsetime P", caption: "End at the last front tick: up to 21020 s. End at the last liveness poll: ≤ 1 s, except the two Outer Wilds exits after recording stopped (~700 s, today's scorer)." },
    { src: "fig-p2-rows.png", title: "C2 rows per session against pulsetime P", caption: "Today's scorer. P = 3600 s reaches mean 1.23 (front end) and 1.46 (liveness end). The 'must ≤ 1.5' label sits on the 1818 lines at P = 60 s." },
    { src: "fig-adversarial.png", title: "Chains on 19 synthetic viewing cases", caption: "P = 180 s with today's columns: 11/19 (6 merged, 2 split); with position columns 16/19. A rewatch inside one row and live-stream title changes stay wrong." }
  ],

  candidates: [
    {
      id: "C1", name: "Lifetime rows, provenance verdict, viewing chains, Pause splits", status: "best",
      statusNote: "Prototype P1 pausesplit. Passes every must on clipped truth; today's scorer reads rows 1.54, end 698.9 s, F1 0.80 / 0.57, titles 0.75.",
      method: "Verdict once per (pid, creation time), exclusion first; row from process creation to exit via a held handle; front ledger for game mode; one game episode per row; media-row chains with P = 180 s; a stored Pause closes rows and splits chains.",
      pipeline: [{ label: "exclusion + verdict, first sight", cost: "21.8 µs" }, { label: "cached verdict + WaitForSingleObject", cost: "~2 µs per check" }, { label: "launcher scan, once a minute", cost: "7-12 ms" }, { label: "game episodes + app runs", cost: "9.2 ms/day" }, { label: "video chains", cost: "63.9 ms/day" }],
      uses: ["G1 Process-lifetime session", "G2 Launcher folders", "G4 Active time over lifetime", "G6 Stop hysteresis", "G7 Manifest name", "D2 GameConfigStore", "D6 Window-class deny", "D7 Package-path deny"],
      cost: "+21.8 µs per check worst, ~2 µs steady, against 1 ms; 71.1 ms/day changed derivation against 100; no thread above 1/60 Hz",
      keeps: "Every must on clipped truth; game-mode time (2.64 h, 6 s apart); active time error median 0.1 s",
      fakes: "Nothing beyond the allowed boundary slack",
      failsWhen: "Handle refused (protected, anti-cheat) → C2 path; Chromium/Electron/UWP games outside launchers → missed until listed; non-game tools under steamapps\\common → false game; rewatch within P without url or position → merged.",
      test: "Replays of 0315, 1818, 0547 + 16 real windows · clipped and today's scorer",
      priorArt: { name: "Playnite folder tracking + process monitor", url: "https://api.playnite.link/docs/manual/library/games/gameActions.html" },
      values: { check: 21.8, derive: 71.1, rows_c: 1.25, start_c: 0.0, end_c: 0.0, f1v_c: 1.0, f1g_c: 1.0, titles_c: 0.857, det: 0, rows: 1.54, end: 698.9, f1v: 0.80, f1g: 0.571, titles: 0.75 },
      capture: "C1.png",
      metricsLine: "CLIPPED+SPLIT: rows/truth mean=1.25 max=2 M6.1 start max=0.0s end max=0.0s M11.1 F1 video min=1.00 game min=1.00 M11.2 min=0.86 | det 0/0/0 | check +21.8 us | changed parts 71.1 ms/day | active err med=0.1s"
    },
    {
      id: "C2", name: "Focus rows + identity reopen, liveness end", status: "works",
      statusNote: "Prototype P2, END=alive, P = 3600 s. Passes the clipped musts at the limit (video F1 0.80, titles 0.80); the measured fallback when the handle is refused.",
      method: "Today's focus stretches keyed by (exe, creation time), merged within 3600 s across other windows; a stored gap closes rows; the last row of an identity ends at the last 1 s IsWindow poll that saw it.",
      pipeline: [{ label: "identity read, new pid", cost: "QUERY_LIMITED + GetProcessTimes" }, { label: "verdict (C1 rules)", cost: "+2.8 µs per check" }, { label: "IsWindow poll, 1 Hz", cost: "0.3 µs" }],
      uses: ["G3 Heartbeat merge", "G4 Active time over lifetime", "G2 Launcher folders", "D2 GameConfigStore", "D6 Window-class deny"],
      cost: "+2.8 µs per check; derivation of changed parts not measured separately",
      failsWhen: "Start error up to 6.4 s (first in-front sighting); chains in this prototype bridge a Pause, which costs video F1 on 1818.",
      priorArt: { name: "ActivityWatch heartbeat merge", url: "https://github.com/ActivityWatch/aw-core" },
      values: { check: 2.8, rows_c: 1.19, start_c: 6.4, end_c: 0.9, f1v_c: 0.80, f1g_c: 1.0, titles_c: 0.80, det: 0, rows: 1.46, end: 698.9, f1v: 1.0, f1g: 0.571, titles: 0.75 },
      capture: "C2.png",
      metricsLine: "rows/truth mean=1.46 max=2 | M6.1 start max=6.4s end med=0.6s max=698.9s | det 0/0/0 | F1 video min=1.00 game min=0.57 | M11.2 min=0.75 | check +2.8 us || clipped: rows 1.19 start max 6.4s end max 0.9s F1 0.80/1.00 M11.2 0.80"
    },
    {
      id: "C1b", name: "C1, chains bridge a Pause (one game episode per row)", status: "works",
      statusNote: "Prototype P1 rowep. Passes the clipped musts at the limit (video F1 0.80, titles 0.80).",
      method: "As C1, but video chains bridge a stored Pause of up to 30 min.",
      pipeline: [{ label: "as C1", cost: "21.7 µs worst" }, { label: "changed derivation", cost: "64.4 ms/day" }],
      uses: ["G1 Process-lifetime session", "G2 Launcher folders", "G4 Active time over lifetime", "G6 Stop hysteresis", "D6 Window-class deny"],
      cost: "+21.7 µs; 64.4 ms/day",
      values: { check: 21.7, derive: 64.4, rows_c: 1.25, start_c: 0.0, end_c: 0.0, f1v_c: 0.80, f1g_c: 1.0, titles_c: 0.80, det: 0, rows: 1.54, end: 698.9, f1v: 1.0, f1g: 0.571, titles: 0.75 },
      metricsLine: "CLIPPED+SPLIT: rows 1.25 max 2, M6.1 0.0/0.0 s, F1 video 0.80 game 1.00, M11.2 0.80"
    },
    {
      id: "C3", name: "Video viewing chains alone", status: "fails",
      statusNote: "Prototype P3. Video F1 1.00 on every run, but game rows and detection stay at today's values; kept inside C1.",
      method: "Chains of same (app, title, artist) media rows across gaps ≤ 180 s and stored recording gaps ≤ 30 min; MIN_PLAYED_MS on the chain sum; emitted once tail + P ≤ horizon.",
      pipeline: [{ label: "chain media rows", cost: "46.6 ms/day with games" }],
      uses: ["G6 Stop hysteresis", "G5 Progress threshold", "G3 Heartbeat merge"],
      cost: "+0.4 µs; 46.6 ms/day",
      failsWhen: "Rewatch within P without url or position data → merged; live-stream title change → split; early ends from capture (58.0 s, 116.6 s) cannot be repaired.",
      priorArt: { name: "Trakt/Simkl re-scrobble block", url: "https://api.simkl.org/api-reference/simkl/scrobble-stop" },
      values: { check: 0.4, derive: 46.6, det: 5, rows: 5.38, end: 4325.7, f1v: 1.0, f1g: 0.316, titles: 0.625 },
      capture: "C3.png",
      metricsLine: "F1 video min=1.00 | media rows 22 -> chains 7 | bounds max start=2.0s end=116.6s | media+game 46.58 ms/day | incremental same_as_one_pass=True | adversarial ok=11/19, +pos ok=16/19"
    },
    {
      id: "C1a", name: "C1 as first built: Pause bridged in chains and game episodes", status: "fails",
      statusNote: "Clipped M11.2 0.70; a Pause must split the row (answer 6).",
      method: "As C1, but chains and game episodes bridge a stored Pause of up to 30 min.",
      pipeline: [{ label: "as C1", cost: "21.7 µs worst" }, { label: "changed derivation", cost: "68.4 ms/day" }],
      uses: ["G1 Process-lifetime session", "G2 Launcher folders", "G4 Active time over lifetime", "G6 Stop hysteresis"],
      cost: "+21.7 µs; 68.4 ms/day",
      values: { check: 21.7, derive: 68.4, rows_c: 1.25, start_c: 0.0, end_c: 0.0, f1v_c: 0.80, f1g_c: 0.857, titles_c: 0.70, det: 0, rows: 1.54, end: 698.9, f1v: 1.0, f1g: 1.0, titles: 0.84 },
      metricsLine: "today's scorer: F1 1.00/1.00 M11.2 0.84 · CLIPPED+SPLIT: F1 video 0.80 game 0.86 M11.2 0.70"
    },
    {
      id: "C1d", name: "P1's no-handle fallback: today's rule + reopen within 300 s", status: "fails",
      statusNote: "End error 4534 s, rows max 6 on today's scorer; clipped start max 4228.7 s.",
      method: "Today's focus rows with the C1 verdict, reopened when the same instance returns within 300 s.",
      pipeline: [{ label: "verdict + focus rows", cost: "22.6 µs worst" }, { label: "changed derivation", cost: "72.3 ms/day" }],
      uses: ["G3 Heartbeat merge", "D2 GameConfigStore", "D6 Window-class deny"],
      cost: "+22.6 µs; 72.3 ms/day",
      values: { check: 22.6, derive: 72.3, rows_c: 1.50, start_c: 4228.7, end_c: 4534.2, f1v_c: 0.80, f1g_c: 0.50, titles_c: 0.50, det: 0, rows: 1.85, end: 4534.2, f1v: 1.0, f1g: 0.571, titles: 0.62 },
      metricsLine: "rows/truth mean=1.85 max=6 | M6.1 start max=413.7s end max=4534.2s | CLIPPED: rows 1.50 max 5, F1 game 0.50, M11.2 0.50"
    },
    {
      id: "C2a", name: "Focus rows + identity reopen, end at last front tick", status: "fails",
      statusNote: "Rows end at the last in-front tick: M6.1 end median 213.9 s, max 21020.6 s.",
      method: "As C2 without a liveness signal.",
      pipeline: [{ label: "identity + verdict", cost: "+2.8 µs per check" }],
      uses: ["G3 Heartbeat merge", "D2 GameConfigStore", "D6 Window-class deny"],
      cost: "+2.8 µs",
      values: { check: 2.8, det: 0, rows: 1.23, end: 21020.6, f1v: 1.0, f1g: 0.333, titles: 0.62 },
      metricsLine: "rows/truth mean=1.23 max=2 | M6.1 start max=6.4s end med=213.9s max=21020.6s | F1 video min=1.00 game min=0.33 | M11.2 min=0.62 | check +2.8 us"
    }
  ],

  method: {
    summary: "C1: a game row lives as long as the game process, time in front is a ledger inside it, the verdict is made once per process with the exclusion checked first, and video episodes are chains of media rows; a Pause splits rows and chains.",
    equation: "row_i = [max(creation_i, r0), min(exit_i, r1)]   for each recording stretch [r0, r1) after one in-front game verdict\nactive_ms_i = Σ in-front check intervals;  gaming_now reads last_front_ms\nexit_i: WaitForSingleObject(h, 0) signalled → ExitTime from GetProcessTimes\nchain: same (app, title, artist), gap ≤ 180 s with no other item, no stored Pause between; MIN_PLAYED_MS on Σ played_ms",
    steps: [
      { text: "Exclusion first: an excluded game gets a content-free row (no process, no title) and keeps game mode; its name is never logged (answer 7).", assumes: "Exclusions match by exe name, as today." },
      { text: "Verdict once per (pid, creation time): user lists; D3D exclusive → game (#150); launcher game folder or GameConfigStore → game; launcher root outside game folders → not; borderless only: UI-toolkit window class or Windows package path → not; else today's covering rule. Launcher facts rescanned once a minute.", assumes: "Store, Game Pass and Electron games are rare and go in the user's games list (answer 8)." },
      { text: "Open a SYNCHRONIZE | QUERY_LIMITED handle at the first in-front sighting; start = creation time; end = ExitTime once the handle signals. Focus loss never closes the row.", assumes: "An open handle keeps the pid from being reused (implied by the docs)." },
      { text: "A stored Pause, stop, crash, lock or sleep closes the row at its start; it reopens at max(creation, resume) while the process lives (answer 6)." },
      { text: "Keep active_ms and last_front_ms per row; gaming_now reads last_front_ms, so game mode follows time in front as today." },
      { text: "Name: Steam appmanifest name → ProductName when OriginalFilename matches the exe → most frequent title." },
      { text: "Refused handle: that instance follows C2 (identity reopen within 3600 s, IsWindow liveness end)." },
      { text: "Derivation: one game episode per row; app runs drop only the game's own visits; video chains with P = 180 s, split at a stored Pause, bridged across other stored gaps ≤ 30 min, emitted once tail + P ≤ horizon." }
    ],
    cost: "First sight of a process: image path + class + launcher-facts lookup 20.8 µs + decision 1.0 µs = +21.8 µs per check against 1 ms. Steady: cached verdict 1.0 µs + WaitForSingleObject 1.0-1.2 µs. Once per instance: OpenProcess + close 3.7 µs, GetProcessTimes 1.9 µs. Launcher scan once a minute, 7-12 ms. Changed derivation: chains 63.9 + games 2.5 + app runs 6.7 → 71.1 ms/day (maxima over runs) against 100.",
    failsWhen: "Handle refused (protected processes return error 5; anti-cheat may strip rights) → C2 path with start error up to 6.4 s. Games outside launcher folders with a Chromium, Electron or UWP window → missed until listed. Non-game tools under steamapps\\common or unlisted Qt/WPF apps in F11 → false game. Rewatch within P without url or position → merged; live-stream title change → split.",
    reproduce: "uv run python eval/games-and-episodes/P1-lifetime-provenance-chain/post.py eval/games-and-episodes/P1-lifetime-provenance-chain/pausesplit/candidate.py --figs"
  },

  findings: [
    { question: "Why today fragments games", claims: ["Rows per truth session: 0315 [2, 1, 7], 1818 [2, 1, 5], day [7, 6, 3, 3, 4, 4, 25] [4].", "57 splits: 38 from focus on a non-game past the 5 s grace, 9 from another game in front, 10 from a minimised game in front; 29 next rows were flickers under 2 s [4].", "Truth sessions are process lifetimes; games are in front 7-83 % of their span, so focus rows miss the end by up to 4326 s [4]."] },
    { question: "Detection on real windows", claims: ["Today: 4 false games (code F11, terminal F11, Photos, Big Picture) and 1 excluded name written on 16 cases; C1: 0/0/0 [3][1].", "QUNS reads only the primary monitor and the cursor is visible in every case, so neither separates the cases [3][5]."] },
    { question: "Process handles on the target PC", claims: ["SYNCHRONIZE | QUERY_LIMITED granted for the harness game and Outer Wilds; wait 1.0-1.2 µs, GetProcessTimes 1.9 µs; exit seen within 6-22 ms [2].", "msmpeng, lsass and csrss refuse with error 5 [2]."] },
    { question: "Video episodes", claims: ["Today: 15 media-row splits; 2 of 7 videos missed; titles right everywhere [4].", "Chains: 22 rows → 7 chains, video F1 1.00 on every run for P = 30, 180, 600 s; incremental equals one pass [1].", "Two early ends of 58.0 s and 116.6 s come from capture (private-window media rejection, picture-in-picture) [1]."] },
    { question: "Prior art", claims: ["Playnite tracks by install folder or process tree and polls HasExited every 2 s [6].", "Steam and Playnite count wall-clock process time; no product found with a lifetime span plus an in-front ledger [6].", "ActivityWatch merges heartbeats within a pulsetime; Trakt/Simkl block a re-scrobble within 1 h [7][9]."] }
  ],
  mechanisms: [
    { id: "G1", name: "Process-lifetime session", core: "Hold a SYNCHRONIZE | QUERY_LIMITED handle, end at exit.", assumes: "The handle is granted.", breaks: "Launcher stubs, hours minimised, sleep. [6][10]" },
    { id: "G2", name: "Launcher folders", core: "Exe under a launcher install folder is a game.", assumes: "Games live in launcher folders.", breaks: "Games outside them. [6][12]" },
    { id: "G3", name: "Heartbeat merge", core: "Equal keys merge within a pulsetime P.", assumes: "P separates sessions.", breaks: "P too large; restarts under one exe. [7]" },
    { id: "G4", name: "Active time over lifetime", core: "Span from G1, active_ms from foreground ticks.", assumes: "One foreground at a time.", breaks: "Overlapping spans of two games. [6]" },
    { id: "G5", name: "Progress threshold", core: "Watched at ≥ 80-90 %.", assumes: "Playback position stored.", breaks: "No position data. [8][9]" },
    { id: "G6", name: "Stop hysteresis", core: "Same item resuming within P extends the episode.", assumes: "Titles identify items.", breaks: "Rewatches, title changes." },
    { id: "G7", name: "Manifest name", core: "Appmanifest name, then ProductName, then title mode.", assumes: "Manifest or version resource exists.", breaks: "Renamed host binaries. [5][12]" },
    { id: "D1", name: "QUNS", core: "SHQueryUserNotificationState 2/3.", assumes: "One monitor.", breaks: "Primary monitor only here. [3][11]" },
    { id: "D2", name: "GameConfigStore", core: "MatchedExeFullPath list.", assumes: "Game Bar saw the game.", breaks: "Misses Outer Wilds. [5]" },
    { id: "D3", name: "Steam RunningAppID", core: "Registry value while a Steam game runs.", assumes: "Launched through Steam.", breaks: "Not set in the Outer Wilds spike. [2][5]" },
    { id: "D4", name: "Cursor and GPU share", core: "Hidden cursor, 3D engine load.", assumes: "Games hide the cursor.", breaks: "Not verdicts alone. [5]" },
    { id: "D5", name: "GamesEnumeration", core: "Windows' installed-games list.", assumes: "Capability granted.", breaks: "Restricted capability. [5]" },
    { id: "D6", name: "Window-class deny", core: "Chromium, Cascadia, WinUI, ApplicationFrame, CoreWindow, Console classes are not games.", assumes: "Games use their own window class.", breaks: "Electron or UWP games. [3]" },
    { id: "D7", name: "Package-path deny", core: "%WINDIR%, SystemApps, Microsoft.Windows* packages are not games.", assumes: "Windows apps live there.", breaks: "None seen. [3]" }
  ],
  explored: [
    { name: "C1a Pause bridged", reason: "Clipped M11.2 0.70; a Pause must split the row (answer 6)." },
    { name: "C1b Chains bridge a Pause", reason: "Passes at the limit (0.80 / 0.80); C1 reaches 1.00 / 0.86." },
    { name: "C1c C1a with P = 600 s", reason: "Identical to C1a on every metric (+21.7 µs, 68.4 ms/day, clipped M11.2 0.70)." },
    { name: "C1d Today's rule + reopen 300 s", reason: "End error 4534 s, rows max 6." },
    { name: "C2a End at last front tick", reason: "End error up to 21020.6 s." },
    { name: "C2 pulsetime sweep", reason: "P = 60, 300, 900, 3600 s: rows 2.85 / 1.85 / 1.54 / 1.46 with the liveness end; end error does not depend on P." },
    { name: "C3 alone", reason: "Fixes video only; game rows and detection stay at today's values." },
    { name: "Chromium runtime file deny", reason: "Not built; the window-class deny already gives 0 false games." },
    { name: "Start at first in-front sighting", reason: "Not built; creation time gives 0.0 s start error." },
    { name: "QUNS, cursor, GPU share as verdicts", reason: "Rejected by the real-window table and earlier research [3][5]." }
  ],
  caveats: [
    "Two scorings: today's scorer does not clamp the truth end to capture stop and counts every overlapping row; the clipped scoring reads answer 6 as truth split at a stored Pause. C1 passes only on the clipped scoring.",
    "Rows are replays: 100 ms simulated checks, exits from the observer's ~1 s process record, not a held handle. Handle calls were measured on two processes only.",
    "Two games, 13 truth game sessions, 7 truth videos; detection rests on 16 real windows.",
    "C2's clipped figures come from a rescoring run while writing this report (clipped_alive.py); its changed-parts derivation was not measured.",
    "realwin.json records under_steam_root false for Outer Wilds while the spike reads true; the verdict is unchanged because the launcher-folder check comes first.",
    "Steam RunningAppID was not set while Outer Wilds ran in the spike.",
    "The rewatch guard needs position columns not stored today.",
    "Visible artefacts: every side.png draws Outer Wilds' truth past capture stop; fig-p2-rows.png puts the must label on the 1818 lines; P3's fig-0315-v1 (not embedded) draws a ~1.6 min early end where metrics.json reports 58.0 s.",
    "WEAK: open handle preventing pid reuse (implied); Plex and Trakt thresholds (Jellyfin and Simkl only verified)."
  ],
  sources: [
    { n: 1, title: "Games-and-episodes harness and prototypes", url: "eval/games-and-episodes/", tier: 1, version: "target PC, 2026-11-20" },
    { n: 2, title: "Process-handle spike", url: "eval/games-and-episodes/P1-lifetime-provenance-chain/spike_handles.json", tier: 1, version: "2026-11-20" },
    { n: 3, title: "Real-window detection table", url: "eval/games-and-episodes/harness/realwin.json", tier: 1, version: "2026-11-20" },
    { n: 4, title: "Diagnosis of today's rows and episodes", url: "eval/games-and-episodes/diagnosis.json", tier: 1, version: "2026-11-20" },
    { n: 5, title: "Windows game detection signals (research)", url: "docs/research/windows-game-detection-signals.md", tier: 1, version: "2026-11-19" },
    { n: 6, title: "Playnite game actions and FAQ", url: "https://api.playnite.link/docs/manual/library/games/gameActions.html", tier: 1, version: "current" },
    { n: 7, title: "ActivityWatch aw-core heartbeats.py", url: "https://github.com/ActivityWatch/aw-core", tier: 1, version: "current" },
    { n: 8, title: "Jellyfin UserDataManager / ServerConfiguration", url: "https://github.com/jellyfin/jellyfin", tier: 1, version: "current" },
    { n: 9, title: "Simkl scrobble stop", url: "https://api.simkl.org/api-reference/simkl/scrobble-stop", tier: 1, version: "current" },
    { n: 10, title: "Process Security and Access Rights", url: "https://learn.microsoft.com/en-us/windows/win32/procthread/process-security-and-access-rights", tier: 1, version: "Win32" },
    { n: 11, title: "SHQueryUserNotificationState", url: "https://learn.microsoft.com/en-us/windows/win32/api/shellapi/nf-shellapi-shqueryusernotificationstate", tier: 1, version: "Win32" },
    { n: 12, title: "GameFinder", url: "https://github.com/erri120/GameFinder", tier: 2, version: "wiki" },
    { n: 13, title: "Fact-check, prior-art and refuter notes", url: "_dev/invent/games-and-episodes/checks.md", tier: 1, version: "local, 2026-11-20" },
    { n: 14, title: "Akashic game and episode code", url: "src/akashic/capture/gaming.py", tier: 1, version: "2026-11-20" }
  ]
};
