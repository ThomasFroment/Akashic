window.REPORT = {
  title: "Screenshot Store Growth",
  date: "2026-11-20",
  depth: "full",
  context: "Python 3.14, Pillow 13.0 (libwebp 1.6), numpy, PyAV 19.0.2 (SVT-AV1 4.2.0, dav1d), Windows 11, Ryzen 7 7800X3D, RTX 4080 Super; 4K 125 % primary and two 1440p monitors",
  problem: "Akashic's store grows 4.2-4.9 GB a day on the quality runs and 9.3 GB per 24 h of activity on one real hour; screenshots are 96 % of it, one WebP q70 still per stored monitor frame.",
  goals: [
    "Cut store growth per day well below today's 4.2-4.9 GB",
    "Keep every screenshot useful for the timeline, evidence images and deferred OCR",
    "Keep capture CPU on the capture thread within budget"
  ],
  contract: {
    must: [
      "OCR of stored frames within 0.01 CER of today at every font size",
      "Screen text on evidence images legible at 100 % zoom",
      "Every moment the change rules store today still decodes to its picture"
    ],
    fake: [
      "Pixels of regions unchanged since the last stored frame",
      "Colour detail of video and game content"
    ]
  },
  budget: [
    "≤ 3 ms extra per 4K frame on the capture thread",
    "No GPU model per frame; NVENC only without VRAM growth",
    "A new dependency only on request",
    "Every stored part encrypted at rest; random access to any moment stays fast"
  ],
  constraints: ["Retention forever by default", "AES-GCM per stored file today"],
  nonGoals: [
    "Deleting or expiring history",
    "Changing which frames are stored (change rules belong to R7)",
    "Audio and database size",
    "The store's drive location"
  ],

  answer: {
    text: "**C1, key-relative tile delta**, stores 0.54x today's bytes on one real desktop hour (208.7 against 389.0 MB/h) with OCR within +0.001 CER at every word height, every moment decoded, cold 4K decode p95 147 ms, +0.5 ms on key frames, no background CPU, no GPU and no new dependency. On the synthetic runs it stores 0.79x, because their lossy sources never repeat bit-exactly. Prior art: RDP's surface cache and RemoteFX tile difference [8, 9], used live, never at rest. The largest saving measured within the budget is **C5, deferred SVT-AV1 compaction**: 0.36x on the real hour at 954 s of worker CPU per recorded hour, with the synthetic corpus's ≤ 11 px text at +0.021 CER; prior art screenpipe [4]."
  },
  bestFit: "C1",

  metrics: [
    { key: "extra_ms", label: "Extra capture/writer ms (4K, p95)", unit: "ms", limit: 3, budget: true },
    { key: "ratio_r", label: "Bytes vs today, real hour", unit: "x" },
    { key: "mbh_r", label: "Real hour", unit: "MB/h" },
    { key: "dcer", label: "Worst dCER, real hour", limit: 0.01 },
    { key: "dec_p95", label: "Cold 4K decode p95", unit: "ms" },
    { key: "worker_s", label: "Background CPU per recorded hour", unit: "s" }
  ],
  chart: { x: "ratio_r", y: "dcer" },

  reference: { src: "ref.png", label: "Synthetic frame, today's WebP q70 source" },
  compareNote: "A 960x540 crop at native size of a synthetic quality-run frame (the harness's public-domain text page); no real-screen pixels are shown anywhere in this report.",
  captureColumns: ["MB per hour of recording, today vs candidate, real (R) and synthetic (S) corpus", "OCR dCER per word height on the real corpus, limit ±0.01"],

  figures: [
    { src: "fig-size.png", title: "Store growth per hour, every measured variant", caption: "MB per hour of recording, encrypted files, real corpus (blue) and synthetic corpus (orange)." },
    { src: "fig-c1-delta-shot.png", title: "C1: what a delta stores", caption: "A synthetic frame stored as a delta on its key: blue tiles go to the atlas, the rest is reused from the key." },
    { src: "fig-c1-seam.png", title: "C1: atlas run edges", caption: "PSNR against distance to a run edge on the real corpus, as numbers: −3.3 dB on the edge column, ≤ 0.9 dB one pixel in." },
    { src: "fig-c1-decode-position.png", title: "C1: decode time after the key", caption: "Cold decode is flat with the position after the key: the chain is always 1." },
    { src: "fig-c4-decode-position.png", title: "C4: decode time in the GOP", caption: "Cold decode grows with the position in the group of pictures." },
    { src: "fig-c5-decode-position.png", title: "C5: decode time in the GOP", caption: "4K median from about 45 ms at the first frame to about 85 ms at the tenth on the real corpus." },
    { src: "fig-c5-worker.png", title: "C5: worker CPU by stage", caption: "Seconds per recorded hour: still decode, colour conversion, AV1 encode, verify decode." }
  ],

  candidates: [
    {
      id: "C1", name: "Key-relative tile delta", status: "best",
      method: "A frame is a full WebP q70 key or a delta file with only the 64-px tiles changed since the key, verified bit-exact at full resolution; a moment decodes from its key plus its own file.",
      pipeline: [
        { label: "Gray tile map (signature already computed)", cost: "0.45 ms" },
        { label: "Exact full-res check of unchanged tiles", cost: "~22 ms with array copies" },
        { label: "Atlas of changed tiles, WebP q70", cost: "64 ms p50 (4K)" },
        { label: "Key when f > 0.45, 64 deltas or 1 h", cost: "today's encode + 0.34 ms" }
      ],
      uses: ["M7 Tile references", "M8 Tile difference with reset", "M9 Dirty-region map", "M4 Per-file stills"],
      cost: "Writer +0.34/+0.51 ms p50/p95 on keys, 82 ms p50 per 4K delta against 217 ms for a key; RAM +47 MB; no VRAM, no background CPU",
      keeps: "Every must on both corpora: OCR within +0.003, 100 % decoded, 0 reused tiles differing from raw",
      fakes: "Unchanged tiles show the key's WebP reconstruction of identical pixels",
      failsWhen: "Full-screen video, games, drags or scrolls make every frame a key (no saving, no artefact); lossy sources never match bit-exactly (synthetic corpus 0.79x); atlas run edges read 3.3 dB lower on one column.",
      condition: "The full-size browser view must compose and re-encode a delta (JPEG q90 +12 ms at 4K) or compose it in the browser; deleting a range rewrites at most one key group per monitor.",
      test: "Real hour and synthetic runs against today's WebP q70 · MB/h, dCER per word height, decode p95, bit-exact reuse · pass ≤ 0.6x, |dCER| ≤ 0.01, 100 % decode",
      priorArt: { name: "RDP surface cache and RemoteFX progressive tile difference", url: "https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-rdpegfx/33fee2d9-e103-411c-9342-c51db32a9ace" },
      values: { extra_ms: 0.51, ratio_r: 0.537, mbh_r: 208.7, dcer: 0.0016, dec_p95: 147.4, worker_s: 0 },
      capture: "C1.png", shot: "C1-shot.png",
      metricsLine: "R 208.7 MB/h x0.537, dCER ≤11 +0.0010 12-15 +0.0003 16-23 −0.0016 ≥24 +0.0004, dec 4K p95 147 ms, 1821/1821 · S 137.1 MB/h x0.785, 1997/1997"
    },
    {
      id: "C5", name: "Deferred SVT-AV1 compaction", status: "works",
      statusNote: "Passes every must on the real hour; the synthetic corpus's ≤ 11 px text reads +0.021 CER, over the 0.01 limit.",
      method: "Capture keeps today's WebP still; a worker compacts each monitor's stills older than 10 min into SVT-AV1 low-delay groups (preset 8, crf 35, K 10 at 4K, 30 at 1440p), verifies the group, then deletes the stills after the commit.",
      pipeline: [
        { label: "Today's WebP still at capture", cost: "+0 ms" },
        { label: "Decode still + colour conversion", cost: "209 s per recorded hour" },
        { label: "SVT-AV1 preset 8 encode", cost: "713 s per recorded hour" },
        { label: "Verify decode, switch rows, delete stills", cost: "32 s per recorded hour" }
      ],
      uses: ["M3 Deferred compaction", "M2 Software AV1", "M6 Screen-content tools", "M12 Encrypted groups"],
      cost: "Capture +0 ms; worker 954 s per recorded hour on the real corpus (291 on the synthetic); worker RAM +0.5-0.6 GB; no VRAM",
      keeps: "Every must on the real hour (worst dCER +0.0073 / −0.0089), 100 % decoded",
      fakes: "4:2:0 chroma, P-frame skips of unchanged blocks; two lossy generations",
      failsWhen: "Long gaming sessions grow the still backlog at today's rate; small synthetic text after two lossy generations (+0.021 at ≤ 11 px); deleting one moment re-encodes its group.",
      condition: "Worker CPU of about a quarter of one core while recording; groups on sparse monitors span up to 36 min.",
      test: "Same corpora · steady-state MB/h, dCER, worker s per recorded hour, decode p95",
      priorArt: { name: "screenpipe snapshot compaction", url: "https://github.com/mediar-ai/screenpipe" },
      values: { extra_ms: 0, ratio_r: 0.359, mbh_r: 139.8, dcer: 0.0089, dec_p95: 151.8, worker_s: 954 },
      capture: "C5.png", shot: "C5-shot.png",
      metricsLine: "R 139.8 MB/h x0.359, dCER ≤11 +0.0058 12-15 +0.0034 16-23 +0.0073 ≥24 −0.0089, worker 954 s/h · S 42.1 MB/h x0.241, dCER ≤11 +0.021"
    },
    {
      id: "C4", name: "Live low-delay SVT-AV1 per monitor", status: "fails",
      statusNote: "0.59x against its own 0.35x claim: SVT-AV1 4.2.0 turns screen-content tools off at preset ≥ 9, and preset 8 costs a 4K CPU p95 of 1,368 ms.",
      method: "Each stored frame becomes one AV1 packet of a per-monitor low-delay stream (preset 11, rtc, crf 35), sealed alone and grouped by GOP; a write failure forces a key.",
      pipeline: [
        { label: "RGB to YUV 4:2:0", cost: "38 ms (4K)" },
        { label: "SVT-AV1 preset 11 encode", cost: "98/203 ms CPU p50/p95 incl. conversion" },
        { label: "Seal packet, index (gop, n)", cost: "< 0.3 ms" }
      ],
      uses: ["M1 Long-GOP video", "M2 Software AV1", "M12 Encrypted packets"],
      cost: "Writer CPU below today's (mean 111 against 233 ms per 4K frame); encoder RAM ~0.3 GB at 4K; no VRAM",
      failsWhen: "Screen-content tools are off at the presets that fit CPU; keys cost 453 KB against 229 KB for WebP at 4K and make 30-43 % of the bytes.",
      values: { extra_ms: 0, ratio_r: 0.594, mbh_r: 231.1, dcer: 0.0093, dec_p95: 139, worker_s: 0 },
      capture: "C4.png", shot: "C4-shot.png",
      metricsLine: "R 231.1 MB/h x0.594, dCER ≤11 −0.0041 ≥24 +0.0093, dec 4K p95 139 ms · S x0.911, dCER ≤11 +0.028"
    },
    {
      id: "C2", name: "Damage-rect manifest", status: "unbuilt",
      statusNote: "Not built: same family as C1 with more reuse; its damage hint must first be accumulated in screen.py.",
      method: "Changed regions encoded as WebP crops; unchanged regions point at the part that last stored them, up to 16 files per moment.",
      pipeline: [
        { label: "Damage hint, exact compare inside it", cost: "~0.1-21 ms" },
        { label: "≤ 8 WebP crops", cost: "266 ms × changed area" },
        { label: "Manifest of references", cost: "< 1 ms" }
      ],
      uses: ["M7 Tile references", "M9 Dirty-region map"],
      cost: "Estimated writer ≤ +0.1 ms; decode ≤ 192 ms at 4K",
      values: { extra_ms: 0.1, dec_p95: 192 }, estimated: true
    },
    {
      id: "C3", name: "Scroll and move reuse", status: "unbuilt",
      statusNote: "Not built: needs bit-exact shifted text, which the 125 % primary's re-rasterised scrolling breaks; the share of such frames is unmeasured.",
      method: "Find a vertical shift of a mostly-dirty window, verify tiles bit-exact at that shift and reference them; encode only the exposed strip.",
      pipeline: [
        { label: "Row profile shift search", cost: "0.5-1 ms" },
        { label: "Exact verify at the shift", cost: "∝ reused area" }
      ],
      uses: ["M10 Moved-rectangle reuse", "M7 Tile references"],
      cost: "Estimated 0.5-2.5 ms on frames that do not scroll",
      values: { extra_ms: 2.5 }, estimated: true
    }
  ],

  method: {
    summary: "C1: per monitor, a WebP key plus delta files that hold only the 64-px tiles changed since the key, so every moment decodes from two files.",
    equation: "frame_n = WebP(key) outside D_n, WebP(atlas of C_n over D_n) inside\nD_n = tiles where gray signature differs, then every other tile checked raw == raw_key bit-exact\nnew key when |D_n| > 0.45, 64 deltas, or key age > 1 h",
    steps: [
      { text: "Keep the monitor's last full WebP q70 still as the key, with its raw picture in memory (25 MB at 4K, 11 MB per 1440p).", assumes: null },
      { text: "Mark candidate changed tiles from the 1/4-resolution gray signature the capture loop already computes.", assumes: "Changes this map misses are caught by the exact check." },
      { text: "Check every other tile bit-exact against the raw key at full resolution; any mismatch becomes changed.", assumes: null },
      { text: "Store a new key when more than 45 % of tiles changed, after 64 deltas or after 1 h; else pack changed tiles in 16-px-aligned row runs into one WebP q70 atlas and store bitmask, layout and atlas as one encrypted file." },
      { text: "Decode: key plus atlas pasted over it; chain length 1." }
    ],
    cost: "4K delta p50: gray map 0.45 + arrays 19 + compare 3.0 + pack 3.1 + atlas WebP 64 = 82 ms against 217 ms for a full encode; key: today's encode + 0.34 ms. Decode 4K: key 90 ms, delta 112 ms p50 / 152 ms p95.",
    failsWhen: "Full-screen video, games, drags and scrolls → every frame a key (no saving). Lossy sources → no bit-exact reuse. Atlas run edges → one column 3.3 dB lower, no OCR effect measured.",
    reproduce: ".venv\\Scripts\\python.exe _dev\\invent\\screenshot-store-growth\\locked_run.py _dev\\invent\\screenshot-store-growth\\C1\\cand.py --corpus all (local, gitignored; the real corpus was deleted after the study)"
  },

  findings: [
    { question: "How memory and screen-recording tools store long screen histories", claims: [
      "screenpipe writes a JPEG per capture, then compacts snapshots at least 10 min old into MP4 chunks of 25-100 frames with GOP 25, deleting the JPEGs only after the chunk and the database commit [4].",
      "Windows Recall stores independent JPEG files [14]; Rewind's method is undocumented [15].",
      "As intra-only frames AV1 NVENC is about WebP's size; the saving comes from inter-frame prediction [1]."
    ] },
    { question: "Screen-content coding", claims: [
      "SVT-AV1 enables screen-content tools by default (`--scm 2`) [3]; version 4.2.0 forces the non-screen-content path at preset 9 and above (measured).",
      "FFmpeg's libsvtav1 wrapper is asynchronous [7]; with `pred-struct=1`, `rtc=1` and no lookahead it gave one packet per frame on 1,824 encodes (measured)."
    ] },
    { question: "Reusing unchanged screen regions", claims: [
      "RDP keeps a client bitmap cache and sends 64x64 tile differences against decoder state [8, 9]; VNC CopyRect moves rectangles in the client framebuffer [10]; all rely on a live receiver."
    ] },
    { question: "The real desktop against the synthetic runs", claims: [
      "One real hour as today's change rules store it: 1,821 frames, 389.0 MB/h, 9.34 GB per 24 h of activity, 214 KB per frame; synthetic runs 174.6 MB/h, 272 KB per frame."
    ] }
  ],
  mechanisms: [
    { id: "M1", name: "Long-GOP video per monitor", core: "One packet per stored frame, key every K.", assumes: "Stored frames of one monitor are coherent.", breaks: "Keys larger than a still; moving 4K content. [1]" },
    { id: "M2", name: "Software AV1", core: "SVT-AV1 through PyAV.", assumes: "Spare CPU.", breaks: "Contention; screen tools off at preset ≥ 9. [3]" },
    { id: "M3", name: "Deferred compaction", core: "Still now, GOP later, delete after commit.", assumes: "Idle CPU and a worker.", breaks: "Long backlogs. [4]" },
    { id: "M4", name: "Per-file stills", core: "One encoded file per moment.", assumes: "Disk is cheap.", breaks: "High stored frame rates. [1]" },
    { id: "M5", name: "Still codecs on text", core: "WebP lossy or lossless, AVIF.", assumes: "Text-heavy content.", breaks: "Photographic regions. [12]" },
    { id: "M6", name: "Screen-content tools", core: "Intra block copy and palette.", assumes: "Exact repeats, few colours.", breaks: "ClearType phases, video. [5, 6]" },
    { id: "M7", name: "Tile references", core: "Unchanged tiles point at earlier pixels.", assumes: "Exact repeats.", breaks: "Scrolling, sub-tile changes. [8]" },
    { id: "M8", name: "Tile difference with reset", core: "Difference against a reference tile, chain reset.", assumes: "Static tiles.", breaks: "Scrolling, video. [9]" },
    { id: "M9", name: "Dirty-region map", core: "Encode only damaged blocks.", assumes: "Reliable damage.", breaks: "Over-reported damage. [13]" },
    { id: "M10", name: "Moved-rectangle reuse", core: "Copy a moved rectangle from a held reference.", assumes: "Bit-identical moved pixels.", breaks: "Fractional-scale scrolling. [10]" },
    { id: "M11", name: "Chroma and OCR", core: "4:4:4 costs 5-21 % more than 4:2:0.", assumes: "Luma carries text.", breaks: "Subpixel rebuild needs RGB. [16]" },
    { id: "M12", name: "Encrypted groups", core: "Per-packet AES-GCM with an index, or one file per group.", assumes: "Index kept.", breaks: "Torn appends. [17]" }
  ],
  explored: [
    { name: "C2 Damage-rect manifest", reason: "Not built: C1's family with more reuse; its damage hint must be accumulated in screen.py because desk.py resets frame sequences at every check." },
    { name: "C3 Scroll and move reuse", reason: "Not built: bit-exact shifted text fails at the primary's 125 % scale; scroll share unmeasured." },
    { name: "C4 Live low-delay SVT-AV1", reason: "0.59x at preset 11 against its 0.35x claim; preset 10 crf 42 (0.31x) fails OCR at 16-23 px (+0.028) and CPU p95; preset 8 crf 38 (0.27x) costs a 4K CPU p95 of 1,368 ms." },
    { name: "C5 other presets and staging", reason: "Preset 6 fails OCR at ≥ 24 px (+0.013); preset 10 loses screen tools (0.53x); JPEG staging crf 40 (0.40x, worker 301 s/h) reads +0.026 at ≤ 11 px on the synthetic runs." },
    { name: "NVENC streams", reason: "A CUDA context and 1.0-1.2 GB of VRAM for three sessions, against no VRAM growth [1]." }
  ],
  caveats: [
    "The real corpus is one busy hour of one desktop (2.3x the synthetic runs' frame rate); quieter or video-heavy days are unmeasured. Only built-in exclusions were applied while recording; the recording was deleted after the study.",
    "Synthetic sources are stored WebP: no bit-exact reuse and one more lossy generation for every candidate; indicative only.",
    "OCR CER is Windows OCR of each decoded frame against OCR of the raw frame, 150 frames per corpus; the ≥ 24 px bucket holds 2.4-3.2 k characters.",
    "C1's key CPU inside the harness was contaminated by C4's unlocked sweeps; measured directly at +0.34/+0.51 ms.",
    "C4's browser-view benchmark hung and was stopped; its view cost comes from C1 and C5.",
    "The MERL SCC paper [5] could not be read as text; no candidate depends on it."
  ],
  sources: [
    { n: 1, title: "Window composition baseline", url: "docs/baselines/2026-11-15-window-composition.md", tier: 1, version: "2026-11-15" },
    { n: 2, title: "PyAV 19.0.2 in the project venv", url: ".venv/Lib/site-packages/av", tier: 1, version: "19.0.2" },
    { n: 3, title: "SVT-AV1 Parameters", url: "https://gitlab.com/AOMediaCodec/SVT-AV1/-/raw/master/Docs/Parameters.md", tier: 1, version: "master 2026-11-20" },
    { n: 4, title: "screenpipe snapshot compaction", url: "https://github.com/mediar-ai/screenpipe", tier: 1, version: "local clone 2026-11-20" },
    { n: 5, title: "Overview of the HEVC Screen Content Coding Extension", url: "https://shadow.merl.com/publications/docs/TR2015-126.pdf", tier: 1, version: "2016" },
    { n: 6, title: "A Technical Overview of AV1", url: "https://arxiv.org/pdf/2008.06091", tier: 1, version: "2020 preprint" },
    { n: 7, title: "FFmpeg libsvtav1.c", url: "https://raw.githubusercontent.com/FFmpeg/FFmpeg/master/libavcodec/libsvtav1.c", tier: 1, version: "master" },
    { n: 8, title: "MS-RDPEGFX cache-to-surface", url: "https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-rdpegfx/78c00bcd-f5cb-4c33-8d6c-f4cd50facfab", tier: 1, version: "rev 18.1" },
    { n: 9, title: "MS-RDPEGFX progressive codec", url: "https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-rdpegfx/33fee2d9-e103-411c-9342-c51db32a9ace", tier: 1, version: "rev 18.1" },
    { n: 10, title: "RFC 6143 Remote Framebuffer Protocol", url: "https://www.rfc-editor.org/rfc/rfc6143.html", tier: 1, version: "2011" },
    { n: 11, title: "Prior-art search for sealed per-frame AV1 storage", url: "", tier: 3, version: "2026-11-20" },
    { n: 12, title: "WebP near-lossless announcement", url: "https://groups.google.com/a/webmproject.org/g/webp-discuss/c/0GmxDmlexek", tier: 2, version: "2015" },
    { n: 13, title: "Chromium remoting VPX encoder", url: "https://chromium.googlesource.com/chromium/src.git/+/main/remoting/codec/webrtc_video_encoder_vpx.cc", tier: 1, version: "main" },
    { n: 14, title: "Forensic artifacts in Windows 11", url: "https://securelist.com/forensic-artifacts-in-windows-11/117680/", tier: 3, version: "" },
    { n: 15, title: "Rewind compression claims", url: "https://9to5mac.com/2022/11/02/rewind-ai-records-everything-you-do-mac/", tier: 3, version: "2022" },
    { n: 16, title: "Small Text Windows OCR study", url: "docs/solutions/2026-11-20 - Small Text Windows OCR/report.md", tier: 1, version: "2026-11-20" },
    { n: 17, title: "Akashic MediaStore", url: "src/akashic/storage/media.py", tier: 1, version: "2026-11-20" }
  ]
};
