# Storage and retention

## Persistence contract

Captured and derived content stays encrypted at rest, including media, transcripts, voice
features, embeddings, references, questions and saved answers. The encrypted local database and
media files form one evidence store. [SECURITY.md](SECURITY.md) explains the trust boundaries.

Preserve original evidence and provenance through derived episodes and indexes. Publishing a
result must not resurrect a source changed or deleted while processing was in progress. The
schema lives in `storage/migrations/0001_schema.sql`. Until real data exists, schema
changes are edited into it in place; afterwards they become new numbered migrations. That file
and its tests define the actual format; do not maintain a second schema in docs.

## Retention choices

The user chose indefinite retention by default; never delete recorded history automatically
just to free space ([DECISIONS.md](DECISIONS.md)).
Retention uses capture time rather than processing time and is independently configurable by
evidence category.

Removing screenshots or raw audio alone is intended to preserve searchable text. Removing,
expiring or replacing a transcript must also remove its derived text from episode digests,
comparison results and affected saved answers/source excerpts, including later replies that
used it. User questions and unrelated evidence can remain. Unknown legacy provenance must not
be treated as permission to retain copied speech.

Screen text follows the same rule ([DECISIONS.md](DECISIONS.md), D3): removing or changing a
frame's text clears the saved answers that cited it and later replies in the same thread; user
questions and unrelated answers stay. Removing only a screenshot leaves answers untouched, but
cached thumbnails must not outlive the image. The `transcript_copies_*` and
`frame_text_copies_*` triggers and their tests are the authoritative description. Read the [audit findings](audits/2026-11-10.md) before changing any
category.

## Full-data deletion

Stop and join writers before clearing recorded data, including queued/open records, derived
indexes, saved answers, orphaned media and caches. A new table or cache must not silently survive
this boundary. Keep setup, keys, models/runtime, content-free logs and the stored paused choice (the
`capture_paused` state row); restore the prior
recording/paused choice only after the deletion attempt ends.

Report file or database failures rather than false success. Retry must remain possible, but
already removed files cannot be restored by database rollback. Do not follow directory links
outside the dedicated media store. New activity may be recorded after successful deletion if
the user was recording beforehand.

Deletion is not secure erasure of freed disk blocks. Validate lifecycle races, upgrades and
failure recovery with disposable encrypted stores; never use the user's history as a deletion
fixture ([TOOLING.md](TOOLING.md), "Testing on real data").

Comparison references can include manually supplied preceding context without a dependable
source ID. Replacing or deleting a transcript therefore invalidates draft-derived or context-bearing
comparison samples/results (independent samples with empty context survive unrelated expiry), including late decode publication. Legacy references retain unknown
model/language/condition provenance; old scores are not presented as new-method summaries.
