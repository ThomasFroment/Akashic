# Manual verification checklist

Automated tests cover core behavior and `scripts\selftest.ps1` covers the real audio devices,
Windows OCR, GPU, speaker models and Whisper. Use this checklist for behavior that needs a person,
real apps or a real day of data. Run against a throwaway `AKASHIC_DATA_DIR` where practical.

This lists checks to perform, not results that passed. Record scope/date with a result.
Frames are monitor composites; evidence images are the cited window's visible part.

## Unlock, tray and local UI

1. Create a store, confirm a wrong passphrase fails and cancelling exits without starting capture.
2. After unlock, confirm the app, capture and worker processes run and all stop on Quit.
3. Open the UI from the tray: it lands on Ask. Reusing its `/open?code=` URL returns 403, the
   session cookie is HttpOnly. While the tab is visible nothing is recorded and the sidebar says
   "Not recording while Akashic is open"; closing or hiding the tab resumes recording. Open
   Akashic's address from a bookmark: the failsafe halts capture. Autostart does not open the UI.
4. Pause and resume from both the tray and sidebar. Lock must stop both child processes, forget the
   key and show the unlock dialog again; unlocking starts fresh processes and a new session.
5. Toggle Settings > Startup and verify the `Akashic` HKCU Run value and a real sign-in start.

## Screen capture and Windows adapters

1. On every monitor, switch windows, type, scroll and change browser tabs. Search > Screen should
   show per-window hits with the correct monitor, title, process, URL and UIA/OCR text, and the
   timeline stage should show every monitor.
2. Verify `displays.py` reports every monitor in physical-pixel order; `windows.py` reports the
   focused window and the visible windows; Win+L produces only `locked` outcomes.
3. Verify `uia.py` reads Notepad, browsers and Discord without making Discord stutter. Firefox/Zen
   and Chromium URLs should work; a page with a focused password field is kept.
4. Verify `ocr.py` reads an image or game menu. Without the French language pack, the log should
   report the fallback to English.
5. Private windows, password managers, configured exclusions and finance sites must show as a
   blank area with no window row or text; a monitor showing only them stores nothing (Hidden).
   DRM video keeps its window list and no image. Logs must contain counts and
   exception types only, never titles, URLs or captured text.
6. Moving a high-rate gaming mouse should not raise capture CPU materially above idle.
7. On three monitors, with a private or bank window excluded in each case, check that nothing of
   it reaches the timeline or search:
   - a private window on a side monitor, and a translucent window over it;
   - Alt-Tab, Snap Assist, a taskbar preview and Win+Shift+S with a private window open;
   - a password-manager extension popup;
   - dropdowns, suggestions and tooltips on a private page;
   - a page navigating to a bank address;
   - a hung private window (its "Not Responding" ghost);
   - a bank page, excluded by its readable address but kept while typing in its address bar;
   - minimizing a private window;
   - elevated windows, mixed DPI and negative coordinates;
   - the evidence crop of a partly covered window and of one crossing a monitor edge;
   - Chrome picture-in-picture is blank; Edge and Zen picture-in-picture follows its window.
   Inspect multi-column text, repeated chat messages and navigation controls for useful reading
   order.

## Audio and transcription

1. In Settings, verify the real microphone/playback device lists and the live voice meter (it
   reads "Levels are not available while Akashic is open"). Speak
   quiet French and normal English; Search > Speech should show both, with uncertain lines dimmed.
2. Play a video and hold a Discord call. Microphone lines must be You, system lines must name the
   audible processes, and music-player-only sound must not be stored.
3. Play every line and confirm it matches the right recording. A long monologue must not duplicate
   overlap words. Measure whether vocabulary hints improve names; do not assume exact spelling.
4. Pause and speak: no audio is stored and the microphone-in-use indicator turns off. Unplug or
   switch the device and verify capture recovers on the replacement.
5. Build a transcription backlog. It must drain oldest-first when idle, wait during a game or high
   GPU/CPU load, and resume afterwards. Measure unload delay on starting a game; running jobs
   are not preempted, so do not assume a two-second bound.
6. In Settings > Models, select/save/download each speech candidate. Build corrected references
   from 6–12 real clips, mark meaning-changing terms and compare the same models locally. Inspect
   FR/EN/mixed outputs, omissions and hallucinations alongside word errors. Verify original
   transcripts remain unchanged and removing a sample/transcript removes comparison copies.
7. With Discord's output on the headset and `[audio] system_device` another endpoint, hold a call
   while a video plays on the recorded device. System lines name only the recorded device's
   processes, and Ask "what did I hear at <hh:mm>?" (a time during the call) says voice was not
   recorded. With sound playing through Wave Link, run
   `uv run pytest -m hardware -s tests/hardware/test_real_hardware.py -k audible_processes_are_keyed`
   and confirm the mixer's process name matches `MIXER_PROCESSES` (`elgato.wavelink.exe`,
   unconfirmed).

## Media, sessions and gaming

1. Play, pause, resume, change and close media in a browser and a desktop player. Timeline/search
   should reflect titles, played time and a URL where available. Include concurrent windows/sessions
   and focused/background private playback; the limitations are in SECURITY.md and audit #145,
   not a blanket exclusion pass. A private window on a side monitor cuts that browser's media.
2. Switch among normal windows and verify contiguous app sessions are built without excluded apps.
3. Test borderless and exclusive-fullscreen games. The tray should show game mode, the game
   monitor should be kept at the game rate and the others at the side rate, without UIA and with
   OCR deferred, and the text should be searchable after the game closes. F11 video and
   maximized windows must not be mistaken for games.
4. On three monitors, open the timeline: every monitor at its real position; states with the age
   of the check; "Monitor N" labels matching the problem messages; hover lists a frame's
   windows; an Ask video link opens its moment, and Back returns to it.
5. Play a browser video, pause 5 min, resume, then lock for 2 min and accept a UAC prompt. One row
   runs through the lock and the prompt, and its end does not advance while paused. A video
   started while locked gets no row until unlock. After sleep onto the lock screen, the row ends
   at the last poll before sleep and a new one opens after unlock.

## Speakers

1. Record two real voices through Discord or a podcast. Speakers should contain distinct playable
   samples; name them and verify later turns reuse the names. Mixed transcripts must remain
   unattributed; finding a recording for a participant does not prove they said all its words.
2. Correct a wrong turn, merge duplicate profiles, and confirm the affected lines update without
   changing unrelated speakers. Tune the match threshold only if real voices split or merge.

## Search and Ask

1. After a representative day, verify video, call, game, browsing and work episodes appear with
   evidence links. Cross-language meaning search should find French from English and vice versa.
2. Ask the three product questions in `AGENTS.md`, in French and English. Answers must use the
   right time/person/kind filters, cite playable or viewable moments and never invent an answer
   when nothing matches. Flagged or unclear speech alone never supports a factual answer, even
   hedged: it is shown as a possible match linking the recording, with a note that it is too
   uncertain to answer. For "Give me a list of all the series I watched", every series watched is
   listed with a link, or the answer says how many of how many it shows.
3. Ask while a game runs. Explicit requests bypass background deferral but still wait for the
   active job. Observe response time, peak memory and unloading after returning to the worker
   loop; there is no guaranteed two-second bound. STT and chat must release one another between
   jobs; chat/embeddings may remain together, and the desktop uses additional memory.
4. For a synthetic model comparison, run `scripts/eval_ask.py` against a separate model/runtime
   folder, with `--repeats 3`, `--output` and `--compare` against the `--output` of the latest
   baseline in `docs/baselines/`. Inspect
   per-question differences and keep the frozen split intact. Use
   `eval_hard_memory.py` for harder failures; neither script tests real OCR/ASR.

## Settings, retention and diagnostics

1. Change each recorder setting, restart recording and verify it applies; a pause must survive the
   restart. Invalid values must not save, comments must remain and `config.toml.bak` must hold the
   previous version. Theme changes apply immediately.
2. Settings > Models must download one model or all missing models with progress; Ask should work
   without restarting after the chat model finishes.
3. Shorten screenshot and audio retention against disposable old data. Confirm the warning, cancel
   once, then apply it: only the selected media disappears while searchable text remains. Restore
   `forever`; deleted media must not return. Verify one retention pass per day.
4. Debug > Overview and Logs must explain stalled jobs and capture outcomes without exposing
   captured content. Settings must report disk use by type.
5. While recording, Settings > Export one day (From/To) with screenshots and transcripts into an
   empty folder on another drive. Cancel a second export into another empty folder and confirm no
   `.partial` is left and that folder is still there, empty. Open the first with
   `AKASHIC_DATA_DIR=<folder>` and `AKASHIC_CAPTURE=0`: the live passphrase fails and the new one
   unlocks. There is no `crashed` gap and no Ask history. Search and Ask find only that day. The
   live store is unchanged.
