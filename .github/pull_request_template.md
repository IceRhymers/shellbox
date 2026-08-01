<!--
This repo has a writing standard: docs/writing-style.md
A PR description is Tier 2. Active voice, sentences of 25 words or fewer, one topic per
paragraph, a list for 3 or more items, no emoji, and every reference resolvable from a clone.
Delete any section that does not apply. Do not delete the checklist.
-->

## What changed

<!-- One sentence. What a reader needs to know before reading the diff. -->

## Why

<!--
The reason, and the alternative you rejected with the reason you rejected it.
"Extend X" or "do nothing" is usually a real alternative worth naming.
-->

## What proves it

<!--
The test, the measurement, or the CI lane that would fail if this change were wrong.
Say whether a claim about behaviour was measured or assumed. If it was measured, name what
measured it and on which platform or version.
-->

## Checklist

- [ ] Sentences are 25 words or fewer, active voice, one topic per paragraph.
- [ ] No emoji in any tracked file. `CRITICAL:` / `WARNING:` / `NOTE:` instead.
- [ ] Every cross-reference **this PR adds** resolves from a clone alone: no new bare `§N`,
      and nothing newly cited from `.omc/`. See
      [`docs/plan-sections.md`](../docs/plan-sections.md), which resolves the identifiers
      already in the tree. Existing bare `§N` in `.py` files are a known backlog, not a
      blocker for an unrelated PR.
- [ ] A new domain term is defined in [`docs/glossary.md`](../docs/glossary.md), and an
      existing one is used the way that file defines it.
- [ ] A tool docstring, error message, or CLI string changed here follows Tier 1: active
      voice, simple tense, 20 words or fewer, and it lists its error codes.
- [ ] Every claim stated as measured was measured, with the version or platform named.
