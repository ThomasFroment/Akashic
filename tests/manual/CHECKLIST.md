# Manual verification checklist

Automated tests cover core behavior and `scripts\selftest.ps1` covers the real audio devices,
Windows OCR, GPU, speaker models and Whisper. Use this checklist for behavior that needs a person,
real apps or a real day of data. Run against a throwaway `AKASHIC_DATA_DIR` where practical.

This lists checks to perform, not results that passed. Record scope/date with a result.
Images are selected-window renders; episode images require app/time/URL/title constraints.

## Unlock, tray and local UI

1. Create a store, confirm a wrong passphrase fails and cancelling exits without starting capture.
2. After unlock, confirm the app, capture and worker processes run and all stop on Quit.
3. Open the UI from the tray: it lands on Ask. Reusing its `/open?code=` URL returns 403, the
   session cookie is HttpOnly, and Akashic's own browser tab produces `excluded:own_ui`.
4. Pause and resume from both the tray and sidebar. Lock must stop both child processes, forget the
   key and show the unlock dialog again; unlocking starts fresh processes and a new session.
5. Toggle Settings > Startup and verify the `Akashic` HKCU Run value and a real sign-in start.

## Screen capture and Windows adapters

1. On every monitor, switch windows, type, scroll and change browser tabs. Search > Screen should
   show focus/input/title/idle frames, the correct monitor, title, process, URL and UIA/OCR text.
2. Verify `screen.py` reports every monitor in physical-pixel order; `windows.py` reports the
   focused window and the window under the pointer; Win+L produces only `locked` outcomes.
3. Verify `uia.py` reads Notepad, browsers and Discord without making Discord stutter. Firefox/Zen
   and Chromium URLs should work; a focused password field must produce `excluded:password_field`.
4. Verify `ocr.py` reads an image or game menu. Without the French language pack, the log should
   report the fallback to English.
5. Private windows, password managers, configured exclusions and finance sites must never create a
   frame. DRM video may keep metadata but must drop a black image. Logs must contain counts and
   exception types only, never titles, URLs or captured text.
6. Moving a high-rate gaming mouse should not raise capture CPU materially above idle.
7. Cover the selected app with another app: its render must contain only the selected window.
   Check Firefox/Zen, Discord, elevated windows, mixed-DPI monitors and games. Unsupported/hung
   rendering must skip without monitor fallback or accumulating threads. Navigate/switch/lock
   during capture: mismatched/excluded content must not be stored. Inspect multi-column text,
   repeated chat messages and navigation controls for useful reading order.

## Audio and transcription

1. In Settings, verify the real microphone/playback device lists and the live voice meter. Speak
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

## Media, sessions and gaming

1. Play, pause, resume, change and close media in a browser and a desktop player. Timeline/search
   should reflect titles, played time and a URL where available. Include concurrent windows/sessions
   and focused/background private playback; ownership and background-private playback have known
   limitations recorded in CAPTURE.md and audit #145, not a blanket exclusion pass.
2. Switch among normal windows and verify contiguous app sessions are built without excluded apps.
3. Test borderless and exclusive-fullscreen games. The tray should show game mode, about one frame
   per minute should be kept without live OCR, other monitors must not be walked, and deferred OCR
   should make game text searchable after the game closes. F11 video and maximized windows must not
   be mistaken for games.

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
4. For a synthetic model comparison, run the "Local Ask baseline" command in
   `docs/baselines/2026-11-11-m00.md` against a separate model/runtime folder, with `--repeats 3`,
   `--output` and `--compare` against `docs/baselines/2026-11-11-m00/ask-gemma.json`. Inspect
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
   `forever`; deleted media must not return. Verify the disk-space warning and one retention pass
   per day.
4. Debug > Overview and Logs must explain stalled jobs and capture outcomes without exposing
   captured content. Settings must report disk use by type.
