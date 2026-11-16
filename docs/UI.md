# UI direction

The interface should feel calm and familiar, with Claude as a visual reference: warm neutral
surfaces, restrained accent colour, readable typography, generous space and minimal chrome.
Use the existing visual language; exact tokens, dimensions, assets and layout belong in code.
Serve every asset locally and keep the UI simple enough to maintain without a framework.
`src/akashic/ui/logo.png` and `favicon.png` are derived from `packaging/logo-source.webp` (alpha
channel thickened with Pillow `MaxFilter(13)`, resized to 256 and 64 px).

## Interaction principles

Ask is the primary entry point. Answers should connect directly to viewable or playable moments,
with follow-up questions in the same conversation. Keep the difference between retrieved sources,
cited support and uncertain attribution understandable.

Search, timeline and speaker correction help the user inspect and repair their memory. Expose
what the app understood and enough source context to assess an answer. Do not expose internal
implementation details unless they help the user make a meaningful choice.

Recording and processing are different states. Show blocked, degraded or stopped processing
honestly, and keep pause, lock and quit available. Model comparisons must not silently replace
memories or choose a winner from an incomplete score.

## While Akashic is open

The UI opens only from the tray or on the first run, never at autostart. Recording halts while a
page is visible: the page shows a waiting note and nothing recorded until the halt is confirmed,
and hiding the page conceals it again. The sidebar says "Not recording while Akashic is open", and
the microphone meter says levels are unavailable. Rules: [CAPTURE.md](CAPTURE.md).

## Timeline

- **Stage:** every monitor present at the chosen moment, at its real relative position, labelled
  "Monitor N · name" and scaled to fit. A shown monitor displays its latest frame with the age of
  its last check; any other state is drawn as that state, never as an old frame. The focused
  monitor is marked, hovering lists a frame's windows, and images are pre-sized so scrolling
  stays smooth.
- **Strip:** app bands, zoomable from a day down to 60 s; scroll, drag, keys and day navigation.
  The app filter highlights bands, it never hides monitors.
- **Moments:** each has its own address, so Back returns to it. A cited moment enlarges its
  monitor and outlines its window.
- Problems name their monitor, with the same "Monitor N".

Evidence images are cut from frames as described in [RETRIEVAL.md](RETRIEVAL.md).

## Deletion

Deletion must communicate scope, progress, failure and whether recording resumes afterward.
Follow [STORAGE.md](STORAGE.md) for the underlying contract. Current routes, tabs, controls and
responsive behaviour should be read from the UI and exercised directly rather than copied here.
