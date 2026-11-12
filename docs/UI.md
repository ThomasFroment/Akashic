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

Deletion must communicate scope, progress, failure and whether recording resumes afterward.
Follow [STORAGE.md](STORAGE.md) for the underlying contract. Current routes, tabs, controls and
responsive behaviour should be read from the UI and exercised directly rather than copied here.
