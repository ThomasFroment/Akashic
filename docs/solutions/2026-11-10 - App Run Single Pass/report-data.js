window.REPORT = {
  title: "App Run Single Pass",
  date: "2026-11-10",
  depth: "full",
  context: "Windows 11, CPython 3.14.7, stdlib only",
  problem: "App-run building in `src/akashic/pipeline/episodes.py:383-472` recomputes `max(v.end_ms for v in runs[-1])` for every visit and re-sums the whole run once per distinct process when it picks the dominant process, which is O(V·R). The game-overlap filter checks every visit against every game session, which is O(V·G) [1]. At 50,000 visits and 500 game sessions one pass takes 1,856.9 ms today.",
  goals: [
    "The same runs, title, dominant process, URL and body as today on any input",
    "O(V) per pass (plus the sort), including the game-overlap filter"
  ],
  contract: {
    must: [
      "Byte-identical episode rows (`kind`, `ref`, `start`, `end`, `title`, `app`, `url`, `body`, `items`) for any input, including the tie-breaking of the dominant process and the URL",
      "Stdlib only",
      "Today's dominant-process tie follows hash-randomized set order, so identity is checked within one interpreter across `PYTHONHASHSEED` 0, 1, 2 and random [10]"
    ],
    fake: ["Nothing"]
  },
  budget: [
    "≤ 20 ms per pass at V = 50,000 visits and G = 500 game sessions, compute only (`add_episode` replaced by a stub, writes excluded)"
  ],
  constraints: ["Python standard library only, no new dependency"],
  nonGoals: [
    "The run rules themselves (gap, minimum lengths, browser set)",
    "The `MAX(item_id)` watermark and the rebuild after expiry",
    "The SQL that loads the visits"
  ],

  answer: {
    text: "**C1 Two-phase index-range runs + suffix-min game pointer** gives byte-identical rows on all 10,002 cases under each of the four hash seeds (0 mismatches) and runs a realistic 50k-visit pass in 15.67 ms against the 20 ms budget. Today takes 1,856.9 ms on the same input. On the synthetic fragmented shape (18,207 runs, 4,014 episodes) it takes 20.95 ms with the harness's recording stub, 0.95 ms over, and 17.8 ms with a do-nothing stub. Time is linear in V (ratio 2.03 from 50k to 100k) and nearly flat in G (14.92 / 15.67 / 17.62 ms at G = 50 / 500 / 5000). Prior art: the running-max-end split is the packing-intervals pattern [6] and ActivityWatch's heartbeat merge [3]; per-key duration sums match ActivityWatch's `merge_events_by_keys` [4]; overlap tests by binary search over sorted ends follow BITS [7]. No named source was found for the suffix-min existence test or the lazy accumulator start. No candidate relaxes the contract to beat it."
  },
  bestFit: "C1",

  metrics: [
    { key: "realistic_p50_ms", label: "Realistic p50, V=50k G=500 (today 1,856.9)", unit: "ms", limit: 20, budget: true, log: true },
    { key: "fragmented_p50_ms", label: "Fragmented p50, V=50k G=500 (today 1,727.0)", unit: "ms", limit: 20, log: true },
    { key: "fragmented_nostub_ms", label: "Fragmented, do-nothing stub", unit: "ms", limit: 20, log: true },
    { key: "mismatches", label: "Mismatches, 4 seeds × 10,002 cases", limit: 0 },
    { key: "v_scaling_ratio", label: "Time ratio V=100k / V=50k (linear ≈ 2; today 2.19)" }
  ],
  chart: { x: "realistic_p50_ms", y: "fragmented_p50_ms" },

  captureColumns: ["p50 per shape vs today and 20 ms (log)", "Time vs V and vs G (log-log)"],

  figures: [
    {
      src: "fig-scaling-overlay.png",
      title: "Scaling in V and G, today against C1 and C2",
      caption: "Quiet-machine p50 per pass on the realistic shape. Both candidates double with V like today, but stay within 15–19 ms from G = 50 to 5000, where today grows from 322 ms to about 11,740 ms. Today's G = 5000 point was measured at V = 12,500 and multiplied by 4 (hollow marker)."
    }
  ],

  candidates: [
    {
      id: "C1", name: "Two-phase index-range runs + suffix-min game pointer", status: "best",
      statusNote: "Best fit: the most margin. Meets the budget on the realistic shape; the fragmented shape is 20.95 ms with the recording stub and 17.8 ms with a do-nothing stub.",
      method: "Scan 1 walks the raw row tuples once. It drops a visit when some game overlaps it (games sorted by end, suffix minimum of their starts, a forward pointer keyed on the visit start), splits runs by kind and running max end, applies the minimum-length rule, and stops at the first run whose end + 300 s passes the horizon. Runs are kept as index ranges. Scan 2 visits only the emitted runs and derives the titles, dominant process and URL with today's tie-breaking.",
      pipeline: [
        { label: "Sort games by end, suffix-min of starts", cost: "O(G log G)" },
        { label: "Scan 1: game test + run split + min length + horizon stop", cost: "~7.5 ms fragmented (game test ~2.5)" },
        { label: "Scan 2: titles, per-process sums, URL per emitted run", cost: "~2.8 ms fragmented" },
        { label: "items list per episode", cost: "~0.6 ms fragmented" },
        { label: "Per-episode emit (4,014 episodes) incl. recorder stub", cost: "~10 ms fragmented by subtraction, ~5 ms of it the stub" }
      ],
      uses: ["M1 Running-max-end gap sessionization", "M3 Suffix-min overlap test", "M4 Merge-then-two-pointer", "M2 Per-key accumulators with first-seen tie order", "M5 First-wins argmax over insertion order", "M6 Best-URL single pass"],
      cost: "Realistic 15.67 ms and fragmented 20.95 ms against 20 ms (quiet machine). Under concurrent load: realistic 16–18 ms, fragmented 22.8–25.4 ms. With a do-nothing stub: realistic 16.2 ms, fragmented 17.8 ms. V 25k/50k/100k: 8.13 / 15.67 / 31.76 ms. G 50/500/5000: 14.92 / 15.67 / 17.62 ms.",
      keeps: "Byte-identical rows and return value on every fuzz case under all four hash seeds, including dominant-process and URL ties, REAL durations, ±inf and NaN (inf − inf) durations",
      fakes: "Nothing",
      relaxes: null,
      failsWhen: "Many short runs make per-episode overhead dominate: the fragmented shape is 0.95 ms over budget with the recording stub. Rows out of start order cost one `bisect_right` each to reposition the game pointer; the harness does not include unsorted rows because the SQL orders by `start_ms, id`.",
      condition: "Exact only when per-process sums use builtin `sum()` over lists (compensated float summation since 3.12) and the URL scan starts from `None` [8].",
      test: "today's code in the same interpreter · `repr` of recorded `add_episode` kwargs + return value · pass = 0 mismatches",
      priorArt: { name: "Ben-Gan packing intervals", url: "https://itprotoday.com/sql-server/new-solution-packing-intervals-problem" },
      values: { realistic_p50_ms: 15.67, fragmented_p50_ms: 20.95, fragmented_nostub_ms: 17.8, mismatches: 0, v_scaling_ratio: 2.03 },
      estimated: false,
      capture: "C1.png",
      metricsLine: "mismatches_by_seed=0:0,1:0,2:0,random:0 cases_per_seed=10002 realistic_p50_ms=15.67 fragmented_p50_ms=20.95 budget_ms=20 V25k/50k/100k_ms=8.13/15.67/31.76 ratio_V50/25=1.93 ratio_V100/50=2.03 G50/500/5000_ms=14.92/15.67/17.62"
    },
    {
      id: "C2", name: "Fused single pass with threshold-triggered accumulation", status: "works",
      statusNote: "Works on the realistic shape (16.11 ms). The fragmented shape is over budget at 25.91 ms, from the per-episode emit (~2.4 µs) plus the recorder stub (~1.4 µs) per episode.",
      method: "One loop does the same game filter, run split and horizon stop. A run gets accumulators only when its span first reaches its kind's minimum (spans only grow), its earlier visits are folded once, later visits are folded as they arrive, and the episode is emitted when the run closes. An open run at the horizon is discarded.",
      pipeline: [
        { label: "Sort games by end, suffix-min of starts", cost: "O(G log G)" },
        { label: "One loop: game test, run split, horizon stop", cost: "~12 ms loop + fold on both shapes" },
        { label: "Backfill once at the minimum span, then fold per visit", cost: "~150 ns per folded visit" },
        { label: "Emit at close", cost: "~2.4 µs per episode (9.6 ms fragmented)" }
      ],
      uses: ["M1 Running-max-end gap sessionization", "M3 Suffix-min overlap test", "M4 Merge-then-two-pointer", "M2 Per-key accumulators with first-seen tie order", "M5 First-wins argmax over insertion order", "M6 Best-URL single pass"],
      cost: "Realistic 16.11 ms and fragmented 25.91 ms against 20 ms (quiet machine). V 25k/50k/100k: 8.60 / 16.11 / 32.57 ms. G 50/500/5000: 15.14 / 16.11 / 18.59 ms. An open 40k-visit run at the horizon (10k realistic prefix): 13.23 ms, of which ~6 ms is folding that is then discarded; the same rows closed and emitted: 14.76 ms.",
      keeps: "Byte-identical rows on every fuzz case under all four hash seeds",
      fakes: "Nothing",
      relaxes: null,
      failsWhen: "Many short runs: fragmented 25.91 ms. A long run still open at the horizon is folded and then discarded (~6 ms per 40k visits). Rows are unpacked as exactly 6 columns, so a row of another width raises where today's indexing would not.",
      condition: "Needed two fixes: the game pointer is bounded by G (a visit at +inf raised `IndexError` on 46 of 3,000 real cases), and a NaN process total triggers today's literal URL sort.",
      test: "today's code in the same interpreter · `repr` of recorded `add_episode` kwargs + return value · pass = 0 mismatches",
      priorArt: { name: "ActivityWatch heartbeat_merge", url: "https://raw.githubusercontent.com/ActivityWatch/aw-core/master/aw_transform/heartbeats.py" },
      values: { realistic_p50_ms: 16.11, fragmented_p50_ms: 25.91, mismatches: 0, v_scaling_ratio: 2.02 },
      estimated: false,
      capture: "C2.png",
      metricsLine: "mismatches_by_seed=0:0,1:0,2:0,random:0 cases_per_seed=10002 realistic_p50_ms=16.11 fragmented_p50_ms=25.91 budget_ms=20 V25k/50k/100k_ms=8.60/16.11/32.57 G50/500/5000_ms=15.14/16.11/18.59"
    }
  ],

  method: {
    summary: "C1: one scan that filters, splits and closes runs as index ranges, then one scan over only the emitted runs that reproduces today's titles, dominant process and URL, ties included.",
    equation: "drop(v)   <=>  exists g: g.start < v.end and g.end > v.start\n          <=>  suf[j] < v.end,   games sorted by end,\n               j = #{g : g.end <= v.start},  suf[k] = min(g.start for g in games[k:]),  suf[G] = +inf\nnew run   <=>  kind(v) != kind(run)  or  v.start - max_end(run) > 300,000\nstop      <=>  max_end(run) + 300,000 > horizon    (first such run; later runs are newer)\nemit      <=>  max_end(run) - start(run) >= 180,000 (browsing) | 300,000 (work)\nprocess   =   max({p in first-seen order}, key = sum(list of durations of p))\nurl       =   first visit with a truthy url and d > best (strict), best starting from None",
    steps: [
      { text: "Sort the games by end and build the suffix minimum of their starts. Walk the visits in start order with a pointer `j` that counts games ending at or before the visit start; drop the visit when `suf[j] < v.end`. This is exactly today's strict test `g.start < v.end and g.end > v.start` for any game set, inverted and zero-length games included [7].", assumes: "Visits arrive in start order, as `ORDER BY start_ms, id` returns them. Out-of-order rows re-seat the pointer with `bisect_right`, so they cost O(log G) each but stay correct." },
      { text: "Split runs on the kind (browser set or not) and the gap from the running max end, updating the max only on a strict `>` so the first of equal ends is kept, as builtin `max()` does [6][8].", assumes: "Same input order as today; the gap is measured from the running max end, not from the last visit's end." },
      { text: "Apply the minimum length while scanning and stop at the first run whose end + 300 s passes the horizon, which equals today's `break`. Keep each emitted run as an index range `(i0, i1)` into the kept rows." },
      { text: "For each emitted run, sum the title time with the 1,000 ms floor in a first-seen dict and take the stable `sorted(reverse=True)[:8]`, the same as today (equivalent to `heapq.nlargest`) [9]." },
      { text: "Integer fast path: when the run's running total stays an `int`, per-process `+=` sums are exact and the URL is the first truthy URL with a strictly larger duration, starting from `None`.", assumes: "Integer durations, as capture writes them (`pipeline/sessions.py` copies `frames.ts_ms`)." },
      { text: "Any float duration (REAL columns can hold fractions, ±inf and values beyond int64 because `0002_sessions.sql` uses non-STRICT INTEGER affinity [2]) switches to per-process lists summed with builtin `sum()`, which has used compensated float summation since Python 3.12 [8]. A NaN duration (inf − inf) falls back to today's literal stable sort for the URL.", assumes: "Same interpreter version as today's code; a plain running `+=` gives a different dominant process on cancelling REAL durations." },
      { text: "Pick the dominant process with `max` over a set built from the distinct processes in first-seen order. Repeated inserts are no-ops, so the set table and its iteration order equal today's comprehension, and `max` keeps the same first maximum on ties [5][8][10].", assumes: "Same interpreter and hash seed as the code it is compared with: the tie result itself varies with `PYTHONHASHSEED`." },
      { text: "Memoize `app_label` per process for the pass and call `add_episode` with the same kwargs and the same `items` list as today." }
    ],
    cost: "Games: O(G log G) sort plus O(G) suffix minimum. Scan 1: one pointer step or one comparison per visit, O(V + G). Scan 2: O(length of emitted runs) ≤ V, plus a sort of each run's distinct titles. Measured on the fragmented shape (V = 50k, 18,207 runs, 4,014 episodes): scan 1 ~7.5 ms (game test ~2.5 ms), scan 2 per-visit loop ~2.8 ms, `items` ~0.6 ms, and ~10 ms by subtraction of per-episode overhead, about half of it the recorder stub. Realistic shape (531 runs, 492 episodes): 15.67 ms against 1,856.9 ms today, about 118× less.",
    failsWhen: "Many short runs → per-episode overhead dominates: 20.95 ms on the fragmented shape with the recording stub, 0.95 ms over. V well above 50k → time grows linearly (31.76 ms at 100k). Game bounds made inclusive → the suffix-min test no longer matches (the `<=` mutation gives 733 mismatches). Restart with a different hash seed → today's own tie result changes, so byte identity holds only within one interpreter.",
    reproduce: "From `eval/app-run-single-pass/`: `..\\.venv\\Scripts\\python.exe harness.py C1\\cand.py C1_quiet`"
  },

  findings: [
    {
      question: "How do existing tools split activity into sessions?",
      claims: [
        "ActivityWatch's `heartbeat_merge` extends the previous event when the next heartbeat carries the same data and arrives within `pulsetime` of its end [3].",
        "Google Analytics 4 ends a session after a period of inactivity, 30 minutes by default [5].",
        "Itzik Ben-Gan's packing-intervals solution finds group starts by comparing each start with the running maximum of earlier ends, which is the rule C1 and C2 use for runs (M1) [6]."
      ]
    },
    {
      question: "How are per-key durations and overlap counts computed in one pass?",
      claims: [
        "ActivityWatch's `merge_events_by_keys` sums event durations per key value into one event per key [4].",
        "BITS, as implemented in rust-lapper's `count()`, counts intervals overlapping a query with two binary searches over sorted starts and sorted ends [7]. C1 and C2 only need existence, so one suffix minimum over games sorted by end replaces the count (no named source found)."
      ]
    },
    {
      question: "Which Python semantics fix today's tie results?",
      claims: [
        "`max()` returns the first maximal item it meets, and `sorted()` is stable, also with `reverse=True` [8].",
        "`sum()` of floats has used compensated summation since Python 3.12, so a running `+=` can differ from today's generator sum [8].",
        "`heapq.nlargest(n, it, key)` is equivalent to `sorted(it, key=key, reverse=True)[:n]` [9].",
        "`str` hashes are salted per process unless `PYTHONHASHSEED` is set, so set iteration order, and with it today's dominant-process tie, can change between runs [10]."
      ]
    },
    {
      question: "What values can the stored visit times hold?",
      claims: [
        "Today's code recomputes the run's max end per visit, re-sums the run per distinct process, and tests every visit against every game [1].",
        "`app_sessions.start_ms` and `end_ms` are `INTEGER NOT NULL` in a non-STRICT table, so REAL values (fractions, ±inf, values beyond int64) can be stored; NaN becomes NULL and is rejected, but inf − inf durations are NaN inside the code [2]."
      ]
    }
  ],

  mechanisms: [
    { id: "M1", name: "Running-max-end gap sessionization", core: "A visit starts a new run when its start minus the running max end exceeds the gap.", assumes: "Input sorted by start; gap measured from the running max end.", breaks: "Unsorted input, or a gap measured from the last visit's end. [3][5][6]" },
    { id: "M2", name: "Per-key accumulators with first-seen tie order", core: "Dict accumulators keep first-seen key order; `heapq.nlargest` ≡ `sorted(reverse=True)[:n]`.", assumes: "Ties must follow first-seen order.", breaks: "Ties must follow another order. [4][9]" },
    { id: "M3", name: "Suffix-min overlap test", core: "Games sorted by end, suffix minimum of starts, pointer or bisect: a game overlaps iff `suf[j] < v.end`. Exact for the strict predicate `g.s < v.e and g.e > v.s` on any input, O(G log G + V).", assumes: "Strict bounds.", breaks: "Inclusive bounds (the `<=` mutation gives 733 mismatches). [7]" },
    { id: "M4", name: "Merge-then-two-pointer", core: "Advance a pointer over games sorted by end as visit starts grow.", assumes: "Pointer keyed on the visit start; it skips games whose end is at or before that start.", breaks: "Merged on `<=`, or pointer keyed on the visit end." },
    { id: "M5", name: "First-wins argmax over insertion order", core: "`max()` keeps the first maximum in iteration order; a set of `str` iterates in hash order.", assumes: "Same interpreter and hash seed as the reference.", breaks: "Comparing across processes with different `PYTHONHASHSEED`. [8][10]" },
    { id: "M6", name: "Best-URL single pass", core: "First truthy URL with strictly larger duration, starting from `None` = first truthy URL of a stable reverse sort.", assumes: "Durations totally ordered (no NaN).", breaks: "Starting from −inf (the only URL may have duration −inf), or NaN durations." }
  ],

  explored: [
    { name: "Running `+=` per-process sums", reason: "Wrong on REAL data: a.exe (0, 1e12), (1, 1.0001), b.exe (2, 2.00011), a.exe (1e12, 0) picks a different dominant process than today's compensated `sum()`. The mutation gives 11–12 mismatches per seed." },
    { name: "URL scan starting from −inf", reason: "Wrong when the only URL has duration −inf: 8 mismatches." },
    { name: "Deterministic tie rules (first-seen or sorted process)", reason: "Change today's tie result: 343–546 mismatches per seed, so they would relax the contract." },
    { name: "Inclusive (`<=`) game test", reason: "Mutation self-check: 733 mismatches." },
    { name: "Column-wise builtin unrolling of scan 2", reason: "Slower than the per-row loop." },
    { name: "`try`/`except KeyError` dict updates", reason: "Slower on the fragmented shape, where most lookups miss." },
    { name: "C2 Fused single pass", reason: "Not chosen: exact and within budget on the realistic shape, but 25.91 ms on the fragmented shape against C1's 20.95 ms, and it folds then discards a long run still open at the horizon." }
  ],

  caveats: [
    "Today's dominant-process tie depends on `PYTHONHASHSEED` (a code.exe vs notepad.exe tie gives code.exe under seed 3 and notepad.exe under the others), so it is not reproducible across restarts and byte identity holds only within one interpreter [10].",
    "REAL and ±inf values only arrive through non-capture writers or edited data; capture writes integers (`pipeline/sessions.py` copies `frames.ts_ms`).",
    "The fragmented shape (18,207 runs per 50k visits) is synthetic and far more fragmented than real app sessions.",
    "The recorder stub builds a tuple and a key set per call (~5 ms on the fragmented shape), and that cost is inside every measured number.",
    "First-round timings were taken under heavy concurrent load (±15%); the verdict uses the quiet re-timing.",
    "Unsorted visit rows are not in the main harness, since the SQL orders by `start_ms, id`. C2's own check on 10,000 shuffled-row cases gave 0 mismatches; C1 was not run on shuffled rows.",
    "Timings are compute only: `add_episode` is a stub and database writes are excluded.",
    "Today's G = 5000 point is extrapolated from V = 12,500 × 4."
  ],

  sources: [
    { n: 1, title: "episodes.py (build_app_runs)", url: "src/akashic/pipeline/episodes.py:373-472", tier: 1, version: "pre-audit" },
    { n: 2, title: "0002_sessions.sql (app_sessions)", url: "src/akashic/storage/migrations/0002_sessions.sql:64-74", tier: 1, version: "pre-audit" },
    { n: 3, title: "ActivityWatch heartbeats.py", url: "https://raw.githubusercontent.com/ActivityWatch/aw-core/master/aw_transform/heartbeats.py", tier: 1, version: "master, 2026-11-10" },
    { n: 4, title: "ActivityWatch merge_events_by_keys.py", url: "https://raw.githubusercontent.com/ActivityWatch/aw-core/master/aw_transform/merge_events_by_keys.py", tier: 1, version: "master, 2026-11-10" },
    { n: 5, title: "GA4 session timeout", url: "https://support.google.com/analytics/answer/9191807", tier: 1, version: "2026-11-10" },
    { n: 6, title: "Ben-Gan, New solution to the packing intervals problem", url: "https://itprotoday.com/sql-server/new-solution-packing-intervals-problem", tier: 2, version: "article" },
    { n: 7, title: "rust-lapper count() (BITS)", url: "https://docs.rs/rust-lapper/1.3.0/src/rust_lapper/lib.rs.html", tier: 1, version: "1.3.0" },
    { n: 8, title: "Python built-in functions (max, sorted, sum 3.12 change)", url: "https://docs.python.org/3/library/functions.html", tier: 1, version: "Python 3" },
    { n: 9, title: "Python heapq", url: "https://docs.python.org/3/library/heapq.html", tier: 1, version: "Python 3" },
    { n: 10, title: "Python data model, __hash__ and PYTHONHASHSEED", url: "https://docs.python.org/3/reference/datamodel.html#object.__hash__", tier: 1, version: "Python 3" }
  ]
};
