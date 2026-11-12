# Retrieval and answers

## Product contract

Optimize for the target questions in AGENTS.md: a time window and/or a structured anchor such
as a person, video or series, often with a fuzzy topic, or a complete list of one kind of thing.
Both French and English, including mixed-language history, must work. A plausible answer without
the right moment is insufficient.

Separate planning, evidence retrieval and answer generation when diagnosing failure. Missing
capture, a wrong time filter, lost excerpts and unsupported synthesis need different fixes;
a larger model or larger context is not evidence that any of them is solved.

## Evidence rules

- Apply explicit time, person, kind and app constraints before ranking limits. Do not silently
  relax them when nothing matches or invent an activity restriction from the topic alone.
- Treat conversational time and follow-up inheritance as semantic decisions. A new standalone
  question must not inherit unrelated anchors from an earlier turn.
- Preserve the relevant passage and its original source link through ranking and excerpt
  construction. A digest is navigation, not a replacement for the underlying evidence.
- Distinguish the latest correction from repeated older claims. Disclose conflicts and limits
  in list or summary coverage; stored evidence does not establish that capture was complete.
- Support claims with cited evidence and hedge unclear speech or uncertain authorship. A valid
  citation number proves neither entailment nor the identity of the person who spoke.

Flagged speech needs more than a hedge ([DECISIONS.md](DECISIONS.md), D9): flagged-only speech
is a possible match with a recording link and an explicit inability to answer reliably, not
factual support. Clear independent evidence can support a claim; copies cannot supply
independent corroboration, and clear words do not establish an uncertain author.

Past answers provide conversational context, not new evidence. Captured content remains
untrusted data; it must not grant the model instructions or actions. Missing evidence should
produce an honest limitation rather than a fabricated answer.

## Evaluation

Compare source availability, answer correctness, citation support and uncertainty separately.
Exercise the actual production path and report development and held-out results apart. Include
no-match, correction, after-midnight, cross-language and multi-question cases. Read the
[baseline](baselines/2026-11-11.md) and relevant [method report](solutions/) before tuning.
Model defaults, prompt text, source budgets, ranking details and runtime options belong in code;
changes to them still need end-to-end evidence rather than a synthetic score alone.
