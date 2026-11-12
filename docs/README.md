# Documentation map

The guides explain decisions, constraints and validation requirements. Read the implementation
and its tests to establish current behaviour before changing an area.

## Reading order

1. [AGENTS.md](../AGENTS.md): product scope and working agreement.
2. [DECISIONS.md](DECISIONS.md): the user's decisions and which ones the code does not follow yet.
3. The relevant guide below and [TOOLING.md](TOOLING.md).
4. The matching audit or method report, then the code and tests.

| Area | Guide |
| --- | --- |
| Process boundaries, source map and resource ownership | [ARCHITECTURE.md](ARCHITECTURE.md) |
| Screen, media, audio and exclusions | [CAPTURE.md](CAPTURE.md) |
| Transcription and speaker identity | [SPEECH.md](SPEECH.md) |
| Finding evidence and answering questions | [RETRIEVAL.md](RETRIEVAL.md) |
| Persistence, retention and deletion | [STORAGE.md](STORAGE.md) |
| Threat model and security boundaries | [SECURITY.md](SECURITY.md) |
| Distribution | [PACKAGING.md](PACKAGING.md) |
| Product interaction and visual direction | [UI.md](UI.md) |

## Evidence and maintenance

The [audit synthesis](audits/2026-11-11-synthesis/synthesis.md) explains the findings, and
[TODO.md](../TODO.md) assigns them to milestones.
[Audits](audits/), [method reports](solutions/) and [baselines](baselines/) are dated evidence,
not specifications of today's code. Use them for rationale and comparisons; a proposed method or
a passing synthetic evaluation does not establish daily-use quality.

Keep current guides focused on intent, important tradeoffs and boundaries a refactor must
preserve. Leave module inventories, schemas, constants, model pins, algorithms and UI mechanics
in code and tests. Keep measurements in dated evidence rather than repeating them across guides.
Record each new user decision in [DECISIONS.md](DECISIONS.md) and state its rule in the guide for
its area. Open work belongs to [TODO.md](../TODO.md), not the guides.

## Revision labels

Git history was restarted from a single commit on 2026-11-12. Dated evidence names the earlier
revisions it measured by label; those revisions are not kept in this repository.

- **pre-audit**: the code examined by the 2026-11-10 audits, the method reports and the synthesis.
- **2026-11-11 baseline**: the code measured in [baselines/2026-11-11.md](baselines/2026-11-11.md),
  before any audit fix.
- **M00 base**, **M01 base**, **M02 base**: the code each milestone started from; batches such as
  M01.2 name the changes that delivered them.
- **schema consolidation**: the 2026-11-12 change that merged the migrations into one schema file.
