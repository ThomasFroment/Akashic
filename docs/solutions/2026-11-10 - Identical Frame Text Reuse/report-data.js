window.REPORT = {
  title: "Identical Frame Text Reuse",
  date: "2026-11-10",
  depth: "full",
  context: "Windows 11, CPython 3.14, numpy 2.5.4, sqlcipher3 (SQLCipher 4.20.0, SQLite 3.54.1), stdlib `hmac`",
  problem: "Frames whose window text is byte-identical to earlier frames are each split into 400-character passages and embedded again (`embed.py:176-195`, `passages.publish`) [17][18]. This happens because the OCR gate reuses text while the pixels change, for example during video playback, and capture stores a frame at least every 5 s. At query time the identical passages tie and fill the 5 frame result slots (`hybrid.py:515-560`, `628`) [21].",
  goals: [
    "Embed each distinct window text once, so GPU time and stored vectors grow with distinct text instead of capture rate",
    "Stop frames with identical text from filling the 5 frame result slots",
    "Keep deletion and expiry of text removing every derived vector"
  ],
  contract: {
    must: [
      "Every frame with text T stays findable and links to its own timestamp",
      "Deletion, text expiry, embedding expiry and full delete propagate in the same transaction as today",
      "Everything stays encrypted",
      "Any content hash is keyed (HMAC with a DB subkey), never a plain hash of captured text",
      "No new dependency"
    ],
    fake: ["Which of the identical frames is shown as the representative"]
  },
  budget: [
    "Embedding work proportional to distinct texts",
    "≤ 1 ms added per stored frame on the write path",
    "Query cost no worse than today",
    "Vector-storage reduction reported on a realistic fixture (video playing, static page)"
  ],
  constraints: [
    "`foreign_keys = ON` is set on every connection (`db.py:60`) [23]",
    "`recursive_triggers` is off (SQLite default) [7]",
    "`db.migrate` runs SQL files only, so any Python backfill needs its own step"
  ],
  nonGoals: [
    "Near-duplicate (fuzzy) text reuse",
    "Audio segment passages",
    "Changes to the OCR gate or screenshot dedupe",
    "Exact-scan speed"
  ],

  answer: {
    text: "C3, a link table, keeps capture's write path as it is today. The embed worker keys each text with `HMAC-SHA256(subkey, text)` [1][2] and stores one passage set per `(key, model)`. Each frame gets one link row, and the last link of a key deletes its vectors through a trigger [5][6]. Search collapses results to `(key, window_title, url)` groups before the 5 slots, in the style of Elasticsearch collapse [9]. Embeds equal the ideal on all four fixtures, and stored vectors drop 89.7 % on video and 99.2 % on a static page. Result slots are correct on 36/36 queries (today 17/36), with 50/50 + 50/50 findable and 0 orphans. Query p50 is 7.6 ms against 52.7 ms. The single +1.194 ms write p99 reading is within today-vs-today noise: `insert_frame` is unchanged, and three reruns gave −1.218, +0.713 and −1.635 ms. Prior art covers only parts: LangChain CacheBackedEmbeddings [3] and Elasticsearch collapse [9]. No shipped system combining all three parts was found."
  },
  bestFit: "C3",

  metrics: [
    { key: "video_reduction_pct", label: "Vector rows saved, video", unit: "%", better: "higher" },
    { key: "static_reduction_pct", label: "Vector rows saved, static page", unit: "%", better: "higher" },
    { key: "embeds_vs_ideal", label: "Embeds sent / ideal", unit: "×", limit: 1 },
    { key: "groups_ok", label: "Queries with correct slots", unit: "/36", better: "higher", limit: 36 },
    { key: "orphans", label: "Orphan vectors", limit: 0 },
    { key: "write_add_p99_ms", label: "Write p99 added", unit: "ms", limit: 1, budget: true },
    { key: "write_add_p50_ms", label: "Write p50 added", unit: "ms" },
    { key: "query_p50_ratio", label: "Query p50 / today", unit: "×", limit: 1, budget: true },
    { key: "query_p95_ratio", label: "Query p95 / today", unit: "×", limit: 1 }
  ],
  chart: { x: "write_add_p99_ms", y: "query_p95_ratio" },

  captureColumns: ["Stored vector rows per fixture vs today (log)", "Query p50/p95 and write p99 added vs budgets"],

  figures: [
    {
      src: "fig-write-p99.png",
      title: "Write-path p99 added, every repeated run",
      caption: "Each dot is one run of 10k inserts against today. The grey band is today measured against itself (−0.64 to +1.69 ms). C3 does not change `insert_frame`, and its four runs fall on both sides of 0. C1 and C5 add a random-key index to capture's write path, and their runs sit above the 1 ms budget in 3 of 4 runs, up to +8.73 ms for C1."
    }
  ],

  candidates: [
    {
      id: "C3", name: "Link table, capture path untouched", status: "best",
      statusNote: "Every check passes. The single +1.194 ms write p99 reading is noise: `insert_frame` is literally today's function, three reruns gave −1.218, +0.713 and −1.635 ms, and today measured against itself gave up to +1.685 ms.",
      method: "`frame_text_links(frame_id → frames ON DELETE CASCADE, model, key)` holds one row per embedded frame, where `key = HMAC-SHA256(subkey, text)`. Vectors live once per `(key, model)` in `text_passages`. Trigger `AFTER DELETE ON frame_text_links` deletes a key's vectors when no other link remains, and a text change deletes the frame's links. The embed worker hashes the texts, embeds only new keys, and publishes vectors and links in one transaction after re-checking each text. FTS and vector hits are collapsed to `(key, window_title, url)` groups, fused with RRF, and each group fills one slot with a real frame plus `also_seen` (≤ 50) and `also_seen_count`.",
      pipeline: [
        { label: "Capture insert, unchanged", cost: "+0 statements" },
        { label: "Embed worker: HMAC per frame", cost: "9.7 µs at 7.9 K chars" },
        { label: "Embed new keys only", cost: "2,590 windows vs 25,200 (video)" },
        { label: "Publish vectors + links, one transaction", cost: "1 link row per frame" },
        { label: "Query: FTS + per-key vectors → groups → RRF → 5 slots", cost: "p50 7.6 ms vs 52.7 ms" }
      ],
      uses: ["M1 Keyed text tag", "M5 Trigger-maintained shared rows", "M8 Post-fusion key collapse", "M9 RRF duplicate over-weighting"],
      cost: "0 added write-path statements, since `insert_frame` and `set_text` are unchanged [22] (reruns −1.218 to +1.194 ms p99) against 1 ms. Query p50 0.14× and p95 0.68× today against 1×. Embeds 2,590 = ideal on video, and the DB file shrinks from 29.6 to 15.4 MB.",
      keeps: "Every `must`: 50/50 findable by keyword and by meaning at the frame's own timestamp, 0 orphans on every deletion path, 18/18 rollbacks, 0 unkeyed hashes, 0 raw leaks, upgrade without re-embedding",
      fakes: "Which identical frame is the representative: the best-bm25 frame, otherwise the latest in the window",
      relaxes: null,
      failsWhen: "Text that differs by one character (a clock, a counter) gets a new key, which is why mixed browsing saves only 21 %. A long identical run can still take most of the 200 FTS candidates. A connection with `foreign_keys = OFF` or a link written with `INSERT OR REPLACE` leaves orphans. Rotating the data key needs a re-key.",
      condition: "Links must never be written with `INSERT OR REPLACE`, and `foreign_keys` must stay on for every connection [5][6][7].",
      test: "Four fixtures (video, static, mixed, two videos), fake deterministic embedder · embeds vs ideal, orphan scan after every deletion path, 18 rollbacks, 36 slot queries, 50 + 50 findability, 10k-insert write p99, query p50/p95 · pass = ideal embeds, 0 orphans, 36/36, ≤ 1 ms, ≤ 1× today",
      priorArt: { name: "LangChain CacheBackedEmbeddings (skip half only)", url: "https://raw.githubusercontent.com/langchain-ai/langchain/master/libs/langchain/langchain_classic/embeddings/cache.py" },
      values: { video_reduction_pct: 89.7, static_reduction_pct: 99.2, embeds_vs_ideal: 1, groups_ok: 36, orphans: 0, write_add_p99_ms: 1.194, write_add_p50_ms: 0.003, query_p50_ratio: 0.144, query_p95_ratio: 0.683 },
      estimated: false,
      capture: "C3.png",
      metricsLine: "video:sent=2590/ideal=2590 rows=2590 MB=1.33 red=89.7% static:sent=32/ideal=32 rows=32 MB=0.02 red=99.2% mixed:sent=37330/ideal=37330 rows=37330 MB=19.11 red=21.0% twovid:sent=15/ideal=15 rows=15 MB=0.01 red=99.2% orphans=0 rollback=18/18 survivors=5/5 expired_returned=0 stale_sem=0 rows_after_delete_all=0 findable_kw=50/50 findable_sem=50/50 groups_ok=36/36 mean_groups=3.11 top1_agree=34/36 win_viol=0 ts_mismatch=0 write_add_p50=0.003ms write_add_p99=1.194ms q_p50=7.6ms(today 52.7) q_p95=183.6ms(today 268.6) unkeyed_hash=0 raw_leak=0 upgrade=ok"
    },
    {
      id: "C1", name: "Shared content-addressed passages, write-path tag", status: "fails",
      statusNote: "Write-path p99 is over 1 ms in 3 of 4 runs: +1.865, +0.976, +1.976 and +8.730 ms. The random-key index `frames(text_key, ts_ms)` on capture's write path enlarges WAL checkpoints.",
      method: "Both writers store `frames.text_key = HMAC-SHA256(subkey, stored text)` together with the text, and text expiry sets it to NULL. An index on `frames(text_key, ts_ms)` serves key lookups. `text_passages(text_key, model, start_char, …)` is `WITHOUT ROWID`. `AFTER DELETE` and `AFTER UPDATE OF text_key` triggers delete a key's rows when no other frame carries it, and `BEFORE` guards abort writes where text and key disagree. A `vector_floor` table records the embedding-expiry cutoff. Queries group by `(text_key, title, url)` through a `MATERIALIZED` CTE, because `bm25` is not allowed inside aggregates. The representative is the latest frame in the window.",
      pipeline: [
        { label: "Capture insert: HMAC + text_key", cost: "HMAC 9 µs p50" },
        { label: "Index frames(text_key, ts_ms)", cost: "+0.06–0.15 ms mean per frame (larger checkpoints)" },
        { label: "Guard triggers", cost: "2 BEFORE triggers" },
        { label: "Embed missing keys only", cost: "2,590 windows (video)" },
        { label: "Query: MATERIALIZED CTE groups → RRF → 5 slots", cost: "p50 2.9 ms vs 52.1 ms" }
      ],
      uses: ["M1 Keyed text tag", "M5 Trigger-maintained shared rows", "M8 Post-fusion key collapse", "M9 RRF duplicate over-weighting"],
      cost: "Write p99 added: +1.865 ms, with repeats +0.976, +1.976 and +8.730 ms, against 1 ms. Query p50 0.06× and p95 0.80× today. Embeds equal the ideal.",
      keeps: "Every quality check: ideal embeds, 0 orphans, 18/18 rollbacks, 36/36 slots, 50/50 + 50/50 findable",
      fakes: "The representative: the latest frame of the group in the window",
      failsWhen: "The write path pays for a random-position index insert on every frame, which shows up in checkpoint-heavy p99 tails.",
      condition: "Refuter PARTIAL: groups must be `(text_key, title, url)`, because `frames_fts` weights title 3.0 and url 2.0. The migration needs a Python backfill before the guard triggers are installed, because `db.migrate` runs SQL only.",
      priorArt: { name: "LangChain CacheBackedEmbeddings (skip half only)", url: "https://raw.githubusercontent.com/langchain-ai/langchain/master/libs/langchain/langchain_classic/embeddings/cache.py" },
      values: { video_reduction_pct: 89.7, static_reduction_pct: 99.2, embeds_vs_ideal: 1, groups_ok: 36, orphans: 0, write_add_p99_ms: 1.865, write_add_p50_ms: 0.034, query_p50_ratio: 0.0565, query_p95_ratio: 0.797 },
      estimated: false,
      capture: "C1.png",
      metricsLine: "video:sent=2590/ideal=2590 rows=2590 MB=1.33 red=89.7% static:sent=32/ideal=32 rows=32 MB=0.02 red=99.2% mixed:sent=37330/ideal=37330 rows=37330 MB=19.11 red=21.0% twovid:sent=15/ideal=15 rows=15 MB=0.01 red=99.2% orphans=0 rollback=18/18 survivors=5/5 expired_returned=0 stale_sem=0 rows_after_delete_all=0 findable_kw=50/50 findable_sem=50/50 groups_ok=36/36 mean_groups=3.11 top1_agree=33/36 win_viol=0 ts_mismatch=0 write_add_p50=0.034ms write_add_p99=1.865ms q_p50=2.9ms(today 52.1) q_p95=266.9ms(today 334.9) unkeyed_hash=0 raw_leak=0 upgrade=ok"
    },
    {
      id: "C5", name: "Shared passages + per-sighting collapse", status: "fails",
      statusNote: "Write-path p99 is borderline: +1.722, +1.586, +0.627 and +1.761 ms over 4 runs, against today's self-noise of −0.64 to +0.79 ms. With WAL autocheckpoint off it is +0.544 ms. Its per-sighting collapse is the alternative to use only if results should list each viewing separately.",
      method: "Same storage as C1, with a 16-byte HMAC key. The index is widened to `(text_key, ts_ms, window_title, url)`, and `frame_vec_horizon` holds the vector cutoff. A query collapses hits to `(text_key, title, url)` groups. Each group is split into runs at gaps over 15 min, at most 2 runs per group. Each run fills one slot, and its representative is its best-bm25 frame, otherwise its latest. `extra` carries `snippet`, `first_ts`, `last_ts` and `count`.",
      pipeline: [
        { label: "Capture insert: HMAC + text_key", cost: "HMAC 6 µs on 8.8 KB" },
        { label: "Index frames(text_key, ts_ms, title, url)", cost: "+0.04–0.06 ms p50" },
        { label: "Embed missing keys only", cost: "2,590 windows (video)" },
        { label: "Query: groups → RRF → split into runs (> 15 min gap)", cost: "runs 13.6 ms on a 17,280-frame key" },
        { label: "≤ 2 runs per group into 5 slots", cost: "p50 6.6 ms vs 62.8 ms" }
      ],
      uses: ["M1 Keyed text tag", "M5 Trigger-maintained shared rows", "M8 Post-fusion key collapse"],
      cost: "Write p99 added: +1.722 ms, with repeats +1.586, +0.627 and +1.761 ms, against 1 ms. Query p50 0.11× and p95 0.43× today. All-day text: keyword query 453 ms against 965 ms, and meaning-only 25 ms against 529 ms.",
      keeps: "Every quality check, plus sightings: a page seen 09:00–09:10 and 18:00–18:10 gets 2 slots for the whole day and 1 for the morning",
      fakes: "The representative of each sighting",
      failsWhen: "A page opened 1 min every 20 min makes 72 runs: 2 are shown and 70 are hidden without a count. Capture throttled to one frame every 16 min splits a one-hour viewing into 4 runs.",
      priorArt: { name: "Elasticsearch collapse", url: "https://www.elastic.co/guide/en/elasticsearch/reference/current/collapse-search-results.html" },
      values: { video_reduction_pct: 89.7, static_reduction_pct: 99.2, embeds_vs_ideal: 1, groups_ok: 36, orphans: 0, write_add_p99_ms: 1.722, write_add_p50_ms: 0.038, query_p50_ratio: 0.106, query_p95_ratio: 0.425 },
      estimated: false,
      capture: "C5.png",
      metricsLine: "video:sent=2590/ideal=2590 rows=2590 MB=1.33 red=89.7% static:sent=32/ideal=32 rows=32 MB=0.02 red=99.2% mixed:sent=37330/ideal=37330 rows=37330 MB=19.11 red=21.0% twovid:sent=15/ideal=15 rows=15 MB=0.01 red=99.2% orphans=0 rollback=18/18 survivors=5/5 expired_returned=0 stale_sem=0 rows_after_delete_all=0 findable_kw=50/50 findable_sem=50/50 groups_ok=36/36 mean_groups=3.11 top1_agree=34/36 win_viol=0 ts_mismatch=0 write_add_p50=0.038ms write_add_p99=1.722ms q_p50=6.6ms(today 62.8) q_p95=144.3ms(today 339.3) unkeyed_hash=0 raw_leak=0 upgrade=ok"
    },
    {
      id: "C2", name: "Lazy tags in a background job (compare-and-set)", status: "refuted",
      statusNote: "Not built. Refuted on its bound: the untagged backlog grows without limit while the worker is paused or a long job runs, and Ask still runs while paused. Capture adds up to ~30 frames per monitor per 30 s interval.",
      method: "Capture stays unchanged. A background job tags untagged frames with their HMAC key through compare-and-set, and embedding and grouping then work per key.",
      pipeline: [
        { label: "Capture insert, unchanged", cost: "+0" },
        { label: "Background tag job, compare-and-set", cost: "HMAC ~9 µs per frame, lag has no bound" },
        { label: "Embed per key, group at query", cost: "as C1" }
      ],
      uses: ["M1 Keyed text tag", "M5 Trigger-maintained shared rows", "M8 Post-fusion key collapse"],
      cost: "No write-path cost by design. Its correctness depends on a backlog that has no bound.",
      failsWhen: "The tag worker is paused or blocked behind a long job: untagged frames are neither collapsed nor shared.",
      priorArt: { name: "LlamaIndex IngestionPipeline (skip by hash)", url: "https://raw.githubusercontent.com/run-llama/llama_index/main/llama-index-core/llama_index/core/ingestion/pipeline.py" }
    },
    {
      id: "C4", name: "Generated-column key via an app-defined SQL function", status: "fails",
      statusNote: "Not built. On a connection without the function, an INSERT, an unrelated UPDATE and even a DELETE fail with \"unknown function\", which blocks capture, retention and full delete. With `trusted_schema` OFF SQLite rejects it as \"unsafe use\", and Python's `create_function` has no `SQLITE_INNOCUOUS` flag [7][8].",
      method: "`frames.text_key` as a generated column calling an HMAC function registered on each connection.",
      pipeline: [
        { label: "Register HMAC SQL function per connection", cost: "required on every connection" },
        { label: "Generated column text_key", cost: "computed on each write" },
        { label: "Shared passages + triggers", cost: "as C1" }
      ],
      uses: ["M1 Keyed text tag", "M5 Trigger-maintained shared rows"],
      cost: "Not measured: blocked by the platform.",
      failsWhen: "Any connection, tool or migration that has not registered the function touches `frames`.",
      priorArt: null
    }
  ],

  method: {
    summary: "C3: one passage set per keyed text, reached from frames through a link table. Capture is not touched, and the key, the embedding and the link are all created in the embed worker's publish transaction.",
    equation: "key(frame)          = HMAC-SHA256(K_frame_text, utf8(frame.text))   (32 B)\ntext_passages       : PK (key, model, start_char) -> (end_char, dim, vec fp16)\nframe_text_links    : PK (frame_id, model) -> key, frame_id REFERENCES frames ON DELETE CASCADE\nvectors(key, model) exist  <=>  some link (., model, key) exists\ngroup(frame)        = (key, window_title, url)\nscore(group)        = sum over {FTS list, vector list} of 1 / (60 + rank of group)",
    steps: [
      { text: "Migration 0019 creates `frame_text_links` with an index on `(key, model)`, and `text_passages`. It drops `frame_passages` and its update trigger.", assumes: "The subkey is derived with HKDF from the data key, like the existing subkeys [1]." },
      { text: "Trigger `AFTER DELETE ON frame_text_links WHEN NOT EXISTS (another link for old.key, old.model)` deletes the key's passages. Trigger `AFTER UPDATE OF text ON frames WHEN old.text IS NOT new.text` deletes the frame's links.", assumes: "`foreign_keys = ON` so the cascade fires the link trigger, set at `db.py:60` [6][23]. Nested triggers fire with `recursive_triggers` off [5][7]." },
      { text: "The embed worker selects frames with text ≥ 12 characters and no link for the model. It hashes each text (9.7 µs at 7.9 K characters), drops keys whose passages already exist, and embeds the rest in 400-character windows with a 300-character step.", assumes: "Identical text gives an equivalent vector, by cosine rather than bit for bit [15]." },
      { text: "Publish in one transaction. Re-read each text, keep only frames whose text is unchanged, insert passages and links with `INSERT OR IGNORE`, and skip a key whose vectors vanished since the check, so the next pass embeds it.", assumes: "Links are never written with `INSERT OR REPLACE`: with `recursive_triggers` off, REPLACE's delete fires no trigger [5][7]." },
      { text: "Deletion paths keep their shape. A frame delete cascades to its links, text expiry deletes them, and `expire_embeddings` deletes links older than the cutoff in batches of 500. The last link removes the vectors. `delete_all` deletes frames, then sweeps `text_passages`, which holds 0 rows when the data is consistent." },
      { text: "Query. FTS ranks frames as today with `bm25(frames_fts, 1.0, 3.0, 2.0)`, depth 200 [21]. Unlinked hits get an HMAC in Python, and frames whose text has expired stay one group each. The vector side scores each key once, using keys linked from frames in the window. Both lists become `(key, window_title, url)` groups and are fused with RRF k = 60.", assumes: "Title and url belong in the group: two videos with identical player text differ only in title." },
      { text: "Each of the 5 slots gets one real frame of its group, with its own `(frame_id, ts_ms)`: the best-bm25 frame, otherwise the latest. `extra` carries `snippet`, `also_seen` (≤ 50) and `also_seen_count`." },
      { text: "Upgrade from v18 in one transaction: hash frames that already have passages, copy one passage set per `(key, model)`, insert the links, drop `frame_passages`. On ~420 frames: 0.095 s, 0 re-embeds, 0 orphans, 20/20 findable." }
    ],
    cost: "Embeds: video 74 distinct texts → 2,590 windows against 25,200 (9.73× less GPU work); static page 32 against 3,840. Vector rows are 512 B (256 dims fp16): video 1.33 MB against 12.90 MB (−89.7 %), static 0.016 MB against 1.97 MB (−99.2 %), mixed −21.0 %. The DB file shrinks from 29.6 to 15.4 MB on video and from 4.8 to 2.3 MB on static. Links add one row per embedded frame (8 B id + model + 32 B key, plus an index entry). The write path adds 0 statements. The query adds at most 200 HMACs (~2 ms) for unlinked FTS hits, and measured p50 7.6 ms against 52.7 ms and p95 183.6 ms against 268.6 ms.",
    failsWhen: "One-character differences (a clock or a counter) → no sharing, as in mixed browsing (−21 % only). A long identical run → it can fill most of the 200 FTS candidates. `foreign_keys = OFF` or `INSERT OR REPLACE` on links → orphan vectors. Data-key rotation → every link and passage needs a re-key.",
    reproduce: "cd eval/identical-frame-text-reuse; ..\\.venv\\Scripts\\python.exe harness.py C3\\cand.py C3"
  },

  findings: [
    {
      question: "How do existing systems avoid re-embedding identical text?",
      claims: [
        "LangChain `CacheBackedEmbeddings` looks keys up with `mget`, embeds the misses and stores them with `mset`. It has no delete path, and its default key is an unkeyed SHA-1 [3].",
        "LlamaIndex `IngestionPipeline` docstore strategies skip a document by hash and delete by `ref_doc_id` on upsert. Rows shared between documents have no reference count [4].",
        "FTS5 indexes each rowid separately, so identical text is indexed once per frame [13].",
        "OpenRecall drops screenshots by image similarity (MSSIM ≥ 0.9), which is pixel-based and out of scope here [14]."
      ]
    },
    {
      question: "How can a content key stay private?",
      claims: [
        "An HMAC with a secret key cannot be recomputed from a guessed text without the secret [2]. Akashic already derives independent subkeys with HKDF [1].",
        "The harness self-test that groups by plain SHA-256 finds 2,960 hash hits. Every built candidate found 0."
      ]
    },
    {
      question: "Which SQLite rules decide whether trigger-based cleanup is complete?",
      claims: [
        "`ON DELETE CASCADE` runs only with `foreign_keys` on, and Akashic sets it on every connection [6][23].",
        "Nested triggers fire with `recursive_triggers` off. The delete done by `REPLACE` fires no delete trigger in that mode [5][7].",
        "`AFTER UPDATE OF <unknown column>` is accepted silently and never fires [16].",
        "A generated column that calls an application function fails on connections without the function, and `trusted_schema = OFF` rejects functions not marked innocuous [7][8]."
      ]
    },
    {
      question: "How do search engines stop duplicates from filling the top results?",
      claims: [
        "Elasticsearch `collapse` keeps the top hit per field value, returns the others through `inner_hits`, and works after an `rrf` retriever [9][10]. Solr and Vespa group results the same way [11][12].",
        "RRF adds `1/(k + rank)` from each list, so identical frames on consecutive ranks collect near-equal scores and crowd out other content [10]. Today 17/36 queries have correct slots, with a mean of 2.03 distinct groups."
      ]
    },
    {
      question: "Can identical text be detected from its vectors?",
      claims: [
        "No, not exactly. llama.cpp output for the same text varies with batch composition [15], so the harness matches vectors by cosine ≥ 0.999."
      ]
    },
    {
      question: "Where does today's duplication happen?",
      claims: [
        "`embed.py:176-195` embeds and publishes one passage set per frame [17] through `passages.publish` [18] into `frame_passages` [19]. `expire_embeddings` deletes per frame [20].",
        "`hybrid.py` ranks frames by bm25 weights 1.0 / 3.0 / 2.0 and RRF, then keeps 5 frames [21]."
      ]
    }
  ],

  mechanisms: [
    { id: "M1", name: "Keyed text tag", core: "`HMAC-SHA256(subkey, exact stored text)`, UTF-8 with surrogatepass, ~19 µs per 20 KB.", assumes: "The hashed string is the stored string, and the subkey is secret.", breaks: "The hashed and stored strings differ, tags are logged, or the key rotates. [1][2]" },
    { id: "M2", name: "Secret-keyed convergent identity", core: "Identical content maps to one key only for the key holder.", assumes: "The key stays out of reach.", breaks: "An unkeyed hash is used, which allows confirmation-of-file attacks (background: message-locked encryption, uncited)." },
    { id: "M3", name: "Cache-backed embeddings", core: "`mget`, embed misses, `mset`.", assumes: "Cache entries never need deleting.", breaks: "Deletion must propagate: there is no delete path, and the default key is unkeyed SHA-1. [3]" },
    { id: "M4", name: "Docstore hash strategies", core: "Skip by hash, and delete by `ref_doc_id` on upsert.", assumes: "Each stored row has one owner.", breaks: "Rows are shared: there is no reference count. [4]" },
    { id: "M5", name: "Trigger-maintained shared rows", core: "Delete a shared row when `NOT EXISTS` another referrer.", assumes: "Every writer sets the key, and every delete path runs triggers.", breaks: "A misspelled `UPDATE OF` column (silent), `foreign_keys` off, `REPLACE` with `recursive_triggers` off, or a writer bypassing the key. [5][6][7][16]" },
    { id: "M6", name: "Explicit reference counter", core: "Increment and decrement a count per shared row.", assumes: "Every path updates the count.", breaks: "The counter drifts from the real reference set." },
    { id: "M7", name: "Copy-on-embed", core: "Per-frame rows, with vectors copied from an existing key.", assumes: "Storage is cheap.", breaks: "The storage goal: rows stay ∝ frames." },
    { id: "M8", name: "Post-fusion key collapse", core: "Group hits by a key after ranking, keep one per group.", assumes: "The key captures \"same content\".", breaks: "The key is too coarse (identical player text across videos) or too fine. [9][10][11][12]" },
    { id: "M9", name: "RRF duplicate over-weighting", core: "Duplicates on consecutive ranks in both lists collect near-equal fused scores.", assumes: "Ranking happens per frame.", breaks: "Result diversity, whenever identical items are ranked separately. [10]" },
    { id: "M10", name: "FTS5 per-rowid index", core: "Every row is indexed separately.", assumes: "Rows are independent.", breaks: "Sharing: identical text is indexed once per frame. [13]" },
    { id: "M11", name: "Image-similarity dedupe", core: "Drop a screenshot when MSSIM ≥ 0.9.", assumes: "Similar pixels mean identical content.", breaks: "Video playback, where pixels change and text does not. A non-goal here. [14]" }
  ],

  explored: [
    { name: "C1 Shared content-addressed passages, write-path tag", reason: "Added write p99 was +1.865 ms, with repeats +0.976, +1.976 and +8.730 ms, against 1 ms. The random-key index on capture's write path enlarges WAL checkpoints. The mean is +0.06 to +0.15 ms per frame, and the HMAC alone is 9 µs." },
    { name: "C2 Lazy tags in a background job", reason: "Not built. The untagged window has no bound while the worker is paused or a long job runs, at up to ~30 frames per monitor per 30 s interval." },
    { name: "C4 Generated-column key via an app SQL function", reason: "Not built. Connections without the function cannot insert, update or delete frames, and `trusted_schema = OFF` rejects it [7][8]." },
    { name: "C5 Shared passages + per-sighting collapse", reason: "Added write p99 was +1.722, +1.586, +0.627 and +1.761 ms (+0.544 ms with autocheckpoint off). Its sightings hide 70 of 72 runs without a count when a page is opened 1 min every 20 min." },
    { name: "M6 Explicit reference counter", reason: "The count can drift. `NOT EXISTS` derives it from the rows instead." },
    { name: "M7 Copy-on-embed", reason: "Removes repeated GPU work, but stored vectors stay ∝ frames." },
    { name: "Grouping by text key alone", reason: "Merges two videos whose player text is identical but whose titles differ. Groups use `(key, window_title, url)`." }
  ],

  caveats: [
    "The embedder is a fake (a seeded bag-of-words model), not the GPU model. Embed counts and storage are exact, while semantic findability and ranking agreement only approximate the real model.",
    "Timings were measured on the target PC under concurrent load. Today's query p50 varies from 52 to 70 ms and p95 from 268 to 363 ms between runs.",
    "Write p99 is dominated by WAL checkpoints (a ~17–23 ms tail), with ±0.8–1.7 ms of run-to-run noise. C3's pass rests on its unchanged `insert_frame` and three reruns, not on a single run below 1 ms. The comparison table shows its one harness value of 1.194 ms.",
    "Dropping `frame_passages` leaves free pages in the file until `VACUUM`.",
    "The keyword candidate list still counts frames, so a long identical run can use most of the 200 FTS candidates.",
    "No key rotation exists today. Adding one would require re-keying links and passages.",
    "Embedding retention keeps per-frame semantics: C3 by construction, C1 and C5 through a cutoff floor. C5's upgrade takes the cutoff as a parameter.",
    "C3 `top1_agree` is 34/36: group-level RRF changes the top result in 2 queries.",
    "Refuter checks ran on stdlib SQLite 3.50.4; the app uses SQLite 3.54.1 through SQLCipher 4.20.0.",
    "M2's background (message-locked encryption) and the git-style reachability analogy for C3 are uncited."
  ],

  sources: [
    { n: 1, title: "Akashic HKDF subkeys", url: "src/akashic/storage/crypto.py:108-115", tier: 1, version: "pre-audit" },
    { n: 2, title: "RFC 2104, HMAC: Keyed-Hashing for Message Authentication", url: "https://www.rfc-editor.org/rfc/rfc2104", tier: 1, version: "1997" },
    { n: 3, title: "LangChain CacheBackedEmbeddings, cache.py", url: "https://raw.githubusercontent.com/langchain-ai/langchain/master/libs/langchain/langchain_classic/embeddings/cache.py", tier: 1, version: "master, read 2026-11-10" },
    { n: 4, title: "LlamaIndex IngestionPipeline, pipeline.py", url: "https://raw.githubusercontent.com/run-llama/llama_index/main/llama-index-core/llama_index/core/ingestion/pipeline.py", tier: 1, version: "main, read 2026-11-10" },
    { n: 5, title: "SQLite CREATE TRIGGER", url: "https://www.sqlite.org/lang_createtrigger.html", tier: 1, version: "current" },
    { n: 6, title: "SQLite foreign key support", url: "https://www.sqlite.org/foreignkeys.html", tier: 1, version: "current" },
    { n: 7, title: "SQLite PRAGMA statements", url: "https://www.sqlite.org/pragma.html", tier: 1, version: "current" },
    { n: 8, title: "SQLite generated columns", url: "https://www.sqlite.org/gencol.html", tier: 1, version: "current" },
    { n: 9, title: "Elasticsearch collapse search results", url: "https://www.elastic.co/guide/en/elasticsearch/reference/current/collapse-search-results.html", tier: 1, version: "current" },
    { n: 10, title: "Elasticsearch reciprocal rank fusion", url: "https://www.elastic.co/guide/en/elasticsearch/reference/current/rrf.html", tier: 1, version: "current" },
    { n: 11, title: "Solr result grouping", url: "https://solr.apache.org/guide/solr/latest/query-guide/result-grouping.html", tier: 1, version: "latest" },
    { n: 12, title: "Vespa grouping", url: "https://docs.vespa.ai/en/grouping.html", tier: 1, version: "current" },
    { n: 13, title: "SQLite FTS5", url: "https://www.sqlite.org/fts5.html", tier: 1, version: "current" },
    { n: 14, title: "OpenRecall screenshot.py", url: "https://raw.githubusercontent.com/openrecall/openrecall/main/openrecall/screenshot.py", tier: 1, version: "main, read 2026-11-10" },
    { n: 15, title: "llama.cpp PR 6950", url: "https://github.com/ggml-org/llama.cpp/pull/6950", tier: 1, version: "2024" },
    { n: 16, title: "SQLite docsrc check-in 998c5310ebb9d6e1", url: "https://sqlite.org/docsrc/info/998c5310ebb9d6e1", tier: 1 },
    { n: 17, title: "Akashic frame embedding loop", url: "src/akashic/pipeline/embed.py:176-195", tier: 1, version: "pre-audit" },
    { n: 18, title: "Akashic passage publish", url: "src/akashic/storage/passages.py:40", tier: 1, version: "pre-audit" },
    { n: 19, title: "Akashic frame_passages migration", url: "src/akashic/storage/migrations/0018_frame_passages.sql", tier: 1, version: "pre-audit" },
    { n: 20, title: "Akashic retention (expire_frame_text, expire_embeddings)", url: "src/akashic/storage/retention.py:120, 266", tier: 1, version: "pre-audit" },
    { n: 21, title: "Akashic hybrid frame ranking", url: "src/akashic/search/hybrid.py:515-560, 523, 628", tier: 1, version: "pre-audit" },
    { n: 22, title: "Akashic frame writer (set_text)", url: "src/akashic/storage/frames.py:129", tier: 1, version: "pre-audit" },
    { n: 23, title: "Akashic connection pragmas (foreign_keys = ON)", url: "src/akashic/storage/db.py:60", tier: 1, version: "pre-audit" }
  ]
};
