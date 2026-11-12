# Audit implementation handoff

2026-11-11. **Initial implementation statuses were UNVERIFIED; dated execution records below supersede only their stated scope.** This is an assignment map, not a certification of current code. It covers 185 findings (#1–#186; #59 unused), all nine method reports, conditional proposals, partial refutations and workspace observations. Tests, strict xfails and prototypes do not establish a production fix.

## Sources and execution contract

Read the [synthesis](2026-11-11-synthesis/synthesis.md), [code audit](2026-11-10.md) and [quality audit](2026-11-10-2.md). Their file/line evidence describes the pre-audit code (the second audit saw identical code). Use the [local baseline](../baselines/2026-11-11-m00.md) for comparisons, without treating its introductory completion claims as a task status. Original study timings often used a shared RTX 4090; target/local baseline is RTX 4080 Super. Ratios and synthetic contracts are evidence, not daily-use quality.

**Every batch** preserves: exclusions applied before anything touches disk; encryption of everything at rest; for a new table or cache, retention, full-delete completeness, stopped writers, late-publication and rollback protection; transcript deletion clears derived text; deletion is not secure erasure; no dependency is added without asking. Remove an xfail only when its intended defect is fixed.

Disposition: **R** recommended default implementation; **C** conditional on stated decision/spike/evidence; **X** rejected/excluded; **K** preserve recorded user decision. R is this handoff's recommendation, not a claim that the original audits approved/queued every fix. Listing a proposal never grants dependency, privacy narrowing or model-policy approval.

Priorities: **P0** privacy, integrity and silent liveness failures; **P1** evidence/target-question correctness; **P2** efficiency and maintainability; **P3** optional experiments. Measurement enables claims; urgent privacy repairs need not wait for unrelated evaluation work.

Superseded documents are not kept in this repository.

## Assignment map and dependency rules

Paths imply `src/akashic/`; locate moved files at preflight. Dependencies are accepted contracts, never assumed completion. Reserve migration numbers from the current tree; prototype 0019 is not reserved. Serialize shared-file/migration changes, including narrow integration patches.

| Assignment milestone (owned batches) | Priority | Required predecessor contracts | Exclusive primary responsibility |
| --- | --- | --- | --- |
| M00A Ask measurement (.1–.2) | P0 foundation | none | production Ask/scorers/splits and fixture provenance |
| M00B STT comparison (.3) | P0 foundation | current production processing boundary | comparison API/UI, condition reports and parity harness |
| M00C Regression quality (.4–.5) | P0 tests then P2 hygiene | meaningful tests before .5 cleanup | platform/backend regression fixtures and test hygiene |
| M01 Exclusions | P0 | none | capture privacy inputs/checks and approved narrowing |
| M02 Storage safety | P0 | none | crypto bounds, transactions, retention/delete, filenames/cache identity |
| M03A Terminal lifecycle (.1) | P0 | M02.1 transaction/delete boundary | app/supervisor/tray controls and pause persistence |
| M03B Worker recovery (.2–.4) | P0 | M02.1; M03A terminal interface for integration | queue/models, housekeeping and processing health |
| M04 Episode foundation | P0 .1 then P1 .2–.3 | M02.1/M03.3 for .1; own migration/horizon fixtures and identity oracles | source IDs, horizons, episode algorithm refactors |
| M05 Media | P1 | M01/M04; M06.3 refresh interface for final .3 integration | tracker/sessions, neutral identity, media builder |
| M06A Retention safety (.0) | P0 | M02.1; own cutoff/rebuild fixtures | episode cutoff on every rebuild |
| M06B Episode behavior/refresh (.1–.3) | P1 | M04, M05.1/.2 identity/playing-span and M06A cutoff contracts | run semantics, participants, SQL/Python digests and refresh API |
| M07A Capture recovery (.1–.2) | P0 | necessary M01 checks, M02.1, M03A/B lifecycle/health interfaces | bounded screen/audio recovery, short tails, durable close/flush |
| M07B Coverage (.3–.4) | P1 | M07A; M05 identity/playing-state contracts | gaps/endpoints/music, side windows and deferred OCR |
| M08 Transcription | P1 | M00B, M02.1, M03B, M07A; M06.3 refresh API for .1 | prompt/trim/confidence and final comparison parity |
| M09 Speakers | P1 | M00A attribution fixtures, M00B, M06B/M08 | profiles, Route A and deferred-author integration |
| M10 Planning | P1 | M00A scorer/splits, M05.1 identity | planner/time resolver/plan-call boundary |
| M11 Retrieval/answers | P1 | M06B/M07B/M08/M10 | hybrid ranking, excerpts, answer sources/UI |
| M12 Vectors | P2 | M00A/M02.1/M06B/M11; accepted M13A/B contracts for integration | vector/reuse schema and final read/discovery revalidation |
| M13A API operations (.1–.2) | P0 .1 then P2 .2 | M02.1/M03 health interface; M02.2 thumbnail identity for .2 | polling/read locks, media/reports and FTS churn |
| M13B Discovery/scheduling (.3) | P0 #34 then P2 | M02.1/M03B failed-ID/model contracts | incremental embed discovery and scheduling |
| M14A Structure (.1/.2/.4) | P2 | affected M00–M13 contracts integrated | import/config/persistence sources, removals/docs |
| M14B Shell repairs (.3) | P1 reliability | M03A/M02.1 lifecycle/persistence | downloads, mounted views, Ask polling and Debug values |
| M15A Package (.1) | P3/C | accepted native inference/bundle contract; M08/M09 if their stack changes | package/lock/bundle compatibility |
| M15B KDF (.2) | P3/C | M02.4 bounds and existing-keyfile compatibility | compatible KDF alternative only |
| M15C Production priority (.3) | P3/C | M03B lifecycle, M13B scheduling/peak evidence | measured worker/server production priority only |
| M16 Speaker extensions | P3/C | M09; M12 final thread/scan revalidation | optional word/span/model attribution |
| M17 Ask extensions | P3/C | M00A/M10/M11; M12 when ranking schema changes | model assistance, reranker and quotes |
| M18 Audio/STT extensions | P3/C | relevant M00B/M07/M08/M09 contracts | process-loopback spike/challenger study |
| M19 Workspace disposition | P3/C | M14A docs policy; M15A where relevant | workspace observations and unnumbered leads |
| M21 Doc-review follow-ups (.1–.2) | P1 .1, P2 .2 | M02.1 encryption/deletion boundary | encrypted export for testing; storage-warning removal |
| M20 Speaker comparison feature | P1 ongoing; not a release gate | M00.2 label/metric contract, M02.1 encrypted deletion boundary, current M09 prediction/score interface | user-labelled local speaker comparison, refined alongside everyday use |

The family sections below retain their original batch IDs. Each assignment's objective, boundary and definition of done are stated below the family objective; the map supplies its priority and dependencies. One owner delivers every owned batch plus its integration evidence. An implementer may take several milestones sequentially; no family is a parked assignment spanning unrelated work.

Serialize migrations and shared files, including `episodes.py`, capture loops and worker/API state. M03A publishes terminal/pause/writer-stop behavior; M03B consumes it and publishes bounded queue/health state. M07A coordinates media-close retry with M05 without waiting for refined URL/short-video rules. The episode contract sequence is M04→M05.1/.2→M06B refresh interface→final M05.3 integration; M06B does not wait for M05.3 completion. Final integration evidence is attached to both owners’ logs without a reciprocal completion gate; give M06A retention safety and M07A recovery the next safe shared-file slot before optional speed refinements. M13A/B begin on their actual contracts without waiting for M12; M12 later owns final-schema integration/revalidation of those accepted contracts, not their original fixes. M10 can proceed on its scorer/identity contracts without waiting for speech work.

Contract delivery is distinct from final integration: M00B first tests the current production boundary; M08 later reruns parity on its final changed pipeline. M06.3 delivers and tests a versioned/idempotent refresh API with fake consumers before M05.3/M08/M09 exercise it end to end; M05.3 metadata publication need not wait for final speaker validation. M03B delivers health fields before M13A optimizes their queries. These are forward contracts, not circular completion gates. No P0 repair waits for unrelated M00 panel/test cleanup or final vector optimization.

## M00 — Trustworthy measurement

Objective: measure the production path and distinguish planning/retrieval/speech/support failures. P0 foundation; assignment dependencies are in the map. Scope excludes production defect fixes/model selection. **Family outcome:** five batches validated, splits/provenance reproducible and limitations recorded. Sources: [evaluation](2026-11-10-2.md#evaluation-methodology), [tests](2026-11-10.md#tests), [synthesis measurement](2026-11-11-synthesis/synthesis.md#76-hot-spot-5-measurement), [eval material](../../eval/README.md).

**Assignment contracts (priority/dependencies in the map):** M00A (.1–.2) makes production Ask/scoring/splits reproducible; excludes STT panel and broad test cleanup; done when negative controls/provenance/split-language-repeat reports satisfy both batches. M00B (.3) makes STT comparison unbiased; excludes model selection; done when UI/API denominators, condition reports and shared parity harness pass. M00C (.4–.5) makes regressions sensitive to broken behavior before hygiene; excludes production fixes; done when negative controls fail appropriately, resources close and native limits are recorded.

- [x] **M00.1 Production Ask and scorers** — R; #113–#116
  - **Scope:** Legacy vectors/unrealistic fixtures and permissive scoring give false passes. Index frames/segments via production EmbedHandler, selected-window scope and semantic_query; exercise real builders at their boundary. Strip citations before boundary-aware facts; reject gold alternatives matching distractors, avoid bare digits without units. Require anchored refusal/no cited in-window episode and forbidden plausible fabrications; report citation precision/recall/false citations and include planning correctness in acceptance. First identify current interfaces.
  - **Accept:** Fabricated Netflix/refusal answers, citation-digit facts and every-video answers fail; two target questions in one thread, 00:40/09:00 and ≥8-video windows exercise production.
  - **Validate:** Validate scorer negative controls and disposable local-model run.
  - **Limits:** Automated matching/valid citations still do not prove entailment.
- [ ] **M00.2 Splits, fixtures and provenance** — R; #118, #123–#127, #99
  - **Scope:** Separate development/held-out/fresh definitions and frozen candidate hashes; report FR/EN/mixed, repeats, per-question JSON and one local baseline. Add wrong/NULL/mixed speakers, D9 unclear-evidence cases (flagged-only, clear support, independent corroboration, conflicts, duplicate transcript copies and uncertain authorship) and capture-row→builder→search delay. Consolidate disposable store/server setup (#99). Reuse 445 follow-up pairs, 577 time questions, 1,008 speaker segments/500 drift segments and hallucination builders, retaining licences/hashes and live-versus-cache distinction.
  - **Depends:** Coordinate content-free unbuilt-session/oldest/horizon counters with M03.4 (M00 owns fixture/assertions, M03 production counter).
  - **Decisions:** D6 allows provisional everyday use before this comparison and assigns its ongoing local feature/workflow to M20. Compare reference and candidate on the same independently labelled encrypted clips, reporting correct/wrong/unknown authors separately. M00A owns metrics/fixtures, M20 owns the user-facing comparison; recordings are selected explicitly by the user for comparison.
  - **Accept:** Scores cannot merge splits/languages silently and count/uncertainty/failure cases are visible. Under D9, flagged-only evidence produces a separate possible match with a recording link and an explicit inability to answer reliably, not a hedged factual claim. Score hallucination detection, rejection and unsupported answer claims separately; a flag alone is not successful answer protection.
  - **Validate:** Test that repeated copies cannot masquerade as independent corroboration and a clear transcript cannot establish an uncertain speaker identity.
  - **Limits:** Reused study fresh sets become regression sets; use newly independent labels for generalization claims. Local turn corrections require user-labelled local material, not test access to history.
- [ ] **M00.3 Unbiased STT comparison** — R; #117, #119–#122
  - **Scope:** Start reference empty or show draft read-only separately; record draft model/provenance and metrics with/without draft-derived clips. Require language/condition tags and per-condition scores, clip counts, failures and disclosed denominators alongside aggregate/per-language results; include empty segments/references. Apply production prompt/trim/gate using fixed previous reference; normalize FR/EN numbers/hyphens symmetrically and add CER/meaning-changing terms. Deliver a shared comparison-versus-production parity harness with the fixed preceding reference.
  - **Depends:** M08 owns a final parity rerun after its last prompt/trim/language/gate change; initial M00B delivery does not wait for M08.
  - **Decisions:** Decide failure denominator: full-deletion scoring, or explicitly common-completed subset alongside failures. Manual context has no dependable source IDs, so transcript replacement or deletion invalidates draft-derived or context-bearing comparison samples and their results; see [M00 evidence](../baselines/2026-11-11-m00.md).
  - **Accept:** Failed decodes, missed speech, no-speech insertions, names/negations and 12/douze affect the right metric.
  - **Validate:** Validate API/UI and processing with fakes plus independently corrected samples.
  - **Limits:** Pooled read-speech WER cannot choose a production model.
- [ ] **M00.4 Behavior-sensitive tests** — R; #75–#83, #85–#88
  - **Scope:** Check actual default speech model pin; fake torch/transformers dispatch/model-size guards/language/unload/log suppression. Assert runner alive before stop and work after resume, use predicate waits/fixed clocks, assert row exists before inequality. Test selected-HWND 1-second bound/single render thread/late discard/crop; unpaused encrypted-store wiring vs explicit pause; TextBlock fakes, controls-only OCR/text cap; shell state/Ask blocker/autostart/STT recordings route. Missing UI must fail network test and worker error test must assert pending. Tighten scheduler smoke, add French OCR/SAPI when installed and fix references.
  - **Accept:** Deliberately broken wiring/crashed loops/wrong models fail.
  - **Validate:** Hardware occlusion, COM timeout and FR OCR need native checks.
  - **Limits:** Unavailable packs/voices are missing evidence, not passes.
- [x] **M00.5 Fixture hygiene** — R; #84, #89
  - **Scope:** After meaningful tests exist, remove duplicate connection fixtures, yield/close owners, delete dataclass/__module__ trivia and stale test inventories.
  - **Accept:** Affected suite retains useful behavior checks and releases Windows files; test count alone is not validation.

## M01 — Exclusions before persistence

Objective: close privacy fail-open paths across monitors/browser metadata/audio. P0; no dependencies. Own privacy inputs/decisions, not source ranking. **Done:** transition/fault tests prove forbidden content never reaches DB/media writers, native checks bound remaining gaps. Sources: [capture bugs](2026-11-10.md#bugs-capture-and-platform), [capture quality](2026-11-10-2.md#capture), [capture guide](../CAPTURE.md).

- [ ] **M01.1 Password, URL and local-UI checks** — R; #16–#20, #27
  - **Scope:** Deduplicate HWND per tick and apply focused password checks on every monitor containing it. UIA error skips/counts password_unknown; no focused element is distinct. Publish browser URL exclusion state to media before writing; unreadable URL must conservatively preserve user exclusions, never read arbitrary in-page Edit as omnibox. Exclude entire Akashic API origin including media/reopen.
  - **Decisions:** Decide stale/unknown-state behavior conservatively first. User instruction: unknown or stale browser state is rejected. An unreadable address with user rules skips the frame. Rejected: letting a window title prove an address is readable.
  - **Accept:** Writer-spy tests leave zero prohibited rows/files/log text for spanning windows, COM failures, PWA input, excluded media titles/channels and local UI.
  - **Validate:** Validate focus/exclusion transitions on 3+ mixed-DPI monitors.
  - **Limits:** Title fallback cannot prove URL readability; accepted background-private-session gap remains narrow.
- [x] **M01.2 Process/domain decisions** — R; #24–#26, #28
  - **Scope:** Distinguish failed audio enumeration from no processes; discard excluded/unknown-policy spans in RAM rather than persist unclear. Resolve UWP CoreWindow PID and complete password-manager fallback patterns; retry AUMID misses with backoff rather than caching fallback permanently. Built-in finance matching uses parsed host suffixes; user globs stay globs. Include baseline's unnumbered .bnpparibas gap.
  - **Depends:** Depends on M01.1 state contract.
  - **Accept:** COM/source transitions/delayed names/finance subdomains and innocent booking.fr/camping.fr/search text have correct pre-write behavior.
  - **Validate:** Native hosted-app/endpoint tests; an uninstalled hosted app is an accepted point in the execution record, not a pass.
  - **Limits:** Process lists do not identify words.
- [ ] **M01.3 Approved narrowing** — R/K D2; #148
  - **Scope:** Method: private markers only browser suffixes, password-manager globs only non-browser processes, keeping videos named Bitwarden/Incognito.
  - **Decisions:** User approved 2026-11-11 with fail-closed unreadable-metadata safeguards. D2 settles permission to narrow; preserve exclusions on unknown/unreadable metadata and validate native ambiguous layouts before release. A single-word private marker counts only inside brackets, because Edge writes `[InPrivate]`. Rejected: reading private state from window titles alone.
  - **Accept:** Ordinary named video survives while actual private/password targets remain excluded before disk, including unreadable metadata.
  - **Validate:** Validate ambiguous layouts and negatives; historical overmatching is not approval to loosen privacy.

## M02 — Storage integrity and deletion

Objective: safe transactions/deletion, stable media identity and bounded unlock work. P0; no dependencies. Own storage/delete/cache safety; M03 stops writers and M06 governs rebuilds. **Done:** disposable encrypted upgrade, expiry, full/category delete, failure and rollback tests satisfy four batches. Sources: [storage findings](2026-11-10.md#bugs-search-and-storage), [#10](2026-11-10.md#architecture), [shell](2026-11-10.md#bugs-app-shell-api-ui), [storage](../STORAGE.md), [security](../SECURITY.md).

- [ ] **M02.1 Transaction and deletion recovery** — R; #10, #44, #49, #56, #73
  - **Scope:** sqlite_master completeness test accounts for every content/derived table and justified content-free keep-list, including virtual/shadow tables. Index continues_id; rollback only in_transaction, rollback/rethrow failed COMMIT without masking original error. Handle MediaPathError per reference so retention progresses without outside-store traversal. Map category-delete OSError/CryptoError to honest retry guidance as full-delete does.
  - **Decisions:** Decide bad-reference cleanup before clearing it.
  - **Accept:** Automatic rollback/failed commit/busy DB/bad paths/partial files/new table cannot create false success or block future work.
  - **Validate:** Benchmark FK-enabled deletion scaling with concurrent inserts.
  - **Limits:** No user history or erasure claim.
- [x] **M02.2 Derived screen copies and thumbnails** — R; #57, #62
  - **Scope:** Add frame provenance and invalidate copied screen text/sources/dependent answers on text removal/expiry, preserving unrelated content and screenshot-only retention. Key thumbnails by id/path/width and clear category caches.
  - **Depends:** Depends M02.1; choose conservative legacy provenance policy first.
  - **Decisions:** D3 records the user’s 2026-11-11 propagation decision; documenting retained copies was the audit alternative and is not selected. Thumbnail safety is unconditional; legacy provenance policy must conservatively implement propagation. Transcript deletion remains settled.
  - **Accept:** Edit/expiry/delete/rowid reuse/late publish cannot expose stale excerpts or pixels; transcript propagation survives.
  - **Validate:** Test upgrades with unknown provenance.
- [x] **M02.3 Collision-free names** — R; #52
  - **Scope:** Use UTC ts_ms identity, local date only for folders, with explicit same-ms collision policy and old-path compatibility.
  - **Accept:** DST fallback/rapid captures cannot overwrite, and deleting one leaves another intact.
  - **Validate:** Test encrypted frame/audio read/write/delete.
  - **Limits:** Conversational time study did not validate DST.
- [x] **M02.4 Keyfile bounds** — R; #55
  - **Scope:** Validate Argon2 memory/time/parallelism ceilings before derivation, preserving supported keyfile bytes/defaults/authentication.
  - **Decisions:** Choose compatible ceilings first.
  - **Accept:** Extreme/malformed parameters fail with KeyfileError without costly allocation, normal/boundary/old fixtures unlock.
  - **Limits:** Authentication tests alone cannot bound RAM/CPU.

## M03 — Bounded processing and lifecycle

Objective: no silent permanent queue stalls or recording after lock/quit. P0; M03A/B depend on the map’s transaction/terminal-state contracts. Own queue/runner/supervision/health UI; episode grouping stays M04–M06. **Done:** every state has a bounded or visibly blocked exit, fault tests show later work progresses and recording choice survives lifecycle. Sources: [pipeline](2026-11-10.md#bugs-pipeline), [shell](2026-11-10.md#bugs-app-shell-api-ui), [reliability](2026-11-10-2.md#reliability), [architecture](../ARCHITECTURE.md).

**Assignment contracts (priority/dependencies in the map):** M03A (.1) owns terminal shutdown/pause controls; excludes queue algorithms; done when restart/Lock/Quit/delete races preserve writer stop and recording choice. M03B (.2–.4) owns bounded queue/model/housekeeping recovery and health; excludes capture algorithms; done when poison/locked-DB/missing-model cases progress or visibly block and lifecycle integration cannot revive writers. Test their terminal/status interface and serialize shared shell/API edits.

- [ ] **M03.1 Terminal shutdown and controls** — R; #60, #67–#69, #72, #74, #163
  - **Scope:** Stop API acceptance before supervisor, terminal flag disables restart/while_stopped/poll resurrection. Preserve pause through lock/unlock/restart; tray actions cannot block its event thread. Guard monitor iterations/type-only threading hook; distinct safe ConfigError/key exit; Services.close under lock even after timeout. Add -I installed/autostart/child launch with self-contained import verification.
  - **Accept:** Queued settings restart racing Lock/Quit cannot record at unlock; pause persists, controls work under deletion/restart, invalid config exits visibly.
  - **Validate:** Fault/race plus native tray tests.
- [ ] **M03.2 Poison jobs and durable transitions** — R; #33, #154–#156
  - **Scope:** Exhaust crash attempts in release_running, invoke give_up/backoff. Skip/isolate failed embed IDs so new batch keys cannot requeue them forever; age failed records. Retry transcription with retryable/reset segment state, not false done. Retry complete/fail DB writes and release abandoned running rows without releasing active job.
  - **Depends:** Depends M02.1 retry/transaction contract; coordinate M03A terminal state without waiting for unrelated lifecycle UI work.
  - **Decisions:** Decide ownership/attempt budgets/idempotence.
  - **Accept:** Repeated worker kill/locked completion/manual retry yields bounded failure, actual retranscription, later runnable work and progressing horizon.
  - **Validate:** Test restart/give-up paths. claim already increments attempts historically.
- [ ] **M03.3 Models and server ownership** — R; #30, #35, #39–#41
  - **Scope:** Mic speaker jobs run without models; do not enqueue impossible system jobs, resolve existing pending jobs consistently with retention. Cache environment load failure as recoverable visible blocked state rather than burning all segment attempts. Gate unload on live model; catch HTTPException/terminate every failed server start; Ask uses 1–2 attempts without repeated failed start per run.
  - **Accept:** Missing models/CUDA do not freeze raw retention or permanently fail all audio; recovery works, no untracked VRAM server, Ask ends promptly.
  - **Validate:** Inject errors plus separate real local-inference smoke.
- [ ] **M03.4 Independent housekeeping/health** — R; #157, #159, #162; supports #124
  - **Scope:** Separate step guards and last-success timestamps. Status/tray/sidebar distinguish capture and worker states, blocked kinds, failed count, oldest transcribe/horizon age; add unbuilt-session counters for M00.2. Define heartbeat/degraded thresholds.
  - **Accept:** One builder failure leaves retention/embed enqueue alive, recording cannot mask stopped processing, failed capture cannot claim ready.
  - **Validate:** Validate UI and content-free bounded queries; M13 later optimizes.

## M04 — Stable episode foundations

Objective: unblock builders after deletion/backlog and refactor quadratic work before behavior changes. P0 #164/#31 then P1; M04.1 requires M02.1/M03.3 and its own migration/horizon fixtures. M04.2/.3 require identity oracles before replacement, not full M00 completion. Own source-ID migrations/pipeline episodes until M05. **Done:** upgrade/reuse tests and both oracles pass with explicit builder horizons. Sources: [episodes](2026-11-10-2.md#memory-episodes), [pipeline](2026-11-10.md#bugs-pipeline), [conversation][conversation], [app runs][app-run].

- [ ] **M04.1 IDs and horizons** — R; #164, #31
  - **Scope:** Rebuild media_sessions/game_sessions/audio_segments with AUTOINCREMENT preserving IDs/FKs/indexes/triggers/encrypted data. Ignored refs skip/log/advance instead of stalling. Games/app runs use now minus close gap independently of speech jobs; media contract is completed M05.3.
  - **Accept:** Deleting highest/all source rows then capturing builds games/videos/conversations; absent speakers cannot block non-speech.
  - **Validate:** Test upgrade/rollback/FK integrity/queued IDs and repeated collision recovery.
- [ ] **M04.2 Conversation sweep** — R; #102, #36
  - **Scope:** Select C4: sorted voice gap sweep, frozen voice-only bounds, strict 30-second mic overlap, nearest single owner/ties earlier, pass-local sources JSON memo. Promote oracle/fuzz/replays before replacing code, preserving finality/watermark/member order. Intentionally fixes chaining/double attachment.
  - **Accept:** Zero mismatches on 10k fuzz/27 edge/1k replay checks and target ≤50 ms compute/20k segments locally.
  - **Validate:** Test all-distinct sources, ordering assumptions and inherited exactly-at-horizon/5-minute boundary. Document any boundary policy change separately.
  - **Limits:** Prototype excludes SQL/final object assembly.
- [ ] **M04.3 App-run identity refactor** — R; #103
  - **Scope:** Select C1 two-phase index ranges/suffix-min game pointer, running ends/per-key totals/exact URL ties; no run-rule changes yet. Also include baseline's unnumbered earlier-longer-game query-bound regressions for app visits/conversations: SQL input completeness is outside the in-memory oracle.
  - **Accept:** Byte-identical emitted kwargs on 10,002 cases across seeds 0/1/2/random; preserve strict overlap and non-STRICT REAL/NaN duration fallback. Target ≤20 ms compute at 50k visits/500 games; historical fragmented 20.95 ms with recorder vs 17.8 without, DB writes excluded.
  - **Limits:** Hash-dependent ties survive only this proof.

## M05 — Faithful media evidence

Objective: correct process/URL/playing spans/soundtracks without stealing conversation speech. P1; depends M01/M04. Own tracker/sessions, neutral identity and media builder, including narrow planner aliases. M05.3 final refresh integration consumes M06.3 after M05.1/.2 have delivered identity/playing spans; do not require all M05 before starting M06B. **Done:** Chrome/Edge/pause/suspend/short-video cases yield bounded linked episodes and uncertain ownership stays explicit. Sources: [capture](2026-11-10-2.md#capture), [episodes](2026-11-10-2.md#memory-episodes), [identity](2026-11-10.md#architecture), [#90 decision](2026-11-10-2.md#open-questions).

- [ ] **M05.1 Identity/URLs** — R; #11, #54, #145, #165, #166
  - **Scope:** Neutral apps module maps GSMTC labels to normalized exact executables; lowercase before .exe stripping, keep display names in bodies and recognize Edge aliases. Resolve URL using mapped process/title prefix, exclude search pages and retry during 120-second association window.
  - **Depends:** M10 consumes this contract without redefining it.
  - **Accept:** Chrome/Edge filters and Edge audio match; EXCEL.EXE normalizes; unrelated same-title frames cannot donate URL.
  - **Validate:** Test delayed frames/multiple tabs; ambiguous correlation is not ownership proof.
- [ ] **M05.2 Playing/pause/suspend** — R/K; #90, #142, #146, #167
  - **Scope:** Do not bundle deletion of FTS/triggers under #90; M14.4 may independently assess them.
  - **Decisions:** Keep media_events/writer as user decided. Freeze paused end, close at paused_since, associate speech only through playing spans. Poll gaps close at last poll/game seen and reopen playing state. Windows lock/UAC preserve media/audio continuity while Akashic pause stops capture. Decide gap and legacy event reconstruction; do not invent historical intervals.
  - **Accept:** 23:40 pause/09:15 wake never becomes morning watching, UAC does not split playing video and paused audio is unattached.
  - **Validate:** Sequence/native suspend/lock tests; #142 is wrong bounds, not overnight audio.
- [ ] **M05.3 Conservative source claims/early availability** — R; #168, #171, #173; integrates #31
  - **Scope:** Unknown audio is not claimed if voice/game could explain it; specify any safe fallback. At media close publish metadata episode even with deferred STT/speakers; attach ready text and refresh via M06.3. Transcribe-only readiness controls body completeness, not existence of known metadata.
  - **Decisions:** Choose documented 10–15-second minimum or verified played-to-end clips. Provisional unlinked media hits are an alternate design only if this proves impractical, not a second concurrent path.
  - **Accept:** Hour-old video searchable during backlog, short clips count, Minlo remains conversation-eligible.
  - **Validate:** Test search/source links, including baseline's unnumbered missing soundtrack episode link.

## M06 — Episode behavior and refresh

Objective: corrections improve evidence and expiry never resurrects it. P0 M06A retention, then P1 M06B behavior/refresh; prerequisites are separated in the map. Own episode retention/rules/participants/digest view/refresh plus merge integration. **Done:** retry/correction/delete update proper sources and SQL/Python digests agree. Sources: [episodes](2026-11-10-2.md#memory-episodes), [digest](2026-11-10-2.md#answer-accuracy), [merge](2026-11-10.md#bugs-search-and-storage), [decision](2026-11-10.md#open-questions), [app-run][app-run], [conversation][conversation].

**Assignment contracts (priority/dependencies in the map):** M06A (.0) owns P0 cutoff enforcement only; excludes run semantics; done when disposable rebuild/expiry/restart fixtures prove no resurrection and the cutoff contract is delivered. M06B (.1–.3) owns run/people/digest/refresh behavior; excludes source-ID migration and capture tracking; done when parity/late-evidence/idempotence/source-link tests pass while revalidating the cutoff. The episode file has one active editing owner at a time.

- [ ] **M06.0 Retention safety** — R; #32
  - **Scope:** Enforce episode cutoff on every builder/rebuild so longer-lived raw sessions cannot recreate expired episodes/transcript copies/embed jobs.
  - **Depends:** M06A starts after M02.1 with its own disposable fixtures, before unrelated M04 speed refactors/M05 refinements when shared-file serialization permits.
  - **Decisions:** Select the source’s cutoff alternative rather than an irreversible watermark obstructing #169 rewind.
  - **Accept:** Shorter episode than source retention, partial/full expiry, restart/retry and repeated housekeeping cannot resurrect; M06.3 rewind/retranscription must revalidate this contract.
  - **Validate:** Test disposable encrypted upgrade/rebuilds and late-publication boundaries.
  - **Limits:** Historical reasoning is not completion evidence.
- [ ] **M06.1 Run semantics** — R; #170, #172
  - **Scope:** After M04 identity proof and M05.1/.2 source contracts, chain visits on gap only, choose kind by dominant duration and first-seen ties; prefer overlapping game’s process for in-game conversation.
  - **Accept:** Alternating editor/docs produces coherent runs and restarts are deterministic.
  - **Validate:** Rerun C1/C4 with explicit changed expectations and M06.0 cutoff safety.
- [ ] **M06.2 People and labelled digests** — R/K; #3, #46, #174, #178
  - **Scope:** Upsert/sum episode_people into merge target before deletion, recompute on reassignment. Label You/name/someone in the digest. User, 2026-11-12: one digest builder, in Python; remove the SQL rebuild view (supersedes "keep both/equality tests"). Preserve utterance uncertainty and source provenance through the builder and rebuilds so M11 can enforce D9 without treating copied flagged words as clear evidence; test mixed clear/unclear digests. Preserve episode's own headline/channel when sessions expire.
  - **Accept:** Minlo remains person-searchable, speech_ms/corrections right, partial/full transcript deletion rebuilds identical labels/whitespace and keeps channel without restoring removed speech. Use production builders in Ask fixtures; participation does not prove each line's author.
- [ ] **M06.3 Late evidence/deferred authors** — R; #169; integrates #133/#173
  - **Scope:** Recompute participants on turn changes; revisit conversation range when older transcription becomes done, refresh media bodies/links/vectors. Support deferred author after speaker job completes via this API, not pending-job horizon alone. Define source versions/idempotence/invalidation before consumers.
  - **Accept:** Retry/retranscribe/correction/seam update existing episodes with no duplicate refs, missing future conversations or deleted/expired resurrection.
  - **Validate:** Rerun C4 multi-pass including exact horizon boundary and rollback/restart.

## M07 — Capture continuity and coverage

Objective: preserve short/side-window evidence, isolate failures and expose coverage gaps. P0 M07A hangs/drops, P1 M07B coverage; core recovery has no M05 refinement gate. Own capture/adapters/gaps/visible-session inputs, not STT tuning. **Done:** faults recover or visibly degrade and real multi-monitor/headset checks bound claims. Sources: [platform](2026-11-10.md#bugs-capture-and-platform), [capture](2026-11-10-2.md#capture), [reliability](2026-11-10-2.md#reliability), [speech](2026-11-10-2.md#speech), [guide](../CAPTURE.md).

**Assignment contracts (priority/dependencies in the map):** M07A (.1–.2) preserves bounded capture/fresh audio through faults, stop and device change; excludes refined association/new coverage features; done when native/fault/write-race cases pass with safe degradation and no private late writes. M07B (.3–.4) improves coverage/endpoints/side-window/OCR evidence; excludes STT tuning; done when transition/backfill/storage/native checks pass with podcast/sampling/ownership residuals explicit. Hand shared adapters/loops over sequentially.

- [ ] **M07.1 Bounded capture/accessibility** — R; #15, #21–#23, #29, #158
  - **Scope:** Clear/ignore due game/no-monitor triggers, filter visible UIA/control types before cap, set short IUIAutomation2 timeouts. Guard screen/audio iterations/type-only errors, release/retry resources. Isolate hung HWND; safe text-only rows survive missing/oversized images, downscale large windows and escalate degraded/restart after stated interval. Preserve bounded single render thread/late discard, no replacement-thread storm or monitor fallback.
  - **Accept:** Hung PrintWindow cannot erase all monitors' text, UIA/ONNX/device error cannot silently kill capture, idle game/no-monitor CPU bounded.
  - **Validate:** Blocking fakes plus real Windows hang/occlusion/large-window checks.
  - **Limits:** Proposed platform recovery remains unverified until native spike.
- [ ] **M07.2 Audio tail/write durability** — R; #131, #160, #161; C cut part of #137
  - **Scope:** Keep seeded continuation with fresh speech while retaining quiet gate. Flush open audio on stop/device change, discard on pause; stop flush must join before delete clears store. Retry busy encrypted writes with bounded backoff, pop media state only after successful close; no plaintext spool.
  - **Decisions:** Lowest-VAD cut in last ~3 seconds (part of #137) is conditional on a waveform/VAD spike proving overlap continuity, not a prerequisite to the safe tail fix.
  - **Accept:** 320-ms `pas` survives cut, stop/device change preserves fresh utterance, pause records nothing new; DB lock/interrupted publication leaves no orphan files/open rows.
  - **Validate:** Cut changes need M08 gate/seam revalidation.
- [ ] **M07.3 Gaps/endpoints/music** — R/K D7; #143, #151, #152
  - **Scope:** Encrypted content-free gaps on pause/lock modality limits/device/gating/OCR/process downtime transitions; safe supervisor reconstruction for crashes, OCR backfill queue. Attribute only recorded endpoint sources and gap voice apps audible elsewhere. Preserve exclusions before disk and existing quiet/VAD protection; no new speech detector is authorized or assumed.
  - **Depends:** M02 covers new tables/temp deletion; M08 validates downstream non-speech/meaning and M11 consumes gaps.
  - **Decisions:** D7 (user, 2026-11-11) selects preservation/transcription of uncertain music-app speech candidates: do not drop them solely because sampled sources are music apps. Define sampling/gap closing first. D7 supersedes the audit's process-only dropping policy; its endpoint/exclusion observation contracts remain.
  - **Accept:** Routed-away headset speech/crashes show honest coverage, and podcasts/brief speech over Spotify reaching the segmenter are queued for transcription even with only music-app sources.
  - **Validate:** Measure extra queued work/storage with bounded memory; test sampled/unsampled mixed speech and residual upstream VAD/capture losses separately. Native multi-endpoint/speech/music tests required.
  - **Limits:** The audit's enough-samples/all-samples/≥4-Hz proposal improves observation but cannot prove a Spotify podcast is music. Do not equate queueing with correct recognition.
- [ ] **M07.4 Side windows and deferred OCR** — R with native measured choices; #144, #147, #149, #150, #153
  - **Scope:** Capture cursor HWND for unfocused scroll even on focused monitor, dedup focused target. Idle capture other monitors during games without UIA/deferred OCR; separate visible sessions from focused use. Add players/RDP to game exclusions and spike D3D fullscreen evidence before relying on classifier. Retain encrypted full-res image/crop until OCR with bounded expiry/failure cleanup.
  - **Accept:** Off-center Discord/side stream/no-GSMTC/small game names searchable, fullscreen player/RDP not game solely by omission.
  - **Validate:** Test 3+ mixed-DPI/negative coordinates, responsiveness/storage cost.
  - **Limits:** Visible presence does not prove watching; no desktop fallback/every-frame vision.

## M08 — Transcript continuity and confidence

Objective: retain meaning across cuts and keep non-speech out of confident evidence. P1; depends on M00B/M02.1/M03B/M07A and M06.3 refresh API, not all coverage refinements. Own transcribe/backend/prompt/trim metadata. **Done:** continuity passes and final decode configuration re-evaluated with subset/residual limits. Sources: [speech](2026-11-10-2.md#speech), [Qwen](2026-11-10.md#bugs-pipeline), [hallucination][hallucination], [speakers][speaker], [guide](../SPEECH.md).

- [ ] **M08.1 Overlap truth** — R; #130, #137, #133 metadata
  - **Scope:** Read continues_id predecessor despite overlapping end; use bounded previous prompt. Trim tolerant of half-cut trailing predecessor words without fuzzy name/meaning rewrite. Return/persist actual trim success and overlap length; trimmed cannot assert words removed when no match.
  - **Depends:** Needs M07.2/M06.3.
  - **Accept:** Half-word/negation/no-match/repeated-word/empty predecessor cases preserve fresh content once or explicitly flag unresolved overlap.
  - **Validate:** Test transcribe→speaker metadata/corrections; cut relocation is separate C spike M07.2.
- [ ] **M08.2 Hallucination C7** — R; #128, #129, #138, #141
  - **Scope:** Five-feature standardized logistic uses no_speech_prob, avg_logprob, compression, log words, duration, with stats and keep/unclear/reject outcomes persisted and propagated to derived evidence for D9. Use exact coefficients/scales from `eval/transcript-hallucination-rejection/C5_decode.py`, not rounded prose; starting reject ≥.996/unclear ≥.105. Keep decode -2.0 and post-decode judgment (supersedes audit restore -1.0 suggestion). Preserve empty rejection. Report rejection and flagging separately, without calling 92.1% outright removal.
  - **Decisions:** D5 takes precedence over blanket whole-credit or edge-credit rejection: retain plausibly spoken credits/outros as unclear even when they occupy the whole segment; uncertain speech must not be deleted by phrase matching alone. D5 (2026-11-11) counts unclear separately as handled and keeps plausibly spoken outros flagged rather than broadly deleting them; D9 governs their use in answers instead of hedging alone.
  - **Accept:** Recall reject+unclear ≥.90, FRR no worse/≤1% overall, no new previously-kept real rejections, report reject-only and real unclear separately.
  - **Validate:** Separately test prompt echoes, genuine spoken whole/edge outros and hallucinated credits; re-evaluate the final gate after this policy change. Validate multi-window/FR/EN/mixed short speech and D9 answer-evidence isolation; reused caches are regression evidence. Until a backend is calibrated, treat None confidence from Qwen/Canary as unclear (#141); exp(score) is not automatically calibrated.
  - **Limits:** Qwen/Canary available scores need backend calibration. Historical .921 includes unclear, reject-only .375; .86% FRR upper 95% bound 1.58% and subset failures remain. Read speech/Opus/noisy labels/possible leakage/single-window and n=1 edge-credit case limit evidence.
- [ ] **M08.3 Names/languages** — R; #43, #139, #140
  - **Scope:** Add named non-self voices at job time within cap; compare same-stream language or dual FR/EN decode when unsure/<3 seconds; constrain Qwen or compare both while preserving mixed speech.
  - **Decisions:** Quality first, bounded memory; no smaller/faster model selected for throughput.
  - **Accept:** Minlo/ouais carrément/mixed negation/game vocabulary improves or retains meaning without silent rewriting.
  - **Validate:** Re-run/refit gate after EACH prompt/language/decode change then combined final pipeline, including M00.3 comparison-versus-production parity using the fixed preceding reference and final prompt/trim/language/gate configuration (#121).

## M09 — Conservative speaker identity

Objective: better authorship without confidently wrong quotes/drift. P1; depends M00A attribution fixtures/M00B/M06B/M08. Own speakers/profiles, calling M06 refresh contract. **Done:** Route A measured end-to-end, deferred authors refresh and residual original goals explicitly remain open. Sources: [speech](2026-11-10-2.md#speech), [report][speaker], [chain interactions](2026-11-11-synthesis/synthesis.md#73-hot-spot-2-the-transcription--speakers-chain).

- [ ] **M09.1 Feature parity/profile hygiene** — R with verification gate; #134–#136
  - **Scope:** Check shipped WeSpeaker reference before changing povey/hamming: #136 suspicion unverified. Add feature parity and enrollment compatibility tests. Store match scores, gate updates by duration-aware threshold+runner-up margin; create new profiles only with audible voice/game processes, still match existing profiles generally.
  - **Accept:** Media does not flood profiles, marginal impostors cannot update named centroid, encrypted scores retained.
  - **Validate:** Similar voices/500-segment stream checks; historical zero drift for both baseline/candidates is weak real-call evidence.
- [ ] **M09.2 Route A** — R interim below original target; #132, #133, #134
  - **Scope:** Select C2 duration-bucket LLR fusion/margin/veto + C9 exact-copy seam reconciliation. Ignore lead authorship only if actual trimmed metadata; defer 30-second-cut author until continuation, verify waveform correlation/seam-centered window, refresh M06.3. Missing continuation stays explicitly unknown. Update outdated module docstring.
  - **Decisions:** Two-fold fitted/scored same read-speech set had no separate holdout; D6 (user, 2026-11-11) allows provisional everyday use before the local comparison; M20 owns a reusable comparison feature refined alongside everyday use.
  - **Accept:** Interim accept ≥.90 solo authorship/≤3% wrong on fixture/no reference regression with per-pair/worst-segment CPU.
  - **Validate:** Test sub-second second voices/tails/seams, no ≥300-ms absorption. Validate C7 even-spread windows (without model edit) first: they may fix C2's 3.6× tail cost but the isolated combination is unmeasured.
  - **Limits:** Original ≥.95/≤1%/≤2× remains UNMET, not complete. Public-fixture integration proceeds without history access; provisional use does not establish real-call accuracy or meet the original targets.

## M10 — Standalone planning and time

Objective: independent new questions and correct selective follow-up/FR-EN time intent. P1; depends M00A scorer/splits and M05.1 identity. Own planner/time/plan-call boundary only. **Done:** deterministic defaults, explicit conventions and final semantic_query validated. Sources: [answers](2026-11-10-2.md#answer-accuracy), [time/names](2026-11-10.md#bugs-search-and-storage), [follow-up][follow-up], [time][time].

- [ ] **M10.1 Standalone + follow-up C1** — R; #175
  - **Scope:** Plan new question without earlier context; retain previous standalone plan. Typed cues/per-field carry with stated-field overrides/topic-kind drops; embed final semantic_query, not previous+new.
  - **Decisions:** Apply D4: explicit continuation carries the previous topic/omitted filters, an explicitly stated weekday-as-topic may carry, and a topic follow-up after “What did I do on Tuesday?” retains Tuesday. Keep source-conflicting labels traceable when updating expected results; these user choices do not authorize unrelated standalone inheritance.
  - **Accept:** Both target thread orders match standalone, ellipsis carries only missing fields, p99 ≤5 ms/no target-pair errors.
  - **Validate:** Evaluate all 445 pairs by split/field/language; .903 weakest-field regression is improvement, not unmet .95 goal.
  - **Limits:** Agent labels/lexicons and convention disagreement limit claims.
- [ ] **M10.2 Time C7 and duration fix** — R/K D1; #45, #179–#181
  - **Scope:** Grammar/verified variants/folding/asking-clause anchor plus RAM-only captured-title veto, refreshed cheaply per process via data_version; deletion/exclusion removes stale titles. Compact relative h/min precedes clock and noun durations guarded separately: study excluded #45. Use actual enumerated cases/labels: the report's '198×5=1,070' is inconsistent.
  - **Decisions:** D1 selects the study’s 05:00 logical-day convention (2026-11-11), including pre-05:00 logical yesterday and its past-preference/clipping rules; document 36 changed expectations. Update provisional expectations to D1 with a dated rationale; preserve the separate relative-duration fix and RAM-only title decision.
  - **Accept:** Aprèm/midi/minuit/earlier/weekends/paired anchors/titles/relative durations in FR/EN, zero future windows, rule p99 ≤1 ms. Report .884 no titles/.894 public/.926 simulated list separately, not .95 or real guarantee.
  - **Validate:** Add local timezone/DST boundary regressions.
  - **Limits:** Timezone/DST is outside the study; do not attribute validation to it.
- [ ] **M10.3 Topics and cues** — R; #50, #182–#184
  - **Scope:** Strip quoted-name key once; bilingual model keywords and ≥4-character FTS prefixes, expanded stopwords removing time/name/kind cues. Tighten regarding/partie-du-doc, add maté, allow lowercase rule-name veto (Max settings) while preserving explicit anchors.
  - **Depends:** Needs prior planning contracts.
  - **Accept:** Accents/shared terms retain matches, game finds jeu/partie, incidental cue words don't dominate titles, quoted names preserve fallback.
  - **Validate:** Test model/no-model paths/per-language retrieval; prefixes not full French stemming.

## M11 — Complete evidence and supported answers

Objective: eligible lines, complete/disclosed lists and links to actual moments. P1; depends M06B/M07B gaps/M08 confidence/M10. Own hybrid/excerpts/answers/source UI; no vector redesign. **Done:** target thread/backlog/count/unclear cases pass with honest source/coverage behavior. Sources: [answers](2026-11-10-2.md#answer-accuracy), [line limit](2026-11-10.md#bugs-search-and-storage), [time C5][time], [retrieval](../RETRIEVAL.md), [UI](../UI.md).

- [ ] **M11.1 Overlap retrieval** — R; #179–#181 retrieval
  - **Scope:** Select C5: strict overlap lines, extend crossing voice runs gap ≤15 minutes/cap 3 hours each side, no music/video chain. Prototype lower bound W.start−30,016 ms requires proven duration bound; verify/remove shortcut.
  - **Depends:** Share overlap contract with M12.
  - **Accept:** Cross-edge lines survive, exact-touch excluded, extension/caps do not silently relax constraints.
  - **Validate:** Compare gold-window recall/outside lines; historical 1.000 vs .779 from 14 synthetic questions/12 outside lines each is directional, not precision proof.
- [ ] **M11.2 Lists/fallback** — R; #53, #176, #177
  - **Scope:** Drop empty/expired lines BEFORE limit; when people/time/kinds are set preserve ranked hits and fill remaining slots with remaining eligible segments newest-first up to the limit, including partially filled rankings. No-topic/no-summary lists chronological including unembedded episodes, count before truncation and pass N of M/MAX_OVERVIEW_EPISODES. #176 is missing lines from the returned episode, not necessarily an empty answer.
  - **Decisions:** Preserve transcript uncertainty and provenance through ranking, fallback lines, excerpts and digest-derived evidence: D9 keeps flagged text searchable as a lead without promoting it to factual answer support.
  - **Accept:** ≥8 videos deterministic complete or explicitly partial, newest retained, cross-language/unembedded game remark beyond digest[:400] accessible.
  - **Validate:** Test a partially filled ranking (an unrelated ranked hit plus a relevant unembedded eligible line), wrong day/kind/person/expired/duplicates/counts after extension.
- [ ] **M11.3 Moments/grounding/coverage** — R; #185, #186; integrates #125/#143
  - **Scope:** Source stores episode bounds and excerpt segment/time. Increase token allowance deliberately with resource/context measurement; propagate finish_reason/truncation and unsupported state without valid citations. Show overlapping capture gaps separately from no evidence.
  - **Depends:** M17.3 optional stronger quotes.
  - **Decisions:** D9 (user, 2026-11-11): flagged/unclear text remains searchable but cannot alone support a factual claim, including a hedged claim. Keep uncertain leads separate from the factual evidence supplied to answer generation, with the same separation for episode digests, excerpts and saved sources. Clear supporting evidence permits a normal cited answer; a genuine user correction or independent clear source may corroborate a claim, cited to that support. Copies/repeated sightings of the same transcript are not independent corroboration. Show flagged matches separately as possible matches with links to the actual recording; when only flagged evidence exists, explain that a possible match was found but transcription is too uncertain to answer reliably. Disclose conflicts between clear and flagged evidence rather than silently choosing one. Check speaker identity separately: clear words do not establish who said them, and uncertain attribution must not become a named-person quotation.
  - **Accept:** 10:40 remark in 08:00 call opens 10:40, truncation visible, unsupported never saved grounded, outage does not imply nothing happened; flagged-only, clear-supported, corroborated, conflicting, duplicated and uncertain-speaker fixtures obey D9 across generation, persistence and rendering. Possible-match entries are not saved or counted as grounded factual claims; follow-up context must preserve the distinction.
  - **Validate:** Validate API/persistence/render/navigation/repeated Ask; citation existence not entailment.

## M12 — Exact vectors and text reuse

Objective: fewer duplicate embeds/row operations without losing evidence/deletion provenance. P2; depends M00A/M02.1/M06B/M11 and accepted M13A/B contracts. Own vector SQL/scoring/schema/embed publication. **Done:** each ordered step independently passes identity/delete/query-plan checks on final schema, with numerical exceptions explicit; M12 revalidates accepted M13A reader/status and M13B discovery/scheduling contracts after schema/publication changes without duplicating their ownership. Sources: [approaches](2026-11-10.md#approaches), [legacy decision](2026-11-10.md#open-questions), [scan][scan], [reuse][reuse], [window scan][window-scan].

- [ ] **M12.1 Legacy retirement then chunk scorer** — R/K; #47, #100
  - **Scope:** Honor drop of legacy whole-segment vectors/covered set/second path; older segments temporarily lose semantic retrieval until passage backfill, not keyword evidence. Make backfill/resume/delete visible. Then C3 columnar chunks/run-level selection plus C1 DAZ/FTZ self-test/cast fallback. Preserve 4,000-row matrix shape/order/masks, owner max/offsets/ties, vector-size guards, bounded ID table and unusual/nonfinite fallback.
  - **Accept:** Identical owners/order/offsets/float32 outside separately reported NaN scope, report NaN-scope drops separately: historical stable-oracle agreement was 15/16 at 100k and 13/13 at 1M; the remaining cross-chunk tie retained reference position-dependent float32 rounding.
  - **Validate:** Test 100k/1M, interleaved/huge IDs/chunk ties/subnormals/±0/inf/query variants. Target ≤500 ns/row after-fetch Python and ≤64 MB extra; separately profile SQLCipher/cursor floor (the report records 1.4–3.0 µs/row SQLCipher decode across runs, which dominates). Revalidate actual BLAS/thread/kernel; synthetic identity not promise across kernels.
  - **Limits:** Preserve/report that exception rather than promise universal stable-oracle identity.
- [ ] **M12.2 HMAC link-table reuse** — R; #101
  - **Scope:** C3: dedicated data-key subkey HMAC of exact text; passage set per key/model, frame/model links. Capture write path unchanged. Re-read source/version at atomic publish, handle concurrent expiry; FK cascades/last-link/text-change cleanup, never REPLACE links. Preserve per-frame embedding retention; group by key/title/URL before RRF slots, actual in-window representative/time and counted sightings.
  - **Depends:** Needs M02/M12.1. Allocate current migration: SQL-only runner cannot derive HMAC; design atomic keyed upgrade hook or resumable worker migration BEFORE dropping old passages.
  - **Accept:** Every source findable, distinct videos separate, zero time/window errors/orphans/unkeyed hashes/plaintext leaks, all deletion/expiry and 18 rollback scenarios pass. Target ideal distinct embeds/query no worse/≤1 ms added capture write, repeated WAL-tail measurement.
  - **Validate:** Test upgrade/cancel/restart/late publication; validate with the real model, FTS-depth saturation, 50-sighting truncation and counts.
  - **Limits:** Fake embedder makes ranking approximate (top1 2/36 changed). Savings 21–99% fixture dependent. Optional controlled VACUUM reclaims pages, not erasure.
- [ ] **M12.3 Window scan/query plans** — R; #104, #48
  - **Scope:** Episode C2: range starts [S,E), primary-key IN(json_each) probes for remaining eligible IDs/old straddlers; no probe join/ORDER BY causing history walk. Float64 score/order score/start/rowid, ≤1e−6 score/near-tie tolerance explicitly unlike bit-exact passages. EXPLAIN passage plan before changing suspected full-model scan; choose verified time-driven CROSS JOIN/PRAGMA optimize only on evidence.
  - **Accept:** Zero hard/true-tie errors, allowed near ties reported, ≤5 ms 1-day/40k step/no unwindowed regression; 60-day straddlers/missing/damaged/other-model/drifted start rows covered.
  - **Validate:** SQLCipher plan/scale/cold-warm validation after reuse.
  - **Limits:** Historical 3 <5e−8 near ties and month 6.1 ms cold do not violate 1-day contract.

## M13 — Bounded operational work

Objective: remove repeated history scans/polling and needless model swaps. P0 #34/#61 then P2; M13A/B deliver on M02/M03 contracts before M12 final-schema revalidation. Own API read/locks/polling/reports, scheduler/discovery/FTS churn. **Done:** responsiveness and scale/resource evidence covers all batches. Sources: [architecture](2026-11-10.md#architecture), [pipeline](2026-11-10.md#bugs-pipeline), [storage](2026-11-10.md#bugs-search-and-storage), [shell](2026-11-10.md#bugs-app-shell-api-ui).

**Assignment contracts (priority/dependencies in the map):** M13A (.1–.2) bounds UI polling/read/media/report work; excludes embed discovery; done when plans/load/delete/lock tests establish responsiveness/cache safety. M13B (.3) bounds discovery/scheduling; excludes vector representation changes; done when idle-history cost/retries/backfill/swaps/game gates/no-starvation checks pass. Each begins with P0 repairs and closes on current-schema evidence; M12 later revalidates final-schema integration.

- [ ] **M13.1 Cheap polling** — R; #61, #64, #65
  - **Scope:** Remove distinct-process scan from /status using separate app endpoint/session/TTL cache; keep health index-bounded. WHERE-count recent frames, cache log size/mtime_ns and parse appended bytes with rotation/truncation.
  - **Decisions:** Decide refresh bounds first.
  - **Accept:** Multiple tabs/large history no status scan stalls, append/rotate/truncate correct; capture plans/query counts/latency under load; small-fixture speed alone is not bounded cost.
- [ ] **M13.2 API readers/media/reports** — R; #13, #58, #63, #51
  - **Scope:** Scoped read-only WAL readers preserve key/auth/lifecycle ownership; snapshot minimal frame/path under lock then decrypt/thumbnail outside using M02 cache identity. Cache storage report refreshed/invalidate after retention, or honestly show file sizes only. frames_au only text/title/URL updates.
  - **Accept:** 120 thumbnails/long search/usage cannot serialize status, concurrent lock/delete no stale data/closed-reader race, screenshot expiry no FTS rewrite.
  - **Validate:** Validate WAL/read snapshots/stale caches and report scope.
- [ ] **M13.3 Incremental discovery/scheduling** — R; #34, #37, #38, #42
  - **Scope:** Per-kind/model watermark with explicit old-item repair/backfill so retries/late OCR are never skipped; integrate M03 failed-ID disposition on the current schema.
  - **Depends:** Deliver the accepted discovery/backfill contract now; M12 later adapts and revalidates it for link-table publication, without delaying M13B acceptance. Prefer runnable transcription, drain embeds only appropriately without repeated swaps; reset/exclude own CPU work in busy checks. Check game/heavy gates between STT clips/requeue NULL predictions.
  - **Decisions:** Do not restore removed development limits (AGENTS.md).
  - **Accept:** Idle tick no forever-history walk, new model/late OCR/deletion/restart/failed batch eligible, no starvation.
  - **Validate:** Measure history scaling/swaps/backlogs/RAM/VRAM/responsiveness.
  - **Limits:** CPU priority does not arbitrate GPU contention.

## M14 — Structure and shell cleanup

Objective: simple dependency direction/single sources after behavior stabilizes. P2 M14A structure after affected M00–M13 integration; P1 M14B shell after lifecycle/persistence contracts. Own import moves/config/shell/dead code/docs, not algorithm redesign. **Done:** layout/native dev loop passes, docs truthful and refuted removals retained. Sources: [architecture](2026-11-10.md#architecture), [shell](2026-11-10.md#bugs-app-shell-api-ui), [cleanup](2026-11-10.md#creep-redundancy), [decisions](2026-11-10.md#open-questions), [docs policy](../README.md).

**Assignment contracts (priority/dependencies in the map):** M14A (.1/.2/.4) restores dependency/config/persistence sources and chosen removals/docs; excludes shell bug repairs; done when direction/compatibility checks pass and capability/source links are truthful. M14B (.3) repairs download/navigation/polling/Debug behavior; excludes import/schema redesign; done when error/race/mounted-view checks pass on lifecycle/persistence contracts. It does not await vector optimization; coordinate later shared-file moves.

- [ ] **M14.1 Dependency boundaries** — R/K; #1, #2, #4–#6
  - **Scope:** Move queue to storage/jobs as selected, runner/scheduler stay pipeline. Single kinds/keys, queue APIs retry_failed/replace/has_pending/oldest_pending/key_for, no external job SQL. Move query-vector enqueue/poll to pipeline/query or narrow API orchestration, confidence constants neutral, search ranks only. Handlers declare resource needs, runner avoids model-class hardcoding. The UI UNCLEAR_BELOW copy is test-pinned (#2 refuted as drift): invent no drift defect.
  - **Accept:** Package-direction checks prevent cycles and M03/M13 transactional retry/horizon/resource behavior unchanged.
- [ ] **M14.2 Persistence/config sources** — R; #7–#9, #12, #94, #97, #98
  - **Scope:** Storage modules for questions/STT samples/runs; one bounds table form/parser/LOG_LEVELS, retention rows from purgers, pinned is_ready/is_complete not GGUF glob. Registry built from speech specs while keeping scored chat alternatives; direct State annotation, one MediaStore, shared now_ms/clamp.
  - **Accept:** Accepted form saves at boundaries, wrong/incomplete models not ready, API/worker deletion propagation and clock/filter behavior unchanged.
  - **Validate:** Validate public contracts rather than creating unnecessary abstractions.
- [ ] **M14.3 Shell/UI completion** — R; #66, #70, #71, #95
  - **Scope:** Unexpected download failure marks active/queued failed safely; stale settings builds use sequence token; mounted Ask polling retries transient errors/backoff, 404 terminal. Debug uses Paths.db_file instead of hardcoded .4 s/path.
  - **Accept:** No forever downloading/orphan intervals/disabled composer after transient error; display follows config.
  - **Validate:** Fake downloads/navigation/race tests without network/content logs.
- [ ] **M14.4 Chosen removals/docs** — R/K; #14, #90–#93, #96
  - **Scope:** Delete change_passphrase/tests as chosen, reword capability docs (do not wire Settings). Delete monitor grab/protocol; fake returns per-window images, drop Rect.translate if unused. Remove dead token/model constants/CSS, registry-based test. Correct absent RESEARCH/deleted manual references with current guides/historical links, fix CUDA/cuDNN claim. Keep guides focused on rationale/contracts and layout checks from code, not stale inventories.
  - **Decisions:** #90 media_events removal is excluded (user choice); media_sessions_fts/triggers are independently removable only after a current-reader check and the M05 contract, otherwise defer the entire removal. Preserve GPT-OSS/Qwen3 chat choices (#94 deletion proposal refuted).
  - **Accept:** Behavior retained/docs links resolve/no absent capabilities/no one-resident-model promise.

## M15 — Conditional dependency/packaging simplification

Objective: smaller self-contained bundle without breaking inference/keyfiles. P3/C; M15A/B/C have independent inference/bounds/scheduling prerequisites in the map. Each owns only its package, KDF or production-priority boundary. **Done:** each proposal validated or explicitly deferred; selected changes pass native relocated/clean-machine checks. Sources: [alternatives](2026-11-10.md#alternatives-smarter-solutions-within-the-projects-intent), [security](../SECURITY.md), [packaging](../PACKAGING.md).

**Assignment contracts (priority/dependencies in the map):** M15A (.1) owns conditional offline package/bundle simplification, excluding key/priority policy; done on required approval plus native/relocated inference evidence or explicit deferral. M15B (.2) owns only compatible KDF substitution; done on both-implementation keyfile/bounds/auth tests or deferral, without a format change. M15C (.3) owns only conditional production priority; done on a policy decision/native load-game-queue evidence or deferral, never restoring development limits.

- [ ] **M15.1 CPU ONNX package** — C; #107
  - **Depends:** Obtain required package approval for replacing approved GPU package, confirm runtime offline behavior and spike actual DLL dependencies. Proposal plain onnxruntime; drop unused CUDA/cuDNN 13/nvrtc/cufft/curand/nvjitlink/override, keep CTranslate2 cuBLAS12; torch CUDA separate.
  - **Accept:** Real large-v3 GPU decode/VAD/speakers CPU/selectable backends from relocated offline bundle without developer PATH/caches.
  - **Validate:** Measure size, not assume historical ~1.2 GB compressed. No package switch before a native smoke test.
- [ ] **M15.2 Argon2 alternative** — C; #108
  - **Scope:** Check existing cryptography Argon2id capability, map key_material=passphrase/iterations=time_cost/lanes=parallelism/memory_cost=memory_kib/length=32. Preserve normalization/auth/atomic replacement/M02 bounds.
  - **Accept:** Existing-format fixtures unlock with both, malformed/wrong passphrase consistent and fresh keyfiles compatible before removing argon2-cffi. No format change; failure defers, never recreates the user's store.
- [ ] **M15.3 Production priority** — C; #109
  - **Scope:** Measure peaks/responsiveness then decide production below-normal worker/llama-server priority, never capture.
  - **Decisions:** Requires policy decision; do not restore removed development limits. Job memory limits (caps fail commits) and per-process GPU priority remain separate unapproved spikes.
  - **Accept:** Measured responsiveness without queue/load starvation, safe native failure/restart.
  - **Limits:** Background I/O/memory priority can slow loading; CPU priority is not GPU arbitration.

## M16 — Conditional speaker extensions

Objective: investigate residual wrong authors beyond interim Route A. P3/C; depends M09; coordinate M12 thread revalidation. Scope optional attribution, not default model changes. **Done:** frozen results/explicit decisions, no unmet contract claimed solved. Sources: [#106](2026-11-10.md#alternatives-smarter-solutions-within-the-projects-intent), [speaker][speaker], [hallucination][hallucination].

- [ ] **M16.1 Word timing** — C; #106
  - **Scope:** Evaluate faster-whisper timestamps and greatest-overlap turn per word with ambiguous spans/times/provenance schema decided first. Refit gate after decoding/seek changes.
  - **Decisions:** Do not enable silence-threshold alone (one-word replies risk).
  - **Accept:** Independently labelled FR/EN/mixed per-line Minlo quotes improve without wrong-author regression; measure decode/memory.
  - **Limits:** Built-in feasibility not quality; merged voices remain hidden.
- [ ] **M16.2 Route B spans** — C; related #132–#134
  - **Scope:** After decision derive C7 extra model output dev-only onnx (approval if new), pin checksum/check profile cosine ≥.9999999. Require a verified bounded production BLAS configuration before Route B acceptance: the reported C7/C8 resource behavior required a thread limit (one thread in measured C7).
  - **Decisions:** The bounded BLAS configuration is a production method contract, not restored development limits.
  - **Accept:** No wrong-author regression/compatibility/per-segment and pair resources plus M12 full identity under final worker threading.
  - **Limits:** Default threading historically took 130–1,400 ms versus about 1.7 ms. C7 .927/3.17% below target; C7+C9 unmeasured, new comparison required. Some historical segments exceed 2×; report.
- [ ] **M16.3 Stronger segmentation** — C/X; related #132
  - **Decisions:** GPU dense verification/Sortformer/DiariZen needs a reopened CPU-only policy, export/runtime/licence review (DiariZen CC BY-NC) and a native spike.
  - **Accept:** Only approved bounded-resource independent FR/EN short/mixed gain without gaming harm. ≥300-ms minor-voice absorption excluded even if headline authorship rises.
  - **Limits:** Hidden voices upstream: 2,723/2,760 frames look solo.

## M17 — Conditional Ask extensions

Objective: measurable residual accuracy at agreed resource/latency cost. P3/C; depends M00A/M10/M11 and M12 when ranking schema changes. Own optional planner schema/reranker/quotes. **Done:** keep/defer decisions with independent quality/latency/resource evidence. Sources: [follow-up][follow-up], [time][time], [alternatives](2026-11-10.md#alternatives-smarter-solutions-within-the-projects-intent), [interactions](2026-11-11-synthesis/synthesis.md#74-hot-spot-3-the-ask-path).

- [ ] **M17.1 Model inheritance/time** — C; #175, #179–#181
  - **Scope:** Default C1 rules/time C7. C6 relation code/carry table optional loaded-model path, deterministic fallback/target pairs rules. C9 grounded time slots fill gaps, matched rules/vetoes win.
  - **Decisions:** Apply D1/D4 conventions; D8 selects rules by default and keeps model assistance optional; any enablement still needs combined evidence and an explicit later choice.
  - **Accept:** ≤one extra follow-up call/no fallback regression/future/ungrounded filters, per-field/language/repeated timings.
  - **Validate:** Measure single-call relation/standalone isolation (unmeasured); prior text must not contaminate plan fields for call savings.
  - **Limits:** Historical .933 vs .903 costs ~1.12 s; time .931 vs .926 ~1.8 s shared GPU; combined ~2.9 s.
- [ ] **M17.2 Reranker** — C; #105
  - **Scope:** Establish retrieval bottleneck in M00, measure real RAM/VRAM, decide third model (`models-max 2` full).
  - **Accept:** Independent FR/EN/mixed retrieval/answers/abstention improve within load/eviction/latency/game budget.
  - **Validate:** Spike classifier-head Qwen3-Reranker GGUF through existing local rerank endpoint.
  - **Limits:** Published MMTEB gains and historical 6× slowdown are leads only. Larger embedder follows insufficient measured reranking with re-embed cost decision.
- [ ] **M17.3 Quote-then-answer** — C; #112
  - **Scope:** Spike schema claims/verbatim quotes checked against sources; decide streaming/parsing interaction first.
  - **Accept:** Fabricated quotes rejected, truncation/unsupported/time links/delete lineage intact and answer quality retained.
  - **Validate:** Inspect entailment manually: a real quote need not support claim; no complete hallucination/injection defense claim.

## M18 — Conditional process audio/STT challengers

Objective: resolve remaining recognition/source quality with native evidence. P3/C; depends relevant M00B/M07A/B/M08/M09 contracts. Own experiments/explicitly selected integration; no default switch. **Done:** quality/resource decisions or deferrals documented. Sources: [#110/#111](2026-11-10.md#alternatives-smarter-solutions-within-the-projects-intent), [hallucination][hallucination], [capture](../CAPTURE.md), [speech](../SPEECH.md).

- [ ] **M18.1 Process loopback** — C; #110
  - **Scope:** Native Discord-tree async COM/manual-format spike incl headset/process restart before building.
  - **Decisions:** Compare simpler M07 endpoint fix; defer if cost not justified.
  - **Accept:** Isolates Discord from game/music/video, pre-disk exclusions/encryption/source-gap contract/no external helper.
  - **Limits:** Cannot separate in-game voice from game's own process. API availability is not comtypes feasibility proof.
- [ ] **M18.2 Challengers** — C; #111; #141 calibration
  - **Scope:** Compare integrated Canary-v2/Whisper/Qwen using M00 independent FR/EN/mixed/no-speech/names/negations/quiet/overlap. Cohere only after gated artifact/local runtime/dependency/offline feasibility. Calibrate backend non-speech; Whisper fixture cannot transfer C7.
  - **Decisions:** Switch only for better meaning/timestamps/authorship/provenance within memory, not read-French WER 4.05/4.83/5.74/6.36 or speed. No quantized/smaller selection for throughput or TTS smoke alone.
  - **Limits:** Cohere needs language/lacks timestamps/weak code-switching.

## M19 — Workspace and unnumbered disposition

Objective: retain all non-numbered actions without unsolicited repo rewrite. P3, mixed R/C/K; depends M14A/M15A where relevant. Own workspace/docs/lead registry only. **Done:** each observation/lead reviewed or deferred with reason/owner/trigger. Sources: [workspace](2026-11-10.md#workspace), [alternatives](2026-11-10.md#alternatives-smarter-solutions-within-the-projects-intent), [synthesis §9](2026-11-11-synthesis/synthesis.md#9-other-alternatives-that-were-examined).

- [ ] **M19.1 Workspace observations** — R/C/K
  - **Scope:** Check historical uv/.venv absence against actual tooling when starting, native direct commands/no implicit sync. Headless stop/status utility C on demonstrated need. Review ignore OS/secrets/local settings/worktrees/log/egg-info and attributes PS1/CMD/binaries/frozen CRLF; accept sensitive artifacts ignored/required files tracked/hashes preserved. Keep personal noncommercial screenpipe scope (repo becomes private, user 2026-11-12), no privacy/relicensing change. Review README licence/commands/history and requested DESIGN/LICENSE/changelog/TODO/ISSUES placement against CURRENT docs policy before adding/moving.
  - **Decisions:** Also disposition the historical missing Commands/Dev-loop section and dated-history maintenance: defer duplication/removal by default under the current docs map, preserve a clear TOOLING entry point and decision rationale, and add/move guidance only if a discoverability gap remains. Shared Claude permissions conditional on actual use, no weakening approvals. Dependency vulnerability audit (C) transmits tool/package names to PyPI/OSV, so it requires policy approval or approved local advisories; never captured/identifying content or a disabled guard.
  - **Limits:** AGENTS @-import bloat unconfirmed: measure actual loading before trimming rules. Historical clean stash/git/one branch observation needs no cleanup.
- [ ] **M19.2 Alternative register** — C/X
  - **Scope:** Apply complete register below. Reopened lead needs focused question/native spike, model/dependency/policy decisions, acceptance budget and independent comparison; no exploratory implementation required just to close disposition.
  - **Accept:** Every lead accounted for and excluded proposals absent from default work.

## M20 — Ongoing local speaker comparison

Objective: provide the reusable local accuracy-comparison feature requested in D6, which can improve alongside everyday use. P1 ongoing; depends on M00.2's attribution-label/metric contract, M02.1 encryption/deletion and the current speaker prediction interface, not completion of Route A or M16. Own the comparison workflow/results only; speaker algorithms/profiles stay M09/M16 and Ask fixtures stay M00A. **Done:** the user can label selected local clips, compare reference/candidate attribution on the same material and inspect correct/wrong/unknown results with explicit provenance/limits; disposal and missing-source behavior are validated. This feature does not gate provisional everyday use. Sources: [evaluation #123](2026-11-10-2.md#evaluation-methodology), [speech comparison limits](../SPEECH.md#comparisons), **user D6, 2026-11-11**. The feature assignment is new user direction, not a requirement purportedly measured or approved in the historical audits.

- [ ] **M20.1 Reusable speaker comparison** — R/K D6; #123 cross-obligation
  - **Scope:** Use the existing clip inspection/speaker correction surfaces where practical for a minimal local label-and-compare workflow; decide the smallest persistence/API/UI contract before building. Retain independent user labels and source/time/prediction-version provenance; evaluate the same selected clips for reference and candidate, report correct/wrong/unknown authors separately and expose short/mixed/uncertain cases. Candidate results must not overwrite ordinary memory attribution; user-confirmed corrections retain their normal correction behavior. Reuse M00 attribution metrics rather than a second incompatible scorer.
  - **Decisions:** Selection/labeling is explicit user work; no unattended history collection, external export, new dependency or automatic model switch is authorized. Refine the workflow in parallel with everyday use (D6).
  - **Accept:** Repeated comparisons preserve labels/versions, wrong names cannot hide as unknown, unavailable/expired audio is explicit, and all labels/results/media remain encrypted with source deletion/expiry/full-delete and late-publication protection.
  - **Validate:** Validate fixture negatives, API/persistence/UI and disposable encrypted deletion; the feature's successful operation does not prove improved attribution or representative real-call accuracy.
  - **Limits:** Retain study targets/residuals until actually measured.

## M21 — Documentation-review follow-ups

Objective: implement the code changes decided in the 2026-11-12 documentation review ([DECISIONS.md](../DECISIONS.md)). P1 for .1 (it unblocks real-data testing), P2 for .2; depends on M02.1 for the encryption/deletion boundary. **Done:** each batch below meets its Accept line. Sources: **user, 2026-11-12**.

- [ ] **M21.1 Encrypted export for agent testing** — R/K
  - **Scope:** An explicit user action writes a copy of the store (database and media, optionally limited by date range or category) re-encrypted under a new passphrase the user chooses. The copy opens with Akashic (`AKASHIC_DATA_DIR`) like any store. Agents test on such exports, never on the live data folder.
  - **Decisions:** User, 2026-11-12: the user gives the export's passphrase to the agent; the live passphrase and keyfile are never shared. No network, no plaintext intermediate files.
  - **Accept:** The export unlocks only with the new passphrase; the live store is unchanged; an interrupted export leaves no partial plaintext; writers are stopped or a consistent snapshot is used.
- [ ] **M21.2 Remove the storage-pressure warning** — R/K
  - **Scope:** Remove `disk_warning_free_gb` from config, Settings and any UI/status warning (including the "Only N GB free" note in `src/akashic/ui/debug.js`); update tests.
  - **Decisions:** User, 2026-11-12: retention stays forever with no low-disk warning; history is never deleted automatically.
  - **Accept:** No code path reads the setting; an existing `config.toml` containing the key still loads.

## Candidate and alternative register

Candidate labels are local to each linked study. These are dispositions, not a second approval queue. C/X changes need reason, authority, evidence and dependent batch recorded below. Unbuilt ideas have no implementation evidence.

| Study | Selected path and complete alternative disposition |
| --- | --- |
| [Exact scan][scan], #100 | R C3+C1 self-test. C C4 plain cast simpler fallback, misses 3/10 budgets; C1 standalone within timing noise. X C2 SQL/group_concat and create_aggregate move object work/no gain, aggregate order unsafe without ordering. X sqlite-vec/ANN/GPU/chunk-blob changes outside contract. C narrow scan/second-offset lookup and fixed-order einsum unmeasured; BytesIO/subblocks only profile-driven. maximum.at slow/no argmax; selection ≤5% of cost, not claimed primary gain. |
| [Text reuse][reuse], #101 | R C3. X C1 capture tags exceed tail budget; C2 lazy tags unbounded paused backlog; C4 generated SQL function blocks unregistered connections/delete/trusted_schema. C C5 per-sighting collapse only after user need/new tail and hidden-count design: 3/4 write tails over budget, 70/72 runs hidden in one fixture. X unkeyed hash privacy, text-key-only merging distinct videos, drifting explicit counters and copy-on-embed storage proportional to frames. Fuzzy/image dedupe outside scope. |
| [Conversation][conversation], #102/#36 | R C4 memo sweep; C C1 simpler no-memo still fits, C2 bisect unbuilt equivalent supported by refuter. X C3 midpoint exact only mic ≤300 s (nonstructural), C5 changed finality relaxes contract, cross-pass state stale under late evidence. |
| [App runs][app-run], #103/#172 | R C1 identity THEN changed semantics. C2 exact but fragmented tail slower/wasted open-run folding. X naive REAL +=, URL from −inf, inclusive overlap and deterministic ties DURING identity violate contract. Deterministic first-seen becomes R only in M06. |
| [Episode scan][window-scan], #104 | R C2 float64 split. C C3 bit-exact if chosen over window-proportional count (grows with history/local crossover). X C1 long-straddler budget, start-only loses owners; joins/ORDER BY poor plans; RTree sync/epoch float precision needless. |
| [Hallucination][hallucination], #128/#129/#138/#141 | R C7 plus separately tested echo/edge complements. C C3 CTC .949 recall, ~2.5 GB models/~1.2–1.6 GB VRAM/22% unclear/reject-only .043; runtime unsettled/test reused. X C5 contrast fusion +20% time no gain, pure contrast 71% real unclear; C1 two-key .826 recall; C2 prompt-removal cannot remove training prior. C C4 backend calibration M18.2 unbuilt on Whisper fixture; X C6 reversed controls redundant. X silence threshold alone flags replies/needs words. Calm-Whisper/phrase bags not validated FR/EN replacements. |
| [Speakers][speaker], #132–#134 | R interim C2+C9 Route A, no best fit. C C7(+C9) Route B unmeasured combination. X C1 .880/4.56%, C6 swap .712/7.94% no useful gain, C8 .733/1.19% sacrifices authorship/unstable gate. Defer unbuilt C3 AS-norm scoring-only, C4 CUSUM alarm never fired (margin/gate mattered), C5 call-level rewrite can increase wrong authors. X1 ≥300-ms absorption excluded; X2 GPU verification/X3 model swap require CPU-only decision. Zero padding/lead-as-body lost authorship; retain seam-centered/trim truth. |
| [Follow-up][follow-up], #175 | R standalone+C1. C C6 M17.1; C5 .927 extra calls/no .95 fit. X C2 undecided-only model misses confident errors, C3 single shift bit .636, C4 lacks licensed French data (QReCC EN only), C7 distilled synthetic classifier lacks generalization. C C8 nearest exemplars/C9 disagreement routing need rule budget 5→30 ms, heuristic stoplist change and functioning shared embed server, unmeasured. |
| [Time][time], #179–#181 | R C7+C5 retrieval; C C9 M17.1. X C1 .684 phrase/C3 .753 clause gaps; C8 model over C3 .881 weaker than final rules. C2 activity-derived day fails open calls or persists fitted state; C4 verbatim fallback adds nothing; C6 soft margin ineffective unscored/changes counts. C C10 soft-window metric requires explicit changed acceptance, not method success. X C11 persisted learned aliases violates user's RAM-only choice. |

### Unnumbered thin leads (C; owner M19.2 for disposition)

Each reopened implementation belongs to named milestone; first spike then decide, no default implementation.

| Lead | Gate and validation/reason deferred |
| --- | --- |
| Qwen3-Embedding-4B | M17 after insufficient reranker, full re-embed/resource decision and held-out FR/EN gain. |
| Convex fusion vs RRF | M17 labelled queries, fixed candidate sets, held-out precision/recall including reuse grouping. |
| Qwen3.6-35B-A3B CPU-offloaded chat | M17 native runtime/peak memory/quality/latency; larger model not missing-evidence fix. |
| NVML free-VRAM/free-RAM admission | M13 production peak/failure evidence/package decision, never mandatory development preflight. |
| Isolated Edge --app UI | M14/M19 lifecycle/session/auth/asset spike, self-contained prerequisite review; no extra app installation requirement. |
| Per-process GPU priority | M15 native scheduling/game/inference measurement; CPU-priority evidence insufficient. |
| French FTS inflections | M10/M17 precision/recall and package decision; no built-in French stemmer. |
| Chrome 138+ native UIA causing blank Edge probe | M07 actual version/accessibility spike; historical causal theory unverified. |
| Multiple GSMTC sessions/browser | M05/M07 concurrent playback/ownership/exclusion spike; process identity cannot identify a tab alone. |
| Full-size French Whisper fine-tune | M18 English/mixed/name/negation/non-speech comparison/offline pins, not French-only WER. |

### Exclusions, decisions and narrowed evidence

- X general alternatives: sqlite-vec pre-1.0/unverified SQLCipher integration; Windows.Graphics.Capture border/D3D complexity versus verified PrintWindow; alternate OCR without FR/EN benchmark (check Windows French pack first); pyannote community-1 worse cited French result; PyInstaller/embeddable Python previously rejected/Tcl-Tk gap; IPC/service/queue frameworks and ty replacement for pyright. Existing SQLCipher/FTS5/numpy, key handling and deterministic time parsing had no clearly better replacement.
- K user choices (2026-11-12 changes in [DECISIONS.md](../DECISIONS.md): repo becomes private; one digest builder, in Python): personal noncommercial; retire legacy segment vectors; queue moves storage; delete change_passphrase; keep media_events. #90 media_events removal is UNQUEUED, not pending approved cleanup; independent unused FTS decision stays M14.4.
- Partial refutations: #94 removing scored GPT-OSS/Qwen3 choices rejected; #2 UI confidence constant already test-pinned; #44 extra stt_results index unnecessary for tiny table; #3 alleged app-format divergence unreachable (NOT NULL); #88 weak smoke, not empty; #107 cuDNN-needed/~540 MB corrected to Windows cuBLAS12/~1.2 GB; #110 Discord only, not in-game voice.
- Narrowings: #132 person filter survives but line author lost; #142 narrow suspend timing/no overnight audio; #154 attempts already increment, only runnable poison kind starves; #157 Recording can correctly describe capture while processing stopped; #164 audio links delete but conversation refs survive; #176 episode returned without lines; #178 digests lose labels but lines retain them; #180 hier aprèm broad, not absent; #182 accent-folded shared spellings match. No full finding was refuted.
- X project expansion without explicit scope decision: cloud/sync/accounts/plugins/agent frameworks/MCP servers/browser extension/multi-OS/every-frame vision/knowledge graphs/proactive suggestions/webcam/echo cancellation/video compaction.

## Recorded user decisions and evidence limits

[DECISIONS.md](../DECISIONS.md) is the current list; where it differs (2026-11-12 review: private repository, one Python digest builder, D7 clarified as "drop songs, keep music-app segments with evidence of real speech"), it wins. The dated source decisions remain binding: legacy-vector retirement, queue move, passphrase-change removal and retained media events were answered on **2026-11-10** ([audit 1 decisions](2026-11-10.md#open-questions), [audit 2 decision](2026-11-10-2.md#open-questions)). Selected-HWND capture, RAM-only learned title state, quality-before-speed and removed development guardrails are also preserved; none was reopened.

| ID | Authority/date, affected work and disposition |
| --- | --- |
| D1 | **User, 2026-11-11:** use the study's 05:00 logical-day convention. M10.2/#179–#181 and optional M17.1 use that convention, including logical yesterday before 05:00; changed expectations retain dated rationale. This does not turn .926 simulated-title accuracy into a guarantee. |
| D2 | **User, 2026-11-11:** approve narrower private-suffix/non-browser password-manager matching with fail-closed safeguards. M01.3/#148 is R/K, not awaiting policy permission. Native-layout and unreadable-metadata checks remain required. |
| D3 | **User, 2026-11-11:** screen-text removal/expiry propagates to copied excerpts and affected answers. M02.2/#57 plus M11.3/M12.2 must preserve lineage, invalidate conservative legacy copies and keep screenshot-only retention separate. Documented retained copies are not selected. |
| D4 | **User, 2026-11-11:** explicit weekday follow-ups carry previous topic/omitted filters; explicitly stated weekday-as-topic may carry; a topic continuation after “What did I do on Tuesday?” retains Tuesday. M10.1/M17.1/#175 update conflicting labels traceably. Do not generalize inheritance to standalone questions. |
| D5 | **User, 2026-11-11:** unclear counts separately as handled; plausibly spoken outros stay flagged. M08.2/#128/#129/#138/#141 retain the distinction; M11.3 applies D9 evidence-use rules, not hedging alone. Keep detected/unclear/rejected/real-false-rejected metrics distinct; 92.1% includes flags, 37.5% is outright rejection. |
| D6 | **User, 2026-11-11:** allow provisional everyday use before the local accuracy comparison; retain the comparison as a feature that can be refined in parallel with everyday use. M09.2/#132–#134 and optional M16 routes have no mandatory local-comparison release gate; M20.1 owns the ongoing feature, related to #123/M00.2. Compare reference/candidate on the same user-labelled local clips, reporting correct/wrong/unknown authors separately; keep clips/labels/results encrypted and separate from ordinary memory predictions. Public-fixture integration continues; local comparison material is selected explicitly by the user, and provisional use is not a real-call accuracy claim. |
| D7 | **User, 2026-11-11:** preserve uncertain music-app segments for transcription, accepting extra queued work/storage. M07.3/#152 must not drop a speech candidate solely because sources are Spotify/music apps; retain existing exclusions/quiet protection and distinguish this policy from the unproven acoustic detection of speech. Validate podcast and brief mixed-speech cases plus backlog/storage behavior with bounded memory. Sampling alone is not the selected resolution. |
| D8 | **User, 2026-11-11:** rules by default; model assistance stays optional. M10.1/.2 and #175/#179–#181 do not await model-latency approval; M17.1 remains conditional on independent combined accuracy/resource/latency evidence and a later explicit enablement choice. |
| D9 | **User, 2026-11-11:** retain flagged/unclear text for search and inspection, but never use it alone to support a factual answer, even with a hedge. M11.2 preserves uncertainty/provenance and M11.3 separates possible matches from factual evidence and grounded saved claims. Clear evidence supports normal cited answers; user corrections or independent clear sources may corroborate, but copies of the same transcript do not. Flagged-only results disclose inability to answer reliably and link the possible match recording; conflicting evidence is disclosed and speaker identity is assessed separately. M00.2 validates these cases and reports detection, rejection and unsupported answer claims separately. D5 still governs transcription retention/outros; this decision governs answer use. |

Remaining evidence limits: real speaker calibration/representativeness despite provisional-use permission D6; the exact acoustic limits of the D7 preservation path; legacy source provenance; unmeasured C7+C9 speaker and combined model-planning/thread/scan configurations. Optional GPU segmentation remains outside the selected CPU-only route. Reranker residency, production priority and package changes retain their existing evidence/approval gates; measure the relevant peaks/feasibility before asking for those later choices. A deferred option is not a completed original goal.

## Execution evidence and decision log

The source links immediately below each milestone objective apply to every batch in that milestone; the coverage ledger additionally links each numbered finding to its original audit section. Read the full numbered row there, including narrowing and historical file references.

### Documentation duties after implementation

Every agent that finishes work (implementer, tester, refuter-driven fix, reviewer) writes the records below **in the same session, after the last code, test or fix round has ended**. A later fix round reopens them. Write only what ran: no "pending", "should pass" or claim beyond a recorded command. Copy the neighbouring entries' format (M00–M02 are the templates); never invent a place, column or status word. A record the work proves false is corrected in place, not appended to. No user history, keyfile, data directory or captured text goes into any record; use counts, ids, hashes and exception types.

| Trigger | Record (where) | Content |
| --- | --- | --- |
| **Step / finding finished** (`Mxx.n`) | Finding coverage ledger row for each numbered finding it owns (and a row for each related obligation it delivers) | Set Status (`validated`, `PARTIAL`, `deferred`, `excluded`; never silently `[x]`), Evidence (link to the baseline section holding the proof: test file, command, native check), Accepted points, Gaps. Tick `[x]` only under the rule in the acceptance checklist below. Findings you did not touch stay UNVERIFIED. |
| **Batch finished** (`Mxx.n`) | Row in the milestone's execution-records table (`### Mxx execution records — <date>`) | Findings, files and migrations, commit shas, Status, Accepted points, Gaps. One row per batch plus one milestone row (`all`, evidence commit, full-run result, Implemented/Delivered). Create the section, with the base sha and the baseline link, on the first batch. |
| **Decision taken** | Batch Decisions cell, plus the baseline's `Decisions` section; a user decision of lasting scope also gets a `D<n>` row in the decisions table | Authority and date (`user, 2026-11-12` vs `agent default`), the chosen variant, rejected alternatives and why. A deviation from the batch text is recorded as a deviation with its reason, never folded into the scope. |
| **Milestone finished** | Acceptance checklist line (`- [ ] Mxx — …`) | Replace `UNVERIFIED; evidence / remaining / deferrals: —` with: state, evidence link, one-sentence result with the full-run counts, then **Accepted points** and **Gaps** separately. Tick `[x]` only when the checklist's milestone rule holds; otherwise leave `[ ]`. Also set the batch lines in the batch bodies when their rule holds. |
| **Contract delivered or consumed** | Baseline `Downstream contracts` and the consumer's batch Depends | State the producer, the consumers and what they may now rely on (API, migration number, count-only key). A dependency is a contract that was accepted, not assumed completion. |
| **Conditional / excluded obligation** | Execution-record Gaps and the ledger Disposition | Record the explicit disposition (done, deferred with reason, consequence, owner and revisit trigger, or rejected). Deferred is not implemented. |
| **Behavior or interface change** | The matching area guide (`docs/CAPTURE.md`, `SPEECH.md`, `STORAGE.md`, `RETRIEVAL.md`, `ARCHITECTURE.md`, `SECURITY.md`, `UI.md`, `TOOLING.md`) and `docs/README.md` if a file is added | Update the current-behavior statement only; the history stays in the handoff and baseline. Remove statements the change made false. |

#### The milestone baseline document

One file per milestone: `docs/baselines/<date>-<milestone-id-lowercase>.md` (for example `2026-11-12-m02.md`), dated with the day the evidence was produced. It is evidence, not a plan, and keeps its original run's numbers: a rerun adds a new dated file or a clearly dated section, it does not overwrite the old data. Use this order; omit a section only when it has nothing to say, never fill it with filler.

1. **Title and header paragraph.** `# Mxx <name> implementation evidence`, then one paragraph: date, OS and Python environment, base sha and branch, what was produced (production code, tester phase, markers removed, new test files with counts), the statement that every store was a disposable encrypted database and no user history, keyfile or data directory was opened, and that no dependency was added (or which one, with approval).
2. **Acceptance matrix.** Table `Batch | Finding | Change | Status`, one row per finding, with the migration, function or test names and a status of Validated, PARTIAL or Not validated plus the reason. State when a regression test was not shown to fail without its fix.
3. **Commands and outcomes.** Each command that ran with its actual result: `ruff format --check`, `ruff check`, strict pyright, the full pytest run on a fresh basetemp (passed / xfailed / deselected, compared with the previous baseline's counts), then targeted suites. Failed or skipped commands are listed with the reason. Removed xfails and `known_defect` markers are listed with their finding.
4. **Measurements** (benchmarks, native or hardware checks, model runs), when the batch's Validate line asks for them: setup, sizes, a results table, number of runs and machine, and the conclusion the numbers support. State the scratch script's location; do not commit throwaway scripts. Native checks that could not run (missing pack, voice, device, app) are listed as missing evidence, never as passes.
5. **Decisions.** Authority, date, chosen value, rejected alternatives, deviations from the handoff text.
6. **Accepted points.** Boundaries knowingly accepted (synthetic-only validation, not secure erasure, single-writer assumption, and similar). They do not block a check.
7. **Gaps.** Unmet acceptance or validation requirements and remaining work, each with an owner and a revisit trigger. A non-empty Gaps section keeps the batch `PARTIAL`. Never mix these with Accepted points.
8. **Downstream contracts.** `<producer> to <consumers>: <what they may rely on>`.

Keep the baseline, the execution-record row and the ledger row consistent: same status, same counts, same gaps, each linking to the next. The ledger and checklist carry a one-line summary and a link; the detail lives only in the baseline. Update `docs/baselines/` dated measurement files (such as `2026-11-11.md`) only when a measured quantity they report was re-measured; compare a change against the latest one rather than rewriting it.

#### Per agent

- **Implementer:** after the final code round, writes the execution-record rows, ledger rows, checklist lines, area-guide updates and the baseline sections 1–2, 5–8 from the plan's `DOCS:` and the final facts. Does not edit code in this phase.
- **Tester:** supplies, in its report, the test files and counts, commands, removed markers, tests not shown to fail without the fix, and native checks run or missing; these feed baseline sections 2–4 and are not summarized away.
- **Refuter / reviewer:** its verdicts, fixes applied and accepted limits become Accepted points or Gaps. A finding it proves false corrects the earlier record (done-mark, number, "verified" claim) in place.
- **Planner:** lists, before implementation, every record the unit owes (path, section, format, trigger) from this section; an unstated place is `none stated` and goes to the user.
- **All:** a missing or ambiguous place is reported as a `DOC-GAP`, not guessed; the final report lists each document written with its path.

### Milestone acceptance checklist

An **accepted point** is a boundary knowingly accepted, recorded as a decision or in the step's Limits; it does not block a check. Limits record only what a step's evidence cannot prove or bound; requirements belong in Accept/Validate (where an unmet one is a gap) and open choices in Decisions. A **gap** is an acceptance or validation requirement that was not met; it blocks a check.

- **Ledger entry `[x]`:** its batch Accept criteria pass with a test that fails when the old behavior is restored; every validation the batch names was run (native or hardware where required, otherwise disposable-store tests); the Evidence column links the proof; the Gaps column is empty (Accepted points may remain). PARTIAL, UNVERIFIED and DEFERRED stay `[ ]`.
- **Batch `[x]`:** every ledger entry it owns is `[x]`, its Decisions are recorded and its Depends contracts were delivered.
- **Milestone `[x]`:** all its batches are `[x]`, the "Done:" line under its objective is met including the integration evidence the map assigns to it, and every conditional or excluded obligation has an explicit disposition.

Documents record the two separately: **Accepted points** and **Gaps** columns or sections, never a mixed "limitations" list. Record evidence in the log.

- [ ] M00A — measurement delivered; validation partial; [evidence/acceptance matrix](../baselines/2026-11-11-m00.md). **Accepted points:** downstream D9/planning/list defects belong to M10/M11. **Gaps:** independent fresh/local labels absent.
- [ ] M00B — implemented; [evidence](../baselines/2026-11-11-m00.md). **Accepted points:** current-boundary fake/native plumbing only. **Gaps:** independently corrected real samples unavailable; M08 final parity remains.
- [ ] M00C — behavior/ownership checks delivered; [evidence](../baselines/2026-11-11-m00.md). **Accepted points:** owned-window occlusion only; no general capture-quality claim. **Gaps:** French OCR/SAPI and real UIA evidence missing; native COM timeout observed (M07).
- [ ] M01 — implemented; [evidence/acceptance matrix](../baselines/2026-11-11-m01.md). #16/#17/#19/#20/#24-#28 validated with writer-spy tests, negative controls and native checks; **Accepted points:** accepted background-tab gap unchanged. **Gaps:** #18 real media-session end to end; #148 Firefox/Zen/Brave native layouts.
- [ ] M02 — implemented; [evidence](../baselines/2026-11-12-m02.md). Migrations 0020-0021, transaction, retention, delete, media-name and keyfile changes validated on disposable stores (full suite 1535 passed/146 xfailed/18 deselected). **Accepted points:** DST synthetic only. **Gaps:** drive-qualified media path is not counted as a bad path (#56, #73).
- [ ] M03A — UNVERIFIED; evidence / remaining / deferrals: —
- [ ] M03B — UNVERIFIED; evidence / remaining / deferrals: —
- [ ] M04 — UNVERIFIED; evidence / remaining / deferrals: —
- [ ] M05 — UNVERIFIED; evidence / remaining / deferrals: —
- [ ] M06A — UNVERIFIED; evidence / remaining / deferrals: —
- [ ] M06B — UNVERIFIED; evidence / remaining / deferrals: —
- [ ] M07A — UNVERIFIED; evidence / remaining / deferrals: —
- [ ] M07B — UNVERIFIED; evidence / remaining / deferrals: —
- [ ] M08 — UNVERIFIED; evidence / remaining / deferrals: —
- [ ] M09 — UNVERIFIED; evidence / remaining / deferrals: —
- [ ] M10 — UNVERIFIED; evidence / remaining / deferrals: —
- [ ] M11 — UNVERIFIED; evidence / remaining / deferrals: —
- [ ] M12 — UNVERIFIED; evidence / remaining / deferrals: —
- [ ] M13A — UNVERIFIED; evidence / remaining / deferrals: —
- [ ] M13B — UNVERIFIED; evidence / remaining / deferrals: —
- [ ] M14A — UNVERIFIED; evidence / remaining / deferrals: —
- [ ] M14B — UNVERIFIED; evidence / remaining / deferrals: —
- [ ] M15A — UNVERIFIED; evidence / remaining / deferrals: —
- [ ] M15B — UNVERIFIED; evidence / remaining / deferrals: —
- [ ] M15C — UNVERIFIED; evidence / remaining / deferrals: —
- [ ] M16 — UNVERIFIED; evidence / remaining / deferrals: —
- [ ] M17 — UNVERIFIED; evidence / remaining / deferrals: —
- [ ] M18 — UNVERIFIED; evidence / remaining / deferrals: —
- [ ] M19 — UNVERIFIED; evidence / remaining / deferrals: —
- [ ] M20 — UNVERIFIED; evidence / remaining / deferrals: —
- [ ] M21 — UNVERIFIED; evidence / remaining / deferrals: —

Each batch/milestone starts UNVERIFIED. Complete a milestone only when required batches meet criteria and every conditional/excluded obligation has explicit disposition. Deferred does not mean implemented; retain unresolved original targets. No aggregate score closes partial findings.

Copy this table for EVERY milestone, one column per batch:

| Field | Batch | Milestone |
| --- | --- | --- |
| Scope | findings | all |
| Files | files and migrations | as listed |
| Tests | commits, test files, commands, native checks, results | full-run result |
| Status | UNVERIFIED → investigating / implementing / validated / deferred / excluded | same |
| Decisions | authority, date, variant, rejected alternatives, reason | — |
| Accepted points | boundaries knowingly accepted; they do not block a check | — |
| Gaps | unmet acceptance or validation, remaining work, deferrals (reason, consequence, owner, revisit trigger); they block a check | — |

Cross-batch findings require all obligations: #133 trim metadata M08.1, Route A M09.2 and refresh M06.3; seam test alone cannot close it. #175 standalone/query embedding required even if C6 deferred. #90 records preservation, not event removal.

### M00 execution records — 2026-11-11

Base: M00 base. Existing repairs were confirmed against current code, not rewritten. Full obligation matrix, commands, hashes, licences, native limits and remaining owners: [M00 evidence](../baselines/2026-11-11-m00.md).

| Batch | Findings | Files | Status | Accepted points | Gaps |
| --- | --- | --- | --- | --- | --- |
| M00.1 | #113–#116 | Ask scorers, production indexing in all three entrypoints, target clock/order matrix | Validated | Synthetic sources; no OCR/ASR/entailment proof | — |
| M00.2 | #99, #118, #123–#127 | shared encrypted store/server owners, split and language identity, candidate/definition hashes, D9 cases, real builder/backlog fixture | PARTIAL | Reused fresh sets are regression sets | Independent fresh/local labels and call corrections absent (explicit user-selected clips, M20); five D9 boundary failures remain strict xfails (M11) |
| M00.3 | #117, #119–#122 | comparison API/UI and shared processing (empty reference, read-only draft, provenance, condition tags, failure denominators, WER/CER/terms, parity) | PARTIAL | JS tested through DOM/API ports; numerals normalized 0–99 only | No independently corrected real clips; M08 owns final parity |
| M00.4 | #75–#83, #85–#88 | platform/backend regression tests, native French SAPI and owned-window checks | PARTIAL | Occlusion checked on owned synthetic windows only | French OCR pack and SAPI voice absent; COM reader exceeded 5 s (M07) |
| M00.5 | #84, #89 | worker/speaker migration test fixtures | Validated | File release shown by the native suite, not test count | — |
| M00 | M00A (.1–.2), M00B (.3), M00C (.4–.5) | as listed | Delivered; full run 1206 passed/181 xfailed | — | Per-batch gaps above |

### M01 execution records — 2026-11-11

Base: M01 base (M00 delivered). Evidence, commands, native checks, limitations and downstream owners: [M01 evidence](../baselines/2026-11-11-m01.md).

| Batch | Findings | Files | Status | Accepted points | Gaps |
| --- | --- | --- | --- | --- | --- |
| M01.1 | #16–#20, #27 | `capture/screen.py`, `capture/media.py`, `capture/exclusions.py`, `platform/win/uia.py` | PARTIAL | Firefox omnibox not run natively; real UI Automation failure not injectable; accepted background-tab gap; the conservative rule may split one video into several rows (M05/M07A) | #18: no real playing browser tab through Windows media sessions; next native pass / M05 |
| M01.2 | #24–#26, #28 | `capture/audio.py`, `platform/win/audio.py`, `platform/win/windows.py`, `platform/win/media.py`, `capture/exclusions.py` | Validated | No hosted password-manager app installed; audible processes with unresolvable names not named (reporting: M07.3) | — |
| M01.3 | #148 (D2) | `capture/exclusions.py`, `capture/media.py` | PARTIAL | An Edge page titled only with a marker word and no profile segment is ambiguous and kept | #148: Firefox/Zen/Brave private layouts not validated natively (Zen is the primary browser); next native pass |
| M01 | all | as listed | Implemented; full run 1441 passed/158 xfailed/18 deselected | — | #18, #148 (see batches) |

### M02 execution records — 2026-11-12

Base: M02 base (M01 delivered). Evidence, decisions, benchmark and limitations: [M02 evidence](../baselines/2026-11-12-m02.md).

| Batch | Findings | Files | Status | Accepted points | Gaps |
| --- | --- | --- | --- | --- | --- |
| M02.1 | #10, #44, #49, #56, #73 | `storage/db.py`, `retention.py`, `media.py`, migration 0020, `api/settings.py` | PARTIAL | 409 check covers screenshots and raw_audio only | Drive-qualified reference such as `C:/outside.webp` resolves inside the media dir on a same-drive store and is not counted as a bad path (#56, #73) |
| M02.2 | #57, #62 (D3) | migration 0021, `pipeline/ask.py`, `api/server.py`, `api/settings.py` | Validated | Not secure erasure; digests contain no frame text | — |
| M02.3 | #52 | `storage/frames.py`, `audio.py`, `media.py` | Validated | DST synthetic only | — |
| M02.4 | #55 | `storage/crypto.py` | Validated | Backend and `change_passphrase` untouched | — |
| M02 | all | as listed | Implemented; full run 1535 passed/146 xfailed/18 deselected | — | M02.1 gap |

Run applicable native checks in TOOLING (ruff format/check, strict pyright, pytest fresh workspace basetemp; appropriate hardware/manual work). Keep actual commands/results/skips. Read current harness interfaces rather than copying obsolete prototype venv paths. Frozen prototypes under eval are outside lint bar and not drop-in patches. Never weaken network guard or tune on known holdout. Final integration exercises both target questions alone/in both thread orders, FR/EN/mixed, after midnight, >5 videos, backlog, deletion/retry and uncertain authors. Capture/STT/retrieval/support/resources/clean-machine packaging remain distinct evidence boundaries.

[app-run]: ../solutions/2026-11-10%20-%20App%20Run%20Single%20Pass/report.md
[conversation]: ../solutions/2026-11-10%20-%20Conversation%20Clustering%20Sweep/report.md
[scan]: ../solutions/2026-11-10%20-%20Exact%20Vector%20Top-K%20Scan/report.md
[reuse]: ../solutions/2026-11-10%20-%20Identical%20Frame%20Text%20Reuse/report.md
[window-scan]: ../solutions/2026-11-10%20-%20Windowed%20Episode%20Vector%20Scan/report.md
[hallucination]: ../solutions/2026-11-10%20-%20Transcript%20Hallucination%20Rejection/report.md
[speaker]: ../solutions/2026-11-10%20-%20Segment%20Speaker%20Attribution/report.md
[follow-up]: ../solutions/2026-11-10%20-%20Thread%20Follow-up%20Field%20Inheritance/report.md
[time]: ../solutions/2026-11-10%20-%20Conversational%20Time%20Windows/report.md

## Finding coverage ledger

One primary batch owns each numbered finding; related batches are additional obligations, not duplicate ownership. Each row is independently checkable. All statuses start UNVERIFIED. Complete the evidence/remaining/deferral cell with links to batch logs; a checked disposition can mean explicitly excluded/deferred, never silently implemented. #59 was never assigned and is not a missing finding.

| Check / audit entry | Historical severity | Primary batch | Related obligations | Disposition | Status | Evidence | Accepted points | Gaps |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| [ ] [#1](2026-11-10.md#architecture) | SHOULD | M14.1 | — | R/K | UNVERIFIED | — | — | — |
| [ ] [#2](2026-11-10.md#architecture) | SHOULD | M14.1 | — | R | UNVERIFIED | — | — | — |
| [ ] [#3](2026-11-10.md#architecture) | SHOULD | M06.2 | M06.3 | R/K | UNVERIFIED | — | — | — |
| [ ] [#4](2026-11-10.md#architecture) | SHOULD | M14.1 | — | R | UNVERIFIED | — | — | — |
| [ ] [#5](2026-11-10.md#architecture) | SHOULD | M14.1 | — | R | UNVERIFIED | — | — | — |
| [ ] [#6](2026-11-10.md#architecture) | SHOULD | M14.1 | — | R | UNVERIFIED | — | — | — |
| [ ] [#7](2026-11-10.md#architecture) | SHOULD | M14.2 | — | R | UNVERIFIED | — | — | — |
| [ ] [#8](2026-11-10.md#architecture) | SHOULD | M14.2 | — | R | UNVERIFIED | — | — | — |
| [ ] [#9](2026-11-10.md#architecture) | SHOULD | M14.2 | — | R | UNVERIFIED | — | — | — |
| [x] [#10](2026-11-10.md#architecture) | SHOULD | M02.1 | M12.2, M07.3 | R | VALIDATED | [M02 evidence](../baselines/2026-11-12-m02.md); self-FK deletion cost removed by index 0020 (50k-row chain: 75 s and a locked concurrent insert before, 0.59 s after); tests in `tests/test_m02_*.py`, `tests/test_frame_text_copies.py`, `tests/test_defects_*.py`; M12.2 and M07.3 inherit the index | — | — |
| [ ] [#11](2026-11-10.md#architecture) | SHOULD | M05.1 | M01.2, M10.3 | R | UNVERIFIED | — | — | — |
| [ ] [#12](2026-11-10.md#architecture) | SHOULD | M14.2 | — | R | UNVERIFIED | — | — | — |
| [ ] [#13](2026-11-10.md#architecture) | SHOULD | M13.2 | — | R | UNVERIFIED | — | — | — |
| [ ] [#14](2026-11-10.md#architecture) | SHOULD | M14.4 | — | R | UNVERIFIED | — | — | — |
| [ ] [#15](2026-11-10.md#bugs-capture-and-platform) | MUST | M07.1 | — | R | UNVERIFIED | — | — | — |
| [x] [#16](2026-11-10.md#bugs-capture-and-platform) | MUST | M01.1 | — | R | VALIDATED | [M01 evidence](../baselines/2026-11-11-m01.md); 3 monitors (96/120 DPI) native spanning-window check | — | — |
| [x] [#17](2026-11-10.md#bugs-capture-and-platform) | MUST | M01.1 | — | R | VALIDATED | [M01 evidence](../baselines/2026-11-11-m01.md); native real password states | Failure injection is fake-only | — |
| [ ] [#18](2026-11-10.md#bugs-capture-and-platform) | MUST | M01.1 | — | R | PARTIAL | [M01 evidence](../baselines/2026-11-11-m01.md); verdict contract validated with writer spy | Accepted background-tab gap remains (Windows does not say which tab publishes a media session) | Real media-session end to end unavailable; next native pass / M05, 2026-11-11 |
| [x] [#19](2026-11-10.md#bugs-capture-and-platform) | SHOULD | M01.1 | — | R | VALIDATED | [M01 evidence](../baselines/2026-11-11-m01.md) | — | — |
| [x] [#20](2026-11-10.md#bugs-capture-and-platform) | SHOULD | M01.1 | — | R | VALIDATED | [M01 evidence](../baselines/2026-11-11-m01.md); native Edge/Chrome omnibox | Firefox/Zen omnibox unavailable; Firefox/Zen native: next native pass, 2026-11-11 | — |
| [ ] [#21](2026-11-10.md#bugs-capture-and-platform) | SHOULD | M07.1 | — | R | UNVERIFIED | — | — | — |
| [ ] [#22](2026-11-10.md#bugs-capture-and-platform) | SHOULD | M07.1 | — | R | UNVERIFIED | — | — | — |
| [ ] [#23](2026-11-10.md#bugs-capture-and-platform) | SHOULD | M07.1 | — | R | UNVERIFIED | — | — | — |
| [x] [#24](2026-11-10.md#bugs-capture-and-platform) | SHOULD | M01.2 | M07.3 | R | VALIDATED | [M01 evidence](../baselines/2026-11-11-m01.md); native endpoint enumeration and forced COM error | Reporting of unknown spans: M07.3, 2026-11-11 | — |
| [x] [#25](2026-11-10.md#bugs-capture-and-platform) | SHOULD | M01.2 | — | R | VALIDATED | [M01 evidence](../baselines/2026-11-11-m01.md); native Calculator resolves to its own process | Hosted password-manager case covered by synthetic titles only | — |
| [x] [#26](2026-11-10.md#bugs-capture-and-platform) | SHOULD | M01.2 | — | R | VALIDATED | [M01 evidence](../baselines/2026-11-11-m01.md); includes the `.bnpparibas` baseline gap | — | — |
| [x] [#27](2026-11-10.md#bugs-capture-and-platform) | SHOULD | M01.1 | — | R | VALIDATED | [M01 evidence](../baselines/2026-11-11-m01.md); native check exercised only the unknown-port loopback fallback | Port-specific path is fake-only (also via `run_capture` wiring) | — |
| [x] [#28](2026-11-10.md#bugs-capture-and-platform) | SHOULD | M01.2 | — | R | VALIDATED | [M01 evidence](../baselines/2026-11-11-m01.md); native listing succeeds/misses | Failing listing not injectable | — |
| [ ] [#29](2026-11-10.md#bugs-capture-and-platform) | SHOULD | M07.1 | — | R | UNVERIFIED | — | — | — |
| [ ] [#30](2026-11-10.md#bugs-pipeline) | MUST | M03.3 | M04.1 | R | UNVERIFIED | — | — | — |
| [ ] [#31](2026-11-10.md#bugs-pipeline) | MUST | M04.1 | M05.3 | R | UNVERIFIED | — | — | — |
| [ ] [#32](2026-11-10.md#bugs-pipeline) | MUST | M06.0 | M06.3 | R | UNVERIFIED | — | — | — |
| [ ] [#33](2026-11-10.md#bugs-pipeline) | MUST | M03.2 | — | R | UNVERIFIED | — | — | — |
| [ ] [#34](2026-11-10.md#bugs-pipeline) | MUST | M13.3 | M12.2 | R | UNVERIFIED | — | — | — |
| [ ] [#35](2026-11-10.md#bugs-pipeline) | MUST | M03.3 | — | R | UNVERIFIED | — | — | — |
| [ ] [#36](2026-11-10.md#bugs-pipeline) | SHOULD | M04.2 | M06.3 | R | UNVERIFIED | — | — | — |
| [ ] [#37](2026-11-10.md#bugs-pipeline) | SHOULD | M13.3 | — | R | UNVERIFIED | — | — | — |
| [ ] [#38](2026-11-10.md#bugs-pipeline) | SHOULD | M13.3 | — | R | UNVERIFIED | — | — | — |
| [ ] [#39](2026-11-10.md#bugs-pipeline) | SHOULD | M03.3 | — | R | UNVERIFIED | — | — | — |
| [ ] [#40](2026-11-10.md#bugs-pipeline) | SHOULD | M03.3 | — | R | UNVERIFIED | — | — | — |
| [ ] [#41](2026-11-10.md#bugs-pipeline) | SHOULD | M03.3 | — | R | UNVERIFIED | — | — | — |
| [ ] [#42](2026-11-10.md#bugs-pipeline) | SHOULD | M13.3 | — | R | UNVERIFIED | — | — | — |
| [ ] [#43](2026-11-10.md#bugs-pipeline) | SHOULD | M08.3 | — | R | UNVERIFIED | — | — | — |
| [x] [#44](2026-11-10.md#bugs-search-and-storage) | MUST | M02.1 | — | R | VALIDATED | [M02 evidence](../baselines/2026-11-12-m02.md); index `audio_segments_continues` (migration 0020), fresh and upgraded stores; benchmark in evidence; tests in `tests/test_m02_*.py`, `tests/test_frame_text_copies.py`, `tests/test_defects_*.py` | — | — |
| [ ] [#45](2026-11-10.md#bugs-search-and-storage) | MUST | M10.2 | — | R | UNVERIFIED | — | — | — |
| [ ] [#46](2026-11-10.md#bugs-search-and-storage) | MUST | M06.2 | M06.3, M09.2 | R | UNVERIFIED | — | — | — |
| [ ] [#47](2026-11-10.md#bugs-search-and-storage) | SHOULD | M12.1 | — | R/K | UNVERIFIED | — | — | — |
| [ ] [#48](2026-11-10.md#bugs-search-and-storage) | SHOULD | M12.3 | M13.3 | R | UNVERIFIED | — | — | — |
| [x] [#49](2026-11-10.md#bugs-search-and-storage) | SHOULD | M02.1 | — | R | VALIDATED | [M02 evidence](../baselines/2026-11-12-m02.md); `transaction()` re-raises the original error after an automatic rollback and after a failed COMMIT, leaving no open transaction; tests in `tests/test_m02_*.py`, `tests/test_frame_text_copies.py`, `tests/test_defects_*.py` | — | — |
| [ ] [#50](2026-11-10.md#bugs-search-and-storage) | SHOULD | M10.3 | — | R | UNVERIFIED | — | — | — |
| [ ] [#51](2026-11-10.md#bugs-search-and-storage) | SHOULD | M13.2 | — | R | UNVERIFIED | — | — | — |
| [x] [#52](2026-11-10.md#bugs-search-and-storage) | SHOULD | M02.3 | — | R | VALIDATED | [M02 evidence](../baselines/2026-11-12-m02.md); names carry UTC `ts_ms`, folder is local date, `MediaStore.write_unique` never overwrites; tests in `tests/test_m02_*.py`, `tests/test_frame_text_copies.py`, `tests/test_defects_*.py` | DST checked synthetically only | — |
| [ ] [#53](2026-11-10.md#bugs-search-and-storage) | SHOULD | M11.2 | — | R | UNVERIFIED | — | — | — |
| [ ] [#54](2026-11-10.md#bugs-search-and-storage) | SHOULD | M05.1 | M10.3 | R | UNVERIFIED | — | — | — |
| [x] [#55](2026-11-10.md#bugs-search-and-storage) | SHOULD | M02.4 | — | R | VALIDATED | [M02 evidence](../baselines/2026-11-12-m02.md); ceilings 1 GiB / 10 / 16 checked before derivation (decision recorded); change_passphrase untouched (#91); tests in `tests/test_m02_*.py`, `tests/test_frame_text_copies.py`, `tests/test_defects_*.py` | — | — |
| [ ] [#56](2026-11-10.md#bugs-search-and-storage) | SHOULD | M02.1 | — | R | PARTIAL | [M02 evidence](../baselines/2026-11-12-m02.md); bad or outside-store media path skipped, reference kept, counted as `bad_media_paths`; locked files as `locked_media_files`; tests in `tests/test_m02_*.py`, `tests/test_frame_text_copies.py`, `tests/test_defects_*.py` | — | Drive-qualified reference such as `C:/outside.webp` resolves inside the media dir on a same-drive store and is not counted as a bad path |
| [x] [#57](2026-11-10.md#bugs-search-and-storage) | SHOULD | M02.2 | M11.3, M12.2 | R/K D3 propagation | VALIDATED | [M02 evidence](../baselines/2026-11-12-m02.md); migration 0021 `question_frames` and triggers (D3 propagation); legacy provenance invalidated once on upgrade; tests in `tests/test_m02_*.py`, `tests/test_frame_text_copies.py`, `tests/test_defects_*.py` | — | — |
| [ ] [#58](2026-11-10.md#bugs-search-and-storage) | SHOULD | M13.2 | — | R | UNVERIFIED | — | — | — |
| [ ] [#60](2026-11-10.md#bugs-app-shell-api-ui) | MUST | M03.1 | — | R | UNVERIFIED | — | — | — |
| [ ] [#61](2026-11-10.md#bugs-app-shell-api-ui) | MUST | M13.1 | — | R | UNVERIFIED | — | — | — |
| [x] [#62](2026-11-10.md#bugs-app-shell-api-ui) | SHOULD | M02.2 | M13.2 | R | VALIDATED | [M02 evidence](../baselines/2026-11-12-m02.md); thumbnail cache keyed (frame id, image path, width) and cleared on per-category screenshot/screen-text delete; tests in `tests/test_m02_*.py`, `tests/test_frame_text_copies.py`, `tests/test_defects_*.py` | — | — |
| [ ] [#63](2026-11-10.md#bugs-app-shell-api-ui) | SHOULD | M13.2 | — | R | UNVERIFIED | — | — | — |
| [ ] [#64](2026-11-10.md#bugs-app-shell-api-ui) | SHOULD | M13.1 | — | R | UNVERIFIED | — | — | — |
| [ ] [#65](2026-11-10.md#bugs-app-shell-api-ui) | SHOULD | M13.1 | — | R | UNVERIFIED | — | — | — |
| [ ] [#66](2026-11-10.md#bugs-app-shell-api-ui) | SHOULD | M14.3 | — | R | UNVERIFIED | — | — | — |
| [ ] [#67](2026-11-10.md#bugs-app-shell-api-ui) | SHOULD | M03.1 | — | R | UNVERIFIED | — | — | — |
| [ ] [#68](2026-11-10.md#bugs-app-shell-api-ui) | SHOULD | M03.1 | — | R | UNVERIFIED | — | — | — |
| [ ] [#69](2026-11-10.md#bugs-app-shell-api-ui) | SHOULD | M03.1 | — | R | UNVERIFIED | — | — | — |
| [ ] [#70](2026-11-10.md#bugs-app-shell-api-ui) | SHOULD | M14.3 | — | R | UNVERIFIED | — | — | — |
| [ ] [#71](2026-11-10.md#bugs-app-shell-api-ui) | SHOULD | M14.3 | — | R | UNVERIFIED | — | — | — |
| [ ] [#72](2026-11-10.md#bugs-app-shell-api-ui) | SHOULD | M03.1 | — | R | UNVERIFIED | — | — | — |
| [ ] [#73](2026-11-10.md#bugs-app-shell-api-ui) | SHOULD | M02.1 | — | R | PARTIAL | [M02 evidence](../baselines/2026-11-12-m02.md); per-category delete returns 409 with delete-all guidance on OSError/CryptoError or kept expirable references; tests in `tests/test_m02_*.py`, `tests/test_frame_text_copies.py`, `tests/test_defects_*.py` | — | Same drive-qualified reference is not counted by the per-category 409 check |
| [ ] [#74](2026-11-10.md#bugs-app-shell-api-ui) | SHOULD | M03.1 | — | R | UNVERIFIED | — | — | — |
| [x] [#75](2026-11-10.md#tests) | MUST | M00.4 | — | R | VALIDATED measurement/test infrastructure | [M00 execution/evidence](../baselines/2026-11-11-m00.md) | Listed in the M00 evidence | — |
| [x] [#76](2026-11-10.md#tests) | MUST | M00.4 | — | R | VALIDATED measurement/test infrastructure | [M00 execution/evidence](../baselines/2026-11-11-m00.md) | Listed in the M00 evidence | — |
| [x] [#77](2026-11-10.md#tests) | MUST | M00.4 | — | R | VALIDATED measurement/test infrastructure | [M00 execution/evidence](../baselines/2026-11-11-m00.md) | Listed in the M00 evidence | — |
| [ ] [#78](2026-11-10.md#tests) | MUST | M00.4 | — | R | PARTIAL | [M00 execution/evidence](../baselines/2026-11-11-m00.md) | — | French OCR pack/SAPI voice absent; COM reader exceeded 5 s (M07); French native evidence awaits installed pack/voice; COM repair to M07, 2026-11-11 |
| [x] [#79](2026-11-10.md#tests) | SHOULD | M00.4 | — | R | VALIDATED measurement/test infrastructure | [M00 execution/evidence](../baselines/2026-11-11-m00.md) | Listed in the M00 evidence | — |
| [x] [#80](2026-11-10.md#tests) | SHOULD | M00.4 | — | R | VALIDATED measurement/test infrastructure | [M00 execution/evidence](../baselines/2026-11-11-m00.md) | Listed in the M00 evidence | — |
| [x] [#81](2026-11-10.md#tests) | SHOULD | M00.4 | — | R | VALIDATED measurement/test infrastructure | [M00 execution/evidence](../baselines/2026-11-11-m00.md) | Listed in the M00 evidence | — |
| [x] [#82](2026-11-10.md#tests) | SHOULD | M00.4 | — | R | VALIDATED measurement/test infrastructure | [M00 execution/evidence](../baselines/2026-11-11-m00.md) | Listed in the M00 evidence | — |
| [x] [#83](2026-11-10.md#tests) | SHOULD | M00.4 | — | R | VALIDATED measurement/test infrastructure | [M00 execution/evidence](../baselines/2026-11-11-m00.md) | Listed in the M00 evidence | — |
| [x] [#84](2026-11-10.md#tests) | SHOULD | M00.5 | — | R | VALIDATED measurement/test infrastructure | [M00 execution/evidence](../baselines/2026-11-11-m00.md) | Listed in the M00 evidence | — |
| [x] [#85](2026-11-10.md#tests) | SHOULD | M00.4 | — | R | VALIDATED measurement/test infrastructure | [M00 execution/evidence](../baselines/2026-11-11-m00.md) | Listed in the M00 evidence | — |
| [x] [#86](2026-11-10.md#tests) | SHOULD | M00.4 | — | R | VALIDATED measurement/test infrastructure | [M00 execution/evidence](../baselines/2026-11-11-m00.md) | Listed in the M00 evidence | — |
| [x] [#87](2026-11-10.md#tests) | SHOULD | M00.4 | — | R | VALIDATED measurement/test infrastructure | [M00 execution/evidence](../baselines/2026-11-11-m00.md) | Listed in the M00 evidence | — |
| [ ] [#88](2026-11-10.md#tests) | SHOULD | M00.4 | — | R | PARTIAL | [M00 execution/evidence](../baselines/2026-11-11-m00.md) | — | French OCR pack/SAPI voice absent; COM reader exceeded 5 s (M07); French native evidence awaits installed pack/voice; COM repair to M07, 2026-11-11 |
| [x] [#89](2026-11-10.md#tests) | SHOULD | M00.5 | — | R | VALIDATED measurement/test infrastructure | [M00 execution/evidence](../baselines/2026-11-11-m00.md) | Listed in the M00 evidence | — |
| [ ] [#90](2026-11-10.md#creep-redundancy) | SHOULD | M05.2 | M14.4 | K keep events; X bundled removal; C FTS only | UNVERIFIED | — | — | — |
| [ ] [#91](2026-11-10.md#creep-redundancy) | SHOULD | M14.4 | — | R/K | UNVERIFIED | — | — | — |
| [ ] [#92](2026-11-10.md#creep-redundancy) | SHOULD | M14.4 | — | R/K | UNVERIFIED | — | — | — |
| [ ] [#93](2026-11-10.md#creep-redundancy) | SHOULD | M14.4 | — | R | UNVERIFIED | — | — | — |
| [ ] [#94](2026-11-10.md#creep-redundancy) | SHOULD | M14.2 | — | R | UNVERIFIED | — | — | — |
| [ ] [#95](2026-11-10.md#creep-redundancy) | SHOULD | M14.3 | — | R | UNVERIFIED | — | — | — |
| [ ] [#96](2026-11-10.md#creep-redundancy) | SHOULD | M14.4 | — | R | UNVERIFIED | — | — | — |
| [ ] [#97](2026-11-10.md#creep-redundancy) | SHOULD | M14.2 | — | R | UNVERIFIED | — | — | — |
| [ ] [#98](2026-11-10.md#creep-redundancy) | SHOULD | M14.2 | — | R | UNVERIFIED | — | — | — |
| [x] [#99](2026-11-10.md#creep-redundancy) | SHOULD | M00.2 | M14.2 | R | VALIDATED measurement/test infrastructure | [M00 execution/evidence](../baselines/2026-11-11-m00.md) | Listed in the M00 evidence | — |
| [ ] [#100](2026-11-10.md#approaches) | SHOULD | M12.1 | M16.2 | R | UNVERIFIED | — | — | — |
| [ ] [#101](2026-11-10.md#approaches) | SHOULD | M12.2 | M13.3 | R | UNVERIFIED | — | — | — |
| [ ] [#102](2026-11-10.md#approaches) | SHOULD | M04.2 | — | R | UNVERIFIED | — | — | — |
| [ ] [#103](2026-11-10.md#approaches) | SHOULD | M04.3 | — | R | UNVERIFIED | — | — | — |
| [ ] [#104](2026-11-10.md#approaches) | SHOULD | M12.3 | M11.1 | R | UNVERIFIED | — | — | — |
| [ ] [#105](2026-11-10.md#alternatives-smarter-solutions-within-the-projects-intent) | CONFIRMED | M17.2 | — | C | UNVERIFIED | — | — | — |
| [ ] [#106](2026-11-10.md#alternatives-smarter-solutions-within-the-projects-intent) | CONFIRMED | M16.1 | M08.2, M09.2 | C | UNVERIFIED | — | — | — |
| [ ] [#107](2026-11-10.md#alternatives-smarter-solutions-within-the-projects-intent) | PARTIAL (corrected) | M15.1 | — | C | UNVERIFIED | — | — | — |
| [ ] [#108](2026-11-10.md#alternatives-smarter-solutions-within-the-projects-intent) | CONFIRMED | M15.2 | — | C | UNVERIFIED | — | — | — |
| [ ] [#109](2026-11-10.md#alternatives-smarter-solutions-within-the-projects-intent) | PARTIAL | M15.3 | — | C | UNVERIFIED | — | — | — |
| [ ] [#110](2026-11-10.md#alternatives-smarter-solutions-within-the-projects-intent) | PARTIAL | M18.1 | — | C | UNVERIFIED | — | — | — |
| [ ] [#111](2026-11-10.md#alternatives-smarter-solutions-within-the-projects-intent) | PARTIAL | M18.2 | — | C | UNVERIFIED | — | — | — |
| [ ] [#112](2026-11-10.md#alternatives-smarter-solutions-within-the-projects-intent) | PARTIAL | M17.3 | — | C | UNVERIFIED | — | — | — |
| [x] [#113](2026-11-10-2.md#evaluation-methodology) | MUST | M00.1 | M12.1 | R | VALIDATED measurement/test infrastructure | [M00 execution/evidence](../baselines/2026-11-11-m00.md) | Listed in the M00 evidence | — |
| [x] [#114](2026-11-10-2.md#evaluation-methodology) | MUST | M00.1 | — | R | VALIDATED measurement/test infrastructure | [M00 execution/evidence](../baselines/2026-11-11-m00.md) | Listed in the M00 evidence | — |
| [x] [#115](2026-11-10-2.md#evaluation-methodology) | MUST | M00.1 | — | R | VALIDATED measurement/test infrastructure | [M00 execution/evidence](../baselines/2026-11-11-m00.md) | Listed in the M00 evidence | — |
| [x] [#116](2026-11-10-2.md#evaluation-methodology) | MUST | M00.1 | — | R | VALIDATED measurement/test infrastructure | [M00 execution/evidence](../baselines/2026-11-11-m00.md) | Listed in the M00 evidence | — |
| [ ] [#117](2026-11-10-2.md#evaluation-methodology) | MUST | M00.3 | M18.2 | R | PARTIAL | [M00 execution/evidence](../baselines/2026-11-11-m00.md) | — | Fake/API validation only; no independently corrected real samples; M08 owns final parity; Corrected FR/EN/mixed/quiet/no-speech clips await explicit labelled material, 2026-11-11 |
| [ ] [#118](2026-11-10-2.md#evaluation-methodology) | MUST | M00.2 | — | R | PARTIAL | [M00 execution/evidence](../baselines/2026-11-11-m00.md) | — | Fixtures and metrics delivered; no independent fresh/local labels; User-labelled clips and new independent labels: M20 / future quality validation, 2026-11-11 |
| [x] [#119](2026-11-10-2.md#evaluation-methodology) | SHOULD | M00.3 | M08.3 final comparison | R | VALIDATED measurement/test infrastructure | [M00 execution/evidence](../baselines/2026-11-11-m00.md) | Listed in the M00 evidence | — |
| [x] [#120](2026-11-10-2.md#evaluation-methodology) | SHOULD | M00.3 | — | R | VALIDATED measurement/test infrastructure | [M00 execution/evidence](../baselines/2026-11-11-m00.md) | Listed in the M00 evidence | — |
| [x] [#121](2026-11-10-2.md#evaluation-methodology) | SHOULD | M00.3 | M08.1, M08.2, M08.3 final parity | R | VALIDATED measurement/test infrastructure | [M00 execution/evidence](../baselines/2026-11-11-m00.md) | Listed in the M00 evidence | — |
| [x] [#122](2026-11-10-2.md#evaluation-methodology) | SHOULD | M00.3 | — | R | VALIDATED measurement/test infrastructure | [M00 execution/evidence](../baselines/2026-11-11-m00.md) | Listed in the M00 evidence | — |
| [ ] [#123](2026-11-10-2.md#evaluation-methodology) | SHOULD | M00.2 | M09.1, M09.2, M20.1 | R fixtures; R/K D6 ongoing comparison | PARTIAL | [M00 execution/evidence](../baselines/2026-11-11-m00.md) | — | Fixtures and metrics delivered; no independent fresh/local labels; User-labelled clips and new independent labels: M20 / future quality validation, 2026-11-11 |
| [ ] [#124](2026-11-10-2.md#evaluation-methodology) | SHOULD | M00.2 | M03.4 | R | PARTIAL | [M00 execution/evidence](../baselines/2026-11-11-m00.md) | — | Fixtures and metrics delivered; no independent fresh/local labels; User-labelled clips and new independent labels: M20 / future quality validation, 2026-11-11 |
| [ ] [#125](2026-11-10-2.md#evaluation-methodology) | SHOULD | M00.2 | M11.3 | R/K D9 answer-evidence policy | PARTIAL | [M00 execution/evidence](../baselines/2026-11-11-m00.md) | — | Fixtures and metrics delivered; no independent fresh/local labels; User-labelled clips and new independent labels: M20 / future quality validation, 2026-11-11 |
| [x] [#126](2026-11-10-2.md#evaluation-methodology) | SHOULD | M00.2 | — | R | VALIDATED measurement/test infrastructure | [M00 execution/evidence](../baselines/2026-11-11-m00.md) | Listed in the M00 evidence | — |
| [x] [#127](2026-11-10-2.md#evaluation-methodology) | SHOULD | M00.2 | — | R | VALIDATED measurement/test infrastructure | [M00 execution/evidence](../baselines/2026-11-11-m00.md) | Listed in the M00 evidence | — |
| [ ] [#128](2026-11-10-2.md#speech) | MUST | M08.2 | M08.3, M11.2, M11.3 | R/K D5 retention; D9 answers | UNVERIFIED | — | — | — |
| [ ] [#129](2026-11-10-2.md#speech) | MUST | M08.2 | M08.3, M11.2, M11.3 | R/K D5 retention; D9 answers | UNVERIFIED | — | — | — |
| [ ] [#130](2026-11-10-2.md#speech) | MUST | M08.1 | M08.2 | R | UNVERIFIED | — | — | — |
| [ ] [#131](2026-11-10-2.md#speech) | MUST | M07.2 | M08.1, M09.2 | R | UNVERIFIED | — | — | — |
| [ ] [#132](2026-11-10-2.md#speech) | MUST (narrowed) | M09.2 | M16.1, M16.2, M16.3 | R interim; K D6 provisional use | UNVERIFIED | — | — | — |
| [ ] [#133](2026-11-10-2.md#speech) | SHOULD | M09.2 | M08.1, M06.3, M16.2 | R interim; K D6 provisional use | UNVERIFIED | — | — | — |
| [ ] [#134](2026-11-10-2.md#speech) | SHOULD | M09.1 | M09.2, M16.2 | R interim; K D6 provisional use | UNVERIFIED | — | — | — |
| [ ] [#135](2026-11-10-2.md#speech) | SHOULD | M09.1 | — | R | UNVERIFIED | — | — | — |
| [ ] [#136](2026-11-10-2.md#speech) | SHOULD | M09.1 | — | R verify; C change only if mismatch | UNVERIFIED | — | — | — |
| [ ] [#137](2026-11-10-2.md#speech) | SHOULD | M08.1 | M07.2, M09.2 | R | UNVERIFIED | — | — | — |
| [ ] [#138](2026-11-10-2.md#speech) | SHOULD | M08.2 | M08.3, M11.2, M11.3 | R/K D5 retention; D9 answers | UNVERIFIED | — | — | — |
| [ ] [#139](2026-11-10-2.md#speech) | SHOULD | M08.3 | M08.2 | R | UNVERIFIED | — | — | — |
| [ ] [#140](2026-11-10-2.md#speech) | SHOULD | M08.3 | M08.2 | R | UNVERIFIED | — | — | — |
| [ ] [#141](2026-11-10-2.md#speech) | SHOULD | M08.2 | M18.2, M11.2, M11.3 | R/K D5 retention; D9 answers | UNVERIFIED | — | — | — |
| [ ] [#142](2026-11-10-2.md#capture) | MUST (narrowed) | M05.2 | — | R | UNVERIFIED | — | — | — |
| [ ] [#143](2026-11-10-2.md#capture) | MUST | M07.3 | M11.3, M02.1 | R | UNVERIFIED | [M02 evidence](../baselines/2026-11-12-m02.md); related: M02.1 transaction and index delivered; Gap/recovery work stays with M07.3 | — | — |
| [ ] [#144](2026-11-10-2.md#capture) | MUST | M07.4 | — | R | UNVERIFIED | — | — | — |
| [ ] [#145](2026-11-10-2.md#capture) | MUST | M05.1 | M10.2 | R | UNVERIFIED | — | — | — |
| [ ] [#146](2026-11-10-2.md#capture) | SHOULD | M05.2 | — | R | UNVERIFIED | — | — | — |
| [ ] [#147](2026-11-10-2.md#capture) | SHOULD | M07.4 | — | R | UNVERIFIED | — | — | — |
| [ ] [#148](2026-11-10-2.md#capture) | SHOULD | M01.3 | M05.2 | R/K D2 fail-closed narrowing | PARTIAL | [M01 evidence](../baselines/2026-11-11-m01.md); Chrome private marker found only in the UI Automation name; feeds M05.2/M05.1 (#11) | — | Firefox/Zen/Brave native layouts unavailable; next native pass, 2026-11-11 |
| [ ] [#149](2026-11-10-2.md#capture) | SHOULD | M07.4 | M06.1 | R | UNVERIFIED | — | — | — |
| [ ] [#150](2026-11-10-2.md#capture) | SHOULD | M07.4 | — | R | UNVERIFIED | — | — | — |
| [ ] [#151](2026-11-10-2.md#capture) | SHOULD | M07.3 | — | R | UNVERIFIED | — | — | — |
| [ ] [#152](2026-11-10-2.md#capture) | SHOULD | M07.3 | — | R/K D7 preserve uncertain speech | UNVERIFIED | — | — | — |
| [ ] [#153](2026-11-10-2.md#capture) | SHOULD | M07.4 | M02.1, M13.3 | R | UNVERIFIED | [M02 evidence](../baselines/2026-11-12-m02.md); related: M02.1 transaction and index delivered | — | — |
| [ ] [#154](2026-11-10-2.md#reliability) | MUST (narrowed) | M03.2 | — | R | UNVERIFIED | — | — | — |
| [ ] [#155](2026-11-10-2.md#reliability) | MUST | M03.2 | — | R | UNVERIFIED | — | — | — |
| [ ] [#156](2026-11-10-2.md#reliability) | MUST | M03.2 | — | R | UNVERIFIED | — | — | — |
| [ ] [#157](2026-11-10-2.md#reliability) | MUST (narrowed) | M03.4 | M13.1 | R | UNVERIFIED | — | — | — |
| [ ] [#158](2026-11-10-2.md#reliability) | MUST | M07.1 | M03.4 | R | UNVERIFIED | — | — | — |
| [ ] [#159](2026-11-10-2.md#reliability) | SHOULD | M03.4 | M04.1 | R | UNVERIFIED | — | — | — |
| [ ] [#160](2026-11-10-2.md#reliability) | SHOULD | M07.2 | M03.1 | R | UNVERIFIED | — | — | — |
| [ ] [#161](2026-11-10-2.md#reliability) | SHOULD | M07.2 | M02.1 | R | UNVERIFIED | [M02 evidence](../baselines/2026-11-12-m02.md); related: M02.1 transaction rollback delivered | — | — |
| [ ] [#162](2026-11-10-2.md#reliability) | SHOULD | M03.4 | M07.1 | R | UNVERIFIED | — | — | — |
| [ ] [#163](2026-11-10-2.md#reliability) | SHOULD | M03.1 | — | R | UNVERIFIED | — | — | — |
| [ ] [#164](2026-11-10-2.md#memory-episodes) | MUST | M04.1 | M02.2, M06.3 | R | UNVERIFIED | [M02 evidence](../baselines/2026-11-12-m02.md); related: M02.2 frame-text copies invalidated; Episode foundation stays with M04.1 | — | — |
| [ ] [#165](2026-11-10-2.md#memory-episodes) | MUST | M05.1 | M10.3 | R | UNVERIFIED | — | — | — |
| [ ] [#166](2026-11-10-2.md#memory-episodes) | MUST | M05.1 | M05.3 | R | UNVERIFIED | — | — | — |
| [ ] [#167](2026-11-10-2.md#memory-episodes) | MUST | M05.2 | M05.3 | R | UNVERIFIED | — | — | — |
| [ ] [#168](2026-11-10-2.md#memory-episodes) | SHOULD | M05.3 | M11.2 | R | UNVERIFIED | — | — | — |
| [ ] [#169](2026-11-10-2.md#memory-episodes) | SHOULD | M06.3 | M08.1, M09.2 | R | UNVERIFIED | — | — | — |
| [ ] [#170](2026-11-10-2.md#memory-episodes) | SHOULD | M06.1 | — | R | UNVERIFIED | — | — | — |
| [ ] [#171](2026-11-10-2.md#memory-episodes) | SHOULD | M05.3 | — | R | UNVERIFIED | — | — | — |
| [ ] [#172](2026-11-10-2.md#memory-episodes) | SHOULD | M06.1 | — | R | UNVERIFIED | — | — | — |
| [ ] [#173](2026-11-10-2.md#memory-episodes) | SHOULD (recurring #31) | M05.3 | M06.3 | R | UNVERIFIED | — | — | — |
| [ ] [#174](2026-11-10-2.md#memory-episodes) | SHOULD | M06.2 | M06.3 | R | UNVERIFIED | — | — | — |
| [ ] [#175](2026-11-10-2.md#answer-accuracy) | MUST | M10.1 | M17.1 | R/K D4 conventions; C D8 model | UNVERIFIED | — | — | — |
| [ ] [#176](2026-11-10-2.md#answer-accuracy) | MUST (narrowed) | M11.2 | M06.2 | R/K D9 uncertainty provenance/answer support | UNVERIFIED | — | — | — |
| [ ] [#177](2026-11-10-2.md#answer-accuracy) | MUST | M11.2 | — | R | UNVERIFIED | — | — | — |
| [ ] [#178](2026-11-10-2.md#answer-accuracy) | MUST (narrowed) | M06.2 | M11.2 | R/K D9 uncertainty provenance/answer support | UNVERIFIED | — | — | — |
| [ ] [#179](2026-11-10-2.md#answer-accuracy) | MUST | M10.2 | M11.1, M17.1 | R/K D1 semantics; C D8 model | UNVERIFIED | — | — | — |
| [ ] [#180](2026-11-10-2.md#answer-accuracy) | MUST (narrowed) | M10.2 | M11.1, M17.1 | R/K D1 semantics; C D8 model | UNVERIFIED | — | — | — |
| [ ] [#181](2026-11-10-2.md#answer-accuracy) | SHOULD (recurring #45) | M10.2 | M11.1, M17.1 | R/K D1 semantics; C D8 model | UNVERIFIED | — | — | — |
| [ ] [#182](2026-11-10-2.md#answer-accuracy) | SHOULD (narrowed) | M10.3 | M11.2 | R | UNVERIFIED | — | — | — |
| [ ] [#183](2026-11-10-2.md#answer-accuracy) | SHOULD | M10.3 | — | R | UNVERIFIED | — | — | — |
| [ ] [#184](2026-11-10-2.md#answer-accuracy) | SHOULD | M10.3 | — | R | UNVERIFIED | — | — | — |
| [ ] [#185](2026-11-10-2.md#answer-accuracy) | SHOULD | M11.3 | M17.3 | R | UNVERIFIED | — | — | — |
| [ ] [#186](2026-11-10-2.md#answer-accuracy) | SHOULD | M11.3 | M17.3 | R/K D9 uncertainty provenance/answer support | UNVERIFIED | — | — | — |

Coverage validation at authoring: parsed both source audit tables; 185 distinct numbers, equal to {1..186} minus {59}; 185 unique primary assignments; no missing/extra IDs. Every related batch ID resolves to a batch heading. This validates document accounting only, not implementation.
