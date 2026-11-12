window.REPORT = {
  title: "Segment Speaker Attribution",
  date: "2026-11-10",
  depth: "full",
  context: "Akashic, Python 3.14, onnxruntime as shipped, pyannote segmentation-3.0 ONNX, WeSpeaker ResNet34-LM ONNX, numpy; CPU only (test PC RTX 4090 unused)",
  problem: "A segment loses its author whenever any diarization turn in it is unidentified. Fixed 10 s windows leave short tail turns unidentified, a continuation's 2 s lead is diarized as the previous speaker, and short second voices are merged into the main speaker, so wrong authors reach 7.9%.",
  goals: [
    "Single-speaker segments keep their author whatever their length or window alignment",
    "Mixed segments are never attributed to the wrong person (unattributed, or per span)",
    "A voice profile does not drift from marginal matches"
  ],
  contract: {
    must: ["Wrong-author rate no worse than today", "Additive schema only", "Offline at runtime; no new runtime Python dependency", "CPU only", "Any other voice ≥ 300 ms makes the segment unattributed (no absorption)", "Allowed: a different local model; a dev-only onnx-derived model file with pinned checksum; a deferred author for 30 s cuts"],
    fake: ["Speech under 300 ms", "Turn boundaries within ±0.5 s"]
  },
  budget: ["Solo-segment authorship ≥ 95%", "Wrong-author rate ≤ 1%", "Compute ≤ 2× today's speakers job per segment"],
  nonGoals: ["Word-level attribution", "Separating overlapping speech", "Naming and merging UI", "Junk profiles from media audio", "The fbank window check"],

  answer: {
    text: "No candidate reaches **95%** authorship with **≤ 1%** wrong authors, so there is no best fit. Today scores 0.701 / 7.94% on the same 1,008 Opus-compressed FR/EN segments. **C7** (closed-form span statistics from one extra embedder output, spread windows, same-voice merges) has the highest authorship, **0.927**, with 3.17% wrong at 1.35× CPU, but 2 of 300 segments exceed 2×. **C9** (exact-copy seam reconciliation with a deferred author, on C2) cuts early-switch wrong authors from 12 to 3 for 0.902 / 2.88%. **C8** (a fitted gate on C7) gets closest to the wrong-author limit, 1.19%, but authorship falls to 0.733. The residual shared by all is upstream: segmentation decodes a short second voice as the main speaker's solo speech (2,723 of 2,760 frames), so no CPU-side test on the shipped segmentation model sees it.",
    relaxed: "X3 (a GPU segmentation model such as Sortformer or DiariZen) is the only route aimed at the merged-voice residual; it needs the GPU, a torch/NeMo export, and DiariZen's weights are CC BY-NC. X1 (absorbing minor voices) and X2 (GPU dense verification) were declined."
  },
  bestFit: null,

  metrics: [
    { key: "auth", label: "Authorship (single enrolled speaker)", limit: 0.95, better: "higher", budget: true },
    { key: "wrong", label: "Wrong-author rate", unit: "%", limit: 1.0, budget: true },
    { key: "cont2", label: "Continuation second-half authorship", better: "higher" },
    { key: "mixed", label: "Wrong authors on mixed segments", unit: "%" },
    { key: "cost", label: "Total CPU vs today", unit: "×" },
    { key: "costmax", label: "Worst segment vs today", unit: "×", limit: 2.0, budget: true }
  ],
  chart: { x: "wrong", y: "auth" },

  captureColumns: ["Authorship by subset vs today", "Wrong-author rate by subset vs today; drift panels below"],
  figures: [
    { src: "ref.png", title: "Today's pipeline (reference)", caption: "Authorship 0.701, wrong 7.94%. Segments 0.5–2 s past a 10 s boundary keep 46%; continuation second halves 42%; wrong authors 32% on mixed segments." },
    { src: "fig-seam.png", title: "Seam reconciliation on continuation pairs (C9 over C2)", caption: "40 same / 50 switch / 30 switch-early pairs. Early-switch first-half wrong authors fall from 40% to 10%; first-switch authorship falls from 86% to 80%. The second-half collapse to 21% appears only when the lead counts as body (t0 and the no-rule control), not from the seam rule." }
  ],

  candidates: [
    {
      id: "C7", name: "Closed-form span statistics + leave-one-out change test", status: "fails",
      statusNote: "Highest authorship: 0.927 with 3.17% wrong > 1%; 2/300 segments over 2×.",
      method: "An extra output on the embedder's pre-pooling frame map (dev-only onnx, pinned sha) with the head rebuilt in numpy; full-span embedding equals today's (cos ≥ 0.9999999). Windows as many as today, evenly spread, nearest-centre ownership; same-voice merge at cos ≥ 0.60 for channels ≥ 1 s; prefix sums make any span one Gemm; a Δ change test vetoes; C2's calibrated decision.",
      pipeline: [{ label: "Segmentation, spread windows", cost: "today" }, { label: "Frame map + prefix sums", cost: "1 embedder pass per window" }, { label: "Merges + span Gemms", cost: "~ms" }, { label: "Calibrated LLR decision", cost: "<0.1 ms" }],
      uses: ["M1 Pyannote overlapped windows", "M2 Overlap-add", "M4 Solo-frame pooling", "M7 Duration-aware calibration", "M9 Margin"],
      cost: "1.35× total, 1.41× p50, 1.75× p95, max 2.32× (BLAS 1 thread); default BLAS threading 1.61× total, max 3.30×",
      keeps: "Stored profiles valid; no drift; additive schema",
      failsWhen: "Backchannels under 1 s merged by segmentation (11 of 32 wrong); early switches at 30 s cuts (10); impostors under looser fitted thresholds (7).",
      priorArt: "pyannote solo pooling [1]; QMF calibration [12]",
      test: "1,008 segments + 500-segment drift stream · authorship ≥ 0.95, wrong ≤ 1%, ≤ 2× per segment",
      values: { auth: 0.927, wrong: 3.17, cont2: 0.905, mixed: 8.5, cost: 1.35, costmax: 2.32 },
      capture: "C7.png",
      metricsLine: "solo_auth=0.9266 solo10k_auth=0.9405 solo10_0_auth=0.9464 cont2_auth=0.9052 wrong=0.0317 wrong_mixed=0.0852 wrong_impostor=0.0875"
    },
    {
      id: "C9", name: "Exact-copy seam reconciliation + deferred author (on C2)", status: "fails",
      statusNote: "Lowest wrong rate with authorship kept: 0.902 / 2.88%; first-half authorship −3.3 pts.",
      method: "A segment cut at 30 s stays NULL until its continuation's job runs. Waveform correlation (≥ 0.5) confirms the 2 s copy; one 10 s window spliced at the copy and centred on the cut judges the seam; every channel ≥ 300 ms there needs ≥ 1 s of solo evidence identified as the segment's author, otherwise NULL. t1 uses an additive `trimmed` column to exclude the lead; t0 counts the lead as body.",
      pipeline: [{ label: "Continuation's base decision", cost: "today" }, { label: "Copy check", cost: "~ms" }, { label: "Seam-centred window", cost: "1 segmentation + 1 embedding" }, { label: "Revision", cost: "1 UPDATE" }],
      uses: ["M4 Solo-frame pooling", "M9 Margin"],
      cost: "1.17× per pair; 1.51–1.59× on the continuation segment; 9% of segments deferred about one minute",
      failsWhen: "Segmentation detects < 300 ms of the next voice before the cut; the centred window merges both voices into one channel; the next voice speaks < 1 s across the cut (a lost authorship).",
      priorArt: "none found",
      values: { auth: 0.902, wrong: 2.88, cont2: 0.879, mixed: 13.6, cost: 1.17, costmax: 2.20 },
      capture: "C9.png",
      metricsLine: "t1: solo_auth=0.9017 cont2_auth=0.8793 wrong=0.0288 · t0: solo_auth=0.8366 cont2_auth=0.4655 wrong=0.0308 · over C1_linked t1: 0.8753 / 0.0387"
    },
    {
      id: "C8", name: "Posterior-proposed spans + fitted gate (proposals dropped)", status: "fails",
      statusNote: "Wrong 1.19% (closest to 1%) but authorship 0.733, below today on solo.",
      method: "C7 plus one logistic gate over LLR sum, margin, minimum turn LLR, merge LLR, overlap, posterior maxima and span z. The proposal step was dropped: posterior proposals recalled 0.42 of merged voices at ≤ 8 per segment (AUC 0.71).",
      pipeline: [{ label: "C7 pipeline", cost: "1.35×" }, { label: "Logistic gate", cost: "µs" }],
      uses: ["M7 Duration-aware calibration", "M9 Margin"],
      cost: "1.40× total, max 2.38× (BLAS 1 thread)",
      failsWhen: "The gate nulls 193 segments (140 solo); its weights flip sign between folds.",
      values: { auth: 0.733, wrong: 1.19, cont2: 0.802, mixed: 3.4, cost: 1.40, costmax: 2.38 },
      capture: "C8.png",
      metricsLine: "solo_auth=0.7327 solo10k_auth=0.7202 solo10_0_auth=0.8214 cont2_auth=0.8017 wrong=0.0119 wrong_mixed=0.0341 wrong_impostor=0.0125"
    },
    {
      id: "C2", name: "Calibrated duration-bucketed fusion + margin + veto", status: "fails",
      statusNote: "0.906 / 3.77%; up to 3.6× on 10.3–10.5 s segments.",
      method: "Cosine → LLR per duration bucket (logistic, 2-fold over speakers); author = argmax of clipped LLR sum with a threshold, a margin and a minimum-turn veto; segmentation and overlap vetoes; a stricter update gate.",
      pipeline: [{ label: "Today's windows", cost: "today" }, { label: "Short-turn embeddings", cost: "≤ 3 × ~10 ms" }, { label: "LLR fusion", cost: "<0.1 ms" }],
      uses: ["M5 Gated update", "M7 Duration-aware calibration", "M9 Margin"],
      cost: "1.17× total; max 3.6× (2.2× at BLAS 1 thread)",
      failsWhen: "Segmentation merges a short second voice (24 of 38 wrong); early switches (12 of 38).",
      priorArt: "QMF calibration [12]",
      values: { auth: 0.906, wrong: 3.77, cont2: 0.879, mixed: 13.6, cost: 1.17, costmax: 3.6 },
      capture: "C2.png",
      metricsLine: "solo_auth=0.9058 solo10k_auth=0.9226 solo10_0_auth=0.9107 cont2_auth=0.8793 wrong=0.0377 wrong_mixed=0.1364 wrong_impostor=0.0250"
    },
    {
      id: "C1", name: "Linked overlapped windows, pooled identity", status: "fails",
      statusNote: "0.880 / 4.56%.",
      method: "10 s windows with hop ≤ 5 s, labels linked by solo-frame IoU with cannot-link, one pooled embedding per global speaker, a single-speaker author rule and a ≥ 1 s other-profile veto.",
      pipeline: [{ label: "Overlapped windows", cost: "~5 × 20 ms" }, { label: "IoU linking", cost: "µs" }, { label: "Pooled embedding", cost: "~50–120 ms" }],
      uses: ["M1 Pyannote overlapped windows", "M2 Overlap-add", "M4 Solo-frame pooling", "M9 Margin"],
      cost: "1.13× p50, 1.21× p95",
      failsWhen: "Segmentation merges a short second voice; impostors pass at 0.5; a spurious unidentified component nulls the segment.",
      priorArt: "diart cannot-link [2]; pyannote [1]",
      values: { auth: 0.880, wrong: 4.56, cont2: 0.741, mixed: 17.6, cost: 1.13 },
      capture: "C1.png",
      metricsLine: "solo_auth=0.8795 solo10k_auth=0.8750 solo10_0_auth=0.9643 cont2_auth=0.7414 wrong=0.0456 wrong_mixed=0.1761 wrong_impostor=0.0625"
    },
    {
      id: "C6", name: "Embedder swap (plain WeSpeaker ResNet34)", status: "fails",
      statusNote: "0.712 / 7.94%: no change.",
      method: "Swap ResNet34-LM for plain ResNet34 (better EER in every bucket up to 8 s on the fixture).",
      pipeline: [{ label: "Today's pipeline", cost: "today" }, { label: "Other ONNX embedder", cost: "~1.0×" }],
      uses: ["M10 Alternative ONNX embedders"],
      cost: "~1.0× (dual variant 1.94×)",
      failsWhen: "Leaves 101 unembedded short turns and 62 lead failures; a matched threshold erases the gain.",
      priorArt: "WeSpeaker [9]",
      values: { auth: 0.712, wrong: 7.94, cont2: 0.431, mixed: 32.4, cost: 1.0 },
      capture: "C6.png",
      metricsLine: "solo_auth=0.7119 solo10k_auth=0.5060 solo10_0_auth=0.9821 cont2_auth=0.4310 wrong=0.0794 wrong_mixed=0.3239"
    },
    {
      id: "C3", name: "AS-norm against a duration-matched cohort", status: "unbuilt",
      statusNote: "Not built: a scoring layer only, and stricter than today at 1–2 s.",
      method: "Normalise scores against a shipped 4 MB Opus FR/EN cohort per duration bucket, thresholds at 0.2% false accept.",
      pipeline: [{ label: "Cohort scores", cost: "~0.1 ms" }],
      uses: ["M7 Duration-aware calibration", "M8 AS-norm", "M9 Margin"],
      cost: "~1.003× + 4 MB cohort",
      priorArt: "Nautsch 2015 [8]",
      values: { cost: 1.003 }, estimated: true
    },
    {
      id: "C4", name: "Anchored profiles + CUSUM drift alarm", status: "unbuilt",
      statusNote: "Not built: in a synthetic run the CUSUM never fired; the gate and margin did the work.",
      method: "Anchor templates from the first accepted turns and a CUSUM on accepted scores that freezes updates.",
      pipeline: [{ label: "Update gate + anchor", cost: "µs" }],
      uses: ["M5 Gated update", "M6 Confidence-weighted adaptation", "M9 Margin"],
      cost: "~1.00×",
      priorArt: "Heck & Mirghafori [4]",
      values: { cost: 1.0 }, estimated: true
    },
    {
      id: "C5", name: "Call-level clustering revision", status: "unbuilt",
      statusNote: "Not built: rewriting a call's authors can raise wrong authors above today.",
      method: "Cluster a whole call's turns offline and fill NULL authors.",
      pipeline: [{ label: "Call clustering", cost: "per call" }],
      uses: ["M1 Pyannote overlapped windows"],
      cost: "per call",
      values: {}, estimated: true
    },
    {
      id: "X3", name: "GPU segmentation swap (Sortformer / DiariZen)", status: "relaxes",
      statusNote: "Only route aimed at the merged-voice residual.",
      method: "Replace pyannote segmentation-3.0 with a stronger GPU diarizer.",
      pipeline: [{ label: "GPU segmentation", cost: "GPU" }],
      uses: ["M11 Heavier diarizers"],
      relaxes: "CPU only; dev-only torch/NeMo export; DiariZen weights CC BY-NC",
      cost: "GPU",
      values: {}, estimated: true
    },
    {
      id: "X1", name: "Minor-voice absorption", status: "relaxes",
      statusNote: "Declined: no absorption of voices ≥ 300 ms.",
      method: "Attribute a segment to its main voice when another voice is short and unidentified.",
      pipeline: [{ label: "Absorption rule", cost: "µs" }],
      uses: ["M4 Solo-frame pooling"],
      relaxes: "Any other voice ≥ 300 ms makes the segment unattributed",
      cost: "µs",
      values: {}, estimated: true
    },
    {
      id: "X2", name: "GPU dense verification", status: "relaxes",
      statusNote: "Declined: CPU only.",
      method: "Dense sliding embeddings on the GPU to find short second voices.",
      pipeline: [{ label: "Dense embeddings", cost: "GPU" }],
      uses: ["M7 Duration-aware calibration"],
      relaxes: "CPU only",
      cost: "GPU",
      values: {}, estimated: true
    }
  ],

  method: {
    summary: "C7 is the candidate with the highest authorship, not a fit. One extra embedder output gives exact span embeddings; windows are spread and frames pooled; same-voice channels merge; C2's calibrated LLR decides.",
    equation: "h = frame map (256·10 × T′) before pooling; per channel: prefix sums n, Σh, Σh²\ne(S) = W·[μ_S ; σ_S] + b − mean_vec   (σ unbiased; e(all) = today's embedding, cos ≥ 0.9999999)\nmerge channels ≥ 1 s when cos ≥ 0.60\nΔ_j = LLR(e_j, k₂) − LLR(e_j, k*); NULL if max Δ > τ_k\nauthor k* if Σ clip(LLR) ≥ τ_a ∧ margin ≥ m ∧ no veto",
    steps: [
      { text: "Derive the embedder with an extra output on the pre-pooling tensor (dev-only onnx, pinned sha256) and rebuild the pooling head in numpy.", assumes: "onnxruntime accepts the derived graph; gate (a) passed on 200 turns." },
      { text: "Run as many segmentation windows as today, starts spread evenly, each frame owned by the nearest window centre." },
      { text: "Merge same-voice channels (≥ 1 s, cos ≥ 0.60) and keep counting their overlap.", assumes: "0 of 48 different-voice pairs merged out of fold." },
      { text: "Test sub-spans and edge spans with Δ; NULL above τ_k fitted to ≤ 1.5% solo nulls.", assumes: "A short second voice changes the span embedding; mostly false here, since segmentation merges it." },
      { text: "Decide with C2's calibrated LLR sum, margin and update gate; update profiles from full-turn vectors only." }
    ],
    cost: "1.35× total, 1.75× p95, max 2.32× with OPENBLAS_NUM_THREADS=1; default BLAS threading on a loaded CPU makes the Gemm 100× slower.",
    failsWhen: "Short backchannels merged by segmentation; early switches at 30 s cuts; impostors under fitted thresholds.",
    reproduce: "eval/.venv/Scripts/python.exe eval/segment-speaker-attribution/harness.py candidates/R2span_C1.py"
  },

  findings: [
    { question: "Why does today fail?", claims: [
      "Authorship 0.701, and 0.464 on segments 0.5–2 s past a 10 s boundary: the tail window's short turn stays unidentified and one unidentified turn clears the author.",
      "Continuation second halves keep 0.422: the previous speaker's 2 s lead is diarized.",
      "Wrong authors 7.94% overall, 32.4% on two-speaker segments: short second voices are missed or merged, so the mixed rule never fires."
    ]},
    { question: "What can identification fix?", claims: [
      "Duration matters: EER 9.1% at 0.3–1 s, 2.5% at 1–2 s, 1.3% at 2–4 s on the fixture; published short-probe EER rises the same way [5][6].",
      "Per-bucket calibration reaches Cllr close to PAV (C2) [12].",
      "The dominant residual is segmentation merging a short second voice into the main channel with no overlap frames: 24 of 38 wrong authors in C2; the hidden voice's frames decode as the main channel's solo speech 2,723 of 2,760 times."
    ]},
    { question: "Do profiles drift?", claims: [
      "No candidate and not today drifted on the 500-segment two-similar-speaker stream (0 wrong in the first and last 100).",
      "C8's fewer updates leave one centroid at 0.905 to its clean enrolment (today 0.957)."
    ]}
  ],

  mechanisms: [
    { id: "M1", name: "Pyannote overlapped-window pipeline", core: "10 s windows, 90% overlap, solo-frame embeddings, global clustering.", assumes: "Local segmentation is right.", breaks: "Brief activity in a chunk. [1]" },
    { id: "M2", name: "Hamming overlap-add", core: "Weighted average of activity and count across chunks.", assumes: "Labels do not permute.", breaks: "Raw local channels. [1]" },
    { id: "M3", name: "Diart incremental clustering", core: "Cannot-link; only long speakers update centroids.", assumes: "Tuned thresholds.", breaks: "Short speakers forced onto a free centroid. [2]" },
    { id: "M4", name: "Solo-frame pooling across windows", core: "Pool a speaker's solo frames, embed once.", assumes: "Linking is right.", breaks: "Merged voices. (design)" },
    { id: "M5", name: "Gated online centroid update", core: "Top-1 only, above a threshold.", assumes: "Clean seeds.", breaks: "Threshold too low or high. [3]" },
    { id: "M6", name: "Confidence-weighted adaptation", core: "Update weight scales with confidence.", assumes: "Calibrated confidence.", breaks: "Impostor bursts. [4]" },
    { id: "M7", name: "Duration-aware calibration", core: "θ(d) or LLR per duration bucket.", assumes: "Trials match live audio.", breaks: "Channel shift. [5][6][12]" },
    { id: "M8", name: "AS-norm", core: "Cohort score normalisation.", assumes: "Duration- and channel-matched cohort.", breaks: "Cohort mismatch. [7][8]" },
    { id: "M9", name: "Margin over the runner-up", core: "s1 − s2 ≥ m.", assumes: "A runner-up exists.", breaks: "One profile. (design)" },
    { id: "M10", name: "Alternative ONNX embedders", core: "ResNet34, CAM++, ResNet293.", assumes: "Same fbank front end.", breaks: "LM variants on < 3 s. [9]" },
    { id: "M11", name: "Heavier diarizers", core: "pyannote community-1, DiariZen, Sortformer.", assumes: "torch or NeMo.", breaks: "CPU-only, licences." },
    { id: "M12", name: "Simulated-conversation fixture", core: "Interleaved per-speaker speech with real pause and overlap statistics.", assumes: "Statistics transfer.", breaks: "Under-produced overlap. [10][11]" }
  ],

  explored: [
    { name: "C1 Linked windows", reason: "0.880 / 4.56%; zero-padded edge windows dropped authorship to 0.716." },
    { name: "C2 Calibrated fusion", reason: "0.906 / 3.77%; up to 3.6× on 10.3–10.5 s segments." },
    { name: "C6 Embedder swap", reason: "No change once thresholds are matched; short turns and leads untouched." },
    { name: "C7 change test", reason: "The span-vs-rest test fitted to τ = ∞; Δ nulls 1 solo segment. Gains come from spread windows, merges and refits." },
    { name: "C8 proposals", reason: "Recall 0.42 at ≤ 8 proposals (AUC 0.71); the proposal step was dropped; gate weights unstable." },
    { name: "C9 iteration 1", reason: "N+1's first window merged the lead voice with the next speaker: −11 to −16 pts first-half authorship." },
    { name: "C9 lead as body (t0)", reason: "Without the `trimmed` column, second-half authorship falls 41 pts on C2, with or without the rule." },
    { name: "C3 AS-norm", reason: "A scoring layer only; stricter than today at 1–2 s; cohort must ship." },
    { name: "C4 CUSUM", reason: "Never fired in a synthetic run; the gate did the work." },
    { name: "C5 Call-level clustering", reason: "Can raise wrong authors above today." }
  ],
  caveats: [
    "Read speech (LibriSpeech EN, MLS French) with synthetic noise and Opus; no game audio, Krisp or real Discord channel. Thresholds fitted here may be too strict or too loose on real calls.",
    "Thresholds and gates were fitted 2-fold over disjoint speakers on the same 1,008 segments they are scored on; there was no separate held-out set. C7's merge threshold is one value for both folds.",
    "Wrong-author counts are small (C7 32, C8 12), so ±0.3 pts at 1%.",
    "Timing ratios were measured back to back on a shared CPU; C9's metrics-line latencies come from parallel runs. C7/C8 need a BLAS thread limit.",
    "C7 + C9 combined was not measured.",
    "The drift stream has no segment ≥ 30 s, so C9 equals its base there.",
    "AGENTS.md lists an RTX 4080 Super; the test PC has an RTX 4090. The GPU was unused."
  ],
  sources: [
    { n: 1, title: "pyannote speaker_diarization.py / inference.py", url: "https://raw.githubusercontent.com/pyannote/pyannote-audio/refs/heads/main/src/pyannote/audio/pipelines/speaker_diarization.py", tier: 1, version: "main" },
    { n: 2, title: "Coria et al., diart", url: "https://arxiv.org/abs/2109.06483", tier: 1, version: "2021" },
    { n: 3, title: "Sholokhov et al., household speaker recognition", url: "https://arxiv.org/pdf/2205.00288", tier: 1, version: "2022" },
    { n: 4, title: "Heck & Mirghafori, confidence-weighted adaptation", url: "https://www.microsoft.com/en-us/research/?p=166284", tier: 2, version: "" },
    { n: 5, title: "Short-duration speaker verification by duration", url: "https://arxiv.org/pdf/2609.25007", tier: 1, version: "2026 preprint" },
    { n: 6, title: "ERes2NetV2", url: "https://arxiv.org/abs/2406.02167", tier: 1, version: "2024" },
    { n: 7, title: "AS-norm analysis", url: "https://arxiv.org/html/2504.04512v1", tier: 1, version: "2025" },
    { n: 8, title: "Nautsch et al., condition-matched cohorts", url: "https://www.isca-archive.org/interspeech_2015/nautsch15_interspeech.html", tier: 1, version: "2015" },
    { n: 9, title: "WeSpeaker pretrained models", url: "https://github.com/wenet-e2e/wespeaker/blob/master/docs/pretrained.md", tier: 1, version: "master" },
    { n: 10, title: "Landini et al., simulated conversations", url: "https://ar5iv.labs.arxiv.org/html/2204.00890", tier: 1, version: "2022" },
    { n: 11, title: "Multilingual LibriSpeech", url: "https://www.openslr.org/94/", tier: 1, version: "" },
    { n: 12, title: "Mandasari et al., quality measure functions", url: "https://www.sri.com/publication/speech-natural-language-pubs/quality-measure-functions-for-calibration-of-speaker-recognition-systems-in-various-duration-conditions/", tier: 1, version: "2013" },
    { n: 13, title: "src/akashic/pipeline/speakers.py", url: "src/akashic/pipeline/speakers.py", tier: 1, version: "local" },
    { n: 14, title: "src/akashic/storage/speakers.py", url: "src/akashic/storage/speakers.py", tier: 1, version: "local" }
  ]
};
