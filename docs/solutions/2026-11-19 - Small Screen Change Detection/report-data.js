window.REPORT = {
  title: "Small Screen Change Detection",
  date: "2026-11-19",
  depth: "full",
  context: "Python 3.14, numpy 2.5.4, Pillow 13.0.0, Windows 11 26200, Ryzen 7 7800X3D; Akashic `capture/imaging.py`",
  problem: "Decide when a captured screen frame changed enough to store, so a one-word chat reply, a notification badge or a short line of small text is kept while a blinking caret, a cursor and a seconds clock are not.",
  goals: [
    "Keep every meaningful small change (a one-word reply at 13-16 px, a 16 px badge, a short line of small text) on 4K and 1440p",
    "Store no more useless frames than today (caret blink, seconds clock, spinner)",
    "The OCR gate uses the same test, so a kept frame's text is re-read"
  ],
  contract: {
    must: [
      "Catch a one-word reply and a badge in every sub-pixel placement at 1440p and 4K, both themes",
      "Never fire on a 1-2 px caret blink",
      "Harness M1.1 focused recall at or above today; M1.2 useless ratio at most today + 0.05"
    ],
    fake: ["Text-shaped timers count as changes", "Fonts under 9 device px", "Today's game-mode behaviour"]
  },
  budget: ["≤ 3 ms extra CPU per 4K compare on one core", "A few MB extra memory per window (taken as ≤ 4 MB)"],
  constraints: ["numpy and Pillow only", "No GPU model per frame", "Production code unchanged during the audit"],
  nonGoals: ["Game-mode capture intervals", "OCR accuracy", "Cursor capture", "Video and animation handling beyond today", "Retention or frame thinning", "Windows builds without the APIs in use"],

  answer: {
    text: "**C5**: today's test OR exact clusters of the few moved pixels (≥ 3 wide, ≥ 2 high), held back where the previous checks show a ticker and capped for exact A-B-A toggles. It misses 0 of 442,368 word placements and 0 badges, never fires on a 1-2 px bar caret, stores a seconds clock 7 times an hour, a spinner 0 and a block caret 8, and costs +0.03 ms (compare 0.65-0.79 ms against today's 1.23-1.32 ms) and 2.6 MB per 4K monitor [1]. On 10 minutes of the real desktop it added the fewest stores of the built candidates (+5 primary, +9 right, unlabelled) [2]. No prior art found; nearest: Stauffer-Grimson multi-modal models [3]."
  },
  bestFit: "C5",

  metrics: [
    { key: "extra_ms", label: "Extra compare", unit: "ms", limit: 3, budget: true },
    { key: "mem_mb", label: "Memory per 4K monitor", unit: "MB", limit: 4, budget: true },
    { key: "miss_words", label: "Word placements missed", limit: 0 },
    { key: "fire_bar", label: "Bar-caret fires", limit: 0 },
    { key: "superset", label: "Superset violations", limit: 0 },
    { key: "seq_nuisance", label: "Spinner + block + minute clock stores", unit: "/h", limit: 12 },
    { key: "live_extra", label: "Real desktop extra stores, primary", unit: "/10 min" }
  ],
  chart: { x: "extra_ms", y: "seq_nuisance" },

  reference: { src: "ref.png", label: "Synthetic 4K pair: a one-word reply and a caret" },
  captureColumns: ["Misses per positive", "Fires per negative", "4K timing", "Sequences, stores per hour"],
  figures: [
    { src: "baseline.png", title: "Today's test on the same harness", caption: "Misses every one-word reply and badge in every placement; fires on nothing small." }
  ],

  candidates: [
    {
      id: "C5", name: "C2 + live/toggle split on the previous check", status: "best",
      method: "Today's dense test; below it, exact clusters of moved points; a cluster stores only if its non-live points are shape-positive; exact A-B-A toggles stored at most 7 times per episode; taskbar band keeps today's behaviour.",
      pipeline: [{ label: "uint8 mask + count", cost: "0.43 ms" }, { label: "flatnonzero + clusters", cost: "≤ 0.5 ms" }, { label: "live + toggle rules", cost: "≤ 0.25 ms" }],
      uses: ["M1 Global changed fraction", "M13 Sparse fast path", "M6 Minimum-shape components", "M9 Two references", "M8 Persistence", "M7 Toggle state"],
      cost: "+0.03 ms worst against +3 ms; 0.52 MB previous gray + 2.07 MB recency map at 4K",
      keeps: "Every word and badge placement, bar carets never, superset of today",
      fakes: "The time of A-B-A reverts; a change inside a ticking region waits one interval",
      failsWhen: "A draft that never holds still is stored late or partially (9 of 70 complete); a 2 px underscore caret fires in 1/4 of 4K placements; a block caret stores up to 8 frames per blink episode; taskbar badges stay missed as today.",
      condition: "Real-desktop extra stores are unlabelled; M1.1 and M1.2 not rerun in the quality harness",
      test: "Synthetic sweep 1,458,176 pairs + one-hour sequences + 10 min real desktop",
      priorArt: { name: "Stauffer-Grimson multi-modal background models", url: "https://www.cse.buffalo.edu/courses/cse725/peter/Stauffer_1999.pdf" },
      values: { extra_ms: 0.027, mem_mb: 2.59, miss_words: 0, fire_bar: 0, superset: 0, seq_nuisance: 8, live_extra: 5 },
      capture: "C5.png",
      metricsLine: "miss_words=0/442368 miss_words_d64=0/442368 miss_words_d40=5120/442368 miss_badge=0/16384 fire_caret=2048/49152 fire_lines=8192/32768 fire_block=16384/16384 superset_viol=0 extra_ms_worst=+0.027 seq_clock_stores_h=7 seq_spinner_stores_h=0 seq_block_stores_h=8 seq_minclock_stores_h=0 seq_typing_stores_h=118 seq_replies_missed=0/50 seq_badge_missed=0/60 seq_draft_missed=0/59"
    },
    {
      id: "C2", name: "Gated exact sparse clusters", status: "works",
      statusNote: "Keeps the musts; a visible spinner or block caret stores at every check.",
      method: "Today's dense test; below it, exact clusters of moved points (Chebyshev link ≤ 2) firing at ≥ 3 wide, ≥ 2 high, ≥ 5 points, with a low-delta re-test and the taskbar band.",
      pipeline: [{ label: "uint8 mask + count", cost: "0.43 ms" }, { label: "flatnonzero + clusters", cost: "≤ 0.5 ms" }],
      uses: ["M1 Global changed fraction", "M13 Sparse fast path", "M6 Minimum-shape components", "M14 Reduction dilutes thin marks"],
      cost: "compare 0.78 ms against today's 2.67 ms in the same run; memory +0",
      failsWhen: "Spinner 359/h, block caret 351/h, seconds clock 719/h; 2 px underscore fires in 1/2 of 4K placements.",
      priorArt: { name: "Stauffer-Grimson connected components", url: "https://www.cse.buffalo.edu/courses/cse725/peter/Stauffer_1999.pdf" },
      values: { extra_ms: 0, mem_mb: 0, miss_words: 0, fire_bar: 0, superset: 0, seq_nuisance: 710, live_extra: 8 },
      capture: "C2.png",
      metricsLine: "miss_words=0/442368 miss_words_d64=0/442368 miss_words_d40=181248/442368 miss_badge=0/16384 fire_caret=6144/49152 fire_lines=8192/32768 fire_block=16384/16384 superset_viol=0 extra_ms_worst=-0.000 seq_clock_stores_h=719 seq_spinner_stores_h=359 seq_block_stores_h=351 seq_minclock_stores_h=0 seq_typing_stores_h=245"
    },
    {
      id: "C3", name: "Anisotropic 4x2 reduction + gated span", status: "works",
      statusNote: "Keeps the musts with 0.1-0.2 ms of median budget margin; spinners and block carets store at every check.",
      method: "Signature from Pillow reduce((4,2)); 16x32 tiles on four grids fire at cols ≥ 3, rows ≥ 3, count ≥ 4 below today's threshold; taskbar band.",
      pipeline: [{ label: "reduce((4,2)) + blake2b", cost: "+2.3 ms" }, { label: "uint8 mask + tiles", cost: "0.8-1.1 ms" }],
      uses: ["M14 Reduction dilutes thin marks", "M3 Per-tile span", "M13 Sparse fast path", "M1 Global changed fraction"],
      cost: "+2.05 / +2.79 / +2.92 ms (min / median / max of 7) against +3 ms; +0.52 MB per 4K window",
      failsWhen: "Spinner and seconds clock 719/h, block caret 351/h; underlines ≥ 4 device px tall fire.",
      priorArt: { name: "RLSA (anisotropic smearing)", url: "https://crblpocr.blogspot.com/2007/06/run-length-smoothing-algorithm-rlsa.html" },
      values: { extra_ms: 2.79, mem_mb: 0.52, miss_words: 0, fire_bar: 0, superset: 0, seq_nuisance: 1070, live_extra: 9 },
      capture: "C3.png",
      metricsLine: "miss_words=0/442368 miss_words_d64=0/442368 miss_words_d40=220672/442368 miss_badge=0/16384 fire_caret=0/49152 fire_lines=0/32768 fire_block=15360/16384 superset_viol=0 extra_ms_worst=+2.569 seq_clock_stores_h=719 seq_spinner_stores_h=719 seq_block_stores_h=351 seq_minclock_stores_h=0 seq_typing_stores_h=245"
    },
    {
      id: "C1", name: "Gated sparse tile shape test", status: "refuted",
      statusNote: "Misses x-height words (\"rn\", \"re\", \"ça\") in 2-8 of 16 placements; a lower width floor fires a 2 px underscore [6].",
      method: "Today's test OR a four-grid 16x16 tile shape test on the moved coordinates.",
      pipeline: [{ label: "uint8 mask + count", cost: "0.43 ms" }, { label: "4 grids on ≤ 52 points", cost: "~0.1 ms" }],
      uses: ["M1 Global changed fraction", "M13 Sparse fast path", "M3 Per-tile span", "M6 Minimum-shape components"],
      cost: "~1.0-1.4 ms compare (estimate)",
      values: { extra_ms: 0, mem_mb: 0 }, estimated: true,
      priorArt: { name: "x11vnc tile heuristics", url: "https://www.mankier.com/1/x11vnc" }
    },
    {
      id: "C4", name: "C1 + per-tile toggle set", status: "refuted",
      statusNote: "A blinking cursor and a badge flap give the same hash sequence, so its two promises contradict [6].",
      method: "C1 plus the last 3 CRCs per changed tile; suppress tiles that toggle twice in 10 checks.",
      pipeline: [{ label: "C1", cost: "~1.2 ms" }, { label: "CRC + set lookup", cost: "~0.03 ms" }],
      uses: ["M7 Toggle state", "M9 Two references"],
      cost: "~+0.03 ms over C1 (estimate)",
      values: { extra_ms: 0.03, mem_mb: 0.1 }, estimated: true,
      priorArt: { name: "RDP persistent bitmap cache", url: "https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-rdpbcgr/2da3e165-d1ba-4b65-8909-7a0f7f858d69" }
    },
    {
      id: "C6", name: "C1 + volatility cells", status: "refuted",
      statusNote: "Inherits C1's misses; a 50 % blinker resets the run at every digest-equal check [6].",
      method: "C1 plus per-cell run counters and minute-rollover flags that suppress cells changing at every check.",
      pipeline: [{ label: "C1", cost: "~1.2 ms" }, { label: "cell runs + reference patch", cost: "≤ 0.5 ms" }],
      uses: ["M7 Toggle state"],
      cost: "~+0.8 ms worst (estimate)",
      values: { extra_ms: 0.8, mem_mb: 0.01 }, estimated: true,
      priorArt: { name: "Percy ignore regions (manual)", url: "https://www.browserstack.com/docs/percy/set-regions/overview" }
    }
  ],

  method: {
    summary: "C5: a superset of today's test whose sparse regime keeps word-shaped clusters, minus regions that ticked at the previous checks and exact toggles past a small budget.",
    equation: "m = (max(cur, ref) − min(cur, ref)) > 16   (uint8)\nstore if count(m) > 1e-4·N   (today)\nelse: points = flatnonzero(|cur − ref| > 6)[:128]; clusters = components(Chebyshev ≤ 2)\nshape(c) = (w ≥ 3 ∧ h ≥ 3) ∨ (w ≥ 4 ∧ h ≥ 2), n ≥ 5\nstore if ∃c: shape(non-live points of c) ∧ ¬(toggle(c) ∧ toggles(c) ≥ 7) ∧ c ⊄ taskbar band",
    steps: [
      { text: "Equal digests → no change; dense regime fires exactly as today.", assumes: null },
      { text: "Sparse regime: exact clusters of the ≤ 51 moved points, shape test.", assumes: "A bar caret ≤ 5 device px covers ≤ 2 reduced columns; x-height words are 2 reduced rows." },
      { text: "Drop points near pixels that moved at either of the previous 2 checks (live).", assumes: "Meaningful text holds still for one check interval." },
      { text: "Store exact A-B-A toggles at a box at most 7 times per episode.", assumes: "Blinks revisit exact states; 7 fits the harness badge flaps." },
      { text: "Keep today's behaviour in the bottom 48 CSS px taskbar band.", assumes: "The taskbar sits at the bottom of each monitor (production reads the work area)." }
    ],
    cost: "Mask and count 0.43 ms against today's 1.40 ms int16 path; flatnonzero 0.026 ms; clusters ≤ 0.5 ms; rules ≤ 0.25 ms. Measured worst extra +0.03 ms.",
    failsWhen: "Drafts that never hold still → late or partial stores; 2 px underscore caret → fires in 1/4 of 4K placements; block caret → up to 8 frames per episode; faint tints (> 6 levels) → unmeasured.",
    reproduce: "uv run python eval/small-screen-change-detection/harness/run.py eval/small-screen-change-detection/C5/c5.py <out>"
  },

  findings: [
    { question: "Screen recorders and remote desktop", claims: ["TigerVNC, FreeRDP and WebRTC compare tiles exactly with no thresholds or history; x11vnc ignores ≤ 4 changed tiles as cursor activity [4][10].", "No memory app documents blink or ticker suppression [5][11]."] },
    { question: "Adjacent fields", claims: ["pixelmatch's windowSize gives the densest NxN diff count; its anti-aliasing test does not remove a caret [12].", "mpdecimate decides on 8x8 block SAD thresholds [13].", "Stauffer-Grimson drop 1-2 pixel components and keep several modes per pixel, naming monitor flicker [3]."] },
    { question: "Windows caret signals", claims: ["GetGUIThreadInfo sees only system carets; Chromium and Firefox expose a caret only with accessibility on, so no OS signal covers every app [14][15]."] }
  ],
  mechanisms: [
    { id: "M1", name: "Global changed fraction", core: "> 1e-4 of the 1/4 gray frame moved > 16.", assumes: "Meaningful changes are large.", breaks: "One word. [16]" },
    { id: "M2", name: "Exact tile compare", core: "memcmp per tile.", assumes: "Every pixel matters.", breaks: "Carets and clocks. [10]" },
    { id: "M3", name: "Per-tile span", core: "≥ 3x3 span in a tile, four grids.", assumes: "Words ≥ 3x3 reduced.", breaks: "x-height words. [6]" },
    { id: "M4", name: "Densest-window count", core: "Max NxN diff count.", assumes: "Density separates meaning.", breaks: "Caret and word counts overlap. [12]" },
    { id: "M5", name: "Block SAD thresholds", core: "8x8 SAD hi/lo/frac.", assumes: "Magnitude marks meaning.", breaks: "High-contrast carets. [13]" },
    { id: "M6", name: "Minimum-shape components", core: "Drop thin or tiny components.", assumes: "Noise is thin.", breaks: "Wide nuisances. [3]" },
    { id: "M7", name: "Toggle state", core: "Recent exact contents per region.", assumes: "Blinks revisit states.", breaks: "Drift, many-state clocks. [3]" },
    { id: "M8", name: "Persistence", core: "Store once a change holds still.", assumes: "Meaning lasts one check.", breaks: "Fast toasts." },
    { id: "M9", name: "Two references", core: "Stored frame and previous check.", assumes: "Blinks return to earlier states.", breaks: "Costs one gray frame." },
    { id: "M10", name: "Caret rectangle hint", core: "OS caret rect as a veto.", assumes: "App exposes its caret.", breaks: "Self-drawn carets. [14]" },
    { id: "M11", name: "Gradient-density text cells", core: "Edge density per cell.", assumes: "Text = dense gradients.", breaks: "Smoothed small text. [16]" },
    { id: "M12", name: "Anti-aliasing rejection", core: "Drop AA pixels.", assumes: "Sub-pixel re-rendering.", breaks: "Carets. [12]" },
    { id: "M13", name: "Sparse fast path", core: "Work on the moved coordinates only.", assumes: "Few moved pixels.", breaks: "Large changes (dense regime)." },
    { id: "M14", name: "Reduction dilutes thin marks", core: "4x box average + threshold.", assumes: "Factor sets the caret/word gap.", breaks: "x-height text at 4x vertical." }
  ],
  explored: [
    { name: "C1 Gated sparse tile shape test", reason: "x-height words missed in 2-8 of 16 placements; fixing it fires a 2 px underscore." },
    { name: "C4 Per-tile toggle set", reason: "Cursor blink and badge flap are the same sequence." },
    { name: "C6 Volatility cells", reason: "Inherits C1's misses; a 50 % blinker is never suppressed." },
    { name: "C5 with width floor 5 at 4K", reason: "Removes underscore fires but misses x-height words in 64 of 27,648 quick pairs." },
    { name: "OCR routing add-on", reason: "Complementary to every candidate; not measured." },
    { name: "WGC dirty regions", reason: "Report the app's redraw, not the changed pixels (earlier spike)." }
  ],
  caveats: [
    "Placement numbers come from Pillow grayscale Segoe UI text on a plain background: no ClearType, no hinting, never over existing text.",
    "The 10-minute real-desktop replay has no labels and checks every monitor at 5 s (Akashic checks side monitors at 15 s); its extra stores are an upper bound on added useless frames.",
    "The quality harness's M1.1 and M1.2 were not rerun with C5: production code stays unchanged during the audit.",
    "A 2 px underscore caret is treated as a failure condition, not as the must's 1-2 px caret.",
    "C3's budget margin (0.1-0.2 ms at the median) is load-sensitive.",
    "The earlier four-grid sweep and sparse-path timings were WEAK and re-measured here; the Stauffer-Grimson text was read through pdftotext by one researcher only."
  ],
  sources: [
    { n: 1, title: "Synthetic harness and candidates (local)", url: "_dev/invent/screen-change-small-text/", tier: 1, version: "2026-11-19" },
    { n: 2, title: "Real-desktop replay, counts only (local)", url: "_dev/invent/screen-change-small-text/harness/live_replay.py", tier: 1, version: "2026-11-19" },
    { n: 3, title: "Stauffer and Grimson, Adaptive background mixture models, CVPR 1999", url: "https://www.cse.buffalo.edu/courses/cse725/peter/Stauffer_1999.pdf", tier: 1, version: "1999" },
    { n: 4, title: "x11vnc man page and scan.c", url: "https://www.mankier.com/1/x11vnc", tier: 1, version: "master" },
    { n: 5, title: "Screen change detection for small text (research report)", url: "docs/research/screen-change-detection-small-text.md", tier: 1, version: "2026-11-19" },
    { n: 6, title: "Refuter analyses (local)", url: "_dev/invent/screen-change-small-text/refuter/", tier: 1, version: "2026-11-19" },
    { n: 7, title: "RLSA, secondary description", url: "https://crblpocr.blogspot.com/2007/06/run-length-smoothing-algorithm-rlsa.html", tier: 3, version: "2007" },
    { n: 8, title: "MS-RDPBCGR Persistent Bitmap Caches", url: "https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-rdpbcgr/2da3e165-d1ba-4b65-8909-7a0f7f858d69", tier: 1, version: "2026-11-19" },
    { n: 9, title: "Percy regions", url: "https://www.browserstack.com/docs/percy/set-regions/overview", tier: 2, version: "2026-11-19" },
    { n: 10, title: "TigerVNC ComparingUpdateTracker", url: "https://raw.githubusercontent.com/TigerVNC/tigervnc/master/common/rfb/ComparingUpdateTracker.cxx", tier: 1, version: "master" },
    { n: 11, title: "Prior-art search (Recall, Rewind, ManicTime undocumented)", url: "docs/solutions/2026-11-19 - Small Screen Change Detection/report.md", tier: 2, version: "2026-11-19" },
    { n: 12, title: "pixelmatch", url: "https://github.com/mapbox/pixelmatch", tier: 1, version: "main" },
    { n: 13, title: "FFmpeg vf_mpdecimate.c", url: "https://raw.githubusercontent.com/FFmpeg/FFmpeg/master/libavfilter/vf_mpdecimate.c", tier: 1, version: "master" },
    { n: 14, title: "GUITHREADINFO", url: "https://learn.microsoft.com/en-us/windows/win32/api/winuser/ns-winuser-guithreadinfo", tier: 1, version: "2024" },
    { n: 15, title: "Chromium AXSystemCaretWin (M66)", url: "https://chromium.googlesource.com/chromium/src.git/+/66.0.3359.53/ui/accessibility/platform/ax_system_caret_win.cc", tier: 1, version: "M66" },
    { n: 16, title: "Akashic imaging.py", url: "src/akashic/capture/imaging.py", tier: 1, version: "2026-11-19" },
    { n: 17, title: "Small-change spike", url: "eval/quality/spikes/change_detection_small.py", tier: 1, version: "2026-11-19" }
  ]
};
