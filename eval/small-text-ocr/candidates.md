Merged candidates (step 2). Brief: brief.md in this folder. Raw inventor returns: candidates-raw.md. Candidate modules plug into harness/run.py (prepare / merge / read / store / load; ctx.path live|deferred|replay).

C1 Single-call strip mosaic with budgeted per-strip scale (decompose-D1 luma variant)
  USES: M7, M8, M1, M4, M12
  DERIVATION: blocks = connected components of find_text_box text cells; strips = row runs of ink inside each block (gaps <= 1 px merged so accents stay attached); small := run height h <= 15 device px; stack all strips into one bitmap with edge-colour gaps max(6, 0.5 h_out); large strips at 1x, small strips at s = min(3, sqrt((A_allow - A_large - A_gaps)/A_small), 10000/W_max) with A_allow = A_crop + (40 ms - t_pre)/k (k ~ 42 ms/Mpix fitted from the harness: t = content + k x pixels); one OCR call replaces today's call; word rects mapped back (x = x0 + x'/s, y by strip). Luma Lanczos.
  COST: preprocessing ~8 ms/Mpix; per call delta <= 40 ms by construction (whitespace removed is credited); CPU <= 2x per call; storage +0.
  FIDELITY: 4K unchanged for >= 16 px; UIA untouched; M3.3 kept (every strip in the mosaic); layout via mapped rects. Fakes: < 8 px.
  FAILS WHEN: all-small windows (small fraction >= 0.5) get s < 2 (9-10 px CER ~0.1-0.3); sidebar and body in one run takes the larger height; too small a gap joins strips; whitespace may cost OCR less than ink (credit overestimated); replay on WebP frames limited to luma quality.
  TEST: corpus (all renderers) + standard/day replay; wrap-tolerant M3.1 by bucket/resolution, M3.3, UIA, dms_p95, CPU ratio; fitted k and prediction error; mapped rects inside strips; sweep 9-18 px both scales.

C2 C1 + ClearType phase split (decompose-D1 phase split, exploit-E1 unfold)
  USES: C1, M5 (as a phase split, not chroma removal)
  DERIVATION: for strips with fringe energy mean|R-B| >= 5 % of contrast (or chroma/luma HF energy > 0.3), per channel a_c = (v_c - bg_c)/(fg_c - bg_c) with bg = per-channel mode, fg = per-channel extreme, clipped [0,1], float32; a channel with |fg_c - bg_c| < 24-48 takes the mean of its neighbours; interleave g[y, 3x+k] = a_k (3W coverage), then Lanczos to sW x sh, dark on light; RGB/BGR order per monitor by lower total variation; grayscale-AA strips and every deferred/WebP frame (4:2:0 destroys the phases) use luma Lanczos.
  COST: as C1 + ~3 ms per strip set.
  FIDELITY: as C1; better small-text quality at the same s when the raw capture carries ClearType.
  FAILS WHEN: unfold misdetected (portrait monitor, coloured text, uneven background) -> doubled stems or ghost edges, worse than Lanczos; OLED or ClearType off -> never triggers; untestable on WebP replays (only on raw corpus renders or new live runs).
  TEST: as C1 on the raw Chromium-ClearType corpus; A/B against C1 at equal s.

C3 Native pass + capped small-line refine with cache and pending drain (trade-T1, exploit-E1 core)
  USES: M9, M7, M8, M1, M4
  DERIVATION: today's native call stays; small lines = native OcrLines whose word-box height < H12 (calibrated) plus text cells no word box covers; one extra OCR call per read on a mosaic of small lines at 3x (2x for 11-12 px), sized for <= 35-43 ms of OCR (<= 0.6 Mpx out); blake2b(line pixels) -> text cache; lines over the cap stay pending and run on later reads of an unchanged window; refined lines replace overlapping native lines; CPU governor/token bucket keeps refine CPU <= 0.9 x native per hour.
  COST: per call <= +43-50 ms; per hour <= 1.96-2.0x by the governor; cache ~0.3-3 MB.
  FIDELITY: 4K small lines refined too, nothing worse; M3.3 native kept; text not yet refined keeps native quality.
  FAILS WHEN: small text changes before pending lines drain (scrolling logs, chat floods, tooltips); gradient backgrounds defeat the cache; governor exhausted on small-text-heavy hours; gate not reading unchanged windows means no drain.
  TEST: corpus + replay in frame order with the cache and governor live (read() loops capped calls until drained); dms_p95, CPU over the day-run replay, lines left unrefined.

C4 Height-normalized strips with water-filled scale, shrinking large text (decompose-D2)
  USES: M2, M8, M7, M1, M4, pack: bracketing root finders (bisection)
  DERIVATION: per strip s_i = min(3, 30/h_i) for h_i <= 15; 1 for 15 < h_i <= 24; max(0.6, 20/h_i) above 24; if sum A_i s_i^2 + A_gaps > A_allow, s_i(lambda) = max(1, lambda s_i) on small strips with lambda by bisection; shrinking large 4K text pays for upscaling small text inside one call; per-strip polarity/contrast normalization to dark-on-light luma.
  COST: as C1 + bisection < 1 ms; 4K dense windows save ~1 Mpix by shrinking 22-27 px runs.
  FIDELITY: as C1; 4K must holds only if 20 px runs read as well as today.
  FAILS WHEN: thin fonts or text over images shrunk on 4K -> 4K CER rises; x-height-only runs over-scaled (cost only); all-small 1440p windows same as C1.
  TEST: as C1 + per-strip 4K before/after for shrunk strips.

C5 Settled-frame worker refine with a per-line scale ladder (trade-T2, T3)
  USES: M8, M1, M4, M11, M7
  DERIVATION: once a window's gate signature has stayed unchanged >= T = 3 s, enqueue a worker job: decode the stored WebP, find small lines by row profiles (|d gray/dx| > 48 counts per row in 256 px strips, f = h/kappa), re-read only lines with f <= 10.5 at 3x and 10.5-12.5 at 2x in chunks <= 0.6 Mpx per call; merge by y-overlap; line cache; governor. Live path untouched.
  COST: per settled state ~33 ms decode+profile + refine; per hour 1 + g (o + m phi k) <= 2x with the governor; per call <= +50 ms in chunks; live +0.
  FIDELITY: as C3 for settled text; refined text searchable after T + queue.
  FAILS WHEN: text shown < T s keeps native CER 0.6-0.8 (notifications, hover cards, chat lines); mixed sizes in one strip take the larger; 12/13 px boundary; governor binds on long small-text reading; WebP limits it to luma quality (replay 3x ceiling 0.049/0.058).
  TEST: replay with dwell from stored frame timestamps; settled share g; CPU incl. decode.

C6 Worker band jobs with transient lossless crops (exploit-E2, decompose-D3 merged)
  USES: M7, M8, M9, M10, M11, M1, M3, M4, M5 (phase split)
  DERIVATION: the live read estimates bands and hashes them; only unseen small bands are enqueued as encrypted lossless crops (PNG level 1 or WebP lossless m2); worker jobs (<= 1 mosaic each, <= 35 ms OCR) apply C2's phase split or Lanczos, OCR, replace the band's lines in the frame's text, delete the crop; bucket 0.8 x native OCR CPU; backlog capped at 0.2 x daily media bytes.
  COST: live +<= 26 ms (encode ~19 ms estimated); worker per call <= 41 ms; storage transient, capped.
  FIDELITY: as C2, including content shown once; M3.3 native kept until replaced.
  FAILS WHEN: worker starved (gaming, long jobs) -> backlog cap, text upgraded minutes late or not at all; crash leaves orphan crops (clean at start); full-screen small text over the encode cap; unfold misdetection as C2.
  TEST: corpus with jobs drained; live dms_p95 incl. encode; job p95; CPU live + jobs; peak backlog bytes; capture-to-upgrade delay.

Inventor QUESTIONS (collected): does +50 ms p95 cover worker refine calls?; may text shown < T s keep native quality, may refined text arrive later?; is the <= 11 px must judged on WebP replays (luma only) or new live runs (phase split testable)?; monitor subpixel layout (RGB/BGR, portrait, ClearType on)?; occasional slow live OCR on all-small windows acceptable (hard per-call cap vs p95 over the real call mix)?
Harness note: baseline.py, explored/up3x_lanczos.py and baseline/metrics.json are in the parent folder eval/small-text-ocr/, not under harness/.

Fact-check (step 3): SUPPORTED: WebP q70 m2 native resolution (imaging.py:30-33, 70-74); only defer_ocr windows OCR'd from WebP, others by the gate on the raw crop (screen_writer.py:356-359, ocr_gate.py:54-66); recognize does no upscaling, only thumbnail above MaxImageDimension (ocr.py:48-62); find_text_box 16x16 cells on reduce(2), gradient > 48, density 0.02-0.5 (imaging.py:34-37, 77-105); PowerToys fixed scale HighQualityBicubic, pixel(0,0) fill, 64 px min + 8 px pad on sides under 64; Text-Grab MagickHelpers filters; screenpipe microsoft.rs no preprocessing; Pillow WebP no near_lossless/sharp_yuv (lines read); JBIG2 Xerox substitution (encoder setting bug). WEAK: Text-Grab 40 px rule (project's own summary only, OcrSourceUtilities.cs not opened); Tesseract "below ~10 px x-height poor" not on the cited page (border ~10 px is); lossy WebP 4:2:0 not stated on the cited Google page (RFC 6386 not opened).

Prior art (step 3, shallow search): C1 strip packing ~ "example packing" (No Padding Please, arXiv 1902.11208; inside a neural model, not an opaque engine); C1/C4 fixed-height line normalization is the recognizer convention (PP-OCR 48 px, snippet only); C4 shrinking large text: none found (screenpipe ocr_scale_bench measures downscale loss on Apple Vision); C2/C6 ClearType unfolding: none found (Microsoft "Displaced Filtering for Patterned Displays" is the forward rendering direction, https://www.microsoft.com/en-us/research/publication/displaced-filtering-for-patterned-displays/); C3/C5/C6 pixel-hash cache: screenpipe ocr_cache.rs (per window, sampled non-cryptographic hash, 5 min, 100 entries, no refine path); progressive/background re-OCR: none found; lossless sidecars beside lossy frames in screen recorders: none found. Empirical risk for C1/C3/C5/C6: whether Windows OCR merges mosaic strips across gaps.

Refuter (step 3; no OCR experiments run, code and measured baselines only):
- C1 PARTIAL: whitespace credited at k = 42 ms/Mpix but blank pixels cost ~15 ms/Mpix (empty windows 24.2 ms at 3.7 Mpix, 51.6 ms at 8.3 Mpix, dense ~63 ms/Mpix: docs/baselines/2026-11-17-ocr-decode-quns.md:84-93); mosaic is denser than the crop. Fix: credit removed area at the blank rate (or not at all), charge mosaic pixels at >= k. Text cells are 32 device px and borders count as text, so sidebar and body form one block with merged offset rows: split at column gutters first (XY-cut). Luma Lanczos supported (up3x read WebP-decoded copy better than raw at <= 11 px, 0.084 vs 0.106). Ceiling: fixed 3x on the day replay scores 0.058 at <= 11 px (n = 35) > 0.05.
- C2 PARTIAL: inherits C1's cost flaw; Chromium uses one global RGB/BGR geometry from ClearType settings and disables LCD text per layer (non-opaque background, will-change transform, non-integral offsets): menus and side views grayscale while editors are ClearType (Cursor forum), so per-strip fringe detection is required. Phase split vs Lanczos untested.
- C3 PARTIAL: the pending drain mostly never runs: an unchanged monitor submits no Picture (screen.py:513-526), the gate returns stored text before OCR (ocr_gate.py:61-63), the writer stores only for a new Picture (screen_writer.py:279-287). Fix: drop the queue; refine the most valuable small lines within the cap on the same read (or a timed re-check bypassing the gate). 0.6 Mpx in ~43 ms consistent with the 3x sweep.
- C4 PARTIAL: same credit flaw; its rule shrinks only runs > 24 px, so the saving is overstated; 20 px runs stay readable (>= 16 px font native CER 0.002).
- C5 PARTIAL: full-monitor WebP decode alone 42.9 ms (1440p) / 113.2 ms (4K); frame_windows.text is plain text without line rects, so merging needs a native re-pass (250-415 ms dense) or new stored rects; other rows of the same window keep native text; text updates break answered-question citations; best case is the up3x ceiling (0.058 day).
- C6 PARTIAL: CHECK (capture_path IS NULL OR text IS NULL) forbids text + crop on one row; needs a new table/column plus retention, export and deletion handling; no line positions in stored text; job payloads are ids only; live encode cost unconfirmed.
Prototypes: C1 (luma, fixed costs: blank credit at 15 ms/Mpix, XY-cut), C2 (C1 + per-strip phase split on raw corpus), C3 (native + same-read capped refine, no pending queue).
