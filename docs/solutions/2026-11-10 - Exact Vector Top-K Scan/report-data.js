window.REPORT = {
  title: "Exact Vector Top-K Scan",
  date: "2026-11-10",
  depth: "full",
  context: "Windows 11, CPython 3.14, numpy 2.5.4 (OpenBLAS 0.3.34; wheel baseline X86_V2, no F16C dispatch for the float16 cast), sqlcipher3 (SQLCipher 4.20.0, SQLite 3.54.1)",
  problem: "Exact top-k vector search over encrypted SQLite rows spends most of its per-row time on Python object work: a bytes copy and an `np.frombuffer` per row, a `Match`/`Scored` object per row and a full sort per 4,000-row chunk, in `passages.py:96-147` (max-passage per owner with char offsets) [1] and `embeddings.py:80-132` (one vector per owner) [2].",
  goals: [
    "Exactly the same top-k results as today",
    "Per-row cost dominated by SQLCipher decode rather than Python object work",
    "Memory bounded by the chunk size"
  ],
  contract: {
    must: [
      "Same owners in the same order (ties: passages by `owner_id`; embeddings by scan order, as today)",
      "Max-passage per owner with its `start_char`/`end_char`",
      "float16 storage unchanged",
      "time, `only`, `exclude`, `apps` and vector-size filters unchanged",
      "No new dependency; encryption unchanged"
    ],
    fake: [
      "Score differences up to 1e-6",
      "Queries where a NaN vector is in scope: reported separately, since today's Python sort with a NaN key is undefined there; dropping non-finite-score rows is accepted"
    ]
  },
  budget: [
    "≤ 0.5 µs (500 ns) per scanned row of Python work after rows arrive from the cursor, at 100k and 1M rows (cursor fetch floor and SQLCipher decode measured and reported separately, not counted)",
    "≤ 64 MB extra peak memory"
  ],
  constraints: [
    "Rows arrive through the sqlite3 cursor of sqlcipher3: one Python tuple and one bytes object per row are paid before any candidate code runs (0.35–0.5 µs/row) [8]",
    "Stock numpy wheel: baseline X86_V2, so the float16 → float32 cast runs a scalar loop even on an F16C/AVX-512 CPU [4][5]"
  ],
  nonGoals: ["ANN indexes", "GPU search", "Storage-format change", "Identical-text dedupe", "SQL join order for time windows"],

  answer: {
    text: "Best fit: **C3 Columnar chunk + run-level selection**. It returns today's owners, order and char offsets on every query without a NaN vector in scope (0 mismatches over 612 queries), with float32 scores bit-identical to today's. Its after-fetch cost is 252–431 ns/row across the 10 timed scenarios against 500 (today 865–2025 ns/row), and its peak extra memory is 9.1 MB against 64 MB. The remaining cost is 15–25% of the SQLCipher decode cost (1.3–2.2 µs/row), so decode now dominates. The 5 differences from today are all NaN-scope queries at 100k, where today's own sort is corrupted by the NaN key. Prior art: the float16 shift-and-rebias is Fabian Giesen's \"half to float done quic\" [13]; no prior art found for folding the 2^112 factor into the query or for the columnar pipeline as a whole. C1 shares the pipeline and is within measurement noise of C3; its one scenario over budget (588 ns/row) measured 310 ns/row in an earlier run of the same code."
  },
  bestFit: "C3",

  metrics: [
    { key: "passages_worst_ns", label: "Passages worst after-fetch", unit: "ns/row", limit: 500, budget: true },
    { key: "embeddings_worst_ns", label: "Embeddings worst after-fetch", unit: "ns/row", limit: 500, budget: true },
    { key: "scenarios_within", label: "Scenarios within 500 ns/row", unit: "of 10", limit: 10, better: "higher" },
    { key: "mismatches", label: "Mismatches (non-NaN scope)", limit: 0 },
    { key: "peak_MB", label: "Peak extra memory", unit: "MB", limit: 64, budget: true }
  ],
  chart: { x: "passages_worst_ns", y: "embeddings_worst_ns" },

  captureColumns: ["Per-row cost: decode, cursor objects, after-fetch, vs today and budget", "|Δscore| vs today per returned owner"],

  figures: [
    {
      src: "fig-after-fetch.png",
      title: "After-fetch cost per scenario",
      caption: "Python work after the cursor, ns per scanned row, median of 9 interleaved runs, for today and the three built candidates over the 5 unwindowed scenarios at 100k and 1M rows. Measured while other harnesses loaded the machine: run-to-run noise is about ±100–150 ns/row, so the ranking rests on relative differences."
    }
  ],

  candidates: [
    {
      id: "C3", name: "Columnar chunk + run-level selection", status: "best",
      statusNote: "All 10 scenarios within 500 ns/row (worst 431).",
      method: "Per `fetchmany(4000)` chunk: columns by `itemgetter`, `only`/`exclude` as one bool lookup table over owner ids (ints in [0, 2^24), else today's sets), rows masked before one `b\"\".join`, float16 bits rebiased into a reused float32 buffer and scored against `q · 2^112` with today's matrix shape. Passages: rows below the retained limit-th score dropped, then per owner run `maximum.reduceat` plus first-argmax by `minimum.reduceat`, with a `lexsort` fallback when an owner has two runs in the chunk; tie-safe partition for the chunk top-k. Embeddings: rows with `s > T` survive, stable merge with the retained set first. Objects only for winners.",
      pipeline: [
        { label: "fetchmany(4000)", cost: "cursor floor, not counted" },
        { label: "Columns (itemgetter)", cost: "~22 ns/row" },
        { label: "Length check + owner ids", cost: "12 + 9 ns/row" },
        { label: "Bool-table filter", cost: "~2 ns/row" },
        { label: "b\"\".join kept blobs", cost: "~57 ns/row" },
        { label: "inf/NaN check", cost: "~7 ns/row" },
        { label: "f16 → f32 rebias", cost: "~155 ns/row" },
        { label: "gemv vs q·2^112", cost: "12–17 ns/row" },
        { label: "Run-level selection", cost: "5–20 ns/row" }
      ],
      uses: ["M1 Chunk join + single frombuffer", "M2 Threshold-pruned top-k", "M3 Sorted-run group max", "M4 Lexsort first-per-owner", "M6 Tie-safe partition", "M11 Vectorized owner filter", "M12 Integer f16 rebias", "M13 Position-dependent sgemv"],
      cost: "Stage profile ~280–300 ns/row; full harness 252–431 ns/row after fetch against 500. Peak 9.1 MB against 64 MB (4 MB float32 buffer + 2 MB blob + chunk objects; bool table ≤ 16 MB by cap, 1 MB at 1M).",
      keeps: "Owners, order, char offsets and bit-identical float32 scores on every non-NaN query; the four filters; float16 storage; encryption",
      fakes: "NaN-scope queries: NaN-score rows are dropped instead of corrupting the sort",
      relaxes: null,
      failsWhen: "A chunk holding an inf/NaN half, a non-float32 query or `|q| ≥ 2^15` uses numpy's cast (~+95 ns/row). Interleaved owner runs take the `lexsort` fallback. DAZ/FTZ in the BLAS threads would flush the float32 subnormals that small halves become; C3 as built lacks C1's self-test.",
      test: "Today's functions on the same disposable SQLCipher DBs · owner order, offsets, |Δscore| · pass: 0 mismatches outside NaN scope, ≤ 500 ns/row, ≤ 64 MB",
      priorArt: { name: "Fabian Giesen, half to float done quic", url: "https://fgiesen.wordpress.com/2012/03/28/half-to-float-done-quic/" },
      values: { passages_worst_ns: 403, embeddings_worst_ns: 431, scenarios_within: 10, mismatches: 0, peak_MB: 9.1 },
      estimated: false,
      capture: "C3.png",
      metricsLine: "mismatches=0 hard=5 tie_order=0 near_tie=0 today_noise=0 errors=0 type_errors=0 nan_scope_mismatches=5 max_abs_dscore=6.07e-08 passages_after_fetch_ns_100k=403 embeddings_after_fetch_ns_100k=360 passages_after_fetch_ns_1m=401 embeddings_after_fetch_ns_1m=431 peak_MB=9.1"
    },
    {
      id: "C1", name: "Columnar chunk with integer f16 rebias", status: "works",
      statusNote: "9 of 10 scenarios within 500 ns/row; the 588 is a 40k-row scenario that measured 310 in round 1 with unchanged code.",
      method: "Same columnar chunk as C3, with the kept blobs written into one reused `io.BytesIO` (`writelines`), the length and owner mask built before the join, and the rebias into a reused int32 buffer (`copyto`, `<< 13`, `& int32(-0x70000001)`) viewed as float32. A DAZ self-test runs once per process plus a tiny per-call check; inf/NaN chunks, overflowing or non-float32 queries fall back to numpy's cast. Top-k: embeddings `s > T` with stable `argsort` (retained first); passages `s ≥ T`, `lexsort((index, -s, owner))` first per owner, then `lexsort((owner, -s))[:limit]`.",
      pipeline: [
        { label: "fetchmany(4000)", cost: "cursor floor, not counted" },
        { label: "Columns + mask", cost: "as C3" },
        { label: "BytesIO writelines", cost: "~12 ns/row" },
        { label: "inf/NaN check", cost: "~7 ns/row" },
        { label: "f16 → f32 rebias", cost: "~155 ns/row" },
        { label: "gemv vs q·2^112", cost: "12–17 ns/row" },
        { label: "Survivor lexsort", cost: "10–22 ns/row (plateau bench)" }
      ],
      uses: ["M1 Chunk join + single frombuffer", "M2 Threshold-pruned top-k", "M4 Lexsort first-per-owner", "M11 Vectorized owner filter", "M12 Integer f16 rebias", "M13 Position-dependent sgemv"],
      cost: "209–588 ns/row after fetch against 500 (9 of 10 within). Peak 11.7 MB against 64 MB.",
      keeps: "Same as C3, plus a DAZ self-test with fallback",
      fakes: "NaN-scope queries, as C3",
      failsWhen: "Same regimes as C3; the DAZ self-test switches to numpy's cast instead of returning wrong scores.",
      test: "Same harness as C3",
      priorArt: { name: "Fabian Giesen, half to float done quic", url: "https://fgiesen.wordpress.com/2012/03/28/half-to-float-done-quic/" },
      values: { passages_worst_ns: 588, embeddings_worst_ns: 393, scenarios_within: 9, mismatches: 0, peak_MB: 11.7 },
      estimated: false,
      capture: "C1.png",
      metricsLine: "mismatches=0 hard=5 tie_order=0 near_tie=0 today_noise=0 errors=0 type_errors=0 nan_scope_mismatches=5 max_abs_dscore=6.07e-08 passages_after_fetch_ns_100k=588 embeddings_after_fetch_ns_100k=393 passages_after_fetch_ns_1m=380 embeddings_after_fetch_ns_1m=381 peak_MB=11.7"
    },
    {
      id: "C4", name: "Columnar chunk, plain preallocated cast", status: "fails",
      statusNote: "3 of 10 scenarios over 500 ns/row (587, 558, 503): numpy's float16 cast alone costs ~250–260 ns/row.",
      method: "Same columnar pipeline with numpy's own `np.copyto(f32buf, f16view)` (exact by construction, no rebias, no DAZ test), joins in 512-row sub-blocks and a bool lookup table for the owner filters.",
      pipeline: [
        { label: "fetchmany(4000)", cost: "cursor floor, not counted" },
        { label: "Columns", cost: "~11 ns/row" },
        { label: "Length check", cost: "~21 ns/row" },
        { label: "512-row joins", cost: "50–70 ns/row" },
        { label: "copyto f16 → f32", cost: "250–260 ns/row" },
        { label: "gemv", cost: "~19 ns/row" },
        { label: "Survivor lexsort", cost: "10–22 ns/row (plateau bench)" }
      ],
      uses: ["M1 Chunk join + single frombuffer", "M2 Threshold-pruned top-k", "M4 Lexsort first-per-owner", "M11 Vectorized owner filter", "M13 Position-dependent sgemv"],
      cost: "303–587 ns/row after fetch against 500 (quiet floor ~360). Peak 10.2 MB against 64 MB.",
      keeps: "Same results as C3 and C1",
      failsWhen: "Any noisy run: numpy's cast is a scalar loop at ~1 ns per element on stock wheels [4][5], which leaves no margin.",
      test: "Same harness as C3",
      priorArt: null,
      values: { passages_worst_ns: 587, embeddings_worst_ns: 558, scenarios_within: 7, mismatches: 0, peak_MB: 10.2 },
      estimated: false,
      capture: "C4.png",
      metricsLine: "mismatches=0 hard=5 tie_order=0 near_tie=0 today_noise=0 errors=0 type_errors=0 nan_scope_mismatches=5 max_abs_dscore=6.07e-08 passages_after_fetch_ns_100k=587 embeddings_after_fetch_ns_100k=558 passages_after_fetch_ns_1m=467 embeddings_after_fetch_ns_1m=503 peak_MB=10.2"
    },
    {
      id: "C2", name: "SQL-side chunk aggregate", status: "refuted",
      statusNote: "Not built. A refuter measured the core on stdlib SQLite 3.50.4: 0.77–0.82 µs/row total against today's 0.79–0.82; it moves the object work into the engine instead of removing it.",
      method: "`CAST(group_concat(vec, '') AS BLOB)` per keyset page, so one bytes object per 4,000 rows reaches Python and one `frombuffer` reads it.",
      pipeline: [
        { label: "Keyset page co-routine", cost: "+0.10 µs/row" },
        { label: "group_concat", cost: "+0.14 µs/row" },
        { label: "One frombuffer per page", cost: "~0" }
      ],
      uses: ["M7 SQL-side chunk aggregate", "M1 Chunk join + single frombuffer"],
      cost: "Total 0.77–0.82 µs/row against today's 0.79–0.82 µs/row (refuter, stdlib SQLite, not SQLCipher)",
      failsWhen: "Always on cost. Bytes were preserved (NUL, invalid UTF-8) and chunks reproduced, but concatenation order is not guaranteed without `ORDER BY` inside the aggregate [11].",
      priorArt: null
    }
  ],

  method: {
    summary: "C3: score each 4,000-row chunk as one matrix in today's exact shape, convert float16 by integer shifts with the 2^112 factor moved into the query, and select the top-k on arrays so Python objects exist only for the winners.",
    equation: "w = (int32(h) << 13) & 0x8FFFFFFF        float32 bits of value(h) · 2^-112, exact for every finite half h\ns = W[:k] @ (q · 2^112)                     same real products as float32(H[:k]) @ q, same (k, 256) shape\npassages:   keep s ≥ T; per owner run m = max(s), first i with s_i = m; order (−s, owner)[:limit]\nembeddings: keep s > T; stable argsort(−s) over [retained, survivors][:limit]",
    steps: [
      { text: "Fetch with `fetchmany(4000)` over today's SQL, unchanged. Take the owner, offset and vector columns with `map(itemgetter)`; build the vector-size mask and the `only`/`exclude` mask from one bool table indexed by owner id [1][2].", assumes: "Owner ids are ints in [0, 2^24); otherwise the code keeps today's sets (slower, same result)." },
      { text: "Drop masked rows before one `b\"\".join`, so the kept rows and their order equal today's filtered list, then one `np.frombuffer(…, int16).reshape(k, 256)`.", assumes: "Every kept blob has the query's byte size, which the mask already checked." },
      { text: "Check two read-only reductions (`int16 max < 0x7C00`, `uint16 max < 0xFC00`) for inf/NaN halves. If clean, sign-extend into the reused int32 buffer, shift left 13, mask with `int32(-0x70000001)` and view as float32: value × 2^-112 exactly, subnormals and ±0 included [13].", assumes: "No inf/NaN half in the chunk; else numpy's cast runs for that chunk." },
      { text: "Multiply by `q · 2^112`. Scaling by a power of two commutes with float32 rounding, so each product and the gemv sum round exactly as today's `astype(float32) @ q`, given the same shape [6][7].", assumes: "`q` is a finite 1-D float32 vector with |q| < 2^15, and BLAS keeps float32 subnormals (no DAZ/FTZ). C3 as built checks the first; C1's per-process self-test covers the second." },
      { text: "Passages: keep `s ≥ T` (T = retained limit-th score; NaN fails both comparisons). Owner runs start where the owner changes; `maximum.reduceat` gives each run's max and `minimum.reduceat` over `where(s == runmax, seq, BIG)` its first max row. If an owner has two runs in the chunk, `lexsort((seq, -s, owner))` replaces it.", assumes: "Owners contiguous within the chunk for the fast path; true in every passage plan tested." },
      { text: "Chunk top-k by `argpartition` on the threshold, with ties at the threshold given to the smallest owner ids; merge with the retained set (≤ 2·limit rows) by `lexsort`, the retained row winning same-owner ties (today's strict `>`), final order `lexsort((owner, −s))[:limit]`." },
      { text: "Embeddings: keep `s > T` (an equal later row sorts after the kept one in today's stable sort), partition ties at the threshold by scan order, and `argsort(-s, kind=\"stable\")` with the retained set first." },
      { text: "Build `Match`/`Scored` objects and read `start_char`/`end_char` only for chunk newcomers to the retained set and the final winners. Free each chunk before the next fetch." }
    ],
    cost: "Stage profile per scanned row: rebias 155 + join 57 + columns 22 + length 12 + owner ids 9 + gemv 12–17 + selection 5–20 + inf/NaN check 7 + filter ~2 ≈ 280–300 ns. Full harness: 252–431 ns/row against 500. Memory: 4,000 × 256 × 4 B = 4.1 MB float32 buffer + 2 MB blob + chunk objects; measured peak 9.1 MB against 64 MB (today 12.8 MB).",
    failsWhen: "Chunk with an inf/NaN half or a query that is not float32, is not finite or has |q| ≥ 2^15 → numpy's cast for that chunk, ~+95 ns/row, which brings some scenarios near 500. Owners interleaved in a chunk → `lexsort` fallback, same result (24.5 ns/row of selection on the fallback bench). DAZ/FTZ set in BLAS threads → small halves flush to zero and scores differ from today; C1's self-test detects this and falls back. A NaN vector in scope → its rows are dropped, so results differ from today's corrupted order.",
    reproduce: "From `eval/exact-vector-top-k-scan/`: `..\\.venv\\Scripts\\python.exe harness.py C3\\cand.py C3` (~20 min full; builds the disposable 100k and 1M SQLCipher DBs into `cache/` on first run). The DBs are gitignored and rebuilt on demand."
  },

  findings: [
    {
      question: "Where does today's per-row time go?",
      claims: [
        "Today copies each blob, calls `np.frombuffer` per row, builds a `Match`/`Scored` per row and sorts each 4,000-row chunk [1][2]. Its after-fetch cost is 865–2025 ns/row across the 10 scenarios.",
        "SQLCipher step and decode cost 1.4–2.3 µs/row in the reference run; the cursor's tuple and bytes objects add 0.35–0.5 µs/row (fetch floor minus decode) [8]."
      ]
    },
    {
      question: "Why is the float16 → float32 cast the largest remaining stage?",
      claims: [
        "numpy's half conversion is a per-element software routine in `half.hpp` [3] driven from the strided cast loops [4]; F16C dispatch needs a baseline above the stock wheel's X86_V2 [5]. Measured: 0.97–0.99 ns per element, ~250 ns per 256-dim row.",
        "Shifting the half's bits left 13 and masking gives a float32 equal to value × 2^-112 [13]. A refuter confirmed it exact for all 63,488 finite halves and bitwise-equal scores in 132 matrix shapes. Measured ~155 ns/row."
      ]
    },
    {
      question: "Can a faster method keep today's scores bit for bit?",
      claims: [
        "numpy's matmul passes a (k, d) @ (d,) product to BLAS gemv [6]; OpenBLAS's `sgemv_t` kernel blocks rows, so one row's float32 result depends on its position and the matrix shape [7]. Today itself gives byte-identical rows different scores by position in 223 of 420 synthetic cases.",
        "Keeping today's chunk rows, order and matrix shape reproduces every score exactly: 32,585 compared deltas were exactly 0; the only nonzero deltas (max 6.07e-8) come from NaN-scope queries."
      ]
    },
    {
      question: "Can SQLite assemble the chunk instead?",
      claims: [
        "`group_concat` concatenates blobs byte for byte [10], but its order is unspecified without `ORDER BY` inside the aggregate [11]. Measured on stdlib SQLite, it costs as much as today's per-row fetch.",
        "A Python `create_aggregate` calls back into Python once per row [9], so it cannot remove per-row object work.",
        "sqlite-vec's `vec0` stores vectors in chunk blobs that one read returns as a matrix [12]; adopting that layout is a storage-format change (non-goal)."
      ]
    },
    {
      question: "How exact are today's scores?",
      claims: [
        "Today's float32 scores are within 1.34e-7 of the float64 exact dot product on this data; the candidates match that exactly. The general accumulation bound γ_n of Higham [14] was not reachable in the fact-check."
      ]
    }
  ],

  mechanisms: [
    { id: "M1", name: "Chunk join + single frombuffer", core: "Join a chunk's kept blobs into one buffer, read it as one (k, d) array.", assumes: "Uniform blob size (checked per row by length).", breaks: "Mixed sizes, which the mask removes before the join." },
    { id: "M2", name: "Threshold-pruned top-k", core: "Only rows scoring at or above the retained limit-th score enter the merge.", assumes: "The retained set is full.", breaks: "Warm-up chunks (everything survives) and NaN scores (comparisons fail)." },
    { id: "M3", name: "Sorted-run group max", core: "`maximum.reduceat` per owner run plus first argmax by `minimum.reduceat`.", assumes: "Owners contiguous in the chunk.", breaks: "Interleaved owners; a `lexsort` fallback covers it." },
    { id: "M4", name: "Lexsort first-per-owner", core: "`lexsort((seq, −s, owner))`, keep the first row per owner.", assumes: "Survivors are few or the sort is cheap.", breaks: "Nothing found: 10–23 ns/row even on 4,000-row plateaus." },
    { id: "M5", name: "np.maximum.at group max", core: "Unbuffered scatter-max per owner.", assumes: "—", breaks: "Slow, and gives no argmax for the char offsets. Not used." },
    { id: "M6", name: "Tie-safe partition", core: "Split at the threshold value; break ties at it explicitly (owner id or scan order).", assumes: "A defined tie rule.", breaks: "Plain `argpartition` picks arbitrary rows among equal scores." },
    { id: "M7", name: "SQL-side chunk aggregate", core: "`CAST(group_concat(vec, '') AS BLOB)` per keyset page.", assumes: "Engine-side concatenation is cheaper than Python objects.", breaks: "Measured no gain; order unspecified without `ORDER BY` in the aggregate. [10][11]" },
    { id: "M8", name: "create_aggregate", core: "Python aggregate class collecting blobs.", assumes: "—", breaks: "One Python callback per row: no gain. [9]" },
    { id: "M9", name: "vec0 chunk-blob layout", core: "Store vectors in chunk-sized blobs.", assumes: "A storage-format change.", breaks: "Non-goal here; idea source for M1. [12]" },
    { id: "M10", name: "Narrow scan + second lookup", core: "Scan owner ids and vectors only; read the char offsets for the winners with a second query.", assumes: "A second lookup per winner set is cheap.", breaks: "Not built or measured in this run." },
    { id: "M11", name: "Vectorized owner filter", core: "Bool lookup table indexed by owner id, one gather per chunk.", assumes: "Integer ids in a bounded range (≤ 2^24 entries).", breaks: "Other id types fall back to sets. Large sets cost cache misses (523 ns/row at 1M before this fix)." },
    { id: "M12", name: "Integer f16 rebias", core: "`(int32(h) << 13) & 0x8FFFFFFF` = value × 2^-112; fold 2^112 into the query.", assumes: "Finite halves, no DAZ/FTZ, finite scaled query.", breaks: "inf/NaN chunks and DAZ fall back to numpy's cast. [3][4][5][13]" },
    { id: "M13", name: "Position-dependent sgemv", core: "Row results depend on position and shape in BLAS gemv.", assumes: "—", breaks: "Any change of chunk or matrix shape changes float32 scores, so the candidates keep today's. [6][7]" },
    { id: "M14", name: "Accumulation error bound", core: "Float32 dot product error ≤ γ_n Σ|a_i b_i|.", assumes: "Standard rounding model.", breaks: "Source unreachable in the fact-check. [14]" },
    { id: "M15", name: "einsum fixed order", core: "Fixed-order reduction instead of BLAS.", assumes: "—", breaks: "Unmeasured and not needed once shapes match. Not used." }
  ],

  explored: [
    { name: "C2 SQL-side chunk aggregate", reason: "Refuter measured 0.77–0.82 µs/row total against today's 0.79–0.82: the keyset co-routine (+0.10) and `group_concat` (+0.14 µs/row) move the object work into the engine; order not guaranteed without `ORDER BY` in the aggregate." },
    { name: "C4 Columnar chunk, plain preallocated cast", reason: "3 of 10 scenarios over 500 ns/row (587, 558, 503); numpy's cast alone costs 250–260 ns/row." },
    { name: "C1 Columnar chunk with integer f16 rebias", reason: "Works (9 of 10 within budget) and carries the DAZ self-test; not the best fit only by measured margin under load." },
    { name: "C3 round 0 (numpy cast, sets, chunk kept alive)", reason: "540–690 ns/row after fetch; fixed by the rebias, freeing each chunk before the next fetch, and the bool owner table." },
    { name: "np.maximum.at group max", reason: "Slow and gives no argmax for the char offsets." },
    { name: "create_aggregate", reason: "One Python callback per row." },
    { name: "sqlite-vec vec0 chunk-blob layout", reason: "Needs a storage-format change (non-goal)." },
    { name: "einsum fixed-order reduction", reason: "Unmeasured; not needed once today's shapes are kept." },
    { name: "Rebias in 256- or 1024-row L2 blocks", reason: "No gain over the whole-chunk rebias." },
    { name: "One 2 MB `b\"\".join` per chunk in a fresh allocation", reason: "Page faults on every chunk; `BytesIO.writelines` (12 ns/row vs 43) or 512-row sub-blocks are cheaper." },
    { name: "Run-level selection as the main gain", reason: "Plateau bench: run-level 17.9/18.8/24.5/7.3 ns/row vs survivor `lexsort` 10.7/22.1/20.7/8.4; the claimed ~180 ns/row `lexsort` cost was not reproduced. Selection is ≤ 5% of per-row cost." }
  ],

  caveats: [
    "All candidate timings were measured while other harnesses loaded the machine; fetch floors moved ±30% between runs and after-fetch values by ±100–150 ns/row. Differences between candidates are meaningful; absolute margins less so.",
    "The 500 ns/row budget excludes the cursor floor (0.35–0.5 µs/row) by the user-confirmed reading; with it, no candidate fits. SQLCipher decode (1.4–3.0 µs/row across runs) now dominates the total.",
    "C3 as built lacks C1's DAZ/FTZ self-test; with DAZ set in BLAS threads the rebias would flush small halves. C1's `_rebias_is_exact` check (once per process plus a per-call check) and its fallback carry over unchanged.",
    "NaN-scope behaviour differs from today by design: the 5 hard results are all NaN-scope queries at 100k (0 at 1M), exactly where today differs from the NaN-dropped stable oracle. Each candidate equals that oracle on 15 of 16 at 100k and 13 of 13 at 1M; the 16th is an exact tie across a chunk boundary where the candidates keep today's per-chunk float32 rounding.",
    "Today breaks its own tie rule when OpenBLAS gives byte-identical rows different float32 scores by position (223 of 420 synthetic cases); the candidates reproduce today's scores, so they reproduce this too.",
    "Owner runs were contiguous in every passage plan tested; another query plan would take the `lexsort` fallback.",
    "numpy sources [3][4][5][6] were read on `main`, not tag 2.5.4. Higham [14] was UNREACHABLE in the fact-check.",
    "The C2 refutation ran on stdlib SQLite 3.50.4, not SQLCipher."
  ],

  sources: [
    { n: 1, title: "passages.py", url: "src/akashic/storage/passages.py:96-147", tier: 1, version: "pre-audit" },
    { n: 2, title: "embeddings.py", url: "src/akashic/storage/embeddings.py:80-132", tier: 1, version: "pre-audit" },
    { n: 3, title: "numpy half.hpp", url: "https://raw.githubusercontent.com/numpy/numpy/main/numpy/_core/src/common/half.hpp", tier: 1, version: "main" },
    { n: 4, title: "numpy lowlevel_strided_loops.c.src", url: "https://raw.githubusercontent.com/numpy/numpy/main/numpy/_core/src/multiarray/lowlevel_strided_loops.c.src", tier: 1, version: "main" },
    { n: 5, title: "numpy meson_cpu x86", url: "https://raw.githubusercontent.com/numpy/numpy/main/meson_cpu/x86/meson.build", tier: 1, version: "main" },
    { n: 6, title: "numpy matmul.c.src", url: "https://github.com/numpy/numpy/blob/main/numpy/_core/src/umath/matmul.c.src", tier: 1, version: "main" },
    { n: 7, title: "OpenBLAS sgemv_t_4.c", url: "https://github.com/OpenMathLib/OpenBLAS/blob/develop/kernel/x86_64/sgemv_t_4.c", tier: 1, version: "develop" },
    { n: 8, title: "CPython _sqlite cursor.c", url: "https://raw.githubusercontent.com/python/cpython/3.14/Modules/_sqlite/cursor.c", tier: 1, version: "3.14" },
    { n: 9, title: "CPython _sqlite connection.c", url: "https://raw.githubusercontent.com/python/cpython/3.14/Modules/_sqlite/connection.c", tier: 1, version: "3.14" },
    { n: 10, title: "SQLite func.c", url: "https://raw.githubusercontent.com/sqlite/sqlite/master/src/func.c", tier: 1, version: "master" },
    { n: 11, title: "SQLite aggregate functions", url: "https://sqlite.org/lang_aggfunc.html", tier: 1, version: "current" },
    { n: 12, title: "sqlite-vec.c", url: "https://raw.githubusercontent.com/asg017/sqlite-vec/main/sqlite-vec.c", tier: 1, version: "main" },
    { n: 13, title: "Fabian Giesen, Half to float done quic", url: "https://fgiesen.wordpress.com/2012/03/28/half-to-float-done-quic/", tier: 2, version: "2012" },
    { n: 14, title: "Higham, Accuracy and Stability of Numerical Algorithms", url: "https://doi.org/10.1137/1.9780898718027", tier: 1, version: "2nd ed. (unreachable in fact-check)" }
  ]
};
