window.REPORT = {
  title: "Thread Follow-up Field Inheritance",
  date: "2026-11-10",
  depth: "full",
  context: "Akashic, Python 3.14, local gemma-4-12b-it-qat-q4_0 via llama-server b12209, RTX 4090 24 GB (test PC)",
  problem: "In an open chat thread, `follow_up` merges every earlier plan field into each new question, so a self-contained question like \"What video did I watch yesterday afternoon?\" asked after the Minlo question plans video + Minlo + \"game\" and finds nothing.",
  goals: [
    "A self-contained question asked in a thread plans exactly as if asked alone",
    "Elliptical follow-ups (\"and on Tuesday?\", \"what else did he say?\", \"et après ?\") inherit only the fields they leave out",
    "Works in French, English and mixed"
  ],
  contract: {
    must: ["Rule-only path still works", "Existing follow-up tests keep passing", "No new dependency", "At most one extra small LLM call per thread question when the model is loaded"],
    fake: ["Genuinely ambiguous questions may go either way"]
  },
  budget: ["≥ 95% correct inherit/no-inherit per field (people, kinds, keywords, time) on ≥ 150 labelled pairs, model loaded; rule-only fallback ≥ the best rules version", "0 target-pair errors", "Rule side ≤ 5 ms per question"],
  nonGoals: ["Coreference beyond the previous turn", "Answer generation", "Time-phrase resolution", "Retrieval ranking", "Planner kind/person cue rules"],

  answer: {
    text: "No candidate meets **95%** per field on unseen questions, so there is no best fit. The closest, C6 (an LLM relation code mapped to fields by a closed form of the labelling conventions), scores **0.933** on 165 fresh pairs (keywords), with 0 target errors and one call on 86% of thread questions. Today's `follow_up` scores 0.618 on the same set. Every candidate loses about 5 points between tuned and fresh sets. The closest prior art is CHIQ's binary topic-switch step [9]; LIF follow-up classifiers reach 78–86% F1 [3]."
  },
  bestFit: null,

  metrics: [
    { key: "min", label: "Lowest field (fresh set)", limit: 0.95, better: "higher", budget: true },
    { key: "people", label: "People", better: "higher" },
    { key: "kinds", label: "Kinds", better: "higher" },
    { key: "keywords", label: "Keywords", better: "higher" },
    { key: "time", label: "Time", better: "higher" },
    { key: "gate_us", label: "Rule gate p99", unit: "µs", limit: 5000, budget: true, log: true },
    { key: "call_ms", label: "LLM call p50", unit: "ms" }
  ],
  chart: { x: "gate_us", y: "min" },

  captureColumns: ["Per-field accuracy on the fresh set: candidate vs today's follow_up vs never-inherit, 95% line"],
  figures: [
    { src: "fig-generalisation.png", title: "Seen vs fresh question sets", caption: "Lowest per-field accuracy on the tuning set, heldout2 (110 pairs) and heldout3 (165 pairs). C5 and C6 were tuned on pairs and heldout2, so only heldout3 is unseen for them." }
  ],

  candidates: [
    {
      id: "C6", name: "LLM relation tagger + closed-form op map", status: "works",
      statusNote: "Closest working candidate: 0.933 < 0.95 on fresh heldout3.",
      method: "Plan the new question alone. If it is complete and fires no cue, keep it standalone with no call. Otherwise one grammar-constrained call returns a relation code (new/more/else/link, own topic, kind change, points back), and a closed form of the labelling conventions maps it to carry/drop per field. The rule-only fallback feeds the same closed form from regex cues.",
      pipeline: [{ label: "Standalone heuristic plan", cost: "≤ 0.1 ms" }, { label: "Cue gate (complete → no call)", cost: "≤ 0.14 ms" }, { label: "LLM relation code, 15 tokens", cost: "p50 1.1 s shared GPU" }, { label: "Closed-form op map", cost: "µs" }],
      uses: ["M10 LLM-call gating", "M7 Rewrite-type labels", "M4 Per-slot operations", "M1 Joint decision", "M3 Follow-up identification"],
      cost: "One call on 86% of thread questions; ~1.16k prompt tokens (1.13k cached) + 15 output; p50 1.12 s, p95 1.30 s with a second server on the GPU",
      keeps: "Rule-only path (0.903), existing tests, 0 target errors, no dependency",
      fakes: "Ambiguous relations",
      failsWhen: "An exclusion is phrased so the model reads it as \"more\"; a determiner points at a new entity; a partial shift keeps the topic; French phrasings (fr min 0.893).",
      condition: "Production must plan the new question without earlier-question context, or S and E already hold merged fields.",
      test: "heldout3 (165 fresh pairs) · per-field accuracy · pass ≥ 0.95",
      priorArt: { name: "CHIQ topic-switch step", url: "https://arxiv.org/html/2406.05013v2" },
      values: { min: 0.933, people: 0.988, kinds: 0.958, keywords: 0.933, time: 0.970, gate_us: 136, call_ms: 1120 },
      capture: "C6.png",
      metricsLine: "all_min=0.933 all_people=0.988 all_kinds=0.958 all_keywords=0.933 all_time=0.970 en_min=0.947 fr_min=0.893 mixed_min=0.942 target_errors=0 n=165"
    },
    {
      id: "C5", name: "Inverted routing: LLM decides every open field", status: "fails",
      statusNote: "0.927 < 0.95 on fresh heldout3.",
      method: "Rules decide only cue-free and complete questions. Every other thread question gets one call choosing carry or drop for each field whose standalone and earlier values differ.",
      pipeline: [{ label: "C1 cue gate (R0 tier)", cost: "≤ 0.3 ms" }, { label: "LLM carry/drop per live field", cost: "p50 931 ms shared GPU" }, { label: "Vetoes after merge", cost: "µs" }],
      uses: ["M10 LLM-call gating", "M4 Per-slot operations", "M1 Joint decision"],
      cost: "78% of questions routed; ~4.2k cached prompt tokens + ~19 output; p50 931 ms, p95 1.33 s (shared GPU)",
      failsWhen: "Conjunction-led questions (0.667) and time links (0.824).",
      values: { min: 0.927, people: 0.964, kinds: 0.952, keywords: 0.927, time: 0.982, gate_us: 483, call_ms: 931 },
      capture: "C5.png",
      metricsLine: "all_min=0.927 all_people=0.964 all_kinds=0.952 all_keywords=0.927 all_time=0.982 en_min=0.895 fr_min=0.929 mixed_min=0.962 target_errors=0 n=165"
    },
    {
      id: "C1", name: "Standalone plan + typed-cue gate + carry table (rules)", status: "fails",
      statusNote: "0.903 on heldout3 after one fix round; seen set 1.000, so the lexicons overfit.",
      method: "Plan the question alone, detect typed FR/EN cues (pronoun, deictic, time link, ellipsis, exclusion, continuation) with regex lexicons, and carry each empty field per a fixed table with vetoes.",
      pipeline: [{ label: "Standalone heuristic plan", cost: "≤ 0.1 ms" }, { label: "~40 regex cue lexicons", cost: "≤ 0.3 ms" }, { label: "Carry table + vetoes", cost: "µs" }],
      uses: ["M2 Rule carryover", "M7 Rewrite-type labels", "M4 Per-slot operations", "M6 Topic shift"],
      cost: "gate p99 306 µs, 0 calls",
      failsWhen: "Unseen continuation, pronoun and time-link phrasings; mixed FR/EN.",
      values: { min: 0.903, people: 0.976, kinds: 0.970, keywords: 0.903, time: 0.933, gate_us: 306, call_ms: 0 },
      capture: "C1.png",
      metricsLine: "all_min=0.903 all_people=0.976 all_kinds=0.970 all_keywords=0.903 all_time=0.933 target_errors=0 n=165"
    },
    {
      id: "C2", name: "C1 + LLM keep/drop for undecided cells only", status: "fails",
      statusNote: "Identical to C1 on heldout2 (0.900): 3 of 110 questions routed, none changed. Not scored on heldout3.",
      method: "C1's rules decide; only cells the rules mark undecided go to one grammar-constrained call.",
      pipeline: [{ label: "C1 rules", cost: "≤ 0.3 ms" }, { label: "LLM on undecided cells", cost: "~390 ms warm" }],
      uses: ["M10 LLM-call gating", "M4 Per-slot operations"],
      cost: "3/110 routed; ~390 ms warm per call",
      failsWhen: "The rules' errors sit in cells they consider decided.",
      values: { min: 0.900, people: 0.982, kinds: 0.964, keywords: 0.900, time: 0.964, gate_us: 192, call_ms: 390 },
      capture: "C2.png",
      metricsLine: "heldout2: all_min=0.900 all_people=0.982 all_kinds=0.964 all_keywords=0.900 all_time=0.964 n=110"
    },
    {
      id: "C3", name: "Single joint topic-shift bit", status: "fails",
      statusNote: "Keywords 0.636 on heldout3.",
      method: "One shift-or-follow-up decision per question; on a shift plan alone, otherwise inherit every empty field.",
      pipeline: [{ label: "Cue + completeness test", cost: "µs" }, { label: "All-or-nothing carry", cost: "µs" }],
      uses: ["M6 Topic shift", "M1 Joint decision"],
      cost: "gate p99 46 µs",
      failsWhen: "Partial shifts and \"what else\" carry stale topics.",
      values: { min: 0.636, people: 0.988, kinds: 0.970, keywords: 0.636, time: 0.952, gate_us: 46, call_ms: 0 },
      capture: "C3.png",
      metricsLine: "all_min=0.636 all_people=0.988 all_kinds=0.970 all_keywords=0.636 all_time=0.952 n=165"
    },
    {
      id: "C4", name: "Offline-fitted logistic link gate", status: "unbuilt",
      statusNote: "Not built: Calor-Dial has no licence; QReCC is English-only.",
      method: "Logistic gate over cue counts and anchor bits fitted on QReCC/Calor-Dial plus synthetic shift negatives.",
      pipeline: [{ label: "Features", cost: "~0.1 ms" }, { label: "Logistic gate", cost: "µs" }],
      uses: ["M9 Feature gate", "M7 Rewrite-type labels", "M8 French labels"],
      cost: "~0.1 ms, 0 calls",
      values: { gate_us: 100 }, estimated: true
    },
    {
      id: "C7", name: "LLM-distilled hashed n-gram relation classifier", status: "unbuilt",
      statusNote: "Not built: synthetic-data classifiers trail real data by ~18 points [10][11].",
      method: "Generate labelled paraphrases with the local LLM offline and fit a hashed n-gram softmax; rules only at runtime.",
      pipeline: [{ label: "Hashed n-grams", cost: "~0.1 ms" }, { label: "Softmax + closed form", cost: "~0.1 ms" }],
      uses: ["M9 Feature gate", "M7 Rewrite-type labels"],
      cost: "~0.2–0.45 ms, 0 calls, 256 KB weights",
      values: { gate_us: 450 }, estimated: true
    },
    {
      id: "C8", name: "Embedding nearest-exemplar cue detector", status: "relaxes",
      statusNote: "Works if the rule-side limit rises from 5 ms to 30 ms (one local embedding).",
      method: "kNN vote of the question's embedding over labelled exemplars, ORed into the regex cues.",
      pipeline: [{ label: "Local embedding", cost: "~10–30 ms" }, { label: "kNN vote", cost: "µs" }],
      uses: ["M9 Feature gate"],
      relaxes: "Rule-side cost from 5 ms to 30 ms; adds the embedder dependency and a `heuristic_plan` stoplist edit",
      cost: "~10–30 ms per question",
      values: { gate_us: 30000 }, estimated: true
    },
    {
      id: "C9", name: "Disagreement-routed LLM call with retrieved few-shots", status: "unbuilt",
      statusNote: "Not built: depends on the unbuilt C8 detector.",
      method: "Call the LLM only where rules and kNN disagree, with the nearest labelled pairs as few-shots.",
      pipeline: [{ label: "C1 + C8", cost: "~30 ms" }, { label: "LLM on disagreement", cost: "~0.5–0.8 s" }],
      uses: ["M10 LLM-call gating", "M9 Feature gate"],
      cost: "≤ 1 call on ~15–30% of questions",
      values: { gate_us: 30000 }, estimated: true
    }
  ],

  method: {
    summary: "C6 is the closest working candidate, not a best fit. The model only labels the relation; a closed form of the labelling conventions decides the fields, and values come only from the two standalone plans.",
    equation: "relation r ∈ {new, more, else, link}, flags o (own topic), k (kind change), t (points back)\nfield f = S.f if r = new or f stated\n        = E.f if r = more (keywords = S.kw if o)\n        = drop keywords if r = else\n        = drop people, keywords if k\n        = E.f otherwise",
    steps: [
      { text: "Plan the new question alone (S) and keep the earlier standalone plan (E).", assumes: "Production plans without earlier-question context." },
      { text: "Complete question with no regex cue → standalone, no call (both target pairs).", assumes: null },
      { text: "Otherwise one grammar-constrained call returns `[r, o, k, t]` in 15 tokens.", assumes: "The model labels relations better than it chooses field values." },
      { text: "Rule gates on the labels (t needs a deictic, k a stated kind or first person, o a real keyword).", assumes: "Gates fitted on seen data hold on fresh questions." },
      { text: "Closed form maps labels to fields; with perfect labels it reproduces gold on both seen sets except 3 rows whose labels conflict." }
    ],
    cost: "Rule gate p99 ≤ 136 µs. One call: ~1.16k prompt tokens (1.13k cached) + 15 output; p50 1.12 s, p95 1.30 s with the GPU shared.",
    failsWhen: "Exclusions read as \"more\", deictic determiners on new entities, partial shifts keeping the topic, and French phrasings.",
    reproduce: "eval/.venv/Scripts/python.exe eval/thread-follow-up-field-inheritance/harness.py R2C2 --pairs heldout3.jsonl --reps 1 (with R2C2_LLM=1 and a local server)"
  },

  findings: [
    { question: "How is the carry decision made in the literature?", claims: [
      "Per-slot carryover: rule baseline P 91.8 / R 67.1; neural F1 84.2 in-house, 97.4 on DSTC2; carry-everything F1 28.7 [1].",
      "Deciding slots jointly beats independent decisions on long dialogues [2].",
      "Follow-up identification on LIF: F1 77.9–86.1, precision about 70–80% [3].",
      "Instructed LLM rewriting reaches CAsT-19 recall@500 0.645 vs 0.737 manual, with no self-contained gate [4]; CHIQ uses an LLM new/old-topic label with no reported accuracy [9]."
    ]},
    { question: "What do the data say about cues?", claims: [
      "QReCC rewrites: replacement 52%, insertion 38%, copy 9%, so pronoun cues alone miss ellipsis [5]; pronouns occur in 53.9% of QuAC questions [6].",
      "Calor-Dial labels French self/coreference/ellipsis on 1,663 questions [7]; its repository states no licence.",
      "A simple-feature ambiguity classifier (92.2%) beat few-shot GPT-3.5 (72.7%) [8]."
    ]},
    { question: "Does synthetic training data reach real-data accuracy?", claims: [
      "LLM-generated intent data gives 70–77% on CLINC150 unseen domains vs 95.4% with real data [10]; the gap widens on subjective tasks [11]."
    ]}
  ],

  mechanisms: [
    { id: "M1", name: "Joint slot carryover", core: "p(carry | slot, utterance, context), slots decided together.", assumes: "Labelled data.", breaks: "Across domains (F1 72). [1][2]" },
    { id: "M2", name: "Rule carryover", core: "Carry on an anaphor or a missing slot of matching type.", assumes: "Lexical cues.", breaks: "Implicit ellipsis (R 67). [1]" },
    { id: "M3", name: "Follow-up identification", core: "Binary follow-up/shift classifier.", assumes: "In-domain shift examples.", breaks: "Precision 70–80%. [3]" },
    { id: "M4", name: "Per-slot operations", core: "{carry, update, delete} per field.", assumes: "Fixed schema.", breaks: "Open vocabulary. [1]" },
    { id: "M5", name: "Query rewriting", core: "Rewrite the question to stand alone.", assumes: "Meaning kept.", breaks: "Changed meaning, hallucinated entities. [4][6]" },
    { id: "M6", name: "Topic shift", core: "Detect a new topic and reset context.", assumes: "Shifts are detectable.", breaks: "Partial shifts. [14]" },
    { id: "M7", name: "Rewrite-type labels", core: "copy / insertion / replacement.", assumes: "Minimal rewrites.", breaks: "Paraphrased rewrites. [5]" },
    { id: "M8", name: "French ellipsis labels", core: "Calor-Dial self/coreference/ellipsis.", assumes: "A usable licence.", breaks: "No licence stated. [7]" },
    { id: "M9", name: "Feature gate", core: "Cheap features (optionally an embedding) → classifier.", assumes: "In-domain data.", breaks: "Out of domain. [8]" },
    { id: "M10", name: "Gated LLM call", core: "Rules or disagreement decide when to call the model.", assumes: "Rule confidence tracks rule error.", breaks: "Confident rule errors (measured in C2). [13]" }
  ],

  explored: [
    { name: "C1 Typed-cue gate (rules)", reason: "Seen set 1.000, fresh sets 0.900 and 0.903: the lexicons do not generalise." },
    { name: "C2 LLM on undecided cells", reason: "Routed 3 of 110 fresh questions and changed nothing." },
    { name: "C3 Single shift bit", reason: "Keywords 0.636 on fresh data." },
    { name: "C4 Logistic gate on public corpora", reason: "Calor-Dial has no licence; QReCC is English-only." },
    { name: "C5 Inverted routing", reason: "Fresh 0.927; conjunctions 0.667, time links 0.824." },
    { name: "C7 Offline-distilled classifier", reason: "Synthetic-data classifiers trail real data by about 18 points [10]." },
    { name: "C8 / C9 Embedding kNN and disagreement routing", reason: "Need the rule-side limit raised to ~30 ms and a stoplist change; the kNN alone caps keywords near 0.945 by arithmetic on C1's error rows." }
  ],
  caveats: [
    "All labels were written by agents under 11 conventions, not by the user; heldout2 and heldout3 were written independently of the candidates by the same kind of author, and they label weekday-only earlier topics inconsistently (3 rows).",
    "pairs.jsonl and heldout2 were used for tuning C5 and C6; only heldout3 is unseen for them.",
    "S and E come from `heuristic_plan` on each question alone; the production plan call currently sees earlier questions.",
    "Latency was measured on an RTX 4090 (this PC; AGENTS.md lists an RTX 4080 Super) with a second llama-server sharing the GPU (~23 GB in use). Uncontended latency is not measured.",
    "Model outputs at temperature 0 changed one row between identical runs (server batching)."
  ],
  sources: [
    { n: 1, title: "Naik et al., Contextual slot carryover for disparate schemas", url: "https://arxiv.org/abs/1806.01773", tier: 1, version: "Interspeech 2018" },
    { n: 2, title: "Chen et al., Improving long distance slot carryover", url: "https://aclanthology.org/W19-4111/", tier: 1, version: "2019" },
    { n: 3, title: "Kongyoung, Macdonald, Ounis, Multi-task query generation and classification", url: "https://eprints.gla.ac.uk/307893/2/307893.pdf", tier: 1, version: "2023" },
    { n: 4, title: "Rewriting conversational utterances with instructed LLMs", url: "https://arxiv.org/html/2410.07797v1", tier: 1, version: "2024 preprint" },
    { n: 5, title: "Anantha et al., QReCC", url: "https://arxiv.org/abs/2010.04898", tier: 1, version: "2021" },
    { n: 6, title: "Elgohary et al., CANARD", url: "https://aclanthology.org/D19-1605/", tier: 1, version: "EMNLP 2019" },
    { n: 7, title: "Béchet et al., Calor-Dial", url: "https://ceur-ws.org/Vol-3178/CIRCLE_2022_paper_28.pdf", tier: 1, version: "2022" },
    { n: 8, title: "Detecting ambiguities to guide query rewrite in enterprise AI assistants", url: "https://arxiv.org/html/2502.00537v1", tier: 1, version: "2025 preprint" },
    { n: 9, title: "CHIQ: contextual history enhancement for query rewriting", url: "https://arxiv.org/html/2406.05013v2", tier: 1, version: "EMNLP 2024" },
    { n: 10, title: "Generate then Refine: data augmentation for zero-shot intent detection", url: "https://arxiv.org/html/2410.01953v1", tier: 1, version: "2024 preprint" },
    { n: 11, title: "Li et al., Synthetic data generation with LLMs for text classification", url: "https://aclanthology.org/2023.emnlp-main.647.pdf", tier: 1, version: "EMNLP 2023" },
    { n: 12, title: "SetFit: efficient few-shot learning without prompts", url: "https://arxiv.org/abs/2209.11055", tier: 1, version: "2022" },
    { n: 13, title: "FrugalGPT", url: "https://arxiv.org/abs/2305.05176", tier: 1, version: "2023" },
    { n: 14, title: "Mele et al., Topic propagation in conversational search", url: "https://arxiv.org/abs/2004.14054", tier: 1, version: "2020" },
    { n: 15, title: "src/akashic/search/planner.py:215-310", url: "src/akashic/search/planner.py", tier: 1, version: "local" },
    { n: 16, title: "src/akashic/pipeline/ask.py:260-340", url: "src/akashic/pipeline/ask.py", tier: 1, version: "local" }
  ]
};
