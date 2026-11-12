window.REPORT = {
  title: "Conversation Clustering Sweep",
  date: "2026-11-10",
  depth: "full",
  context: "Windows 11, CPython 3.14.7, stdlib only",
  problem: "Conversation clustering in `src/akashic/pipeline/episodes.py:265-311` [7] recomputes `_Cluster.end_ms` (a max over all segments) on every access, so one pass costs O(N·K) over the open call. Its microphone attachment, `cluster.segments.extend(<generator reading cluster.end_ms>)`, widens the bound as mic segments are appended [6], so mic speech with gaps under 30 s chains on indefinitely, and one mic segment can attach to two clusters. Today a pass at N = 20,000 takes 7,739.5 ms median, and on a realistic 8 h call no cluster ever becomes final.",
  goals: [
    "The same voice clusters as today",
    "Microphone speech attaches only within 30 s of the cluster's voice-only bounds, which fixes the chaining bug",
    "O(N log N) per 30 s pass during long calls"
  ],
  contract: {
    must: [
      "Voice clustering identical to today: 5 min gap rule, `<=`",
      "A mic segment attaches iff it overlaps [voice start − 30 s, voice end + 30 s) of one cluster's voice segments, to at most one cluster: the nearer by gap distance `d = max(0, vs − m.end, m.start − ve)`, ties to the earlier",
      "Final/growing split unchanged: cluster end including attached mic + 300 s ≤ horizon",
      "Conversation watermark unchanged",
      "Stdlib only"
    ],
    fake: ["Nothing"]
  },
  budget: ["≤ 50 ms per pass at N = 20,000 segments (8-hour call, voice + mic), all Python work after `fetchall`: parsing, voice test, clustering, attachment, finality. SQL fetch excluded."],
  nonGoals: [
    "What counts as voice",
    "Speaker attribution",
    "The SQL that loads segments",
    "The expired-episode rebuild",
    "The transcript horizon"
  ],

  answer: {
    text: "**C4**, a two-pointer sweep over frozen voice-only bounds plus a per-pass memo of parsed `sources` strings, matches the contract oracle on every check (0 fuzz, 0 edge, 0 replay mismatches) at 5.0 ms median and 5.9 ms p95 per pass against 50 ms. Today's code takes 7,739.5 ms and differs from the contract on 1,165 of 10,000 fuzz cases. The voice sweep is the gap merge of pyannote-core `Annotation.support` [1]; no prior art was found for attaching the mic stream to frozen cluster bounds. C1 is the same sweep without the memo: exact, 35.5 ms, 75 % of it today's unchanged parsing. C3 is fastest at 4.3 ms but exact only while non-voice mic segments are ≤ 300 s; capture caps them at 30 s [8]."
  },
  bestFit: "C4",

  metrics: [
    { key: "median_ms", label: "Median pass", unit: "ms", limit: 50, budget: true, log: true },
    { key: "p95_ms", label: "p95 pass", unit: "ms", log: true },
    { key: "mismatches", label: "Mismatches vs oracle", limit: 0 }
  ],
  chart: { x: "median_ms", y: "mismatches" },

  captureColumns: ["Whole post-fetchall pass, ms (log) vs today and 50 ms", "Mismatches vs the contract oracle per scenario"],

  figures: [
    {
      src: "fig-sources-cardinality.png",
      title: "Pass time vs distinct `sources` strings",
      caption: "N = 20,000. C4's memo turns 29.1 ms into 5.0 ms when a pass holds 6 distinct strings (the realistic fixture), and costs about +4.5 ms when every row's string is distinct. All variants equal the oracle. C1 bar from the harness, C4 bars from `C4/bench.py`."
    },
    {
      src: "fig-c3-mic-length.png",
      title: "C3 mismatches by mic segment length",
      caption: "Cases bucketed by their longest non-voice mic segment. 0 mismatches over 207,057 cases with every mic segment ≤ 300 s; every mismatch is a longer segment spanning a whole voice gap. Replay one-shot compares each replay seed's one-shot output with the oracle; the multi-pass replay check itself had 0 mismatches."
    }
  ],

  candidates: [
    {
      id: "C4", name: "Two-pointer sweep + per-pass sources memo", status: "best",
      method: "C1's sweep with the parsing moved behind a dict local to the call, keyed by the raw `sources` JSON string → (parsed tuple, voice-app flag, game processes named in it). Rows stay `fetchall` tuples with an `owner` array; only clusters `lo` and `lo + 1` can be nearest to a mic segment.",
      pipeline: [
        { label: "Memo: parse + voice-app test per distinct `sources`", cost: "6 parses, 20,000 dict lookups" },
        { label: "Game overlap test", cost: "rows naming a game process only" },
        { label: "Voice sweep, running max end", cost: "O(N)" },
        { label: "Mic pointer, clusters lo and lo + 1", cost: "O(M + K)" },
        { label: "Members by owner array", cost: "O(N)" },
        { label: "Finality", cost: "O(K); 5.0 ms total" }
      ],
      uses: ["M1 Gap sweep with running max end", "M3 Two-pointer attach", "Low-cardinality memo"],
      cost: "5.0 ms median, 5.9 ms p95 at N = 20,000 against 50 ms. Without the memo 29.1 ms; with 20,000 distinct `sources` strings 36.5–38.1 ms median, p95 43–45 ms.",
      keeps: "Every `must` item: identical voice clusters, at most one owner per mic segment with nearest-then-earlier ties, today's finality formula and watermark, stdlib only",
      fakes: "Nothing",
      relaxes: null,
      failsWhen: "Every row carries a distinct `sources` string: the memo never hits and adds about 4.5 ms. Rows out of `(start_ms, id)` order break the sweep, the pointer and the member order, as they would break today's voice loop.",
      condition: null,
      test: "Literal O(N·K) contract oracle · 10,000 fuzz, 26 edge sets + realistic 8 h, 1,000 multi-pass replays · pass = 0 mismatches",
      priorArt: { name: "pyannote-core Annotation.support (voice sweep only)", url: "https://raw.githubusercontent.com/pyannote/pyannote-core/develop/src/pyannote/core/annotation.py" },
      values: { median_ms: 5.022, p95_ms: 5.866, mismatches: 0 },
      estimated: false,
      capture: "C4.png",
      metricsLine: "mismatches_fuzz=0 mismatches_edges=0 mismatches_replay=0 median_ms=5.022 p95_ms=5.866"
    },
    {
      id: "C1", name: "Frozen-bounds two-pointer sweep", status: "works",
      method: "Voice sweep with the running max end as a local, freezing (vs_k, ve_k). Non-voice mic segments are assigned by a pointer that only moves forward (ve_k + 30 s strictly increases), scanning while `vs_j − 30 s < m.end` and keeping the strict minimum d. One owner slot per segment, members in one ordered pass, finality with `end_all = max(ve, attached mic ends)`. Today's `_segment` and `_is_voice` run unchanged on every row.",
      pipeline: [
        { label: "`_segment` + `_is_voice` per row (unchanged)", cost: "25.9 ms (75 %)" },
        { label: "Voice sweep, running max end", cost: "O(N)" },
        { label: "Mic pointer + window scan", cost: "O(M + K)" },
        { label: "Members + finality", cost: "O(N + K); ~9.6 ms for these three steps" }
      ],
      uses: ["M1 Gap sweep with running max end", "M3 Two-pointer attach"],
      cost: "35.5 ms median, 41.7 ms p95 at N = 20,000 against 50 ms",
      keeps: "Every `must` item",
      fakes: "Nothing",
      relaxes: null,
      failsWhen: "Rows out of `(start_ms, id)` order. Margin is thin at p95 (41.7 ms) because per-row parsing stays.",
      condition: null,
      test: "Literal O(N·K) contract oracle · same harness · pass = 0 mismatches",
      priorArt: { name: "pyannote-core Annotation.support (voice sweep only)", url: "https://raw.githubusercontent.com/pyannote/pyannote-core/develop/src/pyannote/core/annotation.py" },
      values: { median_ms: 35.519, p95_ms: 41.718, mismatches: 0 },
      estimated: false,
      capture: "C1.png",
      metricsLine: "mismatches_fuzz=0 mismatches_edges=0 mismatches_replay=0 median_ms=35.519 p95_ms=41.718"
    },
    {
      id: "C3", name: "Midpoint cell table, closed form", status: "works",
      statusNote: "Exact while non-voice mic segments are ≤ 300 s; capture caps them at 30 s [8].",
      method: "Same voice sweep and memoised classification. Cell boundaries `S_k = ve_k + vs_{k+1}` are strictly increasing. A mic segment with `σ = m.start + m.end` goes to `k = bisect_left(S, σ)` (σ == S_k → k, the earlier cluster), then one overlap check against [vs_k − 30 s, ve_k + 30 s). Integer sums only, no division, no scan loop.",
      pipeline: [
        { label: "Memo classify + voice sweep", cost: "O(N)" },
        { label: "Build S_k = ve_k + vs_{k+1}", cost: "O(K)" },
        { label: "bisect_left on σ + one overlap check", cost: "O(M log K)" },
        { label: "Members + finality", cost: "O(N + K); 4.3 ms total" }
      ],
      uses: ["M1 Gap sweep with running max end", "Midpoint cell table (bisection on integer sums)"],
      cost: "4.3 ms median, 4.7 ms p95 at N = 20,000 against 50 ms",
      keeps: "Every `must` item for mic segments ≤ 300 s",
      fakes: "Nothing",
      relaxes: null,
      failsWhen: "A non-voice mic segment longer than 300 s spans a whole voice gap (d = 0 to two clusters): it goes to the later cluster instead of the earlier one. 164 of 10,000 fuzz cases and edge set `mic_spans_three_clusters`.",
      condition: "Non-voice mic segments ≤ 300 s (capture caps them at 30 s)",
      test: "Literal O(N·K) contract oracle · same harness plus a length breakdown over 207,057 cases · pass = 0 mismatches",
      priorArt: null,
      values: { median_ms: 4.261, p95_ms: 4.689, mismatches: 165 },
      estimated: false,
      capture: "C3.png",
      metricsLine: "mismatches_fuzz=164 mismatches_edges=1 mismatches_replay=0 median_ms=4.261 p95_ms=4.689"
    },
    {
      id: "C2", name: "Bisect attach", status: "unbuilt",
      statusNote: "Not built: same output and mechanisms as C1. The refuter confirmed 0 mismatches against brute force and a 3,000-seed replay.",
      method: "C1 with `k = bisect_right(ends, m.start)` per mic segment over `ends = [ve_k + 30 s]` instead of a pointer, so mic order does not matter.",
      pipeline: [
        { label: "`_segment` + `_is_voice` per row", cost: "as C1" },
        { label: "Voice sweep", cost: "O(N)" },
        { label: "bisect_right + window scan per mic", cost: "O(M log K)" },
        { label: "Members + finality", cost: "O(N + K)" }
      ],
      uses: ["M1 Gap sweep with running max end", "M2 Bisect over frozen bounds"],
      cost: "Not measured; same parsing floor as C1",
      priorArt: { name: "pyannote-core Annotation.support (voice sweep only)", url: "https://raw.githubusercontent.com/pyannote/pyannote-core/develop/src/pyannote/core/annotation.py" }
    },
    {
      id: "C5", name: "Two-term finality", status: "relaxes",
      statusNote: "Works if the final/growing formula relaxes from (end incl. mic) + 300 s ≤ horizon to max(ve + 300 s, mic_end + 30 s) ≤ horizon. Not built.",
      method: "Final iff `max(ve + 300 s, mic_end + 30 s) ≤ horizon`. Finality becomes voice-driven and lands up to 30 s plus the mic segment's length earlier.",
      pipeline: [
        { label: "Sweep + attach (as C1 or C4)", cost: "as C1 or C4" },
        { label: "Two-term finality", cost: "O(K)" }
      ],
      uses: ["M1 Gap sweep with running max end", "M3 Two-pointer attach", "Two-term finality"],
      cost: "Not measured",
      relaxes: "Final/growing formula, from (end incl. mic) + 300 s to max(ve + 300 s, mic_end + 30 s)",
      priorArt: null
    }
  ],

  method: {
    summary: "C4: freeze each cluster's voice-only bounds in one sweep, then give each non-voice mic segment to at most one cluster with a pointer that only moves forward. Bounds never grow from attached mic, so mic speech cannot chain.",
    equation: "voice row:  extend iff start − cur_end ≤ 300,000;  cur_end = max(cur_end, end)\nmic row m:  advance lo while ve_lo + 30,000 ≤ m.start\n            attach nowhere if lo == K or vs_lo − 30,000 ≥ m.end\n            d(k) = max(0, vs_k − m.end, m.start − ve_k)\n            owner = lo + 1 iff lo + 1 overlaps its window and d(lo) > 0 and d(lo + 1) < d(lo), else lo\nfinal iff max(ve_k, attached mic ends) + 300,000 ≤ horizon",
    steps: [
      { text: "Classify each row through a dict local to the call keyed by the raw `sources` JSON string → (parsed tuple, voice-app flag, game processes named). `json.loads` and the `VOICE_APPS` test run once per distinct string; the game-session overlap test runs per row, only for rows naming a game process.", assumes: "Few distinct `sources` strings per pass; the realistic 8 h fixture has 6." },
      { text: "Voice sweep: extend the open cluster while `start − cur_end ≤ 300 s`, raising `cur_end` to the max end; otherwise open cluster k with `vs_k = start`, `ve_k = end`. Today's rule with the end kept as a local instead of a max over all members [7].", assumes: "Rows sorted by `(start_ms, id)`, as the SQL guarantees." },
      { text: "Mic attachment: since `vs_{k+1} > ve_k + 300 s`, `ve_k + 30 s` strictly increases, so a pointer `lo` only moves forward. Only `lo` and `lo + 1` can be nearest, because `lo + 1` starts after `m.start` and every later cluster is strictly farther. Ties stay with `lo`.", assumes: "Mic rows in start order." },
      { text: "Members: one ordered pass over the `owner` array appends row ids, so members are in `(start_ms, id)` order without a per-cluster sort." },
      { text: "Finality: `end_all_k = max(ve_k, attached mic ends)`; final iff `end_all_k + 300 s ≤ horizon`. `growing_start` is the minimum member start over non-final clusters, so the watermark at episodes.py:367-371 [7] is unchanged." }
    ],
    cost: "Parse once per distinct string + O(N) lookups, O(N) sweep, O(M + K) attachment, O(N + K) members and finality. Measured 5.0 ms median, 5.9 ms p95 at N = 20,000 against 50 ms; 29.1 ms without the memo.",
    failsWhen: "Every row has a distinct `sources` string → the memo never hits and adds about 4.5 ms (36.5 ms median, p95 45.3 ms). Rows out of `(start_ms, id)` order → sweep, pointer and member order break, as today's voice loop would.",
    reproduce: "From eval/conversation-clustering-sweep/: ..\\.venv\\Scripts\\python.exe harness.py C4\\cand.py C4"
  },

  findings: [
    {
      question: "Why is today's pass slow and wrong?",
      claims: [
        "`_Cluster.end_ms` is a property that takes `max` over all segments of the cluster on every access, so the voice loop and the attachment filter cost O(N·K) over an open call [7].",
        "The attachment `extend`s the cluster with a generator whose filter reads `cluster.end_ms`; `list.extend` on a generator appends one item at a time, so each attached mic segment widens the bound the next one is tested against [6][7].",
        "Each cluster filters all segments, excluding only its own voice ids, so one mic segment can attach to several clusters [7].",
        "Measured: 7,739.5 ms median per pass at N = 20,000 (another run 5,289.8 ms). Differs from the contract on 1,165 of 10,000 fuzz cases, 40 of 1,000 replays, 8 of 26 edge sets and the realistic 8 h fixture, where no cluster is ever final against 3 under the contract. The literal oracle takes 678.9 ms.",
        "The 8 edge sets: mic chain after a call (28 extra segments attached, contract 2), chained end blocking finality, a chain bridging into the next call with double attachment, long mic segments chaining, equal-distance and nearer-second-cluster mics attached to both, mic segments spanning 2 and 3 clusters attached to all."
      ]
    },
    {
      question: "How do existing tools merge intervals with a gap?",
      claims: [
        "pyannote-core `Annotation.support(collar)` merges segments whose gap is under the collar, carrying the merged end forward [1].",
        "ActivityWatch merges adjacent heartbeats with a pulse time and a maximum duration [2].",
        "SQL packing intervals (gaps and islands) groups start-sorted rows by a running maximum end [3].",
        "whisperX `merge_chunks` caps chunk length rather than clustering by gap [4].",
        "No published source was found for attaching a second stream to frozen bounds of the first with a nearest-cluster rule."
      ]
    },
    {
      question: "Where does a pass spend its time?",
      claims: [
        "`_segment` over 20,000 rows takes 26.4 ms (11.8 ms of it `json.loads`); `_is_voice` 3.9 ms. In C1's 35.5 ms pass, 25.9 ms (75 %) is this unchanged parsing.",
        "The realistic 8 h fixture has 6 distinct `sources` strings over 20,000 rows; a memo keyed by the raw string brings C4 from 29.1 ms to 5.0 ms.",
        "With 20,000 distinct strings the memo costs about +4.5 ms (36.5–38.1 ms against 31.8–32.7 ms without it).",
        "The refuter measured `json.loads` at 0.50–0.64 µs per row, lower than the design estimate."
      ]
    },
    {
      question: "What bounds microphone segment length?",
      claims: [
        "Capture cuts audio segments at `max_segment_ms = 30_000` (audio.py:79), enforced at audio.py:152 [8]. C3's error class needs a non-voice mic segment over 300 s.",
        "`bisect_left` and `bisect_right` return the leftmost and rightmost insertion points in a sorted list in O(log n) [5]."
      ]
    },
    {
      question: "Do the refuter's independent checks agree?",
      claims: [
        "Brute force over 20,000 layouts per mic-length cap from 30 s to 700 s: C1 and C2 0 mismatches; C3 0 up to 300 s, 1 at 330 s, 805 at 700 s.",
        "Multi-pass simulation on 3,000 seeds gives the one-shot clusters for C1, C2 and C3.",
        "Chaining reproduced on today's code: cluster 1's end chained to 715 s, and 14 segment ids landed in two clusters.",
        "An attached mic segment of a final cluster can never prefer a cluster not yet seen, because any later cluster starts more than 300 s after the final cluster's voice end."
      ]
    }
  ],

  mechanisms: [
    { id: "M1", name: "Gap sweep with running max end", core: "Merge intervals with tolerance: sort by start, carry the end as a local, extend while `start − end ≤ GAP`.", assumes: "Input sorted by `(start, id)`; gap measured from the max end.", breaks: "Unsorted input, or the end taken from the last element. [1][2]" },
    { id: "M2", name: "Bisect over frozen bounds", core: "`ends = [ve_k + M]`, `k = bisect_right(ends, m.start)`, scan while `vs_k − M < m.end`, min d, ties to the lowest.", assumes: "Clusters sorted; margin windows disjoint (gap > 300 s > 2 × 30 s).", breaks: "`CLOSE_GAP ≤ 2 · MIC_MARGIN` (still correct, longer scan), or mic counted into the bounds. [5]" },
    { id: "M3", name: "Two-pointer attach", core: "M2 with one pointer that only moves forward over start-sorted mic segments.", assumes: "Mic sorted by start.", breaks: "Unsorted mic rows." },
    { id: "M4", name: "Incremental open-cluster state", core: "Cache the open cluster's state across passes.", assumes: "No late out-of-order transcripts.", breaks: "The horizon releases late transcripts; judged not worth it." }
  ],

  explored: [
    { name: "C2 Bisect attach", reason: "Same output and mechanisms as C1, so not built. The refuter confirmed 0 mismatches against brute force and a 3,000-seed replay." },
    { name: "C3 Midpoint cell table", reason: "Kept as working, not the best fit: a non-voice mic segment over 300 s that spans a whole voice gap goes to the later cluster (164 of 10,000 fuzz cases, 1 edge set). Exact on all 207,057 cases with mic segments ≤ 300 s." },
    { name: "C5 Two-term finality", reason: "Changes the contract's final/growing split, so not built." },
    { name: "M4 Incremental open-cluster state", reason: "State goes stale when the horizon releases late transcripts." },
    { name: "whisperX merge_chunks", reason: "Caps chunk length; it is not gap clustering [4]." }
  ],

  caveats: [
    "Timings were taken on this PC with other processes running; today's code measured 7,739.5 ms in one run and 5,289.8 ms in another.",
    "C4's margin depends on few distinct `sources` strings per pass, true of capture today. With every string distinct it runs at 36.5–38.1 ms median, p95 43–45 ms.",
    "C3 is exact only for non-voice mic segments ≤ 300 s. Capture caps them at 30 s [8].",
    "Inherited boundary case outside the contract: a pending segment starting exactly at the horizon (gap exactly 300,000 ms) could chain onto a cluster already declared final. A replay with the horizon at the segment's start instead of start − 1 failed 1 seed in 3,000 for the oracle too.",
    "Unsorted input is not tested; the SQL guarantees `ORDER BY start_ms, id` [7].",
    "The candidates return final member id lists and `growing_start`. `build_conversations` needs segment objects for final clusters; building those for final clusters only was not measured.",
    "Prior-art and documentation fetches were summaries, not verbatim code."
  ],

  sources: [
    { n: 1, title: "pyannote-core annotation.py", url: "https://raw.githubusercontent.com/pyannote/pyannote-core/develop/src/pyannote/core/annotation.py", tier: 1, version: "develop, fetched 2026-11-10" },
    { n: 2, title: "ActivityWatch aw-core heartbeats.py", url: "https://raw.githubusercontent.com/ActivityWatch/aw-core/master/aw_transform/heartbeats.py", tier: 1, version: "master, fetched 2026-11-10" },
    { n: 3, title: "Ben-Gan, New Solution to the Packing Intervals Problem", url: "https://itprotoday.com/sql-server/new-solution-packing-intervals-problem", tier: 2, version: "2015 (description only)" },
    { n: 4, title: "whisperX vad.py", url: "https://raw.githubusercontent.com/m-bain/whisperX/main/whisperx/vads/vad.py", tier: 1, version: "main" },
    { n: 5, title: "Python bisect", url: "https://docs.python.org/3/library/bisect.html", tier: 1, version: "3.14" },
    { n: 6, title: "CPython listobject.c list_extend", url: "https://github.com/python/cpython/blob/3.14/Objects/listobject.c", tier: 1, version: "3.14" },
    { n: 7, title: "episodes.py", url: "src/akashic/pipeline/episodes.py:93-102,265-311,363-371", tier: 1, version: "pre-audit" },
    { n: 8, title: "Capture audio segment cap", url: "src/akashic/capture/audio.py:79,152", tier: 1, version: "pre-audit" }
  ]
};
