window.REPORT = {
  title: "App Time Between Frames",
  date: "2026-11-19",
  depth: "full",
  context: "Python 3.14, numpy, SQLCipher, Windows 11 26200, Ryzen 7 7800X3D; Akashic `pipeline/sessions.py`",
  problem: "Turn Akashic's sparse stored observations (frames stored only on change, per-monitor shown runs, stored no-recording gaps) into continuous per-app focused time (audit #196).",
  goals: [
    "The timeline shows which app was in front for nearly all recorded time, with no untyped holes",
    "Per-app durations match truth",
    "No app time across lock, sleep, pause, stop or away-from-keyboard"
  ],
  contract: {
    must: [
      "M5.1 holes ≤ 5 % of truth app time on standard and day",
      "M5.2 overlap ≤ today",
      "No band inside a stored no-recording gap or past the end of recording",
      "M5.3 duration error below today on every run"
    ],
    fake: ["A band's end up to one check interval late", "Away approximated where no input signal is stored", "Today's game-session handling"]
  },
  budget: ["Derivation ≤ 50 ms per day of data on one core", "Capture addition ≤ 1 ms per tick and a few KB/h"],
  constraints: ["No new dependency", "Schema may change (fresh app, no migration)"],
  nonGoals: ["Game detection itself (#188)", "Media sessions and episodes", "Timeline UI", "Visible-but-not-focused time", "Retention", "Ask quality"],

  answer: {
    text: "**C4**: today's sessions held to the next session start, the first later frame on their own monitor, the next stored gap, a game's end and the end of recording, then cut by away spans (every presence channel quiet for 180 s, back-dated): keyboard/mouse idle, the band's own process audible while its picture changes (≤ 3 h), the user's speech paired with the audible voice app, and an open game session. Holes fall from 15-39 % to 0.3-4.0 % of app time, overlap from 55.5 s to 0, duration error is below today on every run, and nine synthetic timelines count 0.5 s across away and miss 9.9 s of present time [1]. 7.5 ms per day of data, about 0.8 KB/h of away rows [1]. Back-dated away is ActivityWatch's and ManicTime's rule [2][3]."
  },
  bestFit: "C4",

  metrics: [
    { key: "ms_day", label: "Derivation", unit: "ms/day", limit: 50, budget: true },
    { key: "holes_day", label: "Holes, day run", unit: "%", limit: 5 },
    { key: "err_day", label: "M5.3, day run (today 0.481)", limit: 0.481 },
    { key: "overlap", label: "Overlap", unit: "s", limit: 55.5 },
    { key: "away_counted", label: "Synthetic away counted", unit: "s" },
    { key: "present_missed", label: "Synthetic present missed", unit: "s" }
  ],
  chart: { x: "ms_day", y: "present_missed" },

  captureColumns: ["M5.1 holes and M5.3 per run", "Synthetic timelines and ms per day"],
  figures: [
    { src: "baseline.png", title: "Today's band rule", caption: "Holes 15-39 % of app time; 3,778 s of synthetic present time missed." },
    { src: "ref-confirmed.png", title: "Hold bounded by the monitor confirmation (earlier research)", caption: "Holes under 1 %, but 2,401 s counted while away and M5.3 above today on the day run." }
  ],

  candidates: [
    {
      id: "C4", name: "Presence split by channel", status: "best",
      method: "C1's band, with away = keyboard/mouse, audible-and-changing own process, paired speech and open game session all quiet for T, back-dated.",
      pipeline: [{ label: "sessions + last-frame monitor", cost: "1 query" }, { label: "hold to earliest edge", cost: "bisect" }, { label: "channels where idle ≥ T", cost: "audio rows only there" }, { label: "cut by away and gaps", cost: "linear" }],
      uses: ["M7 Back-dated AFK", "M17 Audible sampler", "M8 Presence union", "M16 Stored gaps", "M10 XInput (described)"],
      cost: "7.5 ms/day on the runs, 30-40 ms/day with a dense idle record, against 50; ~0.8 KB/h",
      keeps: "Every must; video watched without input, voice calls, controller play in a game session",
      fakes: "Away for silent use; game sessions as today",
      failsWhen: "A playlist left playing counts up to 3 h; music or silent video is invisible to V (only speech segments are stored); jigglers; non-XInput pads outside game sessions.",
      condition: "XInput probing must run off the capture tick",
      test: "Five replayed runs + nine synthetic timelines + extra scenarios",
      priorArt: { name: "ManicTime presence settings", url: "https://www.manictime.com/releases/windows" },
      values: { ms_day: 7.52, holes_day: 4.01, err_day: 0.426, overlap: 0, away_counted: 0.5, present_missed: 9.9 },
      capture: "C4.png",
      metricsLine: "holes_pct_smoke=0.62/0.31 holes_pct_std=1.12/0.49 holes_pct_day=4.01 overlap_max_s=0 err_smoke=0.241/0.323 err_std=0.315/0.08 err_day=0.426 past_end_s=0 syn_away_counted_s=0.5 syn_present_missed_s=9.9 ms_per_day=7.52"
    },
    {
      id: "C1", name: "Three-layer priority partition", status: "works",
      statusNote: "Keeps every must; cuts a video or controller play without input as away.",
      method: "Gap > away > app: today's sessions held to the earliest edge, cut by stored gaps and back-dated keyboard/mouse away.",
      pipeline: [{ label: "sessions + last-frame monitor", cost: "1 query" }, { label: "hold to earliest edge", cost: "bisect" }, { label: "cut by away and gaps", cost: "linear" }],
      uses: ["M3 Monitor-confirmation hold", "M16 Stored gaps", "M7 Back-dated AFK", "M12 WTS lock rows"],
      cost: "8.9 ms/day against 50; ~0.8 KB/h",
      failsWhen: "Video and controller play without input lose 1,800 s each in the synthetic timelines.",
      priorArt: { name: "ActivityWatch AFK back-dating", url: "https://github.com/ActivityWatch/aw-watcher-afk" },
      values: { ms_day: 8.89, holes_day: 4.01, err_day: 0.426, overlap: 0, away_counted: 0.5, present_missed: 3609.6 },
      capture: "C1.png",
      metricsLine: "holes_pct_smoke=0.81/0.31 holes_pct_std=1.21/0.55 holes_pct_day=4.01 overlap_max_s=0 err_smoke=0.234/0.323 err_std=0.305/0.079 err_day=0.426 past_end_s=0 syn_away_counted_s=0.5 syn_present_missed_s=3609.6 ms_per_day=8.89"
    },
    {
      id: "C3", name: "Foreground switch rows as exact edges", status: "works",
      statusNote: "Upper bound: switch rows simulated from the scorer's own truth source. Cuts video and controller play like C1.",
      method: "Band edges from stored switch rows (the existing 0.1 s foreground poll) united with process-changing session starts; identity from sessions; C1's away.",
      pipeline: [{ label: "switch rows + sessions", cost: "3 small fetches" }, { label: "edge merge + span check", cost: "≤ 37 lookups/day" }, { label: "cut by away and gaps", cost: "linear" }],
      uses: ["M4 Foreground switch rows", "M6 Sample-and-hold", "M3 Monitor-confirmation hold", "M16 Stored gaps", "M7 Back-dated AFK"],
      cost: "10.9-12.9 ms/day against 50; ~2.4-3 KB/h of switch rows",
      failsWhen: "Without the session-start fallback, 10 % dropped rows push holes to 9.7 %.",
      priorArt: { name: "Android UsageStats event log (analogue)", url: "https://developer.android.com/reference/android/app/usage/UsageEvents.Event" },
      values: { ms_day: 12.85, holes_day: 0.02, err_day: 0.123, overlap: 0, away_counted: 0.5, present_missed: 3602.9 },
      capture: "C3.png",
      metricsLine: "holes_pct_smoke=0.04/0.01 holes_pct_std=0.18/0.01 holes_pct_day=0.02 overlap_max_s=0 err_smoke=0.211/0.206 err_std=0.063/0.053 err_day=0.123 past_end_s=0 syn_away_counted_s=0.5 syn_present_missed_s=3602.9 ms_per_day=12.85"
    },
    {
      id: "C2", name: "C1 + closing with a same-process audio veto", status: "unbuilt",
      statusNote: "Subsumed by C4; its veto is fooled by a background tab of the focused browser [5].",
      method: "Presence = closing of input runs ∪ audible runs of the band's process, capped at 3 h.",
      pipeline: [{ label: "C1", cost: "~9 ms/day" }, { label: "closing + audible union", cost: "~10-20 ms/day" }],
      uses: ["M8 Presence union", "M17 Audible sampler"],
      cost: "~18-28 ms/day (estimate)",
      values: { ms_day: 23 }, estimated: true,
      priorArt: { name: "ActivityWatch audible tabs", url: "https://github.com/ActivityWatch/aw-webui" }
    },
    {
      id: "C5", name: "Display-request back-dating", status: "refuted",
      statusNote: "The aggregate ES_DISPLAY_REQUIRED bit stayed set in all 24 samples on the target PC, so away never fires [5].",
      method: "Away start = max(last input, last display request).",
      pipeline: [{ label: "CallNtPowerInformation per tick", cost: "6.2 µs" }],
      uses: ["M9 Display-request threshold", "M11 Session presence"],
      cost: "~28 ms/day (estimate)",
      values: { ms_day: 28 }, estimated: true,
      priorArt: { name: "Timing 3 h rule", url: "https://timingapp.com/help/faq" }
    },
    {
      id: "C6", name: "Per-app Otsu idle threshold", status: "refuted",
      statusNote: "Unmeasurable gain, 20 min clamp counts away time, C2 with 300 s for 30 days [5].",
      method: "Per-app threshold from the Otsu split of 30 days of input gaps.",
      pipeline: [{ label: "C2", cost: "~23 ms/day" }, { label: "Otsu per app", cost: "< 1 ms" }],
      uses: ["M7 Back-dated AFK"],
      cost: "~25 ms/day (estimate)",
      values: { ms_day: 25 }, estimated: true,
      priorArt: { name: "Per-user log-gap split (El Kholy et al. 2017)", url: "https://microsoft.com/en-us/research/wp-content/uploads/2017/04/WWW2017.pdf" }
    }
  ],

  method: {
    summary: "C4: gaps over away over app. Bands come from today's sessions held to the earliest stored edge; away comes from presence channels that are all quiet for T, back-dated.",
    equation: "end_i = min(start_{i+1}, first later frame on monitor(i), next gap, game end, rec_end)\naway = ⋂_c quiet_c over ≥ T,  start = max_c last_c(t)\nchannels: K keyboard/mouse, V own process audible ∧ frame on its monitor ≤ 15 s (≤ 3 h past K/S),\n          S own speech paired with the audible voice app, G open game session\nband = [start_i, end_i) − gaps − away",
    steps: [
      { text: "Capture stores a back-dated away row once keyboard/mouse idle reaches 180 s (signed tick difference clamped at 0).", assumes: "GetLastInputInfo moves on real keyboard and mouse use." },
      { text: "Hold each session to the earliest of the next start, the next frame on its own monitor, the next gap, its game end and the end of recording.", assumes: "A focus change stores a frame outside game mode." },
      { text: "Cancel away while the band's own process is audible and its picture changes (≤ 3 h), while paired speech is stored, or while its game session is open.", assumes: "Audio from the focused, changing app means the user is watching or listening." },
      { text: "Cut bands by away and gaps; away is a typed span." }
    ],
    cost: "7.5 ms per day on the replayed runs; 30.2-39.6 ms per day with 28-103 dense idle stretches in 6.9 h; capture ~0.36 µs per tick and ~0.8 KB/h.",
    failsWhen: "Playlist left playing → up to 3 h counted; music or silent video → not seen by V (speech segments only); jiggler → never away; non-XInput pad outside a game session → cut.",
    reproduce: "uv run python eval/app-time-between-frames/harness/run.py eval/app-time-between-frames/C4/c4.py"
  },

  findings: [
    { question: "What Akashic stores today", claims: ["A session ends at its last stored frame [9]; each monitor keeps one shown run for hours, so its last-checked time bounds nothing [5].", "A focus move to an excluded, shell or null window stores a frame without a focused row; holding past it ran 912.6 s into other apps on the day run [5]."] },
    { question: "Trackers", claims: ["ActivityWatch: AFK after 180 s back-dated to the last input, window time intersected with not-AFK [2].", "ManicTime: 10 min away applied retroactively; sound/mic presence replaced in 2025.2 [3]. Timing: 3 h when the frontmost app keeps the screen awake [7]."] },
    { question: "Windows signals", claims: ["GetLastInputInfo is per session and not guaranteed incremental [12]; controllers through Steam Input may not move it [13].", "EVENT_SYSTEM_FOREGROUND is not sent on every switch [5]."] }
  ],
  mechanisms: [
    { id: "M1", name: "Today's band rule", core: "Session ends at its last frame.", assumes: "Frames keep arriving.", breaks: "Static screens. [9]" },
    { id: "M2", name: "Fixed-cap hold", core: "Hold to next session, capped.", assumes: "Static stretches are short.", breaks: "Long reads. [4]" },
    { id: "M3", name: "Monitor-confirmation hold", core: "Bound by last unchanged check.", assumes: "Shown runs end on app change.", breaks: "One run per monitor for hours. [5]" },
    { id: "M4", name: "Foreground switch rows", core: "Store each switch.", assumes: "Every switch seen.", breaks: "Missed events, unreadable processes. [5]" },
    { id: "M6", name: "Sample-and-hold", core: "Exact starts, clipped end.", assumes: "No dropped switch.", breaks: "Dropped switch over-extends." },
    { id: "M7", name: "Back-dated AFK", core: "Away once idle ≥ T, from last input.", assumes: "Input means presence.", breaks: "Controllers, jigglers. [2][12]" },
    { id: "M8", name: "Presence union", core: "Not-AFK ∪ audible ∪ always-active.", assumes: "Audio means a listener.", breaks: "Music after leaving. [2][3]" },
    { id: "M9", name: "Display-request threshold", core: "Longer T while the screen is kept awake.", assumes: "Requests mean use.", breaks: "Bit stuck set on the target PC. [5][7]" },
    { id: "M10", name: "XInput heartbeat", core: "dwPacketNumber change = pad active.", assumes: "XInput pads.", breaks: "Other pads; empty-slot probe costs ms. [13]" },
    { id: "M11", name: "Session presence events", core: "Power-setting notifications.", assumes: "Display timeout ≈ away.", breaks: "Coarse, not back-datable. [14]" },
    { id: "M12", name: "WTS lock rows", core: "Exact lock edges.", assumes: "Message window.", breaks: "≤ 1 s gain." },
    { id: "M16", name: "Stored gaps", core: "Typed no-recording spans.", assumes: "Gaps are stored.", breaks: "None found. [9]" },
    { id: "M17", name: "Audible sampler", core: "Audible processes per endpoint, 1 s.", assumes: "Kept in memory 5 min.", breaks: "Not durable. [10]" }
  ],
  explored: [
    { name: "Hold with a fixed 60 s cap", reason: "Day holes 5.6 % and M5.3 above today on two runs." },
    { name: "Monitor-confirmation hold", reason: "M5.3 above today on the day run; 2,401 s counted while away." },
    { name: "C2 same-process audio veto", reason: "Fooled by a background tab of the focused browser; replaced by C4's picture condition." },
    { name: "C5 display-request back-dating", reason: "Aggregate bit always set on the target PC." },
    { name: "C6 per-app Otsu threshold", reason: "Unmeasurable gain, counts away time, adds state." },
    { name: "C3 without the session-start fallback", reason: "10 % dropped rows → holes 9.7 %." }
  ],
  caveats: [
    "The replayed runs contain no idle, lock, sleep, away or long static screen; those are only in nine synthetic timelines with a simplified capture model.",
    "The real runs have no idle record and are treated as always active (the harness driver gives no input between focus steps).",
    "The scorer's truth is presence-blind and its M5.3 compares each truth run with the longest same-app band unclipped; a clipped variant is reported beside it.",
    "C3's switch rows come from the same log as the scorer's truth: its numbers are an upper bound.",
    "C4's V channel reads stored speech segments only; its G channel counts an idle game until the session ends.",
    "T = 180 s and 300 s gave identical results: no scenario has a 3-5 min break."
  ],
  sources: [
    { n: 1, title: "App-time replay harness and candidates", url: "eval/app-time-between-frames/", tier: 1, version: "2026-11-19" },
    { n: 2, title: "ActivityWatch aw-watcher-afk", url: "https://github.com/ActivityWatch/aw-watcher-afk", tier: 1, version: "default branch" },
    { n: 3, title: "ManicTime release notes", url: "https://www.manictime.com/releases/windows", tier: 1, version: "2025.2" },
    { n: 4, title: "App usage time from sparse samples (research)", url: "docs/research/app-usage-time-from-samples.md", tier: 1, version: "2026-11-19" },
    { n: 5, title: "Refuter analyses and spikes", url: "eval/app-time-between-frames/refuter/", tier: 1, version: "2026-11-19" },
    { n: 6, title: "Android UsageEvents.Event", url: "https://developer.android.com/reference/android/app/usage/UsageEvents.Event", tier: 1, version: "2026-11-19" },
    { n: 7, title: "Timing FAQ", url: "https://timingapp.com/help/faq", tier: 1, version: "2026-11-19" },
    { n: 8, title: "El Kholy et al., WWW'17 Companion", url: "https://microsoft.com/en-us/research/wp-content/uploads/2017/04/WWW2017.pdf", tier: 1, version: "2017" },
    { n: 9, title: "Akashic sessions, gaps and monitors", url: "src/akashic/pipeline/sessions.py", tier: 1, version: "2026-11-19" },
    { n: 10, title: "Akashic hook, poll and audio sampler", url: "src/akashic/platform/win/events.py", tier: 1, version: "2026-11-19" },
    { n: 11, title: "WakaTime FAQ", url: "https://wakatime.com/faq", tier: 1, version: "2026-11-19" },
    { n: 12, title: "GetLastInputInfo", url: "https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-getlastinputinfo", tier: 1, version: "2025-07-01" },
    { n: 13, title: "Getting started with XInput", url: "https://learn.microsoft.com/en-us/windows/win32/xinput/getting-started-with-xinput", tier: 1, version: "2026-11-19" },
    { n: 14, title: "Power setting GUIDs", url: "https://learn.microsoft.com/en-us/windows/win32/power/power-setting-guids", tier: 1, version: "2026-11-19" },
    { n: 15, title: "Brinberg et al., Screenomics, 2020", url: "https://pmc.ncbi.nlm.nih.gov/articles/PMC7543997/", tier: 1, version: "2020" }
  ]
};
