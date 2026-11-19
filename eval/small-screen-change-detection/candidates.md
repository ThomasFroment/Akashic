Merged candidates (step 2). Brief: brief.md in this folder.

C1 Gated sparse tile shape test (merges trade-C1 and exploit-C1)
  USES: M1, M13, M3, M6, M14
  DERIVATION: new verdict = today's verdict (N > 1e-4 of reduced pixels moved > 16) OR shape test, so the new set of stored frames is a superset of today's and M1.1 >= today by construction. The shape test only runs when 0 < N <= 51.84 (4K, 960x540 reduced) / 23.04 (1440p), on the moved coordinates (np.nonzero), binned into 16x16 reduced tiles on four grids offset by 8 (every subset of extent <= 9x9 reduced px lies whole in one tile of one grid: proof, not sweep). Tile fires on (w>=3 and h>=3 and n>=4) or (w>=W2 and h>=2 and n>=5), W2 = 4 at 100 % and 5 at 125 % (x-height-only words like "non" are 2 reduced rows). Mask in uint8: (max(a,b)-min(a,b)) > np.uint8(16), exact, no wrap. Assumes a 4x box average keeps a reduced pixel when ink coverage >= ~8.5 % at contrast ~188; a bar caret <= 5 px wide touches <= 2 reduced columns in every placement.
  COST: compare ~1.0-1.42 ms (uint8 mask + count_nonzero; sparse path adds nonzero + <=52 coords x 4 grids), vs today 1.53-1.9 ms: extra about -0.5 to +0.3 ms vs +3 ms. Memory +0.
  FIDELITY: keeps word and badge in every placement (by construction for extent <= 9x9), never a 1-2 px bar caret, M1.1 >= today, OCR gate same `differs`. Fakes: timers, spinners, taskbar clock count as meaningful; fonts < 9 px not covered.
  FAILS WHEN: x-height-only 2-letter word at 13 px on 4K/125 % loses a fringe column (< W2); underscore caret wider than 11 px at 125 %; low-contrast text (delta < 64); a 2-row hover underline/focus ring at 125 %; seconds clocks; caret thickness >= 5 px.
  TEST: extend change_detection_small.py with "on","no","non","ou","oui","mm","ok","gg","lol" at 13/14/16 CSS px, badge, bar/underscore/block carets, 1-2 px lines, contrast 40/64/100/200, all 64x64 placements x 2 scales x 2 themes; superset check vs today; median compare ms (>= 9 runs) on the 7800X3D; then harness M1.1/M1.2.

C2 Gated exact sparse clusters (decompose-D1)
  USES: M1, M13, M6, M14
  DERIVATION: same regime gate as C1; below it, link moved points at Chebyshev distance <= 2 (bridges one empty reduced pixel inside a word), connected components exactly (n <= 51: n x n adjacency, boolean squarings or union-find), fire when a cluster is >= 3 wide, >= 2 high, >= 5 points. No tile grids, so no grid-cut misses. Keep int16 difference (or the exact uint8 max-min form).
  COST: <= 0.5 ms extra on the sparse path only (flatnonzero + 51x51 distances + components + bboxes); 0 otherwise. Memory +0.
  FIDELITY: as C1 (word and badge when contrast delta >= 64; caret <= 2 columns; M1.1 >= today; OCR gate same differs). Fakes: timers, spinners, clocks.
  FAILS WHEN: block/underscore cursor >= 6 device px wide; contrast delta < 64 (grey timestamps); 2-row hover underline / focus ring at 125 %; seconds clock every check.
  TEST: as C1, plus delta in {40,64,100,200}; exact superset check on random pairs.

C3 Anisotropic reduction 4 wide x 2 tall + gated span (trade-C3)
  USES: M14, M3, M13, M1, M6
  DERIVATION: signature from Pillow reduce((4,2)) gray, so vertical resolution doubles (x-height of 7 px covers >= 4 rows) while horizontal stays 4x (a caret <= 3 px covers <= 2 columns). Tiles 16 cols x 32 rows (64x64 device px), grids (0,0),(0,8c),(16r,0),(16r,8c); fire cols >= 3, rows >= 3, count >= 4; gate on N_42 > f*N (every moved 4x4 block has a moved 4x2 half, so superset of today up to Pillow rounding).
  COST: signature +0.5-1.0 ms (reduce output doubles) + blake2b of +0.5 MB (+0.5 ms); compare ~2.05 ms vs today 1.53-1.9: extra about +1.2-2.1 ms vs +3 ms. Memory +0.52 MB per 4K window, +0.23 MB per 1440p.
  FIDELITY: caret veto, badge, ascender/descender and x-height-only words with >= 1 row margin, M1.1 >= today (up to rounding), OCR gate same differs. Fakes: timers, spinners; fonts < 9 px.
  FAILS WHEN: wide nuisances >= 5 px tall and >= 9 px wide (thick hover highlights, Text cursor indicator, block cursor) now fire; seconds timer; real Pillow reduce((4,2)) cost unmeasured.
  TEST: C1's sweep with 1-2 px horizontal lines as negatives; median signature + compare ms on a real 4K frame on the 7800X3D.

C4 C1 + per-tile toggle set (trade-C2 merged with exploit-C2)
  USES: C1 (M1, M13, M3, M6, M14), M7, M9
  DERIVATION: in the sparse regime only, per changed grid-0 tile keep the last 3 zlib.crc32 of its reduced content in stored frames (LRU per monitor/gate key); a sparse fire is a toggle iff every changed tile's new crc is in its set; suppress only once that tile toggled >= 2 times in the last 10 checks (a block cursor at ~50 % toggles stays suppressed; a badge's first show, clear and re-show all store). Global-regime changes never tested, so M1.1 >= today. Allows W2 = 3 at 125 % (fixes the x-height/underscore overlap) at the cost of <= 2 underscore frames per session.
  COST: <= 4 changed tiles x (256 B copy + crc32 ~1-4 us) + dict/deque ~0.03 ms on top of C1. Memory <= ~100 KB per monitor or gate key (<= 3.2 MB over 32 keys).
  FIDELITY: everything C1 keeps; wide exact blinkers store <= 2 frames. Fakes: the third identical flap of a badge within 10 checks.
  FAILS WHEN: a badge flips back to an exact earlier state >= 3 times within 10 checks (50 s focused, 150 s side); AA drift, fades and spinners never match (fall back to C1).
  TEST: C1 sweep + synthetic 200-check sequences: block and underscore cursor at Bernoulli(0.5), badge flap sequences 1-4 flaps; pass <= 2 cursor frames per session, first 2 flaps stored.

C5 C2 + live/toggle temporal split with the previous check as a second reference (decompose-D2)
  USES: C2 (M1, M13, M6, M14), M9, M8, M7
  DERIVATION: D_s = now vs stored (C2 clusters); D_p = now vs previous check's gray (already computed by signature), restricted to bbox(c) +- 1 with C2's shape rule. live(c) = c is shape-positive in D_p and its box overlapped a cluster fresh at the previous check -> suppress (clock, spinner, typing); toggle(c) = blake2b of gray_now[bbox+-1] among the <= 2 contents recorded at that box -> suppress (A-B-A); a suppressed change stays in D_s and is stored at the first check where it is neither live nor a toggle (persistence without re-grab). OCR gate keeps C2's spatial differs.
  COST: C2 <= 0.5 ms + per cluster (<= 10) slice diff + hash ~0.25 ms: <= 0.8 ms extra. Memory +518 KB per 4K monitor, +230 KB per 1440p (previous gray) + ~0.5 KB digests.
  FIDELITY: keeps C2's musts; a reply in a quiet region is stored at once; clocks/spinners ~1 frame per episode; block caret ~1 per position. Fakes: the time of A-B-A reverts; a change inside a live region is delayed >= 1 interval.
  FAILS WHEN: text that never holds still for one interval within <= 8 px of a ticking clock/spinner, live captions or a status line rewritten in place, a 2 s toast in a live region -> lost; same content returning at the same box (badge 1->2->1) not stored again; after a dense store the history is empty (one extra clock frame).
  TEST: C2's tests + synthetic check streams: seconds clock, 16 px spinner, 9x19 block caret at random phase, typing burst then pause, reply in a quiet region, reply 8 px from a clock; pass <= 1 useless frame per episode, 100 % of replies stored within <= 1 interval after holding still.

C6 C1 + stationary/periodic volatility cells (exploit-C3)
  USES: C1 (M1, M13, M3, M6, M14), M7 (recency-limited state, per cell)
  DERIVATION: per 16x16 reduced cell (2040 at 4K) keep run = consecutive sparse checks with a C1-passing tile in the cell and rollover flags of its last 2 hits (rollover = wall-clock minute changed since previous check). Volatile iff run >= 3 or (last 2 hits at rollover checks and this check is one). Fire iff some C1-passing tile lies in a non-volatile cell. On suppression copy the volatile cells' current pixels into the reference gray and recompute its digest (else the minute clock fires next check). A digest-equal check resets runs. Global regime untouched (M1.1 >= today); OCR gate keeps C1.
  COST: ~0.02 ms per check; on suppression <= 4 tile copies + blake2b of 518 KB ~0.5 ms: worst extra ~0.8 ms. Memory ~6 KB per monitor.
  FIDELITY: keeps C1's musts on a page with no history; seconds clocks/spinners <= 3 frames per run, taskbar clock <= 2 frames per monitor per session. Fakes: changes inside volatile cells.
  FAILS WHEN: a reply lands in the same 64 px cell as a timer, spinner or the user's own draft that changed at each of the last 3 checks (missed until a larger change); a tray badge next to the clock appearing at a rollover check (P ~ 1/12 focused); a draft typed >= 15 s stops storing.
  TEST: C1 sweep + synthetic 60-min sequences at 5 s and 15 s checks: seconds clock, minute taskbar clock, text spinner, typed draft, 50 one-word replies at random times/cells; pass <= 3 frames per seconds-clock run, <= 2 minute-clock frames per monitor, 0 misses outside volatile cells.

Explored (not kept as a candidate): OCR routing add-on (decompose-D3): pass stored-frame cluster boxes to the OCR gate and crop to the union with find_text_box, ~0.05 ms; complementary to every candidate, outside the storage decision.
Inventor QUESTIONS: block/underscore cursor on screen?; losing the time of reverts acceptable?; third identical badge flap; drafts meaningful?; seconds timer in the M1.2 scenario?; Text cursor indicator / caret thickness > 3?

Fact-check (step 3): M1 SUPPORTED; M3 four-grid sweep numbers WEAK (from uncommitted refuter runs; the JSON holds only per-case bbox stats) -> re-measure in the harness; M3/M14 caret/word shape ranges SUPPORTED; M4 pixelmatch windowSize SUPPORTED; M5 mpdecimate SUPPORTED (limit = (w/16)*(h/16)*frac); M6 Stauffer-Grimson UNREACHABLE (PDF text not extractable) -> caveat; M8/M10 GetCaretBlinkTime SUPPORTED, but the 530 ms default is not in the cited page; GUITHREADINFO SUPPORTED; M2 TigerVNC SUPPORTED; M13 timings WEAK (script prints only, 544x960 padded) -> re-time on the 7800X3D.

Prior art (step 3): none of C1-C6 found published or shipped as stated. Nearest: C1 x11vnc -fuzz/-grow/-gaps/-fs tile heuristics (-fs whole-screen fallback ~ the fraction gate) https://www.mankier.com/1/x11vnc ; C2 connected components with removal of short tracks, Stauffer-Grimson 1999 https://www.cse.buffalo.edu/courses/cse725/peter/Stauffer_1999.pdf ; C3 RLSA (Wong, Casey, Wahl 1982, anisotropic smearing; secondary source only) ; C4 Stauffer-Grimson multi-modal models (monitor flicker bimodality, Fig. 2c) and RDP persistent bitmap cache (content-keyed tiles, LRU) https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-rdpbcgr/2da3e165-d1ba-4b65-8909-7a0f7f858d69 ; C5 surveillance blinking-pixel removal papers (not read) ; C6 Percy/Applitools ignore regions (manual) https://www.browserstack.com/docs/percy/set-regions/overview . Recall/Rewind/ManicTime: undocumented. Stauffer-Grimson text was read via pdftotext by this researcher (fact-checker could not extract it).

Refuter (step 3), scripts in refuter/:
- C1 REFUTED: W2 = 4/5 misses x-height words without ascenders ("rn" 7-8/16, "re", "xx", "ça", "vu", "va", "us", "ez", "so", "eu", "un"; both scales, 13-14 px); W2 = 3 fixes words but a 2 px underscore 8-12 px wide fires 1-4/16. Holds: four-grid containment proof, bar carets 1-5 px 0/16 (6 px 4/16), superset, uint8 max-min. Mask + count 0.43 ms vs today's int16 1.40 ms. np.nonzero on 2-D costs 0.75 ms; use np.flatnonzero (0.026 ms).
- C2 PARTIAL: all 30 words 16/16 both scales/themes; bar carets 1-5 px never; extra <= 0.53 ms. Conditions: the taskbar minute clock (painted in every composite) fires 0-36 flips/h per 4K monitor depending on sub-pixel placement (1440p up to 10): worst placement adds ~+0.09 useless ratio vs day baseline (breaks +0.05), mean ~+0.04; 2 px underscore 8-16 px wide fires 1-4/16.
- C3 PARTIAL: detection holds; signature measured 8.50 vs 6.02 ms at 4K (+2.48 ms: reduce((4,2)) +1.55, blake2b +0.73), total extra ~+3.1 ms over budget unless a fused uint8 compare + flatnonzero (0.51 ms on 960x1080, total ~+1.7 ms); taskbar minute clock fires 11-46/h per 4K monitor (mean 28), likely breaks M1.2.
- C4 REFUTED: per-tile crc set cannot both store a badge's show/clear/re-show and keep a blinking cursor to <= 2 frames (same sequence); inherits C1's W2 misses. Narrower variant: W2 = 3 both scales, <= 3 cursor frames per session.
- C5 PARTIAL: words/carets as C2; cost <= 0.8 ms plausible; the live/toggle split does not cover the taskbar minute clock (not live, not a toggle) -> up to 36/h per 4K monitor; drafts held still less than one check interval are never stored (input-triggered checks can be 1 s apart).
- C6 REFUTED: inherits C1's W2 misses; volatility does not suppress a 50 % blinker (digest-equal checks reset the run). Minute-clock rollover handling stands.
Survivors for step 4: C2, C5, C3 (all PARTIAL; shared risk: taskbar minute clock).
