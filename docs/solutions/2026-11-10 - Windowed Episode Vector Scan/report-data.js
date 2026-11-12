window.REPORT = {
  title: "Windowed Episode Vector Scan",
  date: "2026-11-10",
  depth: "full",
  context: "Windows 11, CPython 3.14, numpy 2.5.4 (OpenBLAS 0.3.34, Haswell kernels), sqlcipher3 (SQLCipher 4.20.0, SQLite 3.54.1)",
  problem: "For a windowed question, the episode vector step (`hybrid.py:276-299` calling `embeddings.top(..., only=allowed)`) passes no time bounds [9]. It streams and decodes every episode vector of the model in history and filters them by Python set membership, although an index `embeddings_time (owner_kind, model, start_ms)` exists. An embedding's `start_ms` is the episode start, and candidates are selected by overlap: `e.start_ms < end AND e.end_ms > start`.",
  goals: [
    "The same scored episodes as today for a windowed question",
    "Cost proportional to the episodes overlapping the window"
  ],
  contract: {
    must: [
      "Identical owner set",
      "Scores within 1e-6 of today's",
      "Order identical, except that exact score ties are broken by (score desc, `start_ms`, `rowid`)",
      "Overlap semantics: `e.start_ms < end AND e.end_ms > start`",
      "Only additive schema changes",
      "Encryption unchanged",
      "No new dependency"
    ],
    fake: [
      "Score differences up to 1e-6, and with them the order of owners whose scores differ by less than that. Today's own float32 scores depend on the 4,000-row chunk boundaries."
    ]
  },
  budget: [
    "≤ 5 ms for the episode vector step on a 1-day window (~50 episodes) with 40,000 episodes in history",
    "Unwindowed no slower than today"
  ],
  constraints: ["SQLCipher-encrypted SQLite, with encryption unchanged", "Only additive schema changes", "No new dependency"],
  nonGoals: [
    "Unwindowed speed beyond no slower than today",
    "Approximate nearest-neighbour indexes",
    "Passage scans and their join order",
    "Legacy segment vectors"
  ],

  answer: {
    text: "**C2 Split overlap predicate** reads the window's own vectors by an `embeddings_time` range `[S, E)` and fetches every candidate that the range did not return (episodes that began before `S`) by primary-key probe. A 1-day window takes 0.089 ms warm p50 and 0.50 ms cold against 5 ms. Today takes 104.5 ms (78–111 ms across runs). Over 1,200 trials it has 0 hard mismatches and 0 tie-order mismatches. The largest score difference is 1.8e-7 against the 1e-6 tolerance. The 3 remaining near-tie order differences are between scores less than 4.8e-8 apart, which the contract allows. Unwindowed takes 150.3 ms against today's 151.1 ms in the same run. Prior art: the pre-filter allow-list followed by an exact flat search matches the Weaviate flat-search cutoff [1], the Qdrant full-scan threshold [2], LanceDB prefiltering [3] and pgvector exact search [4]. No named source was found for splitting the overlap predicate. If the order must also equal today's float32 order at near ties, C3 does that with 0 mismatches and bitwise-equal scores. Its 1-day window costs 0.62 ms warm and 2.03 ms cold, but its chunk-boundary count step grows with history."
  },
  bestFit: "C2",

  metrics: [
    { key: "day_p50_ms", label: "1 day, warm p50", unit: "ms", limit: 5, budget: true, log: true },
    { key: "day_cold_ms", label: "1 day, cold", unit: "ms", limit: 5, log: true },
    { key: "month_p50_ms", label: "1 month, warm p50", unit: "ms", log: true },
    { key: "straddle_p50_ms", label: "1 day inside a 60-day episode, warm p50", unit: "ms", log: true },
    { key: "unwindowed_p50_ms", label: "Unwindowed p50 (today 151–167 in the same runs)", unit: "ms" },
    { key: "max_abs_dscore", label: "Max |Δscore| vs today", limit: 1e-6, log: true },
    { key: "mismatches_hard", label: "Hard mismatches", limit: 0 }
  ],
  chart: { x: "day_p50_ms", y: "day_cold_ms" },

  captureColumns: ["Latency per scenario vs today, 5 ms budget (log)", "|Δscore| vs today, 1e-6 tolerance"],

  figures: [
    {
      src: "fig-shape-dependence.png",
      title: "Float32 score depends on matrix shape",
      caption: "For one query and n = 1..4,000, (M[:n] @ q)[i] is compared with (M[i:i+1] @ q)[0]. 75.3% of 8,002,000 pairs differ bitwise, by up to 6,144 ulp (max |diff| 6.3e-8). The float32 error against float64 stays at most 3.7e-8, and repeated calls on the same matrix are bitwise equal. This is why today's scores depend on the 4,000-row chunk boundaries, and why only C3's replay of those boundaries matches today's order at near ties [7][8]."
    }
  ],

  candidates: [
    {
      id: "C2", name: "Split overlap predicate", status: "best",
      statusNote: "Best fit: the most budget margin, and its cost does not depend on how long the stragglers are.",
      method: "Overlap is split into start-in-window, read through an `embeddings_time` range `[S, E)`, plus every candidate that the range did not return, fetched by primary key through `owner_id IN (SELECT value FROM json_each(?))`. Rows are scored in float64 per row and sorted by (-score, `start_ms`, `rowid`).",
      pipeline: [
        { label: "Range [S, E) on embeddings_time, keep candidates", cost: "window rows only (day: ~50)" },
        { label: "PK probe of missing candidates via json_each", cost: "one row per straggler (day: 5 ids)" },
        { label: "float64 per-row dot", cost: "shape-independent" },
        { label: "lexsort (-score, start_ms, rowid)", cost: "0.089 ms total warm, day" }
      ],
      uses: ["M1 Id-list primary-key probe", "M2 Start-bound range", "M3 json_each IN-subquery"],
      cost: "Day 0.089 ms warm p50 (p95 0.12) and 0.50 ms cold against 5 ms. Month 2.9 / 6.1 ms, straddle 0.10 / 0.52 ms. Unwindowed 150.3 ms against today's 151.1 ms: the full-history fetch is 95 ms through the index, and the rest is Python decoding.",
      keeps: "Identical owner set, 0 hard and 0 tie-order mismatches, scores within 1.8e-7, overlap semantics, no schema change, encryption untouched, no new dependency",
      fakes: "The order of 3 near-tie pairs whose exact scores differ by 1.3e-8 to 4.8e-8. Today's float32 order there is the inexact one.",
      relaxes: null,
      failsWhen: "Month-long or longer windows exceed the 1-day budget (6.1 ms cold for a month). Each long episode that began before `S` costs one random primary-key probe. An `ORDER BY` or a join on the probe makes the planner walk `embeddings_time` over the full history.",
      condition: "Scores equal today's only up to float32 rounding (max 1.8e-7), which the 1e-6 tolerance allows.",
      test: "Today's `top(only=allowed)` on the same disposable SQLCipher database · 1,200 trials · pass: 0 hard mismatches, |Δscore| ≤ 1e-6, ties in (score desc, `start_ms`, `rowid`) order",
      priorArt: { name: "Weaviate flat-search cutoff", url: "https://weaviate.io/blog/speed-up-filtered-vector-search" },
      values: { day_p50_ms: 0.089, day_cold_ms: 0.5009, month_p50_ms: 2.8546, straddle_p50_ms: 0.1018, unwindowed_p50_ms: 150.2558, max_abs_dscore: 1.809e-7, mismatches_hard: 0 },
      estimated: false,
      capture: "C2.png",
      metricsLine: "mismatches=3 max_abs_dscore=1.809e-07 day_p50_ms=0.089 day_p95_ms=0.1227 month_p50_ms=2.855 straddle_p50_ms=0.1018 unwindowed_p50_ms=150.3 today_unwindowed_p50_ms=151.1"
    },
    {
      id: "C3", name: "Gated allow-list probe, bit-exact replay", status: "works",
      statusNote: "Works. The only candidate with 0 mismatches and bitwise-equal scores, but its count step costs in proportion to history.",
      method: "If the question is windowed and has at most K = 9,000 candidates, all candidates are fetched by primary-key probe. Otherwise it runs today's scan. The probe rebuilds today's 4,000-row chunk boundaries among the probed rows with an index-only `count(*)` and `LIMIT 1 OFFSET` lookups. Each group is then scored as today (f16 → f32 matrix @ q), so the scores are bitwise today's. Order is (-score32, `start_ms`, `rowid`), with a float64 exact-tie check on the top limit + 64.",
      pipeline: [
        { label: "Gate: windowed and |allowed| ≤ 9,000", cost: "0" },
        { label: "PK probe of all candidates", cost: "0.67 ms + 2.21 µs·k (window ids)" },
        { label: "count(*) up to first probed key", cost: "0.13–0.94 ms warm at 40k" },
        { label: "LIMIT 1 OFFSET per crossed chunk boundary", cost: "day: 1 lookup" },
        { label: "float32 matrix @ q per chunk group", cost: "today's shapes" },
        { label: "Order + float64 tie check on top 164", cost: "0.62 ms total warm, day" }
      ],
      uses: ["M1 Id-list primary-key probe", "M6 Fitted crossover gate"],
      cost: "Day 0.62 ms warm p50 and 2.03 ms cold against 5 ms. Straddle 1.1 ms warm and 5.5 ms cold (3.9–4.2 ms in three other runs). Month 5.9 / 13.1 ms. Unwindowed 144.4 ms against today's 167.5 ms (scan path).",
      keeps: "Every `must` bitwise: all 62,852 compared scores equal today's, with 0 mismatches of any class",
      fakes: "Nothing measured",
      relaxes: null,
      failsWhen: "More than K = 9,000 candidates falls back to the full scan. The count step grows with history, to an estimated ~9 ms at 10 times the history. The bit-exactness copies today's `CHUNK_ROWS` and scan statement, so it changes when either does.",
      condition: "Above K candidates it falls back to the full scan.",
      test: "Today's `top(only=allowed)` on the same disposable SQLCipher database · 1,200 trials · pass: 0 hard mismatches, |Δscore| ≤ 1e-6, ties in (score desc, `start_ms`, `rowid`) order",
      priorArt: { name: "Qdrant full_scan_threshold", url: "https://qdrant.tech/articles/vector-search-filtering/" },
      values: { day_p50_ms: 0.6152, day_cold_ms: 2.0325, month_p50_ms: 5.8758, straddle_p50_ms: 1.082, unwindowed_p50_ms: 144.3873, max_abs_dscore: 0, mismatches_hard: 0 },
      estimated: false,
      capture: "C3.png",
      metricsLine: "mismatches=0 max_abs_dscore=0 day_p50_ms=0.6152 day_p95_ms=0.8199 month_p50_ms=5.876 straddle_p50_ms=1.082 unwindowed_p50_ms=144.4 today_unwindowed_p50_ms=167.5"
    },
    {
      id: "C1", name: "Candidate-bounded range", status: "relaxes",
      statusNote: "Works if no candidate began more than ~30 days before the window (cold).",
      method: "It runs today's single `embeddings_time` range over `[min candidate start, max candidate start + 1)` instead of the whole history. It keeps rows whose owner is a candidate, scores them in float64 per row and sorts by (-score, `start_ms`, `rowid`).",
      pipeline: [
        { label: "Bounds from candidate starts", cost: "0" },
        { label: "Range on embeddings_time [lo, hi)", cost: "~2.5–3.0 µs per scanned row cold (day: 823 rows)" },
        { label: "Keep candidates, float64 per-row dot", cost: "shape-independent" },
        { label: "lexsort (-score, start_ms, rowid)", cost: "0.59 ms total warm, day" }
      ],
      uses: ["M2 Start-bound range"],
      cost: "Day 0.59 ms warm p50 and 2.1 ms cold against 5 ms. Month 4.7 / 12.0 ms, straddle 1.5 / 4.8 ms. Unwindowed 158.6 ms against today's 161.5 ms.",
      keeps: "Same as C2: 0 hard and 0 tie-order mismatches, scores within 1.8e-7",
      fakes: "The same 3 near-tie pairs as C2",
      relaxes: "Episode length: no candidate may begin more than ~30 days before the window, or the cold step passes 5 ms",
      failsWhen: "One early candidate drags the lower bound back. The day window already scans 823 rows for 50 candidates because one candidate starts 14 days earlier. The cold budget breaks past ~1,700–1,800 scanned rows, while warm holds to ~6,500. Open media and game sessions have no length limit.",
      condition: "Within budget only while the stragglers are short. Episode length is unbounded for open media and game sessions.",
      test: "Today's `top(only=allowed)` on the same disposable SQLCipher database · 1,200 trials · pass: 0 hard mismatches, |Δscore| ≤ 1e-6, ties in (score desc, `start_ms`, `rowid`) order",
      priorArt: null,
      values: { day_p50_ms: 0.5856, day_cold_ms: 2.1143, month_p50_ms: 4.6876, straddle_p50_ms: 1.5053, unwindowed_p50_ms: 158.6078, max_abs_dscore: 1.809e-7, mismatches_hard: 0 },
      estimated: false,
      capture: "C1.png",
      metricsLine: "mismatches=3 max_abs_dscore=1.809e-07 day_p50_ms=0.5856 day_p95_ms=0.8736 month_p50_ms=4.688 straddle_p50_ms=1.505 unwindowed_p50_ms=158.6 today_unwindowed_p50_ms=161.5"
    }
  ],

  method: {
    summary: "C2: split the overlap predicate. Episodes that start inside the window come from an index range, and episodes that started earlier are already known by id from the episode query, so they are fetched by primary key.",
    equation: "overlap(e, [S, E))  <=>  e.start in [S, E)  or  (e.start < S and e.end > S)\nscore(row) = sum_d float64(f16 row[d]) * float64(q[d])\norder = sort by (-score, start_ms, rowid), keep the first 100",
    steps: [
      { text: "Take the candidate ids from today's overlap query (`hybrid.py:199`) unchanged. Only ids are needed, so `_candidates` does not change [9].", assumes: null },
      { text: "Range part: `SELECT rowid, owner_id, start_ms, vec FROM embeddings WHERE owner_kind = 'episode' AND model = ? AND start_ms >= S AND start_ms < E` in 4,000-row chunks. Keep candidate rows of the query's size, and record the candidate owners seen.", assumes: "`embeddings.start_ms == episodes.start_ms` (`embed.py` writes the episode start) [9]. This affects cost only, because a drifted row is still found by the probe." },
      { text: "Straggler part: fetch `candidates − found` with `owner_id IN (SELECT value FROM json_each(?))`. Traced plan: `SEARCH ... USING INDEX sqlite_autoindex_embeddings_1` plus `LIST SUBQUERY`. The day scenario probes 5 ids.", assumes: "No join and no `ORDER BY` on the probe. With `ORDER BY start_ms, rowid`, the planner walks `embeddings_time` over the full history." },
      { text: "Score each row in float64 (f16 → f64, times the f64 query, row sum), so identical vectors score identically wherever they sit.", assumes: "The 1e-6 tolerance. Today's float32 scores differ from these by up to 1.8e-7." },
      { text: "Order with `np.lexsort((rowid, start_ms, -score))[:limit]`, so the order of the two fetches does not matter and a cut through a tie group keeps the smallest keys." },
      { text: "Unwindowed: the range is today's full range. The probe still runs for the ~300 candidates without a vector (~1–2 ms)." }
    ],
    cost: "The range reads only rows whose start lies in the window, and the probe reads one primary-key row per straggler, however far back it began. Day (50 candidates): 0.089 ms warm, 0.50 ms cold. Straddle (60): 0.10 / 0.52 ms. Month (1,660): 2.9 / 6.1 ms. Today reads all 39,700 main-model rows for every window (104.5 ms for a day). Unwindowed: 150.3 ms against 151.1 ms, of which the full-history fetch is 95 ms through the index and the rest is Python decoding.",
    failsWhen: "Month-long or longer windows → 6.1 ms cold, above the 1-day budget. Many long episodes before `S` → one cold random primary-key probe each. A join or `ORDER BY` on the probe → a full-history index walk. Scores within ~5e-8 → order may differ from today's float32 order, which the contract allows.",
    reproduce: "From eval/windowed-episode-vector-scan/: ..\\.venv\\Scripts\\python.exe harness.py C2\\cand.py C2"
  },

  findings: [
    {
      question: "How do vector stores combine a selective filter with exact search?",
      claims: [
        "Weaviate builds an allow-list from the filter and switches to a flat search over it when the list is small relative to the dataset (cutoff around 15% by default) [1].",
        "Qdrant's planner uses the payload index and exact scoring below `full_scan_threshold`, and the vector index otherwise [2].",
        "LanceDB filters before the vector search by default (prefilter) [3].",
        "pgvector returns exact results without an approximate index, and recommends an index on the filter column [4].",
        "The candidates here follow the same pattern: an allow-list from the episode overlap query, then an exact scan of only those rows. C3 adds a fitted crossover gate like Qdrant's [2]."
      ]
    },
    {
      question: "How can an overlap query use an index that only has the start?",
      claims: [
        "An index on `start` alone bounds only `start < E`. Intervals that began before `S` need a bound on the longest interval, or a structure such as an interval tree or HINT that indexes both ends [5].",
        "SQLite's R*Tree supports overlap queries but stores 32-bit float coordinates, which round epoch milliseconds [6].",
        "The repository already has `embeddings_time (owner_kind, model, start_ms)`, writes the episode start into `embeddings.start_ms`, and selects candidates by overlap. Today's `top(..., only=allowed)` passes no time bounds [9].",
        "Splitting the predicate into an index range plus a primary-key probe of ids already known needs neither a length bound nor a new structure. No named source for this form was found."
      ]
    },
    {
      question: "Why are today's float32 scores not reproducible from a different matrix?",
      claims: [
        "NumPy's `matmul` hands a matrix-vector product to BLAS gemv [7]. OpenBLAS's x86-64 `sgemv_t` kernel processes rows in blocks with separate paths for the remainder [8].",
        "Measured here: 75.3% of 8,002,000 (n, i) pairs differ bitwise between `(M[:n] @ q)[i]` and `(M[i:i+1] @ q)[0]`, by up to 6,144 ulp (max |diff| 6.3e-8). Shifting a row by 1–32 positions changes it in 1,621 of 127,472 cases. Repeated calls are deterministic.",
        "Today's scan, compared against itself with a different grouping, therefore gives 14 tie-order mismatches between identical vectors."
      ]
    }
  ],

  mechanisms: [
    { id: "M1", name: "Id-list primary-key probe", core: "Fetch the candidate vectors by the `(owner_kind, owner_id, model)` primary key, either chunked ≤ 500 ids or through a `json_each` subquery.", assumes: "The candidate ids are already known.", breaks: "The list is huge, or the tie order must match today's scan order. Measured with stdlib sqlite3: 0.21 ms for 83 ids against 117 ms for a full scan." },
    { id: "M2", name: "Start-bound range", core: "An `embeddings_time` range from the earliest candidate start (or the window start) to the window end.", assumes: "`embeddings.start_ms == episodes.start_ms`: `embed.py` writes the episode start and nothing updates `episodes.start_ms` [9].", breaks: "A long early candidate drags the lower bound back." },
    { id: "M3", name: "json_each IN-subquery", core: "`owner_id IN (SELECT value FROM json_each(?))`, planned as a list subquery plus primary-key probes.", assumes: "No join and no `ORDER BY` on the statement.", breaks: "Written as a direct join (216 ms) or a temp-table join (80 ms) without `ANALYZE`." },
    { id: "M4", name: "R*Tree interval table", core: "A virtual table indexed on `[start_ms, end_ms]`.", assumes: "An extra virtual table and sync triggers are acceptable.", breaks: "32-bit float coordinates round epoch milliseconds. Judged unnecessary [6]." },
    { id: "M5", name: "Pre-filter vs post-filter planning", core: "Build the allow-list first, then run an exact flat search when it is selective.", assumes: "The filter is cheaper than the vector scan.", breaks: "Unselective filters, where the full scan is cheaper [1][2][3][4]." },
    { id: "M6", name: "Fitted crossover gate", core: "Choose between the probe and the full scan with a fitted cost model of the allow-list size.", assumes: "The fitted cost lines hold on the target machine.", breaks: "Page-cache overflow bends the probe cost upward past ~5,000 ids [2]." }
  ],

  explored: [
    { name: "M4 R*Tree interval table", reason: "It needs an extra virtual table and sync triggers, and its float32 coordinates round millisecond epochs. The split predicate needs no schema change." },
    { name: "Temp-table and direct json_each joins", reason: "Poor plans: 80 ms and 216 ms. The IN-subquery keeps the primary-key plan." },
    { name: "Filter by start_ms inside the window only", reason: "883 hard mismatches: it drops episodes that began before the window." },
    { name: "C1 Candidate-bounded range", reason: "Within budget only while stragglers are short. The cold budget breaks past ~1,700–1,800 scanned rows, which one episode beginning ~30 or more days back is enough to cause, and episode length is unbounded." },
    { name: "C3 first try (float64 scores)", reason: "`mismatches=3 max_abs_dscore=1.809e-07 day_p50_ms=0.0866 month_p50_ms=3.147 straddle_p50_ms=0.0991 unwindowed_p50_ms=132.9`. It has the same 3 near ties as C2, so the final C3 replays today's float32 matrices." },
    { name: "ORDER BY start_ms, rowid on the probe", reason: "The planner then walks `embeddings_time` across the full history." }
  ],

  caveats: [
    "SQLCipher decode costs were reasoned about in parts and not profiled.",
    "Timings were taken on this PC under concurrent load. Today's windowed time ranged from 78 to 111 ms between runs, and today's unwindowed p50 from 151 to 167 ms, so unwindowed comparisons are made within the same run.",
    "Shape dependence was measured with this CPU's OpenBLAS Haswell kernel. The source only weakly supports position dependence in the kernel, and the measurement here confirms it [8].",
    "C2's month window is 6.1 ms cold. The 5 ms budget is stated for a 1-day window only.",
    "C3's bit-exactness copies today's `CHUNK_ROWS` and scan statement, so it changes when either does. Its count step is 0.13–0.94 ms warm at 40,000 episodes, and an estimated ~9 ms at 10 times that history.",
    "C3's K = 9,000 was fitted on this PC: probe 4.06 ms + 10.47 µs·k for random ids and 0.67 ms + 2.21 µs·k for window ids; scan 93.7 ms + 0.06 µs·k. Direct crossovers were measured at ~13,000 random ids and ~25,000 window ids.",
    "The harness uses a synthetic disposable SQLCipher database: 40,000 episodes over 730 days, 1,388 longer than 2 h, 168 lasting 1–60 days, 3 forced 60-day episodes, 1,240 sharing a start_ms, 1,003 groups of identical vectors, shuffled rowids, 1,191 re-embeds, and damaged or other-model rows. It does not contain the user's history."
  ],

  sources: [
    { n: 1, title: "Weaviate, Filtering concepts and \"Speed up filtered vector search\"", url: "https://weaviate.io/blog/speed-up-filtered-vector-search", tier: 1, version: "also https://docs.weaviate.io/weaviate/concepts/filtering; accessed 2026-11-10" },
    { n: 2, title: "Qdrant, Filtering in vector search", url: "https://qdrant.tech/articles/vector-search-filtering/", tier: 1, version: "accessed 2026-11-10" },
    { n: 3, title: "LanceDB, Vector search", url: "https://docs.lancedb.com/search/vector-search", tier: 1, version: "accessed 2026-11-10" },
    { n: 4, title: "pgvector README", url: "https://github.com/pgvector/pgvector/blob/master/README.md", tier: 1, version: "master, accessed 2026-11-10" },
    { n: 5, title: "HINT: A Hierarchical Index for Intervals in Main Memory", url: "https://ar5iv.labs.arxiv.org/html/2104.10939", tier: 1, version: "preprint, arXiv 2104.10939" },
    { n: 6, title: "SQLite R*Tree module", url: "https://www.sqlite.org/rtree.html", tier: 1, version: "current docs" },
    { n: 7, title: "numpy matmul.c.src", url: "https://github.com/numpy/numpy/blob/main/numpy/_core/src/umath/matmul.c.src", tier: 1, version: "main" },
    { n: 8, title: "OpenBLAS kernel/x86_64/sgemv_t_4.c", url: "https://github.com/OpenMathLib/OpenBLAS/blob/develop/kernel/x86_64/sgemv_t_4.c", tier: 1, version: "develop" },
    { n: 9, title: "Akashic source: hybrid.py:197-199 and :276-299, embeddings.py:80-132, migrations/0006_episodes.sql:84-95, pipeline/embed.py:153-158", url: "src/akashic/search/hybrid.py", tier: 1, version: "pre-audit" }
  ]
};
