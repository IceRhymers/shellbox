# Before / After

Every example is a real rewrite from this repo, made when the standard was adopted. None are
invented.

## Tier 1 — text an agent parses

### The server `INSTRUCTIONS` string

**Before:**

> Sessions are addressed by name (`shell_create(name=...)`) and by the `session` id returned
> by every tool. Output is returned with ANSI escapes preserved. `shell_send` submits input;
> it cannot confirm the pane's process consumed it, which is why every send reports
> `delivery: "unverified"` -- read the pane back to observe an effect.

**Rules broken:**

- Passive with a known actor: "Sessions are addressed", "Output is returned".
- The last sentence is 27 words and carries three separate facts.
- A semicolon joins two instructions.

**After:**

> Address a session by name, or by the `session` id that every tool returns.
> `shell_create(name=...)` creates one.
>
> `shell_read` preserves ANSI escapes.
>
> `shell_send` submits input to a session. It cannot confirm that the pane's process consumed
> the input. Every send therefore reports `delivery: "unverified"`. Read the session back to
> observe an effect.
>
> Sessions survive a restart of this server. They do not survive a sandbox restart.

The last line is new. An agent deciding whether to rely on a session needs that fact, and it
was only in the README.

### `shell_list` — the foreign-session case

**Before:**

> `foreign: true` with `incarnation: null` marks a session shellbox cannot prove it owns --
> either created by something else, or observed in the window between its creation and its
> identity stamp.

**Rules broken:**

- 33 words.
- Ellipsis: "either created by something else" drops its subject, so the reader has to guess
  what was created.

**After:**

> `foreign: true` with `incarnation: null` marks a session that shellbox cannot prove it owns.
> There are two causes, and this tool does not distinguish them. Something other than shellbox
> created the session, or this call observed the session between its creation and its identity
> stamp.

"There are two causes, and this tool does not distinguish them" is added information, not
padding. The original implied it; an agent should not have to infer it.

### A `registry_warning` an agent may branch on

**Before:**

> inventory deferred: this host has no resolved owner_email yet, so the session was not
> recorded. The shell itself is unaffected.

**Rules broken:** passive with a known actor ("was not recorded").

**After:**

> inventory deferred: this host has no resolved owner_email, so shellbox did not record this
> session. The shell works normally.

The substring `inventory deferred` is preserved deliberately —
`tests/integration/test_registry_non_fatal.py` asserts on it. Check for that before you edit
any agent-facing string.

## Tier 2 — prose that carries an argument

### What Tier 2 protects

This is the sentence that motivated the tier split. It is in `docs/architecture.md`:

> The direction is forced, not chosen. **App → sandbox is impossible**: outbound TCP from the
> App container to the sandbox gateway fails with `[Errno 113] No route to host` in 1 ms.

Strict Tier 1 would produce:

> The App cannot connect to the sandbox. Outbound TCP from the App container to the sandbox
> gateway fails. The error is `[Errno 113] No route to host`. The failure occurs in 1 ms.

Every fact survives, and the point does not. "Forced, not chosen" tells a later reader that
reversing the arrow is not on the table. Without it, someone proposes reversing it. **This
rewrite was rejected.** The original stands.

### A README sentence that genuinely needed splitting

**Before:**

> The first row is a guarantee with a test behind it (`T-RESTART`: `SIGKILL` the MCP process,
> assert the tmux server outlives it, the session is still usable, and its incarnation token
> is unchanged).

**Rules broken:** 34 words, and a parenthetical carrying a four-step procedure.

**After:**

> The first row is a guarantee, and a test stands behind it. `T-RESTART` sends `SIGKILL` to the
> MCP process. It then asserts three things: the tmux server outlives that signal, the session
> is still usable, and its incarnation token did not change.

The emphasis is untouched. Only the packing changed.

## The cross-reference rule

This is the rule the repo broke most often, and it is worth its own example.

**Before**, in `README.md`:

> tmux behaviour that `§7` of the plan depends on is measured by an executable suite

`§7` refers to `.omc/plans/phase-2-session-plane.md`, which is `.gitignore`d. A reader with a
clone cannot follow it. Worse, `§` is overloaded: elsewhere in the repo the same glyph refers
to sections of `docs/sandbox-environment.md`, which **is** committed. The same `§5` means two
different documents in two different files.

**After**, as it now reads in `README.md`:

> An executable suite, `spike/tmux_spike.py`, measures the tmux behaviour that the tmux
> adapter depends on. (`§7` names that part of the plan; see `docs/plan-sections.md` for what
> the plan's section numbers refer to.)

In the real README both of those are markdown links, written relative to the repo root —
`(spike/tmux_spike.py)` and `(docs/plan-sections.md)`. They are shown as plain code spans here
because this file lives at `.claude/skills/simplified-technical-english/examples/`, and a link
that works from the README does not work from here. **Write a relative link from the file it
will live in, not from the file you copied it out of.**

The shorthand survives, because it is useful to the people who use it daily. It now resolves
for everyone else.

## Claims stated as measured

`docs/architecture.md` listed lifecycle timings with no source:

**Before:**

> Lifecycle timings (warm pool, not a cold boot): `create`: 1.8 s, `start`: 25 s

The numbers are precise, which reads as measured, but nothing said so or named the source.
Checking `probe/FINDINGS.md` confirmed both.

**After:**

> Lifecycle timings, **measured** by the Phase 1 probe
> ([`probe/FINDINGS.md`](../../../../probe/FINDINGS.md)) against a warm pool, not a cold boot:
> `create`: 1.8 s, `start`: 25 s

Note the order of operations. **Verify the claim first, then label it.** If the check had
failed, the fix would have been to mark it as an assumption, not to attach a citation that
does not hold it up.
