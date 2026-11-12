window.REPORT = {
  title: "Conversational Time Windows",
  date: "2026-11-10",
  depth: "full",
  context: "Akashic, Python 3.14, sqlcipher3 (SQLite 3.54, FTS5), local gemma-4-12b-it-qat-q4_0 via llama-server b12209, RTX 4090 (test PC)",
  problem: "The rule time resolver misses informal French and English phrasings and night use, and picks the wrong anchor when a question names two times. Retrieval keeps only transcript lines that start inside the window, so a call that runs past the window loses its later lines.",
  goals: [
    "Common phrasings resolve to a window: cet aprèm, ce midi, à midi / at noon, minuit / midnight, tout à l'heure / earlier, this / last weekend, ce week-end",
    "Night use is handled: last night covers the evening, the evening runs past midnight, and before ~05:00 today / tonight / ce soir mean the previous day",
    "Combined anchors pick the window the question is about, and retrieval keeps a call that overlaps the window"
  ],
  contract: {
    must: ["Rule path is deterministic and works without the LLM", "The model may fill, refine or veto a window", "Captured-title list is RAM-only; no persisted learned aliases", "Existing tests pass or each change is listed", "No dependency; local [start, end)"],
    fake: ["Fuzzy part-of-day edges ±30 min"]
  },
  budget: ["≥ 95% of labelled cases covers ∧ tight, model loaded; rules-only no worse than the best rule resolver", "0 future windows", "≤ 1 ms per question on the rule path"],
  nonGoals: ["Absolute dates beyond current support", "Timezones and DST", "Clock-versus-duration misreads (\"a 2h video\")"],

  answer: {
    text: "No candidate reaches **95%** on fresh questions, so there is no best fit. The closest, C9 (model time slots in the existing plan call, merged with the C7 rule resolver), scores **0.931** on 198 fresh questions × 5 times, against 0.190 today, with 0 future windows and about +1.8 s per plan call on a shared GPU. The best rules-only resolver, C7, scores 0.926 with a simulated captured-title gazetteer and 0.884 without it. For retrieval, C5 keeps every line of a call that crosses a correct window (line recall 1.000 vs 0.779). Prior art covers the tables (Duckling [1][3]) but not the after-midnight logical day, informal French or two-anchor choice.",
    relaxed: "C10 (a soft time prior instead of a filter when rules and model disagree) would only count as passing if the metric stopped treating such soft windows as filters; it does nothing for unscored candidates."
  },
  bestFit: null,

  metrics: [
    { key: "pass", label: "Pass, fresh set (model path)", limit: 0.95, better: "higher", budget: true },
    { key: "rules_pass", label: "Pass, rules only", better: "higher" },
    { key: "false_filters", label: "False filters", unit: "cases" },
    { key: "future", label: "Future windows", unit: "cases", limit: 0 },
    { key: "p99_us", label: "Rule p99", unit: "µs", limit: 1000, budget: true },
    { key: "added_s", label: "Added plan-call latency p50", unit: "s" }
  ],
  chart: { x: "p99_us", y: "pass" },

  captureColumns: ["covers ∧ tight by category, rules mode (title shows rules | model)", "future windows and false filters, rules and model"],
  figures: [
    { src: "fig-generalisation.png", title: "Tuned vs fresh question sets", caption: "Pass rate on the tuning set, heldout2 and heldout3. C3, C7 and C8 were tuned on phrases and heldout2, so only heldout3 is fresh for them." },
    { src: "fig-retrieval.png", title: "Retrieval with correct windows (C5)", caption: "14 questions on a synthetic store: a 22:00–01:30 call with a 6 min pause, other calls, a music stream and a video. Both filters return the same 12 out-of-session lines." }
  ],

  candidates: [
    {
      id: "C9", name: "Grounded model time slots on the C7 rules", status: "fails",
      statusNote: "Closest: 0.931 < 0.95 on fresh heldout3.",
      method: "The plan call returns a `when` object (a span copied from the question plus closed-enum day/part/clock/special slots). Each slot is kept only if a cue word inside its span supports it. It is merged with the C7 rule lex (rules win where they lexed the span, the model takes over longer spans or unplaced clocks, titles and stoplist spans veto), and the rule builder makes the window.",
      pipeline: [{ label: "C7 rule lex + title veto", cost: "≤ 0.17 ms" }, { label: "Plan call with `when` slots", cost: "+44 tokens, +1.8 s p50" }, { label: "Grounded merge", cost: "~0.1 ms" }, { label: "Logical-day builder", cost: "µs" }],
      uses: ["M8 Duckling part-of-day ∩ day", "M12 Logical day", "M13 Portable table", "M14 Anchor choice"],
      cost: "Rule p99 ≤ 170 µs; plan call +44 output tokens (129 vs 86), prompt 743 vs 298 (prefix cached); interleaved A/B +1.79 s p50, +3.8 s p95 on a shared GPU",
      keeps: "0 future windows; rules-only fallback 0.926 / 0.884",
      fakes: "Fuzzy edges ±30 min",
      failsWhen: "Time words used as nouns or titles (\"table de nuit\", \"3am challenge\", \"menu du midi\"); the model adds false filters there (40 vs 30); typos; two-anchor questions with no connector (0.68).",
      test: "heldout3 (198 fresh questions × 5 times) · covers ∧ tight · pass ≥ 0.95",
      values: { pass: 0.931, rules_pass: 0.926, false_filters: 40, future: 0, p99_us: 168, added_s: 1.79 },
      capture: "C9.png",
      metricsLine: "rules_pass=0.9262 model_pass=0.9308 future=0/0 false_filter=30/40 cases=1070"
    },
    {
      id: "C7", name: "Closed slot grammar + verified variants + folding + captured-title gazetteer (rules)", status: "fails",
      statusNote: "0.926 with simulated gazetteer, 0.884 without; < 0.95.",
      method: "Slot-ordered FR/EN grammar on top of C3fix. It adds 111 static variants verified by round trip, apocope folding for part-of-day heads only, a governed WE/week-end, \"Xh du matin/soir\", hull modifiers (\"tard\"), structural title rules, and a RAM-only n-gram gazetteer of captured titles.",
      pipeline: [{ label: "Lex + folding", cost: "~60 µs" }, { label: "Gazetteer / title veto", cost: "~10 µs" }, { label: "Clause anchor choice", cost: "µs" }, { label: "Logical-day builder", cost: "µs" }],
      uses: ["M8 Duckling part-of-day ∩ day", "M13 Portable table", "M14 Anchor choice", "M12 Logical day"],
      cost: "p99 137–150 µs",
      failsWhen: "Uncaptured lower-case titles and time nouns; typos; \"sat night\".",
      values: { pass: 0.926, rules_pass: 0.926, false_filters: 30, future: 0, p99_us: 150 },
      capture: "C7.png",
      metricsLine: "gaz set 0.9262 (30 ff) · gaz public 0.8935 (65 ff) · gaz off 0.8841 (80 ff) · future 0"
    },
    {
      id: "C8", name: "Grounded model time slots on C3", status: "fails",
      statusNote: "0.881 on heldout3.",
      method: "The same `when` slots and merge as C9, over the C3fix rules.",
      pipeline: [{ label: "C3fix lex", cost: "≤ 0.13 ms" }, { label: "Plan call with `when`", cost: "+44 tokens" }, { label: "Merge + builder", cost: "~0.1 ms" }],
      uses: ["M8 Duckling part-of-day ∩ day", "M14 Anchor choice"],
      cost: "as C9",
      values: { pass: 0.881, rules_pass: 0.753, false_filters: 40, future: 0, p99_us: 112, added_s: 1.79 },
      capture: "C8.png",
      metricsLine: "rules_pass=0.7533 model_pass=0.8813 false_filter=85/40 future=0"
    },
    {
      id: "C3", name: "Anchor lexer with clause attachment (after one fix round)", status: "fails",
      statusNote: "1.000 on tuned questions, 0.830 / 0.753 on fresh sets.",
      method: "The C1 table plus a clause rule: the asking clause holds the main anchor, otherwise the dislocated head. Tiers: adverbial, noun-attached, subordinate. Structural title rules.",
      pipeline: [{ label: "Lex anchors", cost: "~40 µs" }, { label: "Clause + tier choice", cost: "µs" }, { label: "Builder", cost: "µs" }],
      uses: ["M14 Anchor choice", "M8 Duckling part-of-day ∩ day"],
      cost: "p99 127 µs",
      values: { pass: 0.753, rules_pass: 0.753, false_filters: 85, future: 0, p99_us: 127 },
      capture: "C3.png",
      metricsLine: "rules_pass=0.7533 future=0 false_filter=85"
    },
    {
      id: "C1", name: "Logical-day phrase table + past preference + clip to now", status: "fails",
      statusNote: "0.684 on heldout3.",
      method: "One phrase table on a 05:00 logical day: evening to 02:00, step back if the window would start in the future, clip in-progress windows to now; first anchor only.",
      pipeline: [{ label: "Phrase table", cost: "~60 µs" }, { label: "Builder", cost: "µs" }],
      uses: ["M8 Duckling part-of-day ∩ day", "M12 Logical day", "M13 Portable table"],
      cost: "p99 ~128 µs",
      values: { pass: 0.737, rules_pass: 0.684, false_filters: 110, future: 0, p99_us: 128 },
      capture: "C1.png",
      metricsLine: "rules_pass=0.6841 model_pass(copy)=0.7374 false_filter=110 future=0"
    },
    {
      id: "C5", name: "Session-closure line window", status: "works",
      statusNote: "Retrieval part: line recall 1.000 vs 0.779 with correct windows; needs a correct resolver.",
      method: "Keep lines by overlap (lower bound W.start − 30,016 ms, so the index is still used) and extend the window to voice runs crossing it. Voice-app segments chain while gaps are ≤ 15 min; music and video never chain. The extension is capped at 3 h each side.",
      pipeline: [{ label: "Voice-run walk", cost: "1 SQL" }, { label: "Overlap line query", cost: "1 SQL" }, { label: "Episode query", cost: "1 SQL" }],
      uses: ["M1 Interval overlap", "M5 Midnight event split"],
      cost: "3 statements vs 2; p50 7.3 vs 6.6 ms per Ask",
      failsWhen: "Neighbouring calls on the same app merge without a person filter; continuous voice-app audio pulls in up to the cap; with a wrong-day window it pulls in more of the wrong call.",
      metricsLine: "goldwin recall=1.0 recallwin=1.0 outside=12 eprecall=1.0 · resolver windows recall=0.286 outside=1769"
    },
    {
      id: "C2", name: "Activity-derived day boundary", status: "unbuilt",
      statusNote: "Not built: variant (b) reads closed episodes, so a call in progress looks like sleep.",
      method: "Fit the logical-day cutoff from capture activity.",
      pipeline: [{ label: "Activity scan", cost: "~0.3 ms" }, { label: "C1 builder", cost: "µs" }],
      uses: ["M12 Logical day", "M1 Interval overlap"],
      cost: "~0.5 ms",
      values: { p99_us: 500 }, estimated: true
    },
    {
      id: "C4", name: "Rules-first, lexicon-gated model fallback", status: "unbuilt",
      statusNote: "Not built: today's prompt copies the phrase verbatim, so the fallback adds nothing; superseded by C8/C9.",
      method: "Use the model's time_text only when the rules find nothing.",
      pipeline: [{ label: "Rules", cost: "µs" }, { label: "Gate + resolve(time_text)", cost: "~0.1 ms" }],
      uses: ["M8 Duckling part-of-day ∩ day", "M13 Portable table"],
      cost: "~0.1 ms",
      values: { p99_us: 100 }, estimated: true
    },
    {
      id: "C6", name: "Hard core + soft margin band + labelled rerun", status: "unbuilt",
      statusNote: "Not built: the rank factor has no effect on unscored candidates, and the overview count changes.",
      method: "Hard filter on the window ± m, with a trapezoid rank prior.",
      pipeline: [{ label: "Filter", cost: "SQL" }, { label: "Trapezoid rank", cost: "µs" }],
      uses: ["M6 Trapezoid soft window", "M1 Interval overlap"],
      cost: "~4 float ops per candidate",
      values: { p99_us: 10 }, estimated: true
    },
    {
      id: "C10", name: "Soft window when rules and model disagree", status: "relaxes",
      statusNote: "Works if soft windows on time-free questions do not count as false filters.",
      method: "Hard filter when rules and model agree, otherwise a rank-only time prior.",
      pipeline: [{ label: "C9 merge", cost: "~0.1 ms" }, { label: "Soft prior", cost: "µs" }],
      uses: ["M6 Trapezoid soft window"],
      relaxes: "Budget metric: a soft window on a null-gold question does not count as a false filter",
      cost: "µs",
      values: { p99_us: 170 }, estimated: true
    },
    {
      id: "C11", name: "Learned alias table in the encrypted DB", status: "unbuilt",
      statusNote: "Not built: excluded by the contract (no persisted learned aliases).",
      method: "Promote model-resolved spans into rule aliases after two agreeing observations.",
      pipeline: [{ label: "n-gram lookup", cost: "~20 µs" }],
      uses: ["M13 Portable table"],
      cost: "~20 µs, ~60 KB",
      values: { p99_us: 170 }, estimated: true
    }
  ],

  method: {
    summary: "C9 is the closest candidate, not a best fit. The rules (C7) lex and choose anchors deterministically; the model fills grounded time slots in the existing plan call; every window is built by the same logical-day builder.",
    equation: "L = date(now − 5 h)\nwindow = builder(day, part, clock, special) on L, evening to 02:00, last night = evening + night\nif start ≥ now: step back one occurrence; if in progress: end = now\nmerge: rules win on lexed spans; model wins on longer spans or unplaced clocks; title / stoplist spans veto",
    steps: [
      { text: "C7 lexes the question with a slot grammar, verified variants, part-of-day apocope folding and title vetoes (structural plus a RAM-only captured-title gazetteer), then chooses the anchor by clause and tier.", assumes: "Users' time phrases are surface variants of about 70 concepts." },
      { text: "The plan call returns `when`: a span copied from the question plus enum slots; a slot is kept only if a cue word inside its span supports it.", assumes: "The model recognises slang better than the rules but chooses the main anchor worse (letting its span override the tiers lost 90–98 cases)." },
      { text: "The merge keeps rule slots where the rules lexed the span, fills gaps from the model, and lets the model take over only longer spans or unplaced clocks; no time from the model plus a weak rule anchor means no window." },
      { text: "The logical-day builder makes [start, end) with past preference and clipping, so no window starts in the future." },
      { text: "C5 then keeps every line of a voice run that crosses the window (overlap filter plus run extension, gap ≤ 15 min, cap 3 h)." }
    ],
    cost: "Rule path p99 ≤ 170 µs. Plan call +44 output tokens; interleaved A/B on a shared GPU +1.79 s p50, +3.8 s p95.",
    failsWhen: "Uncaptured lower-case titles and nouns that contain time words; typos in time words; two-anchor questions with no connector.",
    reproduce: "eval/conversational-time-windows/harness.py --phrases heldout3.jsonl --candidate R2slots/candidate.py --name X --no-retrieval (RULES_MODULE=R2rules/candidate.py, CTW_GAZ=set; model cache from R2slots/fill_cache.py)"
  },

  findings: [
    { question: "What do existing resolvers do?", claims: [
      "Duckling FR: soir 18–00, matin 04–12, après-midi 12–19, midi and minuit as points; no aprèm or tout à l'heure [1].",
      "Duckling EN: last night = the 6 h before today; tonight = 18–00 today; weekend = Friday 18:00 to Monday 00:00; \"this\" is predNth 0, so no past preference or after-midnight logic [2][3].",
      "HeidelTime gives symbolic part-of-day values with no hours; French TimeBank strict F1 .85, value F1 .74 [4]; TempEval-3 EN value F1 77.6 [5].",
      "dateparser maps parts of day to points and can prefer past dates for partial dates [6]."
    ]},
    { question: "How do lifelog and IR systems match time to items?", claims: [
      "MyEachtra splits events spanning two days so date filters stay clean [7].",
      "Temporal IR uses decay priors and target-time smoothing, and warns against temporal scoring on non-recency queries [8]; fuzzy interval matching exists in TRank [9].",
      "Wearables assign sleep to days with fixed or sleep-cycle rules, which shift workers break [10]."
    ]}
  ],

  mechanisms: [
    { id: "M1", name: "Interval overlap", core: "Keep iff the item starts before W ends and ends after W starts.", assumes: "Reliable end_ms.", breaks: "Truncated ends; long sessions. [8]" },
    { id: "M5", name: "Midnight event split", core: "Cut events that span two days.", assumes: "Date-granular filters.", breaks: "\"last night\". [7]" },
    { id: "M6", name: "Trapezoid soft window", core: "Rank prior ramping to 0 over a margin.", assumes: "The parsed window is roughly right.", breaks: "Unscored candidates." },
    { id: "M8", name: "Duckling part-of-day ∩ day", core: "Latent part intervals intersected with a day; predNth for this/last.", assumes: "Calendar day.", breaks: "After midnight; French slang. [1][2][3]" },
    { id: "M12", name: "Logical day", core: "L = date(now − 5 h).", assumes: "The user's day ends around 05:00.", breaks: "Users who wake before 05:00." },
    { id: "M13", name: "Portable phrase table", core: "Concept × FR/EN × formal/informal variants.", assumes: "Variants of a closed set of concepts.", breaks: "New slang and typos." },
    { id: "M14", name: "Anchor choice by clause", core: "Asking clause first, otherwise the dislocated head; tiers adverbial > noun-attached > subordinate.", assumes: "Openers mark subordinate clauses.", breaks: "Reduced relatives with no connector." }
  ],

  explored: [
    { name: "C1 Logical-day table", reason: "0.684 on fresh questions; phrase and title gaps." },
    { name: "C2 Activity-derived day boundary", reason: "Variant (b) treats a call in progress as sleep; variant (a) needs stored fitted constants." },
    { name: "C3 Anchor lexer (C3fix)", reason: "0.830 / 0.753 on fresh sets after 1.000 on tuned questions." },
    { name: "C4 Gated model fallback", reason: "Today's prompt makes the model copy the phrase verbatim; the gate is almost always open." },
    { name: "C6 Soft margin band", reason: "No effect on unscored candidates; changes overview counts." },
    { name: "C7 without gazetteer", reason: "0.884; lower-case titles stay false filters." },
    { name: "C8 Model slots on C3", reason: "0.881." },
    { name: "C9 Model slots on C7", reason: "0.931; the model adds title false filters." },
    { name: "C10 Disagreement soft window", reason: "Changes the metric; no effect on unscored candidates." },
    { name: "C11 Learned alias table", reason: "Excluded by the contract." }
  ],
  caveats: [
    "Labels follow a night-owl convention written by agents, not the user: whole past days run 05:00–05:00, and \"hier soir\" before 05:00 is the logical yesterday.",
    "heldout2's failures were published into C7's variant list, so heldout2 is seen data for C7, C8 and C9; heldout3 was written independently and scored once per frozen candidate.",
    "The gazetteer is simulated: \"set\" adds the scored set's time-free titles as if captured, \"public\" uses 411 real titles. Production needs a per-process rebuild keyed on PRAGMA data_version.",
    "Latency was measured on an RTX 4090 while other model servers and Whisper jobs shared the GPU; uncontended latency is not measured (AGENTS.md lists an RTX 4080 Super).",
    "The retrieval fixture has 14 questions on synthetic sessions, so C5's recall is directional.",
    "The C1 family changes 36 existing test expectations, listed with reasons in the C1 prototype folder."
  ],
  sources: [
    { n: 1, title: "Duckling FR time rules", url: "https://raw.githubusercontent.com/facebook/duckling/main/Duckling/Time/FR/Rules.hs", tier: 1, version: "main, 2026-11" },
    { n: 2, title: "Duckling EN time rules", url: "https://raw.githubusercontent.com/facebook/duckling/main/Duckling/Time/EN/Rules.hs", tier: 1, version: "main, 2026-11" },
    { n: 3, title: "Duckling time helpers", url: "https://raw.githubusercontent.com/facebook/duckling/main/Duckling/Time/Helpers.hs", tier: 1, version: "main" },
    { n: 4, title: "Moriceau & Tannier, French resources for HeidelTime", url: "https://aclanthology.org/L14-1382.pdf", tier: 1, version: "LREC 2014" },
    { n: 5, title: "UzZaman et al., TempEval-3", url: "https://aclanthology.org/S13-2001.pdf", tier: 1, version: "2013" },
    { n: 6, title: "dateparser settings", url: "https://dateparser.readthedocs.io/en/latest/settings.html", tier: 1, version: "1.5.x" },
    { n: 7, title: "MyEachtra, LSC'23", url: "https://doras.dcu.ie/29497/1/3592573.3593100.pdf", tier: 1, version: "2023" },
    { n: 8, title: "Campos et al., Survey of temporal information retrieval", url: "https://dias.users.greyc.fr/publications/cs2014.pdf", tier: 1, version: "2014" },
    { n: 9, title: "TRank, SIGIR 2011", url: "https://folk.idi.ntnu.no/noervaag/papers/SIGIR2011TRANK.pdf", tier: 1, version: "2011" },
    { n: 10, title: "Which day does sleep belong to", url: "https://sahha.ai/blog/which-day-does-sleep-belong-to/", tier: 3, version: "vendor blog" },
    { n: 11, title: "src/akashic/search/timeparse.py", url: "src/akashic/search/timeparse.py", tier: 1, version: "local" },
    { n: 12, title: "src/akashic/search/hybrid.py:197-414", url: "src/akashic/search/hybrid.py", tier: 1, version: "local" },
    { n: 13, title: "src/akashic/search/planner.py:190-310", url: "src/akashic/search/planner.py", tier: 1, version: "local" }
  ]
};
