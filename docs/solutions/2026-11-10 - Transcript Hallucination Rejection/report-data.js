window.REPORT = {
  title: "Transcript Hallucination Rejection",
  date: "2026-11-10",
  depth: "full",
  context: "Akashic, Python 3.14, faster-whisper 1.2.1 / CTranslate2 4.8.5, Whisper large-v3 as shipped, Silero v6 ONNX, RTX 4090 (test PC)",
  problem: "Whisper writes fluent text for non-speech audio (subtitle credits, \"thanks for watching\", echoes of the vocabulary prompt). Today's filter is a whole-string list plus a log-prob threshold of −2.0 that disables faster-whisper's own no-speech skip, so 27% of hallucinations are stored as confident transcripts.",
  goals: [
    "Non-speech text (prompt echoes, no-speech output, subtitle credits) is not stored as a confident transcript",
    "Real FR/EN speech is kept, including short replies and vocabulary names",
    "Uncertain cases are flagged \"unclear\" rather than dropped"
  ],
  contract: {
    must: ["Real-speech false rejection ≤ 1% of utterances", "Works with Whisper large-v3 (Qwen3-ASR / Canary where the backend allows)", "No new Python dependency; a new local model is allowed", "Captured text is never logged"],
    fake: ["Hallucinated words inside real speech, mid-sentence"]
  },
  budget: ["Hallucination recall (rejected or unclear) ≥ 90%", "Real-speech false rejection ≤ 1%", "≤ 1 extra decode per suspected segment"],
  nonGoals: ["VAD and segmentation changes", "Music-drop rules", "Language identification", "trim_overlap", "The STT comparison panel"],

  answer: {
    text: "**C7, a decode-statistics logistic gate**, fits. Five numbers Whisper already returns per segment (no_speech_prob, avg_logprob, compression ratio, log word count, duration) feed one fitted logistic that decides keep / unclear / reject. On the test split (253 hallucinations, 1,157 real speech units) it reaches recall **0.921** against 0.727 today. Its false rejection is **0.86%** (ub95 1.58%): the same 10 segments today rejects, because Whisper returned them empty or matched the credit list. It adds 0 new false rejections and cuts real speech flagged unclear from 16.8% to **9.8%**, with no extra decode and no extra time. C3 (a CTC alignment key with 2.5 GB of wav2vec2 heads) reaches 0.949 but turns most of what it catches into \"unclear\" (22% of real speech). Prior art: faster-whisper's no-speech rule [1], which Akashic's −2.0 threshold disables, and a learned fusion of the same statistics on a different task [7].",
    relaxed: ""
  },
  bestFit: "C7",

  metrics: [
    { key: "recall", label: "Hallucination recall (reject or unclear)", limit: 0.90, better: "higher", budget: true },
    { key: "frr", label: "Real-speech false rejection", unit: "%", limit: 1.0, budget: true },
    { key: "reject", label: "Reject-only recall", better: "higher" },
    { key: "unclear", label: "Real speech flagged unclear", unit: "%" },
    { key: "extra", label: "Extra time vs decode", unit: "×" }
  ],
  chart: { x: "unclear", y: "recall" },

  captureColumns: ["score distributions, hallucination vs real speech, test split", "keep / unclear / reject per subset"],
  figures: [
    { src: "fig-tradeoff.png", title: "Recall vs real speech flagged unclear", caption: "Test split. Today: 0.727 recall with 16.8% of real speech unclear. C7 and C5 fusion reach 0.921 with fewer unclear; C3 reaches 0.949 at 22%; pure contrast needs 71%." }
  ],

  candidates: [
    {
      id: "C7", name: "Decode-statistics logistic gate", status: "best",
      statusNote: "Recall 0.921 ≥ 0.90; FRR 0.86% (identical to today); 0 extra decodes.",
      method: "A logistic regression over five statistics from the primary decode: no_speech_prob, avg_logprob, gzip compression ratio, log word count, duration. It is fitted 2-fold by source group on a mining split. P ≥ 0.996 → reject, P ≥ 0.105 → unclear, otherwise keep. Empty text and today's whole-string list still reject.",
      pipeline: [{ label: "Primary decode (unchanged)", cost: "today" }, { label: "Read 5 statistics", cost: "0" }, { label: "Dot product + 2 thresholds", cost: "µs" }],
      uses: ["M1 Whisper no-speech gate", "M2 Compression-ratio guard", "M7 Token-posterior confidence"],
      cost: "0 extra decodes, 0× time; store no_speech_prob per segment",
      keeps: "0 new false rejections among the 1,147 segments today keeps",
      fakes: "Hallucinated words inside real speech",
      failsWhen: "Fluent short ghosts with confident statistics (\"Welcome back.\"); laughter decoded as \"Ha, ha\"; the reject threshold sits just above the highest real-speech out-of-fold P (0.9987), so it is fragile.",
      priorArt: "faster-whisper no-speech rule [1]; HALAS learned fusion [7]",
      test: "Test split · 253 hallucinations, 1,157 real speech units · recall ≥ 0.90, FRR ≤ 1%",
      values: { recall: 0.921, frr: 0.86, reject: 0.375, unclear: 9.8, extra: 0 },
      capture: "C7.png",
      metricsLine: "recall=0.921 reject=0.375 frr=0.0086 (ub 0.0158) unclear=0.098 new_false_rejects=0 decile_gap=0.134"
    },
    {
      id: "C3", name: "CTC monotonic-alignment key (wav2vec2 FR + EN heads)", status: "works",
      statusNote: "Recall 0.949, FRR 0.35%, but reject-only 0.04 and 22% of real speech unclear; test split seen once before iteration 2.",
      method: "Force-align the transcript with Apache-2.0 wav2vec2-xlsr-53 CTC heads (FR and EN) and score per-character alignment support G. Low G → unclear; reject only with a speech-presence guard.",
      pipeline: [{ label: "Primary decode", cost: "today" }, { label: "CTC emission (FR or EN head)", cost: "0.08× time" }, { label: "Forced alignment score", cost: "ms" }],
      uses: ["M8 CTC forced-alignment score", "M12 VAD speech ratio", "M13 Two-signal rule"],
      cost: "0 decodes; 0.08× time; +1.2–1.6 GB VRAM; 2.5 GB of model files",
      failsWhen: "High G cannot tell noise from real speech under a hallucinated transcript; the xlsr heads emit letters on noise.",
      values: { recall: 0.949, frr: 0.35, reject: 0.043, unclear: 22.0, extra: 0.08 },
      capture: "C3.png",
      metricsLine: "recall=0.949 reject=0.043 frr=0.0035 (ub 0.0089) unclear=0.220"
    },
    {
      id: "C5", name: "Audio × prompt likelihood contrast + fusion", status: "works",
      statusNote: "Fusion meets recall (0.921, unclear 8.0%) but the contrast cells get ~0 weight; it is C7 plus 0.20× time.",
      method: "Score the transcript under real audio, silence and noise, with and without the prompt; fuse the contrasts with the decode statistics in a logistic.",
      pipeline: [{ label: "Primary decode", cost: "today" }, { label: "Teacher-forced passes", cost: "0.20× time" }, { label: "Logistic fusion", cost: "µs" }],
      uses: ["M7 Token-posterior confidence", "M11 Prompt-copy check", "M13 Two-signal rule"],
      cost: "0 decodes; 0.20× time (align passes)",
      failsWhen: "Pure contrast: short noise ghosts are better supported by noise than by silence (S_P AUC 0.69).",
      values: { recall: 0.921, frr: 0.86, unclear: 8.0, extra: 0.20 },
      capture: "C5.png",
      metricsLine: "fusion recall=0.921 frr=0.0086 unclear=0.080 · pure contrast recall=0.881 unclear=0.713"
    },
    {
      id: "C1", name: "Two-key gate (credit list, echo, rate, VAD, no-speech)", status: "fails",
      statusNote: "Recall 0.826 < 0.90; short-reply FRR 2.1%.",
      method: "Reject only when two independent keys fire: an expanded FR/EN credit list, a prompt echo, an implausible word rate, a low Silero speech ratio, or no_speech_prob.",
      pipeline: [{ label: "Primary decode", cost: "today" }, { label: "Silero re-run on segment", cost: "0.10× time" }, { label: "Key rules", cost: "µs" }],
      uses: ["M1 Whisper no-speech gate", "M2 Compression-ratio guard", "M4 Bag of Hallucinations", "M11 Prompt-copy check", "M12 VAD speech ratio", "M13 Two-signal rule"],
      cost: "0 decodes; 0.10× time",
      failsWhen: "Fluent prompt-free ghosts fire no key; spoken outros are routed to unclear.",
      values: { recall: 0.826, frr: 0.86, unclear: 17.5, extra: 0.10 },
      capture: "C1.png",
      metricsLine: "recall=0.826 frr=0.0086 (short 0.0208) unclear=0.175"
    },
    {
      id: "C2", name: "Prompt-removal re-decode", status: "refuted",
      statusNote: "Refuted: removing the prompt leaves the training prior (ghosts survive), and its reject rule cannot fire on echo-only suspects.",
      method: "Re-decode suspected segments without the vocabulary prompt and compare.",
      pipeline: [{ label: "Suspect rule", cost: "µs" }, { label: "Re-decode", cost: "1 decode" }],
      uses: ["M10 Cross-model agreement", "M11 Prompt-copy check"],
      cost: "1 decode per suspect",
      values: {}, estimated: true
    },
    {
      id: "C4", name: "Backend-split calibrated confidence (Qwen3-ASR / Canary)", status: "unbuilt",
      statusNote: "Not built: the fixture has no Qwen3-ASR or Canary run.",
      method: "Per-backend calibrated token confidence with the same keep / unclear / reject thresholds.",
      pipeline: [{ label: "Backend decode", cost: "today" }, { label: "Calibrated confidence", cost: "µs" }],
      uses: ["M7 Token-posterior confidence"],
      cost: "0 decodes",
      values: { extra: 0 }, estimated: true
    },
    {
      id: "C6", name: "Time-reversed control stimulus", status: "unbuilt",
      statusNote: "Not built: the contrast family proved redundant in C5.",
      method: "Score the transcript against time-reversed audio as a control for language-model prior.",
      pipeline: [{ label: "Reverse audio", cost: "µs" }, { label: "Teacher-forced pass", cost: "~1.3 encoder passes" }],
      uses: ["M7 Token-posterior confidence", "M11 Prompt-copy check"],
      cost: "~1.3 encoder passes",
      values: {}, estimated: true
    }
  ],

  method: {
    summary: "C7 reads five statistics from the decode Akashic already runs, combines them in one fitted logistic, and maps the probability to keep / unclear / reject with two thresholds.",
    equation: "z = (x − μ) / σ, x = [no_speech_prob, avg_logprob, compression_ratio, ln(words), duration_s]\nP = σ(−2.818 + 1.001·z₁ − 1.481·z₂ + 2.400·z₃ − 1.249·z₄ + 0.163·z₅)\nμ = [0.0558, −0.3350, 0.8978, 2.1161, 5.0150]   σ = [0.1211, 0.2895, 0.4017, 1.1265, 4.0591]\nP ≥ 0.996 → reject; P ≥ 0.105 → unclear; else keep. Empty text or whole-string list hit → reject (as today).",
    steps: [
      { text: "Read no_speech_prob (first window), avg_logprob, gzip compression ratio, log word count and duration from the primary decode.", assumes: "These statistics separate real FR/EN speech from Whisper output on non-speech; no_speech_prob alone has mining AUC 0.92." },
      { text: "Fit the logistic on a mining split with standardised features, IRLS and 2-fold cross-fitting by source group." },
      { text: "Reject at P ≥ 0.996, flag unclear at P ≥ 0.105, keep otherwise.", assumes: "The reject threshold sits just above the highest real-speech out-of-fold P (0.9987); it is fragile." },
      { text: "Keep LOG_PROB_THRESHOLD at −2.0 so faster-whisper never drops text silently; the gate judges after the decode. Empty text and the whole-string list still reject." }
    ],
    cost: "Five numbers per segment plus a dot product; no extra decode or encode.",
    failsWhen: "Real voices recorded in noise (announcements, radio, phone menus) are kept as speech; fluent short ghosts with confident statistics; laughter decoded as \"Ha, ha\"; calibration decile gap 0.13 (target 0.1).",
    reproduce: "eval/.venv/Scripts/python.exe -I eval/transcript-hallucination-rejection/harness.py C5_decode.py --name C5_decode"
  },

  findings: [
    { question: "How often does Whisper hallucinate, and on what?", claims: [
      "On non-speech audio, large-v3 produced text in 40.3% of 301k inferences; 1,270 repeated outputs cover 67% of them, dominated by credits and \"thanks for watching\" [2].",
      "Silero VAD alone cut WER on mixed audio from 104.8 to 8.0 [2].",
      "Calm-Whisper fine-tunes three decoder heads to cut non-speech hallucination from 99.97% to 15.5% [3]."
    ]},
    { question: "Do faster-whisper's built-in gates catch it?", claims: [
      "The no-speech skip needs no_speech_prob > 0.6 and avg_logprob ≤ log_prob_threshold; with Akashic's −2.0 it almost never fires [1].",
      "Tuning the no-speech threshold alone barely moves the hallucination rate (46–47% at 0.5–0.6) [4].",
      "hallucination_silence_threshold needs word timestamps and flags one-word replies by construction [1]."
    ]},
    { question: "What did this run measure?", claims: [
      "On the user's own short replies, today's code already marks 45% unclear.",
      "The 10 false rejections every candidate shares are clips where Whisper heard no reference word at all (e.g. \"super\" decoded as a Radio-Canada credit); rejecting them removes no real word."
    ]}
  ],

  mechanisms: [
    { id: "M1", name: "Whisper no-speech gate", core: "no_speech_prob with an avg_logprob exception.", assumes: "First-token no-speech probability separates silence from speech.", breaks: "Confident noise ghosts. [1][4]" },
    { id: "M2", name: "Compression-ratio guard", core: "gzip ratio of the text.", assumes: "Hallucinations repeat.", breaks: "Short fluent ghosts. [1]" },
    { id: "M3", name: "hallucination_silence_threshold", core: "Skip silence around anomalous word timings.", assumes: "Ghosts have odd word timings.", breaks: "One-word replies. [1]" },
    { id: "M4", name: "Bag of Hallucinations", core: "Mined phrase list with substring removal.", assumes: "Ghosts come from a closed list.", breaks: "English-only; legitimately spoken phrases. [2]" },
    { id: "M5", name: "Calm-Whisper head fine-tune", core: "Fine-tune three decoder heads.", assumes: "Model change allowed.", breaks: "English only. [3]" },
    { id: "M7", name: "Token-posterior / entropy confidence", core: "Aggregate token posteriors.", assumes: "Posteriors track correctness.", breaks: "Confidently wrong on stock phrases. [5]" },
    { id: "M8", name: "CTC forced-alignment score", core: "Monotonic alignment support of the text under a CTC model.", assumes: "The CTC alphabet covers the text.", breaks: "Short or noisy audio; xlsr heads emit letters on noise. [6]" },
    { id: "M10", name: "Cross-model agreement", core: "A second decode must agree.", assumes: "Uncorrelated errors.", breaks: "Shared training prior. (design)" },
    { id: "M11", name: "Prompt-copy check", core: "Text overlapping the prompt is suspect.", assumes: "Echoes depend on the prompt.", breaks: "Real use of vocabulary names. (design)" },
    { id: "M12", name: "VAD speech ratio", core: "Silero speech fraction of the segment.", assumes: "VAD rejects noise.", breaks: "Silero false alarms on noise. [2]" },
    { id: "M13", name: "Two-signal rule", core: "Reject only on two independent signals.", assumes: "Signals fail independently.", breaks: "Correlated signals. (design)" }
  ],

  explored: [
    { name: "C1 Two-key gate", reason: "Recall 0.826: fluent prompt-free ghosts fire no key; reject-only recall fell to 0.31 as spoken outros go to unclear." },
    { name: "C5 Pure contrast", reason: "Short noise ghosts are better supported by noise than by silence (S_P AUC 0.69); 90% recall marks 71% of real speech unclear." },
    { name: "C5 Fusion", reason: "Meets recall, but the decode-only control (C7) matches it with no align passes." },
    { name: "C3 CTC key", reason: "High G cannot tell noise from real speech under a hallucinated transcript; safe rejection needs a speech-presence guard, so it mostly flags." },
    { name: "C2 Re-decode without prompt", reason: "The training prior survives (99.97% non-speech hallucination with no prompt [3])." },
    { name: "C4 Backend scoring", reason: "The fixture has no Qwen3-ASR / Canary run." },
    { name: "C6 Time-reversed control", reason: "The contrast family proved redundant in C5." }
  ],
  caveats: [
    "About 33 of C1's 44 misses, and most of C3/C5/C7's, are real recorded voices inside weakly labelled FSD50K clips (announcements, ATC radio, voicemail), judged from transcripts, not by listening; true recall is likely higher for every candidate.",
    "\"≤ 1% on every subset\" fails for short replies, concatenations and French for today and every candidate, because of the same 10 pre-existing rejections; only \"≤ 1% of utterances\" overall is met.",
    "Speech is read speech (FLEURS, MSWC words); the only channel effect is Opus at 32 kbps; there is no game audio under a voice.",
    "Trailing credits after real speech almost never occur (n=1), so edge stripping is untested; every segment was decoded as a single Whisper window.",
    "FSD50K-dev is split per clip, so recordings from the same uploader may sit in both splits.",
    "C3's second iteration was triggered by its first test result.",
    "Timings are ratios measured while other jobs shared the GPU (an RTX 4090; AGENTS.md lists an RTX 4080 Super).",
    "C1 recomputed Silero on the stored Opus segment, because the app does not store per-window VAD probabilities."
  ],
  sources: [
    { n: 1, title: "faster-whisper transcribe.py", url: "https://raw.githubusercontent.com/SYSTRAN/faster-whisper/master/faster_whisper/transcribe.py", tier: 1, version: "1.2.1" },
    { n: 2, title: "Barański et al., Whisper hallucinations induced by non-speech audio", url: "https://arxiv.org/html/2501.11378", tier: 1, version: "2025" },
    { n: 3, title: "Calm-Whisper", url: "https://arxiv.org/html/2505.12969", tier: 1, version: "2025 preprint" },
    { n: 4, title: "Hallucination space projection for Whisper", url: "https://arxiv.org/html/2609.04561", tier: 1, version: "2026 preprint" },
    { n: 5, title: "Laptev & Ginsburg, entropy-based ASR confidence", url: "https://arxiv.org/abs/2212.08703", tier: 1, version: "2022" },
    { n: 6, title: "torchaudio CTC forced-alignment tutorial", url: "https://docs.pytorch.org/audio/stable/tutorials/ctc_forced_alignment_api_tutorial.html", tier: 1, version: "stable" },
    { n: 7, title: "From text metrics to model internals: Whisper hallucination detection", url: "https://arxiv.org/html/2606.23060", tier: 1, version: "2026 preprint" },
    { n: 8, title: "WhisperX alignment context", url: "https://aphasia.talkbank.org/publications/2024/Wagner24.pdf", tier: 2, version: "2024" },
    { n: 9, title: "AVCD, audio-visual contrastive decoding", url: "https://arxiv.org/html/2505.20862v2", tier: 1, version: "2025" },
    { n: 10, title: "src/akashic/pipeline/whisper.py", url: "src/akashic/pipeline/whisper.py", tier: 1, version: "local" },
    { n: 11, title: "src/akashic/pipeline/transcribe.py", url: "src/akashic/pipeline/transcribe.py", tier: 1, version: "local" }
  ]
};
