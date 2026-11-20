window.REPORT = {
  title: "Screen Text Duplication And Noise",
  date: "2026-11-20",
  depth: "full",
  context: "Python 3.14.10, SQLite FTS5 (in memory), Windows 11 26200, Ryzen 7 7800X3D; harness replays of the stored window texts of quality runs smoke 2241, standard 1818 and day 0547, plus a synthetic set with exact truth",
  problem: "Akashic stores each window's screen text again on every stored frame, so unchanged and near-unchanged text dominates the store (M3.2 duplicate characters 0.86 smoke, 0.96 standard, 0.95 day). OCR of busy screens adds UI chrome repeated in every frame and misread fragments, which crowd FTS results (0.73-0.78 of top-5 slots repeat a window already shown), embeddings and Ask evidence. Exact-duplicate text already shares vectors (#101) but not storage, FTS rows or near-duplicates.",
  goals: ["Store and index each distinct piece of window text once per window run, near-duplicates included (a clock, a counter, a few OCR characters)", "Separate content text from UI chrome and drop or down-weight OCR garbage", "Keep every line the user saw findable at its moment"],
  contract: {
    must: ["M3.3 unique-text recall no worse than today on the standard and day runs, read through the new index", "M3.1 CER no worse than today, on frames rebuilt from the line store", "At most 1 % of content lines (driver-rendered text) dropped or down-ranked as chrome or garbage", "A kept line links to a frame where it was visible: frame-text validity 1.00, per-line truth link no worse than today with the same per-line weighting", "M3.2 at most 0.3, over what is physically stored", "Count of provably misread tokens that search and Ask can return at least halved vs today", "Chrome keeps every visibility interval; only its rank changes"],
    fake: ["UI chrome stored once per app per day instead of per frame", "Which frame represents a repeated line"]
  },
  budget: ["≤ 1 ms extra per stored frame on the capture write path", "Deferred dedupe and cleaning ≤ 2 % of one core averaged over a day", "FTS and Ask query cost no worse than today", "No new dependency; no GPU model per frame", "Schema may change; text stays encrypted, any hash keyed (HMAC) as in #101"],
  nonGoals: ["OCR accuracy itself (#190, French OCR pack F12)", "Image change detection (#208)", "Embedding reuse (#101)", "Ask and answer quality", "A vision model", "UIA read speed", "Cross-day dedupe of the same document"],

  answer: {
    text: "**C1** (prototype P1): a deferred worker turns each window's frame texts into a lossless line store keyed by HMAC per (process, day), with digit templates and exact edit residuals for near-duplicates, on/off sighting intervals per window run, one FTS document per window run, chrome stored once per app-day at a lower bm25 weight, and misread-shaped tokens moved to a lowest-weight junk column [1]. It keeps every must: M3.3 equal to today (0.680 / 0.802 / 0.830), M3.1 on rebuilt frames equal (0 rebuild mismatches), content lines down-ranked 0 / 0.29 / 0.20 %, frame-text link 1.00, per-line truth link 0.985 / 0.948 / 0.875 against today's 0.984 / 0.942 / 0.870, M3.2 0.002 / 0.028 / 0.059 over FTS documents and 0 over physically stored rows, misread tokens search and Ask can return 89 / 448 / 3121 against today's 538 / 7654 / 44363 (0.17x / 0.06x / 0.07x) [1]. Cost: 0 µs on the write path against 1 ms; deferred work 0.027-0.028 % of one core against 2 %; FTS index 0.22x / 0.056x / 0.052x of today [1]. Prior art: CLP for the template and residual [7], template detection for chrome [10]; no line-level store with run intervals found (screenpipe skips whole windows by SimHash) [3][6].",
    relaxed: "If M3.1 may rise by 0.00004 CER on rebuilt frames, **C2** (P2, consensus readings) cuts misreads further (65 / 419 / 2765 against 523 / 7635 / 50611 by `score_v2.py`, 0.12x / 0.05x / 0.05x) and M3.2 to 0.002 / 0.011 / 0.037, at 0 / 0.29 / 0.25 % content lines lost and 1.20 % of synthetic content lines dropped [1]."
  },
  bestFit: "C1",

  metrics: [
    { key: "write_us", label: "Write-path extra per frame", unit: "µs", limit: 1000, budget: true },
    { key: "defer", label: "Deferred work, worst run", unit: "% of a core", limit: 2, budget: true },
    { key: "fts_x", label: "FTS index size vs today, worst run", limit: 1, budget: true },
    { key: "mis_v2", label: "Misread count vs today, worst run (score_v2.py)", limit: 0.5 },
    { key: "mis_p1", label: "Misread count vs today, worst run (run_p1.py)", limit: 0.5 },
    { key: "m33", label: "M3.3 day (today 0.830)", limit: 0.8299572, better: "higher" },
    { key: "cer_st", label: "M3.1 CER on rebuilt frames, standard", limit: 0.1837297 },
    { key: "cer_d", label: "M3.1 CER on rebuilt frames, day", limit: 0.2118925 },
    { key: "cdrop", label: "Content lines lost or down-ranked, worst run", unit: "%", limit: 1 },
    { key: "syn_drop", label: "Synthetic content lines dropped", unit: "%" },
    { key: "link", label: "Frame-text link validity, min", limit: 1, better: "higher" },
    { key: "tlink_d", label: "Per-line truth link, day (score_v2.py; today 0.892)", limit: 0.892, better: "higher" },
    { key: "m32", label: "M3.2 over FTS documents, worst run", limit: 0.3 },
    { key: "crowd_d", label: "Top-5 slots repeating a window, day nonces" }
  ],
  chart: { x: "defer", y: "mis_v2" },

  captureColumns: ["M3.2, M3.3, content dropped, noise panel (share, not count)", "FTS size, links, crowding, cost"],
  figures: [
    { src: "fig-misread-count.png", title: "Misread tokens search and Ask can return (the must)", caption: "Counts on a log axis, by both scoring tools. run_p1.py: today 538 / 7654 / 44363, P1 89 / 448 / 3121. score_v2.py: today 523 / 7635 / 50611, P1 90 / 482 / 3527, P2 65 / 419 / 2765. The dashed line is half of today." },
    { src: "baseline.png", title: "Today on the same harness", caption: "M3.2 0.86 / 0.96 / 0.95; 0.73-0.78 of top-5 slots repeat a window. The candidate column repeats today, so both bars are equal." },
    { src: "fig-noise-views.png", title: "C1 misreads as a share, three readings", caption: "Per document 1.00 / 0.92 / 0.95x, Ask evidence 0.96 / 0.94 / 0.95x, occurrences 1.22 / 1.36 / 1.40x. Dedupe collapses repeated correct text while misreads stay unique, so the share does not fall although the count falls 6-16x. Right panel: stronger garbage settings reach 0.71x only at 1.2 % content lost." },
    { src: "fig-round2.png", title: "Round-2 musts by score_v2.py, today vs P1 vs P2", caption: "P1 violet, P2 blue here. Top right: misread counts in thousands. Bottom left: per-line truth link, both at or above today. Bottom right: P2 configurations; the day-run labels 3, 5, 7 and 8 overlap." },
    { src: "fig-m32-by-app.png", title: "C1 M3.2 by app type", caption: "FTS documents only; physically stored rows have M3.2 0. Day chat reaches 0.18 and browser 0.08, under the 0.3 limit." },
    { src: "fig-content-down.png", title: "C1 content lines lost, by cause", caption: "Standard: 1 browser line taken as chrome, 1 terminal line. Day: 7 lines (6 terminal, 1 browser), none from the garbage rule. The amber line is 1 % of captured lines; the chart's legend lists 'today', which has no bars." },
    { src: "fig-drop-causes.png", title: "C2 content lines lost, by cause", caption: "Day: 3 cut by the garbage rule, 4 false merges or real edits voted away, 2 minority readings of the same text. The axis label says 3552 captured lines; details.json scores 3550." }
  ],

  candidates: [
    {
      id: "C1", name: "Lossless line store, digit template + residual, chrome by spread, junk column", status: "best",
      statusNote: "Prototype P1 (merged C1 + C2 + C4 + C5). Keeps every must strictly, with the widest budget margin.",
      method: "Deferred worker: split each frame text into lines; one row per distinct line per (process, day) under HMAC; near-duplicates stored as a digit template with exact slots or an exact edit residual; frame rows keep an ordered line-id list; sightings as on/off intervals per window run; one FTS document per run; chrome lines in an app-day document at low bm25 weight; misread-shaped tokens in a junk column.",
      pipeline: [{ label: "capture write path", cost: "unchanged, 0 µs" }, { label: "split lines + HMAC key per (process, day)", cost: "deferred" }, { label: "digit template / edit residual", cost: "deferred" }, { label: "run intervals + run FTS document", cost: "deferred" }, { label: "chrome by context spread, junk tokens", cost: "deferred" }, { label: "whole deferred pass", cost: "0.027-0.028 % of a core" }],
      uses: ["D1 Exact content tag", "D11 Per-key sightings", "D7 Drain digit masking", "D6 Length-scaled edit merge", "D10 Incremental FTS5", "N4 Repeated-block detection", "N10 Store once, down-weight", "N6 rmgarbage", "N7 Lexicon hit ratio"],
      cost: "0 µs per frame against 1 ms; 0.027-0.028 % of one core against 2 %; FTS 0.22x / 0.056x / 0.052x of today",
      keeps: "Every must: M3.3 equal, M3.1 on rebuilt frames equal, content down ≤ 0.29 %, frame link 1.00, per-line link above today, M3.2 ≤ 0.059, misreads 0.06-0.17x",
      fakes: "The frame a repeated line links to (middle of its longest streak)",
      failsWhen: "A single stable misreading has no second reading to compare: 60-62 % of the misreads left are such lines. Terminal lines lose most (1.06 % of day terminal lines). Resized windows re-wrap lines into new keys (bytes only).",
      test: "Replays of smoke, standard, day + synthetic set · run_p1.py and score_v2.py",
      priorArt: { name: "CLP template + variables", url: "https://www.usenix.org/conference/osdi21/presentation/rodrigues" },
      values: { write_us: 0, defer: 0.028, fts_x: 0.22, mis_v2: 0.172, mis_p1: 0.165, m33: 0.8299572, cer_st: 0.1837297, cer_d: 0.2118925, cdrop: 0.29, syn_drop: 0, link: 1, tlink_d: 0.896, m32: 0.059, crowd_d: 0.061 },
      capture: "C1.png",
      metricsLine: "m32 0.002/0.028/0.059 (stored 0) · m33 0.680/0.802/0.830 · cerR 0.2289/0.1837/0.2119, rebuild_bad 0 · content down 0/0.29/0.20 % · link 1.00 · tlinkL 0.985/0.948/0.875 (today 0.984/0.942/0.870) · misread 89/448/3121 (today 538/7654/44363) · fts_x 0.22/0.056/0.052 · write 0 µs · defer 0.027/0.028/0.027 %"
    },
    {
      id: "C2", name: "Line store with aligned-reading consensus", status: "relaxes",
      statusNote: "Prototype P2 (merged C1 + C3 decompose variant + C4 + C5). Works if M3.1 may rise by 0.00004 CER on rebuilt frames (standard 0.18377 vs 0.18373, day 0.21191 vs 0.21189).",
      method: "As C1, but consecutive OCR frames of a run are aligned on line ids; gap lines pair by digit change or a D6 edit bound; each track keeps its mode reading, with digits stored exactly per frame; minority readings add only their new tokens, garbage-flagged ones down-ranked.",
      pipeline: [{ label: "capture write path", cost: "unchanged, 0 µs" }, { label: "window runs by context or line overlap", cost: "deferred" }, { label: "align frames, pair gap lines (D6)", cost: "deferred" }, { label: "mode reading per track, digit slots", cost: "deferred" }, { label: "chrome + garbage down-rank", cost: "deferred" }, { label: "whole deferred pass", cost: "0.018-0.029 % of a core" }],
      uses: ["D1 Exact content tag", "D11 Per-key sightings", "D7 Drain digit masking", "D6 Length-scaled edit merge", "N4 Repeated-block detection", "N10 Store once, down-weight", "N6 rmgarbage", "N7 Lexicon hit ratio", "ROVER consensus"],
      cost: "0 µs per frame; 0.018-0.029 % of one core; FTS 0.216x / 0.054x / 0.049x",
      relaxes: "M3.1 on rebuilt frames, by +0.00004 CER (standard) and +0.00001 (day)",
      failsWhen: "Consensus replaces minority readings, so a real one-letter edit can be voted away: 4 false merges on day, and 1.20 % of synthetic content lines (2.59 % of volatile ones) dropped.",
      priorArt: { name: "ROVER", url: "https://doi.org/10.1109/ASRU.1997.659110" },
      values: { write_us: 0, defer: 0.029, fts_x: 0.216, mis_v2: 0.124, m33: 0.8299572, cer_st: 0.1837669, cer_d: 0.2119062, cdrop: 0.29, syn_drop: 1.20, link: 1, tlink_d: 0.896, m32: 0.037, crowd_d: 0.002 },
      capture: "C2.png",
      metricsLine: "m32 0.002/0.011/0.037 · m33 0.680/0.802/0.830 · M3.1 rebuilt 0.22886/0.18377/0.21191 · content down 0/0.29/0.25 % · syn_drop 1.20 % · link 1.00 · tlinkL 0.986/0.944/0.896 (today 0.984/0.937/0.892) · misN 65/419/2765 (today 523/7635/50611) · defer 0.029/0.021/0.018 %"
    },
    {
      id: "C1a", name: "C1 with the token stability guard off", status: "fails",
      statusNote: "Content lines lost 1.45 % on standard against 1 %.",
      method: "Garbage-shaped tokens go to the junk column even when the line was read consistently.",
      pipeline: [{ label: "as C1, unstable guard off", cost: "deferred" }],
      uses: ["N6 rmgarbage", "N7 Lexicon hit ratio"],
      cost: "as C1",
      values: { cdrop: 1.45, m33: 0.8299572, syn_drop: 0 },
      metricsLine: "share mis_doc_x 0.84 (standard) 0.87 (day) · drop 1.45 % / 0.65 %"
    },
    {
      id: "C1b", name: "C1 with minority readings down-ranked", status: "fails",
      statusNote: "M3.3 on day 0.826 against 0.830.",
      method: "Word-shaped tokens that only a minority reading holds also go to the junk column.",
      pipeline: [{ label: "as C1, minority rules on", cost: "deferred" }],
      uses: ["N6 rmgarbage", "N7 Lexicon hit ratio"],
      cost: "as C1",
      values: { cdrop: 0.42, m33: 0.8259629, syn_drop: 0 },
      metricsLine: "share mis_doc_x 0.84 / 0.86 · drop 0.29 % / 0.42 % · m33 0.802 / 0.826"
    },
    {
      id: "C1c", name: "C1 with every guard off, LM at 5 %", status: "fails",
      statusNote: "Content lines lost 3.90 % on standard; M3.3 day 0.826.",
      method: "Every garbage-shaped or LM-unlikely token to the junk column.",
      pipeline: [{ label: "as C1, all guards off", cost: "deferred" }],
      uses: ["N6 rmgarbage", "N7 Lexicon hit ratio"],
      cost: "as C1",
      values: { cdrop: 3.90, m33: 0.8259629, syn_drop: 6.62 },
      metricsLine: "share mis_doc_x 0.64 / 0.69 · drop 3.90 % / 1.15 % · syn_drop 6.6 %"
    },
    {
      id: "C2a", name: "C2 with garbage split inside lines", status: "fails",
      statusNote: "Content lines lost 1.59 % (standard) and 1.10 % (day).",
      method: "Interior garbage always splits the line instead of staying inside it.",
      pipeline: [{ label: "as C2, MODE=split", cost: "deferred" }],
      uses: ["N6 rmgarbage", "ROVER consensus"],
      cost: "as C2",
      values: { mis_v2: 0.119, cdrop: 1.59, m33: 0.8299572, m32: 0.056 },
      metricsLine: "misN 62/364/2631 · drop 0/1.59/1.10 % · m32 0.006/0.021/0.056"
    },
    {
      id: "C2b", name: "C2 with the UIA trigram LM on", status: "fails",
      statusNote: "Smoke M3.3 falls from 0.680 to 0.616: harness lines stop being findable (goal 3).",
      method: "OCR tokens scoring low under a char-trigram LM of the day's UIA tokens are down-ranked too.",
      pipeline: [{ label: "as C2, LM_MIN_TYPES=300", cost: "deferred" }],
      uses: ["N7 Lexicon hit ratio", "ROVER consensus"],
      cost: "as C2",
      values: { mis_v2: 0.105, cdrop: 0.29, m33: 0.8299572, m32: 0.042 },
      metricsLine: "misN 55/424/2884 · drop 0/0.29/0.23 % · m33 0.616/0.802/0.830"
    },
    {
      id: "C2c", name: "C2 with LM and split", status: "fails",
      statusNote: "Content lines lost 3.76 % (standard), 4.41 % (smoke).",
      method: "Both C2a and C2b.",
      pipeline: [{ label: "as C2, LM + split", cost: "deferred" }],
      uses: ["N7 Lexicon hit ratio", "N6 rmgarbage"],
      cost: "as C2",
      values: { mis_v2: 0.078, cdrop: 3.76, m33: 0.8299572, m32: 0.065 },
      metricsLine: "misN 41/311/2349 · drop 4.41/3.76/1.58 %"
    },
    {
      id: "C3", name: "Source-split regimes (UIA landmark flag at capture)", status: "unbuilt",
      statusNote: "Not built: the UIA read is a flat FindAllBuildCache with no ancestry, and UIA already drops buttons, menus and tabs [3][4].",
      method: "UIA rows get a chrome flag from landmark ancestry at capture; OCR rows go through C2.",
      pipeline: [{ label: "UIA ancestry flag at capture", cost: "≤ 0.6 ms (estimate)" }, { label: "OCR rows as C2", cost: "deferred" }],
      uses: ["N8 UIA control/content views", "N4 Repeated-block detection"],
      cost: "~0.6 ms per frame on the write path (inventor estimate)",
      values: { write_us: 600 }, estimated: true
    }
  ],

  method: {
    summary: "C1: the screen text of a window run is stored once per distinct line, near-duplicates as exact deltas, so frames rebuild byte for byte while FTS, embeddings and Ask see each line once.",
    equation: "key(line) = HMAC(K_line, process | day | line)\nrow(line) = T(template, slots)  if digit_template(line) matches a stored template\n          = E(base_id, ops)     if an existing line is within ceil(0.1·len) edits (len ≥ 8)\n          = T(line, ())         otherwise\nframe_text(f) = join(row_text(id) for id in line_ids(f))   (0 mismatches on 10435 rows)\nrun = consecutive frames of one hwnd, same title and url, gap ≤ 300 s\nchrome(key) ⇔ contexts(key) ≥ max(3, 0.5·N_ctx)  or  runs(key) ≥ 0.8·N_runs with N_runs ≥ 3  (len ≤ 200)",
    steps: [
      { text: "Capture writes `frame_windows.text` as today. Nothing is added to the write path.", assumes: "The deferred worker catches up between captures; backlog stays findable through today's FTS." },
      { text: "The worker splits each window text into lines and keys each distinct line by HMAC per (process, local day).", assumes: "Text and keys stay encrypted and keyed as in #101." },
      { text: "A new line that matches a stored digit template stores only its digits; one within ceil(0.1·len) edits of a stored line stores an exact edit residual. Nothing is lost.", assumes: "Residuals stay small for clocks, counters and OCR flips." },
      { text: "The frame row keeps an ordered list of line ids instead of its text. Sightings are on/off intervals per (line, window run).", assumes: "A run closes after 300 s without a stored frame of the hwnd, or on a title or url change." },
      { text: "One external-content FTS document per window run holds its content lines; Ask and embeddings read the same run document.", assumes: "The window run stays the search unit (RETRIEVAL.md: the window is the unit), so implicit AND keeps working across lines of one run." },
      { text: "Chrome: a line key seen in ≥ max(3, 0.5·N) (title, url) contexts of an app-day, or in ≥ 0.8 of ≥ 3 runs of a single-context app, goes to one app-day document at lower bm25 weight, kept in Ask evidence as one block, every interval kept.", assumes: "Static content opened in many contexts is rare; 1 browser line on standard was taken as chrome." },
      { text: "Garbage: an OCR token that misses the day's UIA lexicon and fails FR-adapted rmgarbage, is a 1-2 letter miss, or scores below the 2nd percentile of a UIA char-trigram LM, goes to a junk column (lowest bm25 weight, still searchable) when its line was read once or the token sits only in a minority reading. A line whose garbage-shaped characters reach 34 % goes whole to the junk column.", assumes: "Two readings exist for unstable OCR; 60-62 % of the remaining misreads are single stable readings and stay indexed." },
      { text: "Each kept line links to the middle frame of its longest streak.", assumes: "Which frame represents a repeated line may be faked (fidelity)." }
    ],
    cost: "Write path: 0 µs per frame (p50 and p99) against 1 ms. Deferred: 0.086 s over 0.087 h (smoke), 1.108 s over 1.12 h (standard), 6.919 s over 7.11 h (day) = 0.027-0.028 % of one core against 2 %, median of 9 repetitions, in memory. FTS index 28 / 111 / 619 KB against 127 / 1992 / 12012 KB. Stored text 0.12x / 0.036x / 0.032x of today's characters.",
    failsWhen: "A misreading that is stable across frames (OCR gate reuse) stays indexed as content. Terminal output scrolls through the same rows, so chrome or pairing takes 1.06 % of day terminal lines (0.20 % of all lines). A window resize re-wraps lines into new keys (storage only). An app whose content repeats across ≥ 3 contexts (a pinned document, a chat header) is down-weighted as chrome, still findable.",
    reproduce: "uv run python eval/screen-text-duplication-noise/P1-line-store-lossless/run_p1.py eval/screen-text-duplication-noise/P1-line-store-lossless (needs the quality runs' stores in eval/quality/local/runs/, this PC only)"
  },

  findings: [
    { question: "Where today's stored characters go", claims: ["Whole-text repeats of the window's previous text: 0.33 / 0.42 / 0.30 of stored characters; exact repeated lines: 0.53 / 0.54 / 0.65; near-duplicate lines (digits or ≤ 10 % edits): 0.013 / 0.006 / 0.012 [2].", "Chrome by context spread: 3.0 / 3.5 / 11.1 % of characters; garbage-shaped lines 4.1 / 4.4 / 3.1 % [2].", "0.73-0.78 of top-5 nonce-query slots repeat a window already shown [2]."] },
    { question: "Where misreads come from", claims: ["Of the misread tokens C1 still indexes, 81-85 % come from live OCR, 13-17 % from deferred OCR and 1-3 % from UIA [1].", "60.0 % (standard) and 61.7 % (day) sit in lines with no second reading of the same place: the OCR gate reuses stored text while the crop is unchanged, so a misreading is stable [1][4].", "The line store alone (no garbage rule, no consensus, no pairing) already cuts day misreads to 3866 of 50611 (0.08x) by `score_v2.py`; the garbage rules take it to 2996 and consensus to 2766 (−8 %) [1]."] },
    { question: "Share against count", claims: ["Dedupe removes repeated correct text and leaves each misread once, so misreads as a share rise to 1.22-1.40x of today by occurrences and stay 0.92-1.00x per document, while the count falls to 0.06-0.17x [1].", "Under the round-1 unknown-token share, an oracle removing every garbled token reaches only 0.41x; the rest are real but unlisted words [1]."] },
    { question: "Prior art", claims: ["screenpipe skips a whole window when its 64-bit SimHash is within 10 bits for under 60 s, and references whole frames by content hash; no line-level store [3][6].", "CLP splits log lines into a template and exact variables; Drain masks digits into templates [7][8].", "ROVER votes word-level readings across recognisers; template detection finds blocks repeated across peer pages [9][10].", "Web boilerplate methods miss 5-20 % of content segments, so they can only down-weight, never delete [12][13][3]."] }
  ],
  mechanisms: [
    { id: "N1", name: "jusText block classes", core: "Link density, length and stopword density classify blocks, neighbours relabel.", assumes: "DOM links and stoplists.", breaks: "Short content such as chat replies. [12]" },
    { id: "N2", name: "Boilerpipe shallow features", core: "Words, link and text density in a decision tree.", assumes: "Web pages.", breaks: "P 0.818, R 0.796 on web. [13]" },
    { id: "N3", name: "Readability / Trafilatura", core: "Container scoring.", assumes: "A DOM.", breaks: "Misses 5-20 % of segments, never a delete filter. [13][3]" },
    { id: "N4", name: "Repeated-block detection", core: "count(line, app, day) / frames(app, day) ≥ θ.", assumes: "Chrome repeats across contexts.", breaks: "Static content such as a pinned document. [14][10]" },
    { id: "N5", name: "screenpipe SimHash skip", core: "64-bit SimHash over word 3-shingles per window.", assumes: "Small changes do not matter.", breaks: "Skips small changes, no chrome split. [6]" },
    { id: "N6", name: "rmgarbage", core: "Length, alnum ratio, repeats, vowel/consonant runs, punctuation rules.", assumes: "Vowels extended for French.", breaks: "URLs, paths, identifiers. [11]" },
    { id: "N7", name: "Lexicon hit ratio", core: "Word-list hits or char-trigram likelihood.", assumes: "A FR/EN word list.", breaks: "Names, games, code; systematic OCR errors pass. [3]" },
    { id: "N8", name: "UIA control/content views", core: "IsContentElement and landmark containers.", assumes: "Ancestry is read.", breaks: "Akashic reads a flat cache. [18][4]" },
    { id: "N9", name: "OCR geometry", core: "Word height and gaps as garbage hints.", assumes: "Windows OCR gives boxes.", breaks: "No confidence value; untested. [20]" },
    { id: "N10", name: "Store once, down-weight", core: "Keyed line stored once per app-day, never dropped.", assumes: "Rank changes are enough.", breaks: "Content repeated across contexts is down-weighted." },
    { id: "D1", name: "Exact content tag", core: "Content hash plus a reference frame.", assumes: "Byte-identical text.", breaks: "One changed character. [6]" },
    { id: "D2", name: "Window SimHash gate", core: "Skip if Hamming ≤ 10 within 60 s.", assumes: "Skipped changes do not matter.", breaks: "Findability; short lines. [6]" },
    { id: "D3", name: "Manku pigeonhole probe", core: "Table probe for Hamming-k fingerprints.", assumes: "Long documents.", breaks: "P/R ~0.75 at k = 3. [16]" },
    { id: "D4", name: "Shingle MinHash / LSH", core: "Jaccard of w-shingles.", assumes: "Long text.", breaks: "One OCR error removes w shingles. [17]" },
    { id: "D5", name: "Token-set Jaccard", core: "Jaccard and containment, exact below 4 words.", assumes: "Word-level changes.", breaks: "Short lines. [6]" },
    { id: "D6", name: "Length-scaled edit merge", core: "Exact ≤ 2 chars, 1 edit ≤ 6, 2 edits above.", assumes: "Adjacent texts.", breaks: "'File 1' / 'File 2'. [15]" },
    { id: "D7", name: "Drain digit masking", core: "Digits to a template slot.", assumes: "Digits are variables.", breaks: "Meaningful digits ('Level 2'). [8]" },
    { id: "D8", name: "Spell LCS template", core: "Longest common subsequence ≥ τ.", assumes: "Inserted words.", breaks: "Short unrelated lines. [3]" },
    { id: "D9", name: "Rolling-hash delta", core: "rsync/git deltas.", assumes: "Bytes matter.", breaks: "No per-line key, not FTS-indexable." },
    { id: "D10", name: "Incremental FTS5", core: "Index new line keys only.", assumes: "External content keeps exact values.", breaks: "Contentless tables have no highlight. [19]" },
    { id: "D11", name: "Per-key sightings", core: "(line, frame) or run intervals per key.", assumes: "Keys in the deferred worker.", breaks: "Nothing seen. [5]" }
  ],
  explored: [
    { name: "C1a guard off", reason: "1.45 % of standard content lines lost." },
    { name: "C1b minority readings", reason: "M3.3 day 0.826 against 0.830." },
    { name: "C1c all guards off, LM 5 %", reason: "3.90 % lost; synthetic 6.6 %." },
    { name: "C2 consensus as best fit", reason: "M3.1 on rebuilt frames +0.00004 (standard) and +0.00001 (day); 1.20 % of synthetic content lines dropped." },
    { name: "C2a split", reason: "1.59 % / 1.10 % lost." },
    { name: "C2b UIA LM", reason: "Smoke M3.3 0.616 against 0.680." },
    { name: "C2c LM + split", reason: "3.76 % lost on standard." },
    { name: "C3 source-split regimes", reason: "No UIA ancestry in the cache request; not built." },
    { name: "#101 link table alone", reason: "Whole-text key; one changed character breaks it (21 % of vectors saved on mixed browsing)." },
    { name: "Window SimHash skip (screenpipe)", reason: "Lossy: skipped changes are not findable." },
    { name: "Halving the misread share", reason: "Would need OCR correction (non-goal); 60-62 % of misreads are single stable readings." }
  ],
  caveats: [
    "Two misread countings. `run_p1.py` reads today 538 / 7654 / 44363 and P1 89 / 448 / 3121; `score_v2.py` reads today 523 / 7635 / 50611, P1 90 / 482 / 3527 and P2 65 / 419 / 2765. Both put P1 and P2 under half of today on every run. A rerun of `diag_mis.py` counted 3120 on day.",
    "Two per-line truth links. `run_p1.py`: today 0.984 / 0.942 / 0.870, P1 0.985 / 0.948 / 0.875. `score_v2.py`: today 0.984 / 0.937 / 0.892, P1 0.986 / 0.947 / 0.896, P2 0.986 / 0.944 / 0.896.",
    "Not modelled in the harness (refuter obligations): NULLing `frame_windows.text` fires `frame_windows_passages_au` and `window_text_copies_au`; readers of the text (embed worker, keyword run collapse, snippets, answer sources) must read run documents; `delete_spent_frames`, delete-all, frame_text expiry and export must cover the line tables [3][4].",
    "FTS query cost was not timed; the index is 0.05-0.22x of today and top-5 repeats fall from 0.73-0.78 to 0-0.06. Deferred cost is in-memory Python on the target PC, without SQLite writes or encryption.",
    "M3.1 on kept lines (not rebuilt) is 0.2372 / 0.1798 / 0.1936 for P1; the brief binds M3.1 to rebuilt frames.",
    "Terminal lines lose 0.76 % (standard) and 1.06 % (day) under C1; the 1 % must is on all content lines.",
    "Visible artefacts: the noise panel of C1.png shows the misread share (1.19 / 0.91 / 0.84 %) and its syn bar shows the harness unknown-token share; the noise panel of C2.png and baseline.png shows the round-1 unknown-token share; the truth link in C2.png is frame-weighted. fig-round2.png colours P1 violet; its scatter labels overlap. fig-content-down.png and fig-unknown-left.png list legend entries with no bars.",
    "WEAK: BnL French rmgarbage thresholds (21 / 4 / 6) unverified; the 5-20 % boilerplate miss holds for jusText and trafilatura only [3]."
  ],
  sources: [
    { n: 1, title: "Screen-text harness and prototypes", url: "eval/screen-text-duplication-noise/", tier: 1, version: "target PC, 2026-11-20" },
    { n: 2, title: "Diagnosis of today's stores", url: "eval/screen-text-duplication-noise/diagnosis/summary.txt", tier: 1, version: "2026-11-20" },
    { n: 3, title: "Fact-check, prior-art and refuter notes", url: "_dev/invent/ocr-text-duplication-noise/checks.md", tier: 1, version: "local, 2026-11-20" },
    { n: 4, title: "Akashic code", url: "src/akashic/storage/migrations/0001_schema.sql:682,716; src/akashic/pipeline/embed.py:107-113; src/akashic/storage/retention.py:192; src/akashic/platform/win/uia.py:55,114; src/akashic/capture/ocr_gate.py:1-7; docs/RETRIEVAL.md:46", tier: 1, version: "2026-11-20" },
    { n: 5, title: "Identical Frame Text Reuse (#101)", url: "docs/solutions/2026-11-10 - Identical Frame Text Reuse/report.md", tier: 1, version: "2026-11-10" },
    { n: 6, title: "screenpipe tree cache, event-driven capture, text similarity", url: "_resources/references/screenpipe/crates/screenpipe-a11y/src/tree/cache.rs", tier: 1, version: "local copy" },
    { n: 7, title: "CLP: Efficient and Scalable Search on Compressed Text Logs (OSDI 2021)", url: "https://www.usenix.org/conference/osdi21/presentation/rodrigues", tier: 1, version: "2021" },
    { n: 8, title: "Drain (logparser, Drain3)", url: "https://github.com/logpai/Drain3", tier: 1, version: "current" },
    { n: 9, title: "ROVER (Fiscus, ASRU 1997)", url: "https://doi.org/10.1109/ASRU.1997.659110", tier: 1, version: "1997" },
    { n: 10, title: "Template detection via data mining (Bar-Yossef, Rajagopalan, WWW 2002)", url: "https://doi.org/10.1145/511446.511522", tier: 1, version: "2002" },
    { n: 11, title: "rmgarbage and the BnL adaptation", url: "https://jdmdh.episciences.org/10239/pdf", tier: 2, version: "JDMDH" },
    { n: 12, title: "jusText algorithm", url: "https://github.com/miso-belica/jusText/blob/main/doc/algorithm.rst", tier: 1, version: "current" },
    { n: 13, title: "Trafilatura evaluation", url: "https://trafilatura.readthedocs.io/en/latest/evaluation.html", tier: 2, version: "current" },
    { n: 14, title: "Template detection across peer pages", url: "https://arxiv.org/abs/1501.02031", tier: 2, version: "2015" },
    { n: 15, title: "Subtitle OCR merge of adjacent texts", url: "https://arxiv.org/abs/2601.00299", tier: 2, version: "2026" },
    { n: 16, title: "Detecting near-duplicates for web crawling (Manku et al., WWW 2007)", url: "https://doi.org/10.1145/1242572.1242592", tier: 1, version: "2007" },
    { n: 17, title: "On the resemblance and containment of documents (Broder 1997)", url: "https://doi.org/10.1109/SEQUEN.1997.666900", tier: 1, version: "1997" },
    { n: 18, title: "UI Automation tree overview", url: "https://learn.microsoft.com/en-us/windows/win32/winauto/uiauto-treeoverview", tier: 1, version: "Win32" },
    { n: 19, title: "SQLite FTS5", url: "https://www.sqlite.org/fts5.html", tier: 1, version: "current" },
    { n: 20, title: "Windows.Media.Ocr OcrWord", url: "https://learn.microsoft.com/en-us/uwp/api/windows.media.ocr.ocrword", tier: 1, version: "WinRT" }
  ]
};
