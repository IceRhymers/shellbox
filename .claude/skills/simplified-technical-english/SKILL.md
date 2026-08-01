---
name: simplified-technical-english
description: "Applies shellbox's writing standard, adapted from ASD-STE100, to docs, comments, tool descriptions, error messages, PR descriptions, and specs. Two tiers: strict for text a machine parses, clarity for text that carries an argument. Use when writing or reviewing any prose in this repo; triggers: STE, simplify this, writing standard, style check."
version: 1.0.0
---

# Simplified Technical English for shellbox

ASD-STE100 is a controlled-language standard from the aerospace and defense industry. It
exists for a reader with no back-channel. A maintenance technician working from a manual
cannot call the author and ask "did you mean X or Y?" A misread instruction on an aircraft
kills people. So the standard removes the two largest sources of misreading: words with more
than one meaning, and sentences with more than one possible structure.

shellbox has a reader in that same position. It is an MCP server. An **agent** parses its
tool descriptions, its `INSTRUCTIONS` string, and its error payloads. That agent decides what
to call next on the strength of them, and it cannot ask a follow-up question.

The [upstream skill](https://github.com/danyuchn/asd-ste100-skill) this adapts makes exactly
that transfer. shellbox adopts it with one change. The standard here is **tiered**, because
this repo also contains prose whose job is to carry an argument.

The normative rules are in [`docs/writing-style.md`](../../../docs/writing-style.md). That
file governs; this skill is how an agent applies it.

## When to Use This Skill

- You are writing or editing an MCP tool docstring, an error message, CLI help, or the
  server `INSTRUCTIONS`. These are Tier 1 and the rules are strict.
- You are writing or editing a doc under `docs/`, the README, a code comment, a PR
  description, or a spec. These are Tier 2.
- You are reviewing a diff and want to check prose against the standard.
- Someone asks for a before/after showing which rule a sentence breaks.

Do not apply this to `spike/FINDINGS.md` or `probe/FINDINGS.md`. Those are dated records of
what a measurement found. Rewriting them edits the record. Fix a genuine ambiguity if you
find one, and leave the rest.

## The Two Tiers

Decide the tier from the reader, not from the file type.

### Tier 1 — Strict. The reader is a machine.

Applies to: MCP tool docstrings, the `INSTRUCTIONS` string, error messages and
`registry_warning` text, CLI `--help`, `doctor` output, and any string an agent branches on.

| Rule | Do | Don't |
|---|---|---|
| One word, one meaning | Pick one verb per action and reuse it everywhere | Rotate "check" / "verify" / "confirm" for one action |
| Active voice | "The tool deletes the session." | "The session is deleted." |
| Simple tenses | "The create failed." | "The create has failed." |
| One instruction per sentence | "Read the pane. Check the exit code." | "Read the pane and check the exit code, then retry." |
| Sentence length | 20 words or fewer | Compound sentences with stacked subordinate clauses |
| Noun clusters | 3 words or fewer ("tmux socket path") | "default session incarnation stamp value" |
| No ellipsis | Keep the subject, verb, and article | Drop words to save space |
| State the failure and the action | "The request failed. Check your client version." | "An error may have occurred" |
| List the error codes | End a tool docstring with its closed set of codes | Leave an agent to infer them from prose |
| No emoji | `CRITICAL:` / `WARNING:` / `NOTE:` | A glyph the reader must decode |
| Resolvable references | Name the thing, or cite a path in the repo | A citation only the author can follow |

The last two are not Tier 1 rules specifically. They apply to every tracked file. They are
repeated here because they are the two an agent most often breaks.

### Tier 2 — Clarity. The reader is a person, and the argument is the payload.

Applies to: README, `docs/`, code comments, commit messages, PR descriptions, specs.

Tier 2 keeps emphasis, contrast, and rhetorical structure. Sentences like "The direction is
forced, not chosen" are doing real work: they tell the reader that what follows is a
constraint rather than a preference. Flattening that loses information.

Tier 2 still requires:

- Active voice, unless the actor is genuinely unknown or irrelevant.
- 25 words or fewer per sentence. Break a longer one rather than adding a comma.
- One topic per paragraph, 6 sentences or fewer.
- A list for 3 or more steps, conditions, or alternatives. Do not bury a sequence in prose.
- No emoji.
- Every cross-reference resolves for a reader who has only a clone of this repo.
- A claim about behaviour says whether it was measured or assumed.

## Process

1. Read the text once for meaning. Do not start rewriting before you know what it must
   still say afterward.
2. Decide the tier from the reader.
3. Go sentence by sentence and flag each rule the sentence breaks.
4. Rewrite to fix the break while preserving the meaning exactly. This repo's docs record
   measured facts, version numbers, error strings, and timings. Never round a number,
   generalize a version, or drop a scope qualifier to shorten a sentence.
5. If a rewrite would cost necessary precision, keep the longer wording and say why.
6. If the text already conforms, say so. Do not force changes onto conforming text.

## Output Format

When asked for a review rather than an edit, produce:

```markdown
| Rule broken | Original | Rewritten |
|---|---|---|
| Present perfect | "We have measured the timeout." | "We measured the timeout." |
| Noun cluster (4 words) | "the session incarnation stamp check" | "the check on the incarnation stamp" |
```

Follow the table with one line on anything you deliberately left alone, and why.

## Boundaries

**Will:**

- Name the rule a sentence breaks before rewriting it.
- Preserve every fact, number, version, condition, and scope qualifier.
- Keep Tier 2 emphasis and argument structure intact.
- Suggest a glossary entry for a domain term that has to stay.

**Will not:**

- Flatten Tier 2 rationale prose into Tier 1 declaratives.
- Rewrite `FINDINGS.md` records.
- Drop a measured qualifier to hit a word count. It flags the trade-off instead.
- Reproduce ASD's approved-word dictionary. This applies STE's principle — plainest
  available word, used the same way every time — not its word list. For genuine STE
  compliance, use the official standard at https://www.asd-ste100.org/.

## Additional Resources

- [`docs/writing-style.md`](../../../docs/writing-style.md) — the normative standard.
- `references/writing-rules.md` — what ASD-STE100 says, and its sources.
- `examples/before-after.md` — worked rewrites taken from this repo.
