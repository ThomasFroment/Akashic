window.REPORT = {
  title: "Overlapping Speech",
  date: "2026-11-20",
  depth: "full",
  context: "Python 3.14, faster-whisper 1.2.1 large-v3 (float16, beam 5, FR/EN choice), pyannote segmentation 3.0 + WeSpeaker ResNet34, onnxruntime CPU, WASAPI process loopback via comtypes, Windows 11 26200, RTX 4080 Super; harness replays of the standard (1.08 h) and day (6.87 h) quality runs on simulated per-process streams, plus a 72-mixture synthetic set",
  problem: "When two audio sources overlap (a call over a video or a game, two videos, two people in one call), Akashic transcribes the system stream badly: M9.1 overlap WER 0.639 (standard) and 0.497 (day) against 0.097 and 0.084 clean, M8.2 DER 0.249 and 0.161, and other apps' sound leaks into segments (M7.1 11.7 s and 25.6 s) [1][3]. All of the overlap error is on the system stream, mostly deletions of the quieter talker [3].",
  goals: ["Transcribe each speaker correctly when two sources overlap", "Attribute overlapping speech to the right source and speaker", "Stop other apps' sound leaking into the wrong segment (per-process separation, #110)"],
  contract: {
    must: ["Harness M9.1 overlap WER at most 0.25 on the standard and day runs", "Clean-speech WER (EN, FR, mixed) no worse than today", "M8.2 DER no worse than today", "No audio of an excluded app stored"],
    fake: ["Speech of a third simultaneous source can be dropped", "Word timing within 0.5 s", "Music under speech treated as noise"]
  },
  budget: ["GPU at most +30 % of today's transcription GPU time, as a daily average (worst hour reported)", "VRAM at most +1.5 GB while transcribing", "Capture-side CPU at most +5 % of one core", "Fully local; no new dependency without approval (MossFormer2_SS_16K approved for measurement only)"],
  constraints: ["Process loopback takes one process tree per client (include or exclude)", "Communications-category streams come back silent in process loopback on this PC (local control -96.7 dBFS)", "Each WASAPI client resamples to 16 kHz on its own", "faster-whisper decodes a window as one token sequence in one forced language"],
  nonGoals: ["Speaker enrolment UI", "Transcript language detection (#139, already right)", "Music recognition", "Ask quality", "Real-time captions"],

  answer: {
    text: "No candidate keeps every must within the limits. The closest is **C1**: up to three audible non-voice apps each get a process-loopback include stream, the call is what remains of the endpoint loopback after a 128-tap FIR subtracts those stems, each stream is segmented and transcribed on its own, and any system row overlapping an excluded app's sound is dropped [1][2]. It keeps overlap WER (0.231 standard, 0.193 day), EN and FR clean WER, DER (0.147 and 0.090) and the exclusion (0 s), within budget (GPU x1.15 daily by a two-parameter model, VRAM +0.04 GB, capture CPU about 2 % of a core), and fails one must: mixed FR/EN clean WER on the day run rises from 0.151 to 0.202, because a residual call row holding an English stretch then a French one is decoded in one language and the other is deleted [1]. **C6** (C1 plus overlap-gated MossFormer2 separation on every output stream) is identical to C1 on both runs, since its gate fired 0 times there, and better only on synthetic same-app mixes (cpWER 0.247 against 0.575) [1]. **C4** (gated separation on today's streams) fails overlap WER (0.597 and 0.458) [1]. Every figure comes from simulated per-process streams, and the harness call is category Other, so the silence of real Communications-category streams in process loopback is not exercised [1][5]. Prior art: OBS application audio capture and win-capture-audio for per-app streams [12][22], WebRTC AEC3's delay estimation for the fitted subtraction [19], PixIT for gated separation [14].",
    relaxed: "If the non-goal 'transcript language detection (#139)' relaxes so that a system row of 5 s or more with an unclear FR/EN choice is cut at an energy dip whose two sides get different languages, each side decoded with its own language, **C6 + language split** keeps every must on both scorings: overlap WER 0.223 / 0.156, mixed clean 0.161 / 0.096, DER 0.148 / 0.089, excluded 0 s; GPU x1.23 (standard) and x1.13 (day) as a daily average by model, worst clock hour x1.26, densest 6-minute window x1.97 measured; VRAM +0; capture CPU about 2 % [1]. The gate never fired on the real runs, so C1 + language split equals it on both runs (inferred from the gate counts, not run separately)."
  },
  bestFit: null,

  metrics: [
    { key: "gpu", label: "GPU per day of audio vs today (two-parameter model, worse run)", unit: "x", limit: 1.3, budget: true },
    { key: "vram", label: "VRAM added while transcribing", unit: "GB", limit: 1.5, budget: true },
    { key: "cpu", label: "Capture-side CPU", unit: "% core", limit: 5, budget: true },
    { key: "gpu_win", label: "GPU on the densest 6-min window vs today (measured)", unit: "x" },
    { key: "gpu_hour", label: "GPU worst clock hour of the day run (model)", unit: "x" },
    { key: "ovl_std", label: "M9.1 overlap WER, standard", limit: 0.25 },
    { key: "ovl_day", label: "M9.1 overlap WER, day", limit: 0.25 },
    { key: "ovl_np_std", label: "Overlap WER, standard, paused speech removed", limit: 0.25 },
    { key: "ovl_np_day", label: "Overlap WER, day, paused speech removed", limit: 0.25 },
    { key: "en_std", label: "Clean EN WER, standard (today 0.1132)", limit: 0.1132 },
    { key: "fr_std", label: "Clean FR WER, standard (today 0.1175)", limit: 0.1175 },
    { key: "mixed_std", label: "Clean mixed WER, standard (today 0.1659)", limit: 0.1659 },
    { key: "en_day", label: "Clean EN WER, day (today 0.0593)", limit: 0.0593 },
    { key: "fr_day", label: "Clean FR WER, day (today 0.0835)", limit: 0.0835 },
    { key: "mixed_day", label: "Clean mixed WER, day (today 0.1510)", limit: 0.151 },
    { key: "der_std", label: "M8.2 DER, standard (today 0.2491)", limit: 0.2491 },
    { key: "der_day", label: "M8.2 DER, day (today 0.1611)", limit: 0.1611 },
    { key: "excl", label: "Stored transcript over excluded app sound", unit: "s", limit: 0 },
    { key: "wrong_day", label: "M8.3 wrong authors, day (today 46)" },
    { key: "syn_cpwer", label: "Synthetic cpWER (today 0.615)" }
  ],
  chart: { x: "gpu", y: "ovl_std" },

  captureColumns: ["M9.1 WER per group, M8.2 DER, M8.3 authors and leak", "Overlap errors by type, synthetic talker WER by overlap", "Synthetic cpWER by SIR, layout and bed; GPU, VRAM, capture CPU"],
  figures: [
    { src: "baseline.png", title: "Today on the same harness", caption: "Overlap WER 0.64 (standard) and 0.50 (day), mostly deletions of the quieter talker; synthetic cpWER 0.70 at SIR -6 dB." },
    { src: "fig-overlap-pairs.png", title: "Overlap WER per pair of overlapping sources", caption: "Today grey, C1 (P1 shadow) purple, C6 + language split blue. Cross-app pairs drop to 0.02-0.29. Same-app Discord pairs stay at 0.41-0.46 on the standard run. The 'elsewhere' pairs (0.408) hold speech played on another endpoint, which Akashic never records by design." },
    { src: "fig-mixed-day.png", title: "Mixed FR/EN clean groups, day run", caption: "C1's regression is on the residual call stream (0.10 → 0.17, mostly deletions); the language split brings it to 0.01. The mic share (0.26) is the same for all three." },
    { src: "fig-cancellation.png", title: "Subtraction depth on the target PC", caption: "Per 0.5 s block. VB-CABLE: 8 taps reach -44 dB at 16 kHz and -85 dB at 48 kHz. The real DT-770 endpoint at 16 kHz: 8 taps only about -5 dB (each client resamples on its own, -19.8 ppm drift); a 128-tap FIR gives -57 dB, and held while the call talks, -59 dB of the stem left in the residual. Short, quiet check (11 and 12 blocks)." },
    { src: "fig-gate-recall.png", title: "C4 gate recall on today's streams", caption: "pyannote two-speaker frames ≥ 0.5 s flag 33 % (standard) and 23 % (day) of the truth overlap time, 0 % of same-app overlap; precision 0.71 and 0.65. On the synthetic set 0.62-0.91, 0.26 under music." },
    { src: "fig-music-pairs.png", title: "C4 overlap WER by music bed and app pair", caption: "Separation on gated spans moves overlap WER by 0.00-0.06 per pair; 13 of 19 standard overlap groups have a music bed." },
    { src: "fig-overlap-lang.png", title: "C4 overlap WER by group language", caption: "Multi-language overlap groups stay at 0.52-0.62 with separation." },
    { src: "fig-p1-two-scorings.png", title: "C1a under both scorings", caption: "P1 as first built (before the strict exclusion rule). Lower panel: paused-speech words removed and overlap scored order-free; the standard run's harness overlap WER carries a floor of about 0.13 from speech played while Akashic was paused." }
  ],

  candidates: [
    {
      id: "C1", name: "Per-app stems + call residual, strict exclusion", status: "fails",
      statusNote: "Closest in-limit candidate (P1, strict exclusion). Keeps every must but one: mixed clean WER on the day run rises from 0.151 to 0.202.",
      method: "Up to 3 audible non-voice apps, picked by per-session meters, each get a process-loopback include stream; the residual (endpoint loopback minus each stem through a 128-tap FIR, refitted per minute on stem-only seconds) carries the call; a validity switch falls back to today's mix while the residual is not 25 dB below it; each stem and the residual are segmented, transcribed and attributed on their own, rows stamped with real start times; a system row overlapping an excluded app's sound is dropped.",
      pipeline: [{ label: "A3 meters, pick ≤ 3 stems", cost: "1 Hz poll" }, { label: "process-loopback clients", cost: "+1.09 % core (3 clients)" }, { label: "128-tap FIR residual + validity switch", cost: "0.47-0.59 % core" }, { label: "Silero VAD + segmenter per stream", cost: "in capture" }, { label: "Whisper per stream", cost: "x1.15 GPU daily (model)" }, { label: "speakers job per row", cost: "CPU 453 s/h vs 218" }],
      uses: ["A1 Process loopback include", "A3 Per-session peak meter", "X1 Fitted FIR stem subtraction", "X2 Validity switch", "S6 Per-stream segmenting"],
      cost: "GPU x1.151 standard / x1.038 day as a daily average (0.422 s per decode call + 0.0672 s per audio second, fitted on two same-session windows), x1.76 on the densest 6-minute window; VRAM +0.04 GB; capture CPU 0.90 % in the harness + 1.09 % for three clients in the spike ≈ 2.0 % of a core",
      keeps: "Overlap WER both runs and both scorings, EN and FR clean WER, DER, no excluded audio",
      fakes: "Nothing beyond the allowed may-fake items",
      failsWhen: "A residual row holding an English stretch then a French one is decoded in one language (mixed day 0.202); two talkers in one app stay mixed (Discord pairs 0.41-0.46 standard); a Communications-category call is silent in process loopback, so it cannot be a stem, only the residual; a second voice app or an endpoint APO or limiter breaks the subtraction (validity switch).",
      test: "Replays of the standard and day runs + 72 synthetic mixtures · M9.1, M8.2, M8.3, M7.1, excluded seconds",
      priorArt: { name: "win-capture-audio / OBS application audio capture (include mode)", url: "https://github.com/bozbez/win-capture-audio" },
      values: { gpu: 1.151, vram: 0.036, cpu: 1.99, gpu_win: 1.76, ovl_std: 0.2312, ovl_day: 0.1933, ovl_np_std: 0.1163, ovl_np_day: 0.1933, en_std: 0.0833, fr_std: 0.0910, mixed_std: 0.1610, en_day: 0.0486, fr_day: 0.0604, mixed_day: 0.2022, der_std: 0.1471, der_day: 0.0898, excl: 0, wrong_day: 70, syn_cpwer: 0.343 },
      capture: "C1.png",
      metricsLine: "strict: ovl_std=0.231 ovl_day=0.193 en_std=0.083 fr_std=0.091 mixed_std=0.161 en_day=0.049 fr_day=0.060 mixed_day=0.202 der_std=0.147 der_day=0.090 excl_s=0.0 wrong_std=18 wrong_day=70 | capture shows P1 as first built (excl_s=33.2)"
    },
    {
      id: "C6L", name: "C6 + language split", status: "relaxes",
      statusNote: "Works if the non-goal 'transcript language detection (#139)' relaxes: a ≥ 5 s system row with an unclear FR/EN choice is cut at an energy dip whose sides get different languages, each side decoded with its own. Prototype P3. Passes every must on both scorings.",
      method: "C6 (C1's capture front end with the strict exclusion, then overlap-gated MossFormer2 separation on every output stream), plus a language split: a system row of 5 s or more whose chosen language holds less than 0.98 of the FR+EN probability is cut at the one of its 3 deepest 100 ms energy dips whose two sides (each ≥ 1.5 s) detect different languages, and each side is decoded with its own language.",
      pipeline: [{ label: "C1 capture front end", cost: "≈ 2.0 % core" }, { label: "pyannote overlap gate per row", cost: "79-89 CPU s per recorded hour" }, { label: "MossFormer2 on gated spans (CPU)", cost: "0 spans on the real runs" }, { label: "FR/EN detection per ≥ 5 s row", cost: "6.2-7.1 % of GPU" }, { label: "split at an energy dip, decode each side", cost: "5 and 73 splits per run" }, { label: "speakers job per row", cost: "CPU 451 s/h" }],
      uses: ["A1 Process loopback include", "A3 Per-session peak meter", "X1 Fitted FIR stem subtraction", "X2 Validity switch", "S7 Overlap-gated processing", "S1 Separation before ASR", "L1 Language split at an energy dip"],
      cost: "GPU x1.233 standard / x1.131 day as a daily average (0.479 s per decode call + 0.0577 s per audio second, plus the measured detection GPU), worst clock hour x1.259, densest 6-minute window x1.97 measured; VRAM +0.0 GB; capture CPU ≈ 2.0 % of a core; separation on CPU only while no game is in front",
      relaxes: "Non-goal 'transcript language detection (#139, already right)': the language is chosen per side of a cut instead of per row",
      keeps: "Every must on both scorings",
      failsWhen: "Same-app overlap shorter than the 0.5 s gate stays mixed (Discord pairs 0.41 standard); a Communications-category call in a real session; a row whose language changes at no energy dip keeps one language; a row under 5 s keeps one language.",
      test: "Replays of the standard and day runs (P1 replayed as a shadow on the same front end) · synthetic set replayed before the language split",
      priorArt: null,
      values: { gpu: 1.233, vram: 0.0, cpu: 2.03, gpu_win: 1.97, gpu_hour: 1.259, ovl_std: 0.2235, ovl_day: 0.1563, ovl_np_std: 0.1074, ovl_np_day: 0.1563, en_std: 0.0833, fr_std: 0.0907, mixed_std: 0.1610, en_day: 0.0480, fr_day: 0.0624, mixed_day: 0.0959, der_std: 0.1478, der_day: 0.0889, excl: 0, wrong_day: 71, syn_cpwer: 0.174 },
      capture: "C6.png",
      metricsLine: "ovl_std=0.223 clean_std=0.084 en_std=0.083 fr_std=0.091 mixed_std=0.161 der_std=0.148 wrong_std=19 ovl_day=0.156 clean_day=0.061 en_day=0.048 fr_day=0.062 mixed_day=0.096 der_day=0.089 wrong_day=71 excl_s=0.0 syn_cpwer=0.174 (before the split) gpu_x_avg_std=1.233 gpu_x_avg_day=1.131 gpu_x_worst_hour=1.259 vram_gb=4.23 cap_cpu=0.94"
    },
    {
      id: "C6", name: "Stems, then gated separation on every stream", status: "fails",
      statusNote: "Prototype P3 with the language split off. Identical to C1 on both runs (the gate fired 0 times; within 0.003): mixed day 0.202. Better on synthetic same-app mixes.",
      method: "C1's capture front end, then on every output stream the C4 gate (pyannote two speakers ≥ 0.5 s) and MossFormer2 separation of the gated spans ± 1 s, outputs tied to the track already talking, hybrid tracks, pieces decoded and attributed.",
      pipeline: [{ label: "C1 capture front end", cost: "≈ 2.0 % core" }, { label: "pyannote overlap gate per row", cost: "79-89 CPU s per recorded hour" }, { label: "MossFormer2 on gated spans (CPU)", cost: "RTF 1.7" }, { label: "Whisper per piece", cost: "same decodes as C1 on the real runs" }],
      uses: ["A1 Process loopback include", "A3 Per-session peak meter", "X1 Fitted FIR stem subtraction", "S7 Overlap-gated processing", "S1 Separation before ASR"],
      cost: "Same decodes as C1 on the real runs (0 gated spans), plus 79.1 (standard) and 88.8 (day) gate CPU seconds per recorded hour; VRAM +0 (separation on CPU); capture CPU as C1",
      failsWhen: "As C1 for mixed FR/EN rows. On the real runs the in-stream two-talker overlap left after the stems was 0.18 s (standard) and 0.35 s (day), under the gate's 0.5 s minimum.",
      priorArt: { name: "PixIT (joint separation and diarization)", url: "https://arxiv.org/abs/2403.02288" },
      values: { vram: 0.0, cpu: 2.03, ovl_std: 0.2312, ovl_day: 0.1933, ovl_np_std: 0.1163, ovl_np_day: 0.1933, en_std: 0.0833, fr_std: 0.0910, mixed_std: 0.1610, en_day: 0.0486, fr_day: 0.0631, mixed_day: 0.2022, der_std: 0.1471, der_day: 0.0911, excl: 0, wrong_day: 70, syn_cpwer: 0.174 },
      metricsLine: "p3_no_langsplit: ovl_std=0.231 ovl_day=0.193 mixed_day=0.202 der_std=0.147 der_day=0.091 excl_s=0.0 | synthetic cpWER 0.174 (one process 0.247 vs C1 0.575), gate recall 0.865 precision 0.80"
    },
    {
      id: "C1a", name: "P1 as first built: residual dropped only while an excluded app is audible", status: "fails",
      statusNote: "Stores 33.2 s of transcript overlapping an excluded app's sound (15.3 s standard, 18.0 s day); mixed day 0.202.",
      method: "As C1, but the excluded app's stem is dropped and a residual row is dropped only while the excluded app is audible inside it, so rows that merely touch its sound are kept.",
      pipeline: [{ label: "as C1", cost: "as C1" }],
      uses: ["A1 Process loopback include", "A3 Per-session peak meter", "X1 Fitted FIR stem subtraction", "X2 Validity switch"],
      cost: "As C1 (same capture front end and decodes; costs shown on C1)",
      values: { ovl_std: 0.2312, ovl_day: 0.1933, ovl_np_std: 0.1163, ovl_np_day: 0.1933, en_std: 0.0776, fr_std: 0.0823, mixed_std: 0.1610, en_day: 0.0461, fr_day: 0.0604, mixed_day: 0.2022, der_std: 0.1408, der_day: 0.0887, excl: 33.2, wrong_day: 70, syn_cpwer: 0.343 },
      metricsLine: "ovl_std=0.231 clean_std=0.083 en_std=0.078 fr_std=0.082 mixed_std=0.161 der_std=0.141 ovl_day=0.193 clean_day=0.073 en_day=0.046 fr_day=0.060 mixed_day=0.202 der_day=0.089 excl_s=33.2 gpu_x=1.41 (cross-session) vram_gb=4.27 cap_cpu=0.90"
    },
    {
      id: "C4", name: "Overlap-gated separation on today's streams", status: "fails",
      statusNote: "Overlap WER 0.597 (standard) and 0.458 (day) against 0.25; clean EN also rises (0.116 vs 0.113, 0.061 vs 0.059) and mixed day (0.156 vs 0.151).",
      method: "On each stored system segment: pyannote segmentation 3.0 frames with two speakers ≥ 0.5 s (gaps < 0.3 s merged) gate MossFormer2_SS_16K (ONNX, CPU) on the run ± 1 s; outputs ordered by a gain fit on the alone context (WeSpeaker under 3 dB), hybrid tracks keep the original audio where one talker is alone; each track decoded and attributed.",
      pipeline: [{ label: "pyannote overlap gate", cost: "speakers model" }, { label: "MossFormer2 ONNX on CPU", cost: "RTF 1.70 (8 s)" }, { label: "Whisper per track", cost: "x1.09 decode calls (standard)" }],
      uses: ["S7 Overlap-gated processing", "S1 Separation before ASR"],
      cost: "GPU x1.32 on the densest 6-minute window, decode calls x1.086 (standard) / x1.031 (day); VRAM +0 (separation on CPU; on CUDA RTF 0.049 but +4.79 GB); separation CPU RTF 1.70, 5.8 % and 2.1 % of system audio separated",
      failsWhen: "Cross-app overlap is mostly not gated (recall 0.33 / 0.23) and same-app overlap not at all (0.00); 13 of 19 standard overlap groups have a music bed (gate recall 0.26 under music on the synthetic set).",
      priorArt: { name: "PixIT (joint separation and diarization)", url: "https://arxiv.org/abs/2403.02288" },
      values: { gpu: 1.086, vram: 0.0, cpu: 0, gpu_win: 1.32, ovl_std: 0.5975, ovl_day: 0.4578, ovl_np_std: 0.5352, ovl_np_day: 0.4578, en_std: 0.1158, fr_std: 0.1172, mixed_std: 0.1659, en_day: 0.0608, fr_day: 0.0837, mixed_day: 0.1559, der_std: 0.1964, der_day: 0.1541, excl: 0, wrong_day: 62, syn_cpwer: 0.329 },
      capture: "C4.png",
      metricsLine: "ovl_std=0.597 clean_std=0.097 en_std=0.116 der_std=0.196 ovl_day=0.458 mixed_day=0.156 der_day=0.154 excl_s=0.0 syn_cpwer=0.329 gate_std=0.142 recall_std=0.325 recall_same_std=0.000 recall_day=0.231 sep_rtf_cpu_8s=1.701 sep_vram_gb=4.79 (CUDA)"
    },
    {
      id: "C2", name: "Complementary include/exclude pair", status: "refuted",
      statusNote: "Refuted: Teams is reported silent in exclude mode too (#414), so exclude(X) would drop a Communications call from both streams; one process tree per client.",
      method: "While a voice app and another app X are audible, process loopback include(X) plus exclude(X) replace the endpoint loopback: an exact split with no fitting, if exclude mode keeps Communications streams.",
      pipeline: [{ label: "include(X) + exclude(X) clients", cost: "+0.78 % core for 2 clients (spike)" }],
      uses: ["A1 Process loopback include", "A2 Process loopback exclude"],
      cost: "Two process-loopback clients (+0.78 % core measured for two); GPU as C1",
      priorArt: { name: "win-capture-audio include + exclude", url: "https://github.com/bozbez/win-capture-audio" }
    },
    {
      id: "C3", name: "Wave Link mixer endpoints", status: "unbuilt",
      statusNote: "Not built: not applicable here. discord.exe, steam.exe and wallpaper64.exe render straight to the DT-770 endpoint; the Wave Link Voice Chat and System endpoints had no app sessions.",
      method: "When the recorded endpoint carries only Wave Link, loopback each Wave Link input endpoint with an audible session instead of the final mix.",
      pipeline: [{ label: "endpoint loopback per Wave Link input", cost: "1.5-2.9 % core (estimate)" }],
      uses: ["A4 Per-app endpoint routing"],
      cost: "1.5-2.9 % core by estimate; GPU as C1",
      priorArt: { name: "Elgato Wave Link multitrack (per-channel output devices)" }
    },
    {
      id: "C5", name: "Packed Whisper window", status: "refuted",
      statusNote: "Refuted except for same-language pairs: faster-whisper decodes a window as one token sequence in one forced language; separate passes already fit the daily GPU budget.",
      method: "Two short co-active tracks share one 30 s encoder window with a 1.5 s silence gap, split at the gap after decoding.",
      pipeline: [{ label: "pack two tracks + 1.5 s gap", cost: "~+9.5 % GPU at 20 % co-active (estimate)" }],
      uses: ["S7 Overlap-gated processing"],
      cost: "About +9.5 % GPU at 20 % co-active time, by arithmetic",
      priorArt: null
    }
  ],

  method: {
    summary: "C1, the closest in-limit candidate: per-app capture streams, the call as the residual of a fitted subtraction, every stream transcribed on its own, strict exclusion. The language split of the RELAXES candidate is the one step added after it.",
    equation: "r[n] = y[n] − Σ_k (h_k ∗ x_k)[n]     y: endpoint loopback (16 kHz), x_k: stem k (process loopback include), h_k: 128 taps\nh = argmin ‖y − Σ_k h_k ∗ x_k‖² on 1 s where the meters show only stemmed apps, refit every 60 s, held otherwise\nvalid(block) ⇔ 10·log10(‖r‖² / ‖y‖²) ≤ −25 dB on stem-only 0.5 s blocks; else today's mix for ≥ 10 s, stems muted\nlanguage split (relaxed): row ≥ 5 s and max(p_fr, p_en) / (p_fr + p_en) < 0.98 → cut at one of the 3 deepest 100 ms energy dips whose sides (≥ 1.5 s) detect different languages",
    steps: [
      { text: "Pick stems: per-session peak meters polled once a second; up to 3 audible apps that are not voice apps get a process-loopback include client, excluded apps first, then apps already stemmed, then the loudest non-music apps, then music; a pick stays for 30 s and its first 100 ms stay in the residual.", assumes: "The call app renders in the Communications category, so it cannot be captured per process and must be the residual [5]." },
      { text: "Residual: endpoint loopback minus each stem through a 128-tap FIR fitted jointly by least squares on stem-only seconds, applied by FFT convolution.", assumes: "Each client's own resampler acts as a fixed linear filter between refits; the spike measured -57 dB on the DT-770 endpoint over a short quiet check [2]." },
      { text: "Validity switch: on blocks where only stemmed apps sound, a residual less than 25 dB below the mix switches back to today's mix for at least 10 s, stems muted, so no speech is transcribed twice.", assumes: "Endpoint APOs or a limiter in the mix but not in the stems show up as shallow blocks." },
      { text: "Streams: every stem except music and excluded apps, and the residual, run Akashic's segmenter and Silero VAD; each row is transcribed with today's decode and its own prompt context, attributed with today's speakers job, and stamped with its real start time." },
      { text: "Strict exclusion: an excluded app's stem is never kept, and any system row, stem or residual, is dropped while the meters show the excluded app audible within 1 s of it, as today's capture drops such segments whole." },
      { text: "Relaxed step (C6 + language split only): a system row of 5 s or more with an unclear FR/EN choice is cut at an energy dip whose sides detect different languages, and each side is decoded with its own language.", assumes: "Mixed FR/EN speech changes language at a pause; in the day run's 62 rows holding a mixed utterance, 21 split, and 0 of 80 single-language control rows did." }
    ],
    cost: "GPU daily average x1.151 (standard) and x1.038 (day) by a two-parameter model fitted on two same-session cost windows (0.422 s per decode call + 0.0672 s per audio second), against x1.30; the densest 6-minute window measured x1.76. VRAM +0.04 GB against 1.5. Capture CPU: 0.90 % of a core in the harness (meters, FIR, per-stream VAD) plus 1.09 % for three process-loopback clients in the spike ≈ 2.0 % against 5. Speakers job CPU 453 s per recorded hour against 218. With the language split: x1.233 and x1.131 daily, worst clock hour x1.259, densest window x1.97, VRAM +0.",
    failsWhen: "A residual row holding English then French is decoded in one language → the other language deleted (mixed day 0.202). Two talkers in one app → they stay mixed (Discord pairs 0.41-0.46 on the standard run). A real Discord call in the Communications category → it cannot be a stem; the method relies on it being the residual, which no real call has shown yet. A second voice app, or an APO or limiter on the endpoint → shallow residual, today's mix for 10 s.",
    reproduce: "uv run python eval/overlapping-speech/P1-per-process-streams/p1_run.py eval/overlapping-speech/P1-per-process-streams/candidate.py eval/overlapping-speech/P1-per-process-streams (strict exclusion = detail.json runs_strict_excl); C6 + language split: uv run python eval/overlapping-speech/P3-stems-plus-separation/p3_run.py eval/overlapping-speech/P3-stems-plus-separation; needs the quality runs' stores (this PC only); working copy _dev/invent/overlapping-speech/ (local, gitignored)"
  },

  findings: [
    { question: "Where today's overlap errors come from", claims: [
      "All overlap error is on the system stream: 1226 errors over 1918 words on the standard run (1081 deletions), 3579 over 7143 on the day run (3010 deletions) [3].",
      "By fix route, word level: per-process separation 74.6 % (standard) and 89.2 % (day) of overlap errors, speech separation 5.1 % and 6.5 %, never recorded by design 20.3 % (speech while Akashic was paused) and 4.2 % (voice on another endpoint) [3].",
      "The quieter talker, usually the video at about 0.4x the call level, loses 75 % (standard) and 80 % (day) of its words in overlapped moments [3].",
      "A call over a video reads 0.682 and 0.601; the same call with no bed and no overlap 0.025 and 0.057 [3].",
      "Fixing the per-process route alone projects to 0.237 (standard) and 0.116 (day); the paused-speech floor on the standard run is 0.130 [3]."
    ] },
    { question: "Can stems be subtracted from the endpoint mix on this PC?", claims: [
      "VB-CABLE: an 8-tap fit leaves the residual -44.4 dB below the mix at 16 kHz and -85.0 dB at 48 kHz; lag stable, drift 0 [2].",
      "DT-770 (the recorded default endpoint), 16 kHz: 8 taps give only -5.4 dB because each WASAPI client resamples on its own (lags 64-89 samples, drift -19.8 ppm); a 128-tap FIR gives -57.3 dB (worst block -48.0), and held while the call talks, -59.1 dB of the stem left in the residual [2].",
      "The DT-770 check is short and quiet: 11 stem-only and 12 two-source blocks, mix at -60.9 dBFS [2].",
      "Capture CPU of 0-3 process-loopback clients: 0.31 / 0.78 / 1.09 / 1.41 % of a core (+1.09 % for three); FIR at 16 kHz for three stems 0.47-0.59 % [2]."
    ] },
    { question: "How faithful the simulated per-process streams are", claims: [
      "Re-rendered sources explain 89 % (median) of the stored system band energy and 96 % of the mic [4].",
      "Single-source system segments: Whisper on the simulated stream and on the stored audio differ by 0.016-0.029 WER (standard) and 0.008-0.081 (day) per app [4].",
      "Not sample-exact against the stored audio, so the residual is modelled from the spike's measured per-block depth and leak instead of being subtracted [1][4]."
    ] },
    { question: "What per-app streams change", claims: [
      "C1 cuts overlap WER from 0.639 to 0.231 (standard) and from 0.497 to 0.193 (day); with paused speech removed, 0.116 and 0.193 [1].",
      "Cross-app pairs drop to 0.02-0.29; same-app Discord pairs stay at 0.46 (standard) and 0.21 (day) [1].",
      "Today 253 of 254 standard-run system segments list Akashic's own loopback session as a source; the prototypes filter it by pid and list it on 0 [1][6].",
      "M7.1 leak drops to 0.0 s on both runs; wrong authors rise from 6 to 18 (standard) and from 46 to 70 (day) while correct authors rise from 169 to 214 and 1472 to 1604 [1].",
      "The mixed regression is on the residual call stream: its mixed-group WER goes from 0.095 (today's system rows) to 0.171, mostly deletions; on the 62 day-run rows holding a mixed utterance, the language split takes the row WER from 0.151 to 0.057, and 80 single-language control rows are unchanged [1]."
    ] },
    { question: "What gated separation adds", claims: [
      "On today's streams the gate flags 33 % (standard) and 23 % (day) of truth overlap time, 0 % of same-app overlap, precision 0.71 and 0.65 [1].",
      "MossFormer2_SS_16K on CPU runs at RTF 1.65-1.76 (3-12 s windows); on CUDA at 0.041-0.051 with +4.8 GB VRAM [1].",
      "After the stems, no real-run stream holds a two-talker overlap of 0.5 s or more (0.18 s and 0.35 s in total), so the gate fires 0 times; on the synthetic set it reaches recall 0.865 and precision 0.80, and same-process cpWER falls from 0.575 (C1) to 0.247 [1].",
      "Published: Whisper large-v3 after TF-CrossNet separation gets 5.0 % WER on Libri2Mix against 2.5 % clean [15]; PixIT lowers AMI-SDM cpWER with Whisper from 38.3 to 31.7, and leakage removal is essential (50.1 without) [14]; separation models trained on English lose quality on other languages [16]."
    ] }
  ],

  mechanisms: [
    { id: "A1", name: "Process loopback include", core: "`VAD\\Process_Loopback`, include target process tree, one tree per client.", assumes: "The app renders outside the Communications category.", breaks: "Communications streams come back silent (local control -96.7 dBFS); the capturer's own tree is not captured. [5][8][9]" },
    { id: "A2", name: "Process loopback exclude", core: "Whole mix minus one process tree.", assumes: "Exclude mode keeps Communications streams.", breaks: "Reported silent for Teams in exclude mode too; single tree only. [9]" },
    { id: "A3", name: "Per-session peak meter", core: "`IAudioMeterInformation` per audio session, polled 20-1000 ms, as a per-app activity mask.", assumes: "Energy marks activity.", breaks: "Energy is not speech; several sessions per app. [10]" },
    { id: "A4", name: "Per-app endpoint routing", core: "Route an app to its own endpoint (undocumented AudioPolicyConfig, EarTrumpet) or a mixer channel.", assumes: "The user's audio setup may change.", breaks: "Undocumented, changed at 21H2. [13]" },
    { id: "A5", name: "Ducking notifications", core: "`IAudioVolumeDuckNotification` as a call-active signal.", assumes: "Ducking is enabled.", breaks: "Default ducking lowers other streams during calls. [11]" },
    { id: "X1", name: "Fitted FIR stem subtraction", core: "Residual = mix − Σ FIR_k ∗ stem_k, least squares on stem-only time.", assumes: "Each client's resampler acts as a fixed linear filter between refits.", breaks: "Endpoint APOs or limiters, packet gaps, drift between refits. [2][19]" },
    { id: "X2", name: "Validity switch", core: "Fall back to today's mix while the residual is not 25 dB below it.", assumes: "Stem-only blocks occur often enough to judge depth.", breaks: "Long stretches with the call always talking." },
    { id: "S1", name: "Separation before ASR", core: "2-speaker separation (MossFormer2 16 kHz, 55.7 M) then Whisper per output.", assumes: "Two talkers, close to the training domain.", breaks: "3+ talkers, language shift, artefacts, silent-output hallucination. [15][16][17]" },
    { id: "S2", name: "Target-speaker extraction", core: "Extract one speaker from a clean embedding (VoiceFilter, WeSep).", assumes: "A clean enrolment of the target.", breaks: "No enrolment; one pass per speaker. [18]" },
    { id: "S3", name: "Diarization-conditioned Whisper", core: "DiCoW (large-v3-turbo, 0.9 B) conditioned on diarization masks.", assumes: "A second Whisper-class model fits in VRAM.", breaks: "Likely over +1.5 GB; English-trained. [18]" },
    { id: "S4", name: "Serialized-output Whisper", core: "One decode emits all talkers in turn.", assumes: "Short full-overlap mixtures.", breaks: "Worse on real meetings; leakage 11 %. [18]" },
    { id: "S5", name: "Speech enhancement", core: "Remove non-speech (DeepFilterNet, RNNoise, Demucs).", assumes: "The interferer is not speech.", breaks: "Two talkers; may raise Whisper WER. [18]" },
    { id: "S6", name: "Per-stream segmenting", core: "Segment and decode each capture stream on its own, rows stamped with real start times.", assumes: "Streams are separate sources.", breaks: "Two talkers inside one stream." },
    { id: "S7", name: "Overlap-gated processing", core: "Run separation only where pyannote's powerset names two speakers ≥ 0.5 s.", assumes: "The gate finds the overlap.", breaks: "Music beds, overlaps under 0.5 s. [1]" },
    { id: "L1", name: "Language split at an energy dip", core: "Cut a long row with an unclear FR/EN choice where the two sides detect different languages.", assumes: "Language changes at a pause.", breaks: "Code-switching inside a phrase; rows under 5 s. [1][7]" }
  ],

  explored: [
    { name: "C1a P1 as first built", reason: "33.2 s of transcript stored over an excluded app's sound (15.3 s standard, 18.0 s day); the strict rule (C1) brings it to 0 at +0.007 clean WER on the standard run." },
    { name: "C2 Complementary include/exclude pair", reason: "Refuted: issue #414 reports Teams silent in exclude mode as well as include mode, and the local control shows Communications streams silent in include mode, so exclude(X) would drop the call from both streams [9][5][20]." },
    { name: "C3 Wave Link mixer endpoints", reason: "Not applicable: Discord renders to the DT-770 endpoint directly, and the Wave Link Voice Chat and System endpoints had no app sessions in the local probe [20]." },
    { name: "C4 Overlap-gated separation on today's streams", reason: "Overlap WER 0.597 and 0.458: the gate misses most cross-app overlap (recall 0.33 and 0.23) and all same-app overlap; the GPU variant needs +4.79 GB VRAM [1]." },
    { name: "C5 Packed Whisper window", reason: "Refuted except for same-language pairs: one forced language and one token sequence per window; separate passes already fit the daily budget [20][21]." },
    { name: "C6 without the language split", reason: "Identical to C1 on both runs (gate fired 0 times), so it fails mixed day the same way; better only on synthetic same-app mixes." },
    { name: "C4 separation on the GPU", reason: "RTF 0.049 but VRAM +4.79 GB against 1.5 GB, GPU x1.42 on the densest window [1]." }
  ],

  caveats: [
    "Simulated per-process streams: re-rendered from player logs, not sample-exact (correlation 0.3-0.6 with the stored audio); the residual's leak is modelled from the spike's per-block depth on the DT-770 endpoint (-57 dB, 128-tap FIR, 23 blocks of a quiet check), not subtracted [1][2][4].",
    "**Main caveat:** the harness call is a waveOut player in category Other, so the Communications-category silence of real Discord in process loopback (local control -96.7 dBFS) is not exercised. C1 and C6 rely on the call being the residual; the first thing a real call has to show is that the residual carries it and that no stem picks it up [5][20].",
    "Overlap margin on the standard run is small: 0.231 (C1) and 0.223 (C6 + language split) against 0.25, 0.019-0.027.",
    "Two scorings: the harness M9.1 counts words spoken while Akashic was paused as errors (floor about 0.13 on the standard run); every must is shown under both. M9.1 joins references and transcripts in start order, so per-stream rows need a scorer that groups by stream and source.",
    "Wrong speaker authors rise: standard 6 → 18 (C1) and 19 (C6 + language split), day 46 → 70 and 71; correct authors rise more (169 → 214-215, 1472 → 1604-1706).",
    "The 'elsewhere' overlap pairs (WER 0.408 for C1 and C6 + language split) hold speech played on another endpoint, which Akashic never records by design.",
    "The synthetic numbers of C6 + language split come from a replay before the language split; they were not rerun.",
    "GPU daily figures are a two-parameter model (seconds per decode call + seconds per audio second) fitted on two same-session cost windows, not a measured day; the densest 6-minute window is measured. C4's daily figure is the decode-call ratio.",
    "C1 + language split equals C6 + language split on the real runs by inference from the gate counts (0 gated spans), not a separate run.",
    "MossFormer2_SS_16K (ClearerVoice-Studio, Apache-2.0, training data 'open-sourced and private', 55.7 M) from Hugging Face at pinned commit 407cb030, checkpoint sha256 00a3a48b… matching the LFS oid, exported to ONNX in a throwaway venv; approved for measurement only [17].",
    "C4's separation runs at RTF 1.70 on CPU: 114.9 s of audio separated took 207 s on the 1.08 h standard run.",
    "The C1 capture image is P1 as first built (excluded 33.2 s); the strict-rule numbers are in the table and the metrics line.",
    "Two runs and one synthetic set; one PC (DT-770 endpoint, headset). No real call was recorded for this study.",
    "WEAK: Communications silence as a general finding (one report, synthetic audio); ducking attenuation figure not on Microsoft's page [20]."
  ],

  sources: [
    { n: 1, title: "Overlapping-speech harness and prototypes (baseline, P1, P2, P3: metrics.txt, detail.json)", url: "eval/overlapping-speech/", tier: 1, version: "target PC, 2026-11-20" },
    { n: 2, title: "Subtraction spike on VB-CABLE and the DT-770 endpoint", url: "eval/overlapping-speech/P1-per-process-streams/spike.json", tier: 1, version: "target PC, 2026-11-20" },
    { n: 3, title: "Diagnosis of today's overlap errors", url: "eval/overlapping-speech/diagnosis.json", tier: 1, version: "2026-11-20" },
    { n: 4, title: "Fidelity of the simulated per-process streams", url: "eval/overlapping-speech/fidelity.json", tier: 1, version: "2026-11-20" },
    { n: 5, title: "Can per-process loopback capture separate voice chat, game and browser audio? (research)", url: "docs/research/windows-process-loopback-capture.md", tier: 1, version: "2026-11-19" },
    { n: 6, title: "Does a WASAPI loopback capture show up as an audible session? (research)", url: "docs/research/wasapi-loopback-own-session.md", tier: 1, version: "2026-11-19" },
    { n: 7, title: "How should a local transcriber choose French or English per segment? (research)", url: "docs/research/whisper-language-choice-fr-en.md", tier: 1, version: "2026-11-19" },
    { n: 8, title: "PROCESS_LOOPBACK_MODE", url: "https://learn.microsoft.com/en-us/windows/win32/api/audioclientactivationparams/ne-audioclientactivationparams-process_loopback_mode", tier: 1 },
    { n: 9, title: "Windows-classic-samples issue #414", url: "https://github.com/microsoft/Windows-classic-samples/issues/414", tier: 2, version: "comment of 2026-10-28" },
    { n: 10, title: "IAudioMeterInformation", url: "https://learn.microsoft.com/en-us/windows/win32/api/endpointvolume/nn-endpointvolume-iaudiometerinformation", tier: 1 },
    { n: 11, title: "Handling audio ducking events from communication devices", url: "https://learn.microsoft.com/en-us/windows/win32/coreaudio/handling-audio-ducking-events-from-communication-devices", tier: 1 },
    { n: 12, title: "bozbez/win-capture-audio `audio-capture-helper.cpp`", url: "https://github.com/bozbez/win-capture-audio", tier: 1, version: "default branch, 2026-11-19" },
    { n: 13, title: "EarTrumpet AudioPolicyConfigFactoryImplFor21H2.cs (per-app default endpoint)", url: "_dev/invent/overlapping-speech/mechanisms-capture.md", tier: 2, version: "cited by name in the mechanism catalog" },
    { n: 14, title: "PixIT: joint training of speaker diarization and speech separation", url: "https://arxiv.org/abs/2403.02288", tier: 1 },
    { n: 15, title: "Whisper large-v3 after TF-CrossNet separation on Libri2Mix", url: "https://arxiv.org/abs/2503.17886", tier: 1 },
    { n: 16, title: "Separation models across languages (EN-trained Conv-TasNet on Mandarin)", url: "https://arxiv.org/abs/2204.11032", tier: 1 },
    { n: 17, title: "MossFormer2_SS_16K model card and provenance", url: "https://huggingface.co/alibabasglab/MossFormer2_SS_16K", tier: 1, version: "commit 407cb030cd66340918ebb6c8cc63b18f8592cdbe; eval/overlapping-speech/P2-gated-separation/model/provenance.json" },
    { n: 18, title: "Overlapped-speech ASR mechanism catalog (VoiceFilter, WeSep, DiCoW, SA-DiCoW, DeepFilterNet, RNNoise, Demucs)", url: "_dev/invent/overlapping-speech/mechanisms-capture.md", tier: 2, version: "researcher round 1, 2026-11-20 (local)" },
    { n: 19, title: "WebRTC AEC3 delay estimator and clock-drift detector (prior art for the fitted subtraction)", url: "_dev/invent/overlapping-speech/brief.md", tier: 2, version: "named in the prior-art notes, not fetched (local)" },
    { n: 20, title: "Fact-check, refuter and local probe notes", url: "_dev/invent/overlapping-speech/checks.md", tier: 1, version: "2026-11-20 (local); brief.md 'Local facts'" },
    { n: 21, title: "Akashic code", url: "src/akashic/platform/win/audio.py:456-460", tier: 1, version: "per-client 16 kHz AUTOCONVERTPCM; src/akashic/pipeline/whisper.py:111-118 (one forced language); eval/quality/score.py:743 (m8_m9)" },
    { n: 22, title: "OBS win-wasapi (application audio capture)", url: "https://raw.githubusercontent.com/obsproject/obs-studio/master/plugins/win-wasapi/win-wasapi.cpp", tier: 1, version: "master, 2026-11-19" }
  ]
};
