# When do Chromium and Firefox browsers publish a Windows media session, and why did Akashic's media row seem 16 s late?
Date: 2026-11-19 · Context: Python 3.14, Windows 11 build 26200, winrt GlobalSystemMediaTransportControls (GSMTC) polled every 2 s; Akashic `capture/media.py`; harness `eval/quality/` · Goal: explain Akashic's measured media start error and how to get video identity from Zen (audit #189, M6.1) · Ruled out: none

## Answer
There is no browser or Akashic delay to fix: the 16 s was a scoring artefact. The smoke video
started during the scenario's 60 s Pause, Akashic polls no media while halted by design, and the
row opened at the first poll after resume (264 ms and 87 ms later); with truth starts moved out of
no-recording gaps the start error is 0.26 s and 0.09 s [1][2]. Chromium pushes a play to Windows
through a 10 ms debounce over one browser-wide session, and a spike measured Chrome's session
0.24 s after play [3][4]. Firefox code publishes to GSMTC by default but only for audible,
unmuted media at least 3 s long, which fits the earlier Zen spike's silent audio giving no session;
Zen with audible media is untested [5][6][7].

## Findings
### The measured 16 s
- Smoke run 2220: harness Pause at wall 1795037034330, resume 1795037094331; play at
  1795037078315; row start = play + 16.28 s = 264 ms after resume. Run 2241: row start 87 ms after
  resume [1].
- `MediaTracker` polls nothing while capture is halted (Pause, UI open) and only closes rows
  then (`capture/media.py:23-24, 216-219`), so no exclusion counter could grow during the gap [2].
- After the scorer fix, M6.1 media start error: 0.264 s (run 2220) and 0.087 s (run 2241); end
  error 0.72 s in both [1].
### Chromium (Chrome, Edge)
- One `SystemMediaControls` object per browser, created in `MediaKeysListenerManagerImpl`, shared
  by all tabs and windows; desktop PWAs get their own (Windows and macOS since M130) [3][8].
- A tab feeds the session only when its media has audio and the tab is not muted
  (`IsMediaSessionNeeded()`), apart from picture-in-picture and remote playback [9].
- Metadata, position and playback status reach Windows through a shared 10 ms debounce timer; the
  only other timed behaviour hides the controls 5 s after a non-playing state while the screen is
  locked [3].
- The 2026-11-17 spike measured 239 ms median (280 ms max) from the page's play acknowledgement to
  the session at a 250 ms poll, with a silent looping `<audio>`, autoplay flags and no click [4].
### Firefox and Zen
- `media.hardwaremediakeys.enabled` defaults to true, and `MediaControlKeyManager` monitors keys
  only when it is on [5].
- Media is controllable only when its duration is at least
  `media.mediacontrol.eligible.media.duration.s` (3.0 s), and the key listener waits while the
  element is not audible or its volume is 0 [5][6].
- An all-zero stream never becomes audible: the audibility monitor counts a sample only above
  about -72 dB [7].
- In private windows the published metadata is replaced by defaults (generic title, empty URL) [10].
- The 2026-11-17 spike saw no Zen session in 804 polls with silent audio [11].

## Caveats
- Chromium and Firefox code was read on their main branches, not the exact Chrome 157 or Zen
  1.24.1b releases; Zen may override the defaults [3][5].
- The Chrome latency was measured with flags, a silent `<audio>` element and no click, unlike the
  harness's clicked `<video>` [4].
- `WindowsSMTCProvider` as Firefox's Windows key source was not confirmed in the file read [5].
- One video per smoke run (n = 1 each) [1].

## Sources
[1] Akashic smoke runs and scorer — `eval/quality/results/20261118-smoke-s11-target-2220/truth/driver.jsonl:36,41,42`, `pages.jsonl`, `scores.json` (M6.1), same for `-2241`; `eval/quality/score.py` `m6` — tier 1 — 2026-11-19
[2] Akashic media tracker — `src/akashic/capture/media.py:23-24, 54, 216-255` — tier 1 — 2026-11-18
[3] Chromium `system_media_controls_notifier.cc` — https://raw.githubusercontent.com/chromium/chromium/main/content/browser/media/system_media_controls/system_media_controls_notifier.cc — tier 1 — main, 2026-11-19
[4] Akashic media sessions spike — `docs/baselines/2026-11-17-media-sessions.md:38-55, 89-170` — tier 1 — 2026-11-17
[5] Firefox `MediaControlKeyManager.cpp` and `StaticPrefList.yaml` — https://raw.githubusercontent.com/mozilla-firefox/firefox/main/dom/media/mediacontrol/MediaControlKeyManager.cpp, https://raw.githubusercontent.com/mozilla-firefox/firefox/main/modules/libpref/init/StaticPrefList.yaml — tier 1 — main, 2026-11-19
[6] Firefox `HTMLMediaElement.cpp` (`IsControllableMediaSource`, `StartMediaControlKeyListenerIfNeeded`) — https://raw.githubusercontent.com/mozilla-firefox/firefox/main/dom/media/mediaelement/HTMLMediaElement.cpp — tier 1 — main, 2026-11-19
[7] Firefox `AudibilityMonitor.h` — https://raw.githubusercontent.com/mozilla-firefox/firefox/main/dom/media/AudibilityMonitor.h — tier 1 — main, 2026-11-19
[8] Chromium system_media_controls README — https://chromium.googlesource.com/chromium/src/+/HEAD/content/browser/media/system_media_controls — tier 1 — HEAD, 2026-11-19
[9] Chromium `media_session_controller.cc` — https://raw.githubusercontent.com/chromium/chromium/main/content/browser/media/session/media_session_controller.cc — tier 1 — main, 2026-11-19
[10] Firefox `MediaStatusManager.cpp` — https://raw.githubusercontent.com/mozilla-firefox/firefox/main/dom/media/mediacontrol/MediaStatusManager.cpp — tier 1 — main, 2026-11-19
[11] Akashic media sessions spike, Zen — `docs/baselines/2026-11-17-media-sessions.md:203-209` — tier 1 — 2026-11-17
