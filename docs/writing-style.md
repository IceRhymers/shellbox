# shellbox — writing style

This is the writing standard for this repo. It covers docs, code comments, docstrings, MCP
tool descriptions, error messages, commit messages, PR descriptions, and specs.

It is adapted from **ASD-STE100** (Simplified Technical English), the controlled-language
standard the aerospace and defense industry uses for maintenance documentation. That
standard exists for a reader with no back-channel — a technician on a tarmac who cannot
call the author to ask what a sentence meant.

shellbox has two readers in that position:

1. **An agent.** shellbox is an MCP server. An agent reads `INSTRUCTIONS`, the tool
   descriptions, and the error payloads, then decides what to call next. It cannot ask a
   follow-up question. If a sentence has two parses, the agent may take the wrong one.
2. **A person with only a clone.** Six months out, the reader has this repo and nothing
   else. Not the issue thread, not the working plan, and not the conversation the decision
   happened in.

Both constraints are load-bearing, and the second is the one this repo has broken most
often.

An agent applies this standard through the
[`simplified-technical-english`](../.claude/skills/simplified-technical-english/SKILL.md)
skill. This file is what governs; the skill is how it gets applied.

---

## Two tiers

Pick the tier from the **reader**, not from the file extension.

| | Tier 1 — Strict | Tier 2 — Clarity |
|---|---|---|
| Reader | A machine, or an agent branching on the text | A person who needs the argument |
| Applies to | MCP tool docstrings, `INSTRUCTIONS`, error messages, `registry_warning` text, CLI `--help`, `doctor` output | README, `docs/`, code comments, **module and function docstrings**, commit messages, PR descriptions, specs |
| Sentence cap | 20 words | 25 words |
| Emphasis | The `CRITICAL:` / `WARNING:` / `NOTE:` markers, and capitals for a word a reader must not miss. No decorative emphasis | Allowed, and expected where it carries meaning |
| Voice | Active, always | Active by default |

### Why Tier 2 exists

Strict STE assumes every reader is executing a procedure. Much of this repo is not
procedure — it is rationale, and rationale has to convey *which* of two facts is
load-bearing.

"The direction is forced, not chosen" tells the reader that what follows is a constraint
and not a preference. The flat rewrite — "The App cannot connect to the sandbox. Outbound
TCP fails." — states the fact and drops the point. A later reader who does not know the
direction was forced will propose reversing it.

So Tier 2 keeps contrast, emphasis, and the sentence that says why an alternative is dead.
It does not keep sprawl.

---

## Tier 1 rules

Applies to every string an agent parses.

1. **One word, one meaning.** Pick one verb per action and use it everywhere. This repo
   uses **create**, **send**, **read**, **list**, **resize**, **kill** — the tool names.
   Do not write "spawn a session" in one place and "create a session" in another.
2. **Active voice.** "The tool deletes the session," not "the session is deleted."
3. **Simple tenses only.** "The create failed," not "the create has failed."
4. **One instruction per sentence.** Split on "and then", "after which", "while also".
5. **20 words or fewer per sentence.**
6. **Noun clusters of 3 words or fewer.** "tmux socket path" is fine. "default session
   incarnation stamp value" is not.
7. **No ellipsis.** Keep the subject, the verb, and the article, even if the result is
   longer. "Sessions not stamped are refused" hides which sessions.
8. **State the failure and the action.** An error message says what failed, and what the
   caller can do. It does not hedge. "An error may have occurred" is not an error message.
9. **List the error codes.** Every tool docstring ends with its closed set of codes, so an
   agent can branch without parsing prose.

## Tier 2 rules

Applies to prose a person reads.

1. **Active voice**, unless the actor is genuinely unknown or irrelevant.
2. **25 words or fewer per sentence.** Break a long sentence rather than adding a comma.
3. **One topic per paragraph, 6 sentences or fewer.**
4. **Use a list for 3 or more steps, conditions, or alternatives.** Do not bury a sequence
   in prose.
5. **Say whether a claim was measured or assumed.** This repo's value is in its measured
   facts. "tmux 3.4 is present at `/usr/bin/tmux`" and "tmux is probably present" are
   different claims. A reader must be able to tell which one they are reading. Mark a
   measured result, and name what measured it.
6. **Keep emphasis for load-bearing contrast.** Bold the fact a reader must not miss. Do
   not bold for decoration.

---

## Rules that apply everywhere

These are not tiered. They apply to every tracked file.

### No emoji

Use a word, not a glyph. A glyph needs a decoder that is never written down, it renders
inconsistently across terminals, and it does not survive a grep.

| Instead of | Write |
|---|---|
| Emoji marking a critical invariant | `CRITICAL:` |
| Emoji marking a caveat | `WARNING:` |
| Emoji marking an aside | `NOTE:` |

Commit
[`4a7f97d`](https://github.com/IceRhymers/shellbox/commit/4a7f97d) removed emoji from tracked
Markdown. This rule extends that to Python and to every other tracked file.

A docstring that is not a tool description is Tier 2. `identity.py`'s module docstring argues
for a design; it is prose, and it is not parsed by an agent.

Three exceptions, and only these three:

- **Diagram notation.** [`docs/architecture.dot`](architecture.dot) uses a check mark and a
  cross to mark accepted and rejected designs. A diagram is a visual medium and those are its
  standard notation, not decoration.
- **Generated files.** `docs/architecture.svg` and `docs/architecture.png` are built from the
  `.dot` source. Never hand-edit them. Change the source and regenerate.
- **Test payloads.** `tests/tmux/test_send_delivery.py` and
  `tests/unit/test_send_input_delivery.py` send an emoji as multi-byte UTF-8 test data, and a
  test asserts on the exact bytes. That emoji is the thing under test.

### Do not rewrite a findings record

[`spike/FINDINGS.md`](../spike/FINDINGS.md) and [`probe/FINDINGS.md`](../probe/FINDINGS.md)
are dated records of what a measurement found. Rewriting one edits the record. Fix a genuine
ambiguity if you find one, and leave the rest. The no-emoji rule still applies to them,
because it does not change what they claim.

### Every cross-reference must resolve from a clone

This is the rule this repo has broken most often, and it is the reason the standard exists
now rather than later.

A reference is valid if a reader with only a clone can follow it. Valid forms:

- A repo-relative path: [`spike/tmux_spike.py`](../spike/tmux_spike.py)
- A path and a symbol: `TmuxAdapter.list_sessions` in
  [`packages/shellbox-mcp/src/shellbox_mcp/tmux.py`](../packages/shellbox-mcp/src/shellbox_mcp/tmux.py)
- A full URL to a public issue or commit.
- A term defined in [`docs/plan-sections.md`](plan-sections.md) or in a doc under `docs/`.

**Do not cite a document that is not in the repo.** The Phase 2 plans live under `.omc/`,
which is `.gitignore`d. A bare `§7` therefore resolves to nothing for anyone but its author.

Where a section reference is genuinely load-bearing, do two things. Name what the section
governs, and cite the committed artifact carrying the same fact. That is usually the spike,
the probe findings, or the test.

[`docs/plan-sections.md`](plan-sections.md) maps each section number to what it governs and
to the committed file that is authoritative for it.

### Define a domain term once

STE allows a project dictionary on top of its base vocabulary. shellbox uses one. A term
like *incarnation*, *orphaned*, *foreign*, or *projection* is defined once, in
[`docs/glossary.md`](glossary.md), and used the same way everywhere after that.

### Never restate a measurement imprecisely

Numbers, versions, error strings, and timings are the repo's product. Copy them exactly.
"tmux 3.4" does not become "tmux 3.x". "1.8 s" does not become "under two seconds". If a
rewrite would cost precision, keep the longer sentence.

---

## Applying this to a PR

A PR description is Tier 2. It answers three questions, in this order:

1. **What changed**, in one sentence.
2. **Why**, including the alternative that was rejected and the reason.
3. **What proves it** — the test, the measurement, or the CI lane that would fail if the
   change were wrong.

Use [`.github/pull_request_template.md`](../.github/pull_request_template.md), which asks
these directly.

A commit message follows the same shape at smaller scale: an imperative subject of 72
characters or fewer, a blank line, then the why.

## Applying this to a spec

A spec is Tier 2 prose containing Tier 1 blocks. The rationale sections are Tier 2. Any
normative statement — a tool signature, an error code table, a field order — is Tier 1, and
strict.

Mark a normative block so a reader can tell it from the discussion around it. This repo
uses the word **NORMATIVE** for that, as in the field-order comment in
[`tmux.py`](../packages/shellbox-mcp/src/shellbox_mcp/tmux.py).

## Enforcement

There is no lint gate. A prose linter fires on tables, code blocks, and technical terms too
often, and the ignore list then becomes the real standard. This repo's prose is dense on
purpose.

Enforcement is review. A reviewer may cite a rule from this file the same way they would
cite a failing test. The `simplified-technical-english` skill produces the before/after
table for that conversation.

Two rules are worth failing a review over on their own. Both silently mislead a future
reader:

- A cross-reference that does not resolve from a clone.
- A claim stated as measured that was not measured.
