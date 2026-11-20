# Live check of C5, 2026-11-20
Study: `docs/solutions/2026-11-19 - Small Screen Change Detection/report.md`. Figure: `side.png`. Numbers: `results.json` (edge, edge2, chrome, term3). Invalid runs: `results_term_invalid.json`.

## Setup
Real Edge and Chrome windows showing `chat.html` (synthetic chat page, events logged to 127.0.0.1) and a Windows Terminal window running `term_child.py`, on the right 1440p monitor at 100 %. Capture as production: `WgcCaptures` (min interval 1000 ms), one check every 5 s, frames reduced as `imaging.signature`. `analyze.py` replays the frames through today, C2, C3, C5 and C5fix (`c5_livefix.py`: dense-gate stores also update the last-move map). Scored at clean checks only.

## Small changes missed at the first check (Edge, Edge rerun, Chrome)
| | today | C5 | C5fix | C2 / C3 |
| :- | -: | -: | -: | -: |
| replies, all | 131/356 | 1/356 | 1/356 | 0 |
| replies 13 / 14 / 16 px | 55/122, 39/104, 37/130 | 1/122, 0, 0 | 1/122, 0, 0 | 0, 0, 0 |
| replies dark / light | 75/170, 56/186 | 0, 1/186 | 0, 1/186 | 0, 0 |
| badge on | 11/52 | 0/52 | 0/52 | 0/52 |
| badge off | 9/38 | 1/38 | 1/38 | 1/38 |
| still missed at the second check | 68 replies, 8 on, 3 off | 0 | 0 | 0 |

C5's missed reply (13 px, light theme, on the line under a reply 2 checks earlier) was held back by the live rule and stored at the next check. Today, quiet phase: 14/25 replies missed (synthetic: 33/50).

## False stores per hour
| nuisance (clean hours) | today | C5 | C5fix | C2 | C3 |
| :- | -: | -: | -: | -: | -: |
| native caret (0.275 h) | 0 | 0 | 0 | 0 | 0 |
| 2 px CSS caret (0.168 h) | 0 | 0 | 0 | 0 | 0 |
| seconds clock (0.221 h) | 0 | 0 | 0 | 439 | 534 |
| spinner (0.215 h) | 0 | 0 | 0 | 706 | 706 |
| Windows Terminal bar cursor (0.081 h) | 0 | 0 | 0 | 0 | 0 |
| Windows Terminal block cursor (0.164 h) | 0 | 0 | 0 | 616 | 616 |
| busy phase, stores not tied to a reply or badge (1.217 h) | 2.5 | 54 | 8 | 344 | 346 |

The busy-phase 54/h comes from dense-gate stores that do not update C5's last-move map; C5fix fixes it with the same recall. Synthetic 4K sequences: C5 7 clock, 0 spinner, 8 block-cursor stores per hour.

## Cost per compare (real 1440p frames, loaded PC)
| | today | C5 | C5fix |
| :- | -: | -: | -: |
| median, browser runs | 0.52-0.63 ms | 0.35-0.38 ms | 0.38-0.45 ms |
| mean on changed frames | 0.61-0.73 ms | 0.45-0.48 ms | 0.53-0.58 ms |
| p95 | 1.09-1.49 ms | 0.80-1.00 ms (terminal 1.23 ms) | 0.95-1.37 ms |
| worst single call | 2.9-3.6 ms | 2.9-6.0 ms | 3.2-4.8 ms |

Signature: 3.4-3.8 ms at the median for every candidate.

## Validity
- Valid: edge (786 checks, 686 clean; no caret in its busy phase), chrome (786, 586), edge2 (396, 327), term3 (187, 176).
- Display off (System log, Kernel-Power 566): 02:31 (4 s), 02:43 (13 s), 03:29 (2 min), 04:32 (65 s), 04:51 (1 s), 05:07 (91 s), and from 05:29:37 until at least 05:50 (end of term3). Browsers stopped presenting while off; `analyze.py` drops the browser checks touching those spells, plus the next check (7, 25, 15 checks; numbers unchanged). Windows Terminal kept presenting normally; term3 was captured wholly with the display off.
- Invalid: term and term2. `run_live.py` minimized and restored the terminal at every bar and block check, giving blank whole-window frames (24 and 29 checks). Fixed; `restores` is 0 in term3. term2 was also interrupted at check 132, before its bar phase; term3 replaces both.
- Superseded: `edge_results.json`, `chrome_results.json`, `test_results.json`.

## Limits
One synthetic page and one terminal; no real apps or ClearType. 1440p only. Short clean hours (0/h means below about 4-6/h). Terminal result only from a display-off run. 5 s checks on a single window; 15 s side-monitor checks and full-monitor composition untested. Loaded CPU makes timing tails noisy.
