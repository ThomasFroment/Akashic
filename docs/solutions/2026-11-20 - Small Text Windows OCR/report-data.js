window.REPORT = {
  title: "Small Text Windows OCR",
  date: "2026-11-20",
  depth: "full",
  context: "Python 3.14, Pillow 13, numpy, winrt Windows.Media.Ocr, Windows 11 26200, Ryzen 7 7800X3D",
  problem: "Akashic reads small UI text and 1440p-monitor text badly with Windows OCR (audit #190).",
  goals: ["Cut screen-text error for small fonts and on 1440p monitors", "Explain the residual 1440p error at large sizes", "Keep OCR CPU and latency within budget"],
  contract: {
    must: ["M3.1 CER at ≤ 11 device px ≤ 0.05 on the standard and day runs", "1440p CER ≤ 0.10", "4K and UIA CER no worse; M3.3 no lower"],
    fake: ["Text under 8 px unreadable", "Layout as today", "Game-mode cadence"]
  },
  budget: ["≤ 2x today's OCR CPU per hour", "≤ +50 ms per OCR call at p95", "Lossless crop storage ≤ +20 % of media bytes"],
  constraints: ["Windows.Media.Ocr only", "Pillow/numpy only, no new dependency"],
  nonGoals: ["Language detection", "UI Automation text", "Layout and reading order", "Search ranking", "Retention", "Change detection", "Game-HUD text"],

  answer: {
    text: "**C2** on the live path (raw window pixels): one OCR call on a mosaic of line strips, enlarging only strips under 16 device px and rebuilding ClearType strips from their red, green and blue subpixels instead of interpolating. Raw ClearType Edge text at ≤ 11 px, 1440p: 0.602 → 0.006 (3x Lanczos 0.072), at 1.55x CPU and +20 ms p95 [1]. Not shown on the runs: stored frames are 4:2:0 WebP, which erases the subpixel phases, so the replays fall back to luma (≤ 11 px 0.40-0.49) [1]. The suspected codec cause is not real; the scorer's single-line matching misreads wrapped lines [1][2]. No prior art found for the subpixel rebuild.",
    relaxed: "If the per-call limit relaxed to about +250-700 ms on dense small-text windows, C1 forced to 3x meets the ≤ 11 px must on both replays (0.040 / 0.045) [1]."
  },
  bestFit: "C2",

  metrics: [
    { key: "dms", label: "p95 added per call", unit: "ms", limit: 50, budget: true },
    { key: "cpu", label: "CPU per call", unit: "x", limit: 2, budget: true },
    { key: "le11", label: "≤ 11 px, live corpus 1440p", limit: 0.05 },
    { key: "le11_day", label: "≤ 11 px, day replay (wrap-tolerant)", limit: 0.05 },
    { key: "r1440", label: "1440p, replay (wrap-tolerant)", limit: 0.1 },
    { key: "r4k", label: "4K, replay (wrap-tolerant)", limit: 0.013 }
  ],
  chart: { x: "cpu", y: "le11" },

  captureColumns: ["Corpus CER by bucket; other renderers; replay", "Timing and budget"],
  figures: [
    { src: "baseline.png", title: "Today's OCR on the same harness", caption: "≤ 11 px at 1440p 0.60 on Edge ClearType text." },
    { src: "up3x.png", title: "Uniform 3x Lanczos (earlier research)", caption: "Accurate but 4.5x CPU and +330 ms p95." }
  ],

  candidates: [
    {
      id: "C2", name: "Strip mosaic + ClearType subpixel rebuild", status: "best",
      statusNote: "Measured on raw ClearType renders; the WebP replays fall back to luma and fail the run-level musts.",
      method: "XY-cut strips; small strips enlarged within a fitted time allowance; ClearType strips rebuilt per channel (a_c interleave); one OCR call.",
      pipeline: [{ label: "XY-cut + strips", cost: "~8 ms/Mpix" }, { label: "subpixel rebuild", cost: "~3 ms" }, { label: "mosaic OCR", cost: "≤ today + 40 ms" }],
      uses: ["M7 Text cells", "M8 Ink-row profiles", "M1 Lanczos", "M4 Background gaps", "M5 Subpixel phase split"],
      cost: "CPU 1.55x live, 1.16x deferred; p95 +20 ms; storage unchanged",
      keeps: "≤ 11 px and 1440p on raw ClearType text; 4K no worse",
      fakes: "Grayscale-AA text gets luma quality",
      failsWhen: "Wrong subpixel order (0.78); grayscale AA (0.26 at ≤ 11 px); WebP input; dense windows where OCR time follows word count.",
      condition: "Needs a live run to confirm on real captures",
      test: "Corpus (Edge ClearType, Edge gray, Pillow) + standard/day replay",
      priorArt: { name: "Displaced Filtering (forward rendering only)", url: "https://www.microsoft.com/en-us/research/publication/displaced-filtering-for-patterned-displays/" },
      values: { dms: 20.46, cpu: 1.545, le11: 0.006, le11_day: 0.398, r1440: 0.153, r4k: 0.008 },
      capture: "C2.png",
      metricsLine: "cer_le11_1440=0.006 cer_le11_4k=0.004 cer_1440=0.003 cer_4k=0.002 cer_def_le11_1440=0.076 dms_p95=20.460 cpu_ratio=1.545 replay_join_1440=0.153 replay_join_4k=0.008"
    },
    {
      id: "C1", name: "Single-call strip mosaic, budgeted scale", status: "fails",
      statusNote: "≤ 11 px on the replays 0.095 / 0.101; forced 3x passes (0.040 / 0.045) but costs +250-700 ms on dense windows.",
      method: "XY-cut strips, small strips enlarged with luma Lanczos within a fitted allowance, shelf-packed mosaic, one OCR call.",
      pipeline: [{ label: "XY-cut + strips", cost: "~8 ms/Mpix" }, { label: "shelf mosaic OCR", cost: "≤ today + 40 ms" }],
      uses: ["M7 Text cells", "M8 Ink-row profiles", "M1 Lanczos", "M4 Background gaps", "M12 Thumbnail cap"],
      cost: "CPU 1.61x; p95 +40 ms",
      failsWhen: "OCR time follows words recognised on real windows, so reading small text correctly exceeds +50 ms.",
      relaxes: "Per-call limit to ~+250-700 ms on dense small-text windows (forced 3x)",
      priorArt: { name: "Example packing (No Padding Please)", url: "https://arxiv.org/abs/1902.11208" },
      values: { dms: 40.09, cpu: 1.612, le11: 0.042, le11_day: 0.101, r1440: 0.043, r4k: 0.010 },
      capture: "C1.png",
      metricsLine: "cer_le11_1440=0.042 cer_le11_4k=0.007 cer_1440=0.017 dms_p95=40.090 cpu_ratio=1.612 replay_join_1440=0.043 replay_join_4k=0.010"
    },
    {
      id: "C3", name: "Native pass + capped small-line refine", status: "fails",
      statusNote: "The cap leaves 68-73 % of small lines in real windows unrefined; ≤ 11 px stays at 0.41 / 0.61.",
      method: "Today's call, then one extra call on a mosaic of the smallest lines within a 30 ms predicted cap, with a pixel-hash cache.",
      pipeline: [{ label: "native OCR", cost: "today" }, { label: "refine mosaic", cost: "≤ 30 ms predicted" }],
      uses: ["M9 Native word boxes", "M7 Text cells", "M8 Ink-row profiles", "M1 Lanczos", "M4 Background gaps"],
      cost: "CPU 1.33x; p95 +35 ms",
      priorArt: { name: "screenpipe OCR cache", url: "https://github.com/screenpipe/screenpipe" },
      values: { dms: 34.7, cpu: 1.333, le11: 0.408, le11_day: 0.612, r1440: 0.207, r4k: 0.012 },
      capture: "C3.png",
      metricsLine: "cer_le11_1440=0.408 cer_le11_4k=0.056 cer_1440=0.160 dms_p95=34.7 cpu_ratio=1.333 replay_join_1440=0.207"
    },
    {
      id: "C4", name: "Height-normalised strips, shrinking large text", status: "unbuilt",
      statusNote: "Same cost flaw as C1; its saving is overstated [7].",
      method: "Per-strip scale toward a target height, large 4K text shrunk to fund small text.",
      pipeline: [{ label: "strips + bisection", cost: "< 1 ms" }, { label: "mosaic OCR", cost: "≤ today + 40 ms" }],
      uses: ["M2 Target height", "M8 Ink-row profiles"],
      cost: "~as C1",
      values: { dms: 40, cpu: 1.6 }, estimated: true
    },
    {
      id: "C5", name: "Settled-frame worker refine", status: "unbuilt",
      statusNote: "Stored text has no line positions; WebP ceiling 0.058 [7].",
      method: "Worker re-reads small lines from the stored WebP once a window has been unchanged for 3 s.",
      pipeline: [{ label: "WebP decode", cost: "43-113 ms" }, { label: "refine chunks", cost: "≤ +50 ms each" }],
      uses: ["M8 Ink-row profiles", "M11 OCR before encoding"],
      cost: "~+43-113 ms decode per settled state",
      values: { dms: 50, cpu: 1.8 }, estimated: true
    },
    {
      id: "C6", name: "Worker band jobs with transient lossless crops", status: "unbuilt",
      statusNote: "Schema forbids text and a capture path on one row; needs new storage handling [7].",
      method: "Small bands saved as encrypted lossless crops and re-read by the worker.",
      pipeline: [{ label: "lossless encode", cost: "~19 ms est." }, { label: "worker job", cost: "≤ 41 ms" }],
      uses: ["M10 Lossless crops", "M5 Subpixel phase split"],
      cost: "live +≤ 26 ms (estimate)",
      values: { dms: 26, cpu: 1.96 }, estimated: true
    }
  ],

  method: {
    summary: "C2: enlarge only small strips, and rebuild ClearType strips from their subpixels, inside one OCR call whose predicted time stays within today's + 40 ms.",
    equation: "a_c = clip((v_c − bg_c) / (fg_c − bg_c), 0, 1)   for c ∈ {R, G, B}\ng[y, 3x + k] = a_k   (RGB order by lower total variation)\ns = max s ≤ 3 (0.125 steps) with t_pred(mosaic) + t_pre ≤ t_pred(today) + 40 ms\nt_pred = 44 ms + 57 ms/Mpix · dense + 14 ms/Mpix · blank",
    steps: [
      { text: "Crop with find_text_box, XY-cut into blocks after clearing border columns, and take ink-row strips.", assumes: "Text lines are horizontal runs of ink separated by quieter rows." },
      { text: "Mark strips ≤ 15 device px as small; pick the largest scale that fits the predicted allowance.", assumes: "The fitted cost model holds on the user's windows." },
      { text: "Rebuild small ClearType strips (fringe energy ≥ 0.05) from per-channel coverage; use luma Lanczos otherwise.", assumes: "RGB-stripe ClearType on these monitors." },
      { text: "Pack the strips in reading order with background gaps and make one OCR call replacing today's." }
    ],
    cost: "CPU 1.55x live and 1.16x deferred; p50/p95 72/88 ms against 63/98 ms today; storage unchanged.",
    failsWhen: "Wrong subpixel order; grayscale-AA text; WebP input (deferred game-mode windows); dense windows where OCR time follows word count.",
    reproduce: "uv run python eval/small-text-ocr/harness/run.py eval/small-text-ocr/C2/c2.py"
  },

  findings: [
    { question: "What limits today's OCR", claims: ["Only game-mode windows are OCR'd from the stored WebP; others are read from raw pixels [8].", "OCR of the stored WebP matches the live text within 0.01; the codec is not the cause [1][2].", "The scorer misreads wrapped lines: 1440p 0.371 → 0.201 wrap-tolerant [1]."] },
    { question: "OCR cost", claims: ["On real windows OCR time follows words recognised (~0.29 ms per word), not pixels, and correctly read small text roughly triples the word count [1]."] },
    { question: "Prior art", claims: ["Windows-OCR apps apply at most scaling, padding and a fill [4][6]; no tool inverts ClearType for OCR [5]."] }
  ],
  mechanisms: [
    { id: "M1", name: "Lanczos upscale", core: "Enlarge before OCR.", assumes: "Pixels are cheap.", breaks: "Whole-crop cost. [4]" },
    { id: "M2", name: "Target text height", core: "Scale to ~40 px words.", assumes: "One size per window.", breaks: "Mixed sizes. [4]" },
    { id: "M4", name: "Background gaps", core: "Pad strips with their background.", assumes: "Flat backgrounds.", breaks: "Gradients." },
    { id: "M5", name: "Subpixel phase split", core: "Read R, G, B as three samples.", assumes: "ClearType RGB stripe.", breaks: "Grayscale AA, WebP, wrong order." },
    { id: "M7", name: "Text cells", core: "Gradient density per 16x16 cell.", assumes: "Text = dense horizontal edges.", breaks: "Borders glue blocks. [8]" },
    { id: "M8", name: "Ink-row profiles", core: "Strips from row runs.", assumes: "Horizontal lines.", breaks: "Offset columns without a cut." },
    { id: "M9", name: "Native word boxes", core: "Heights from a first pass.", assumes: "Native pass finds the words.", breaks: "Missed small words." },
    { id: "M10", name: "Lossless crops", core: "Store text crops losslessly.", assumes: "Deferred OCR matters.", breaks: "Not needed for normal windows. [2]" },
    { id: "M11", name: "OCR before encoding", core: "Live path reads raw pixels.", assumes: "Gate runs live.", breaks: "Game mode defers. [8]" },
    { id: "M12", name: "MaxImageDimension cap", core: "Thumbnail above 10000 px.", assumes: "Limit known.", breaks: "None. [8]" }
  ],
  explored: [
    { name: "Uniform 3x Lanczos", reason: "4.5x CPU, +330 ms p95." },
    { name: "C1 forced 3x", reason: "Meets the replay musts at +250-700 ms on dense real windows." },
    { name: "C3 capped refine", reason: "Leaves most small lines unrefined; its queue never drains." },
    { name: "C4, C5, C6", reason: "Not built: cost flaw, no stored line positions, schema conflict." },
    { name: "Lossless storage for deferred OCR", reason: "The codec is not the cause." },
    { name: "Smoothing after the subpixel rebuild", reason: "No gain." }
  ],
  caveats: [
    "The run-level musts were scored on stored WebP replays, where C2's core cannot show; C2's ≤ 11 px results come from headless Edge renders with one page layout.",
    "Whether real WGC captures carry ClearType fringes was not measured on raw captures.",
    "M3.1 was scored wrap-tolerant because of a scorer artefact; score.py needs the same fix before rescoring. M3.3 and UIA were not measured.",
    "Cost models were fitted on the corpus; real dense windows cost more per pixel.",
    "The refuter could run no OCR experiments."
  ],
  sources: [
    { n: 1, title: "Small-text OCR harness and candidates", url: "eval/small-text-ocr/", tier: 1, version: "2026-11-20" },
    { n: 2, title: "Codec and size spikes", url: "eval/small-text-ocr/", tier: 1, version: "2026-11-19" },
    { n: 3, title: "No Padding Please (example packing)", url: "https://arxiv.org/abs/1902.11208", tier: 1, version: "2019" },
    { n: 4, title: "Windows OCR small text upscaling (research)", url: "docs/research/windows-ocr-small-text-upscaling.md", tier: 1, version: "2026-11-19" },
    { n: 5, title: "Displaced Filtering for Patterned Displays", url: "https://www.microsoft.com/en-us/research/publication/displaced-filtering-for-patterned-displays/", tier: 1, version: "MSR" },
    { n: 6, title: "screenpipe", url: "https://github.com/screenpipe/screenpipe", tier: 1, version: "2026-11-19" },
    { n: 7, title: "Refuter analysis", url: "eval/small-text-ocr/candidates.md", tier: 1, version: "2026-11-20" },
    { n: 8, title: "Akashic OCR path", url: "src/akashic/capture/screen_writer.py", tier: 1, version: "2026-11-19" }
  ]
};
