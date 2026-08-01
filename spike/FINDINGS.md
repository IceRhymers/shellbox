# tmux adapter spike — findings

Settles B1–B4 from the iteration-2 architect review of `.omc/plans/phase-2-session-plane.md`
by executing the compositions §7 prescribes, rather than reasoning about them.

Run: `python3 spike/tmux_spike.py` (JSONL to stdout). Two lanes:

| lane | tmux | platform |
|---|---|---|
| local | **3.6b** | macOS arm64 (Darwin 25.5.0) |
| container | **3.4** | Ubuntu 24.04 — *the sandbox's version* (`probe/FINDINGS.md:246`) |

**Every targeting and lifecycle result below is identical across both lanes.** Only the
line-discipline behaviour (F5) differs by platform, and it differs in a way that matters.

---

## F1 — `window-size manual` kills the server on the *next* `new-session`. Not a beta bug.

**15/15 failures in both lanes**, `server exited unexpectedly`.

| variant | 3.6b | 3.4 |
|---|---|---|
| no `window-size manual` | **0/15** fail | **0/15** fail |
| `window-size manual` as a separate call | **15/15** fail | **15/15** fail |
| r2 §7.2's chained form (options after `new-session`) | **15/15** fail | **15/15** fail |

The r1→r2 rewrite moved the options *after* `new-session` on the theory that ordering was
the variable. It is not. **The option itself is the cause**, and the failure lands on the
second create — so under a 1–32 agent pool, one agent's `shell_create` destroys every other
agent's sessions on that server.

The architect's open caveat — "this may be a 3.6b-only regression, and the sandbox runs 3.4"
— is **closed: it reproduces identically on 3.4/Linux.**

**Action:** drop `window-size manual` from the create path. It exists so a Phase 4 attached
client cannot silently override `shell_resize`, but nothing can attach until Phase 3/4. Set it
lazily at first attach, or re-assert dimensions post-attach. Re-open R10.

## F2 — `set-option -t` rejects `=name` and accepts `=name:`

| form | rc | value stored? |
|---|---|---|
| `build` | 0 | yes — but **prefix-vulnerable** |
| `bui` | **0** | **yes — writes into `build`** |
| `=build` | **1** (`no such session: =build`) | no |
| `=build:` | 0 | yes |

r2 §7.2 uses `set-option -t '=<name>'`, which always returns rc=1. So `@shellbox_incarnation`
is **never set**, `list-sessions -F` yields an empty field, and T-RESTART step 7 / T-CONC-3
compare `"" == ""` and **pass green**. The mechanism is inert and its tests validate nothing.

## F3 — the per-verb table collapses to **one** safe form, not three

Measured against a server holding only `build` and `envtest`. `=bui:` is the control — it
should always fail, because no session `bui` exists.

| verb | `bui` | `=bui` | `=build` | `=build:` | `=bui:` |
|---|---|---|---|---|---|
| `has-session` | rc=0 unsafe | rc=1 correct | rc=0 | rc=0 | rc=1 correct |
| `kill-session` | rc=0 unsafe, **kills `build`** | rc=1 correct | rc=0 | rc=0 | rc=1 correct |
| `resize-window` | rc=0 unsafe, **resizes `build`** | **rc=0 unsafe, resizes `build`** | rc=0 | rc=0 | rc=1 correct |
| `capture-pane` | rc=0 unsafe | rc=1 | **rc=1 rejected** | rc=0 | rc=1 correct |
| `send-keys` | rc=0 unsafe | rc=1 | **rc=1 rejected** | rc=0 | rc=1 correct |
| `set-option` | rc=0 unsafe | rc=1 | **rc=1 rejected** | rc=0 | rc=1 correct |

unsafe = prefix match reached a session the caller did not name. rejected = valid session rejected.

**`=name:` is the single universal safe form** — correct for all six verbs, and the only form
that rejects the `=bui:` control everywhere. This is *simpler* than the architect's proposed
three-class `target.py`: two classes suffice.

```python
def target(name)  -> f"={name}:"   # every targeting verb, without exception
def new_name(name) -> name         # `new-session -s` only — NEVER anchored
```

`=name` specifically is the worst of both worlds: it fails to protect `resize-window` while
being rejected outright by the three pane verbs.

**`new-session -s '=build'` creates a session literally named `=build`** (rc=0, both lanes),
after which `has-session -t '=build'` returns **rc=1** — unreachable through the adapter's own
helper. `-s` takes a *name*, not a target; anchoring it is a category error.

## F4 — `history-limit` set after `new-session` never reaches the pane

A pane's history limit is fixed when the pane is created.

| approach | global reads | **pane** reads | works? |
|---|---|---|---|
| r2 §7.2: `set-option -g` *after* `new-session` | `20000` | **`2000`** | no |
| `tmux -f <conf>` with `set -g history-limit` | — | `20000` | yes |
| `start-server ; set-option -g ; new-session` (one invocation) | — | `20000` | yes |

Identical in both lanes. W2's criterion "`show-options` confirms `history-limit`" reads the
**global** and passes green while every real pane runs at the 2000 default — the same
defect-as-oracle failure the plan criticises elsewhere.

**Two working fixes exist**, contrary to the architect's "no clean fix available". Prefer the
chained `start-server` form: no temp file, no config to ship, one invocation.

## F5 — the line-discipline hazard (H4) behaves *differently on Linux*, and the plan describes the macOS case

Single line of N chars + `\n`, pasted into a pane, `delivered / sent`:

| line length | 3.6b macOS canonical | **3.4 Linux canonical** | raw (both) |
|---|---|---|---|
| 500 | 501/501 | 501/501 | ok |
| 1023 | 1024/1024 | 1024/1024 | ok |
| **1024** | **0/1025 — total loss** | 1025/1025 ok | ok |
| **4095** | **0/4096 — total loss** | 4096/4096 ok | ok |
| **4096** | **0/4097** | **4096/4097 — truncated** | ok |
| **8192** | **0/8193** | **4096/8193 — truncated** | ok |

Two distinct failure modes, and the plan only documents the macOS one:

- **macOS**: over-long line → **entire line discarded** (0 bytes) at >1023.
- **Linux — the sandbox** → **silently truncated to 4096**, not dropped.

Truncation is arguably the worse of the two for a shell: a dropped command does nothing
visible, whereas a *truncated* command is a **different, still-executable command**. `rm -rf
/tmp/scratch/…` truncated at 4096 is a real hazard.

`SHELLBOX_MAX_SEND_LINE_BYTES = 1000` remains the right default — it is below both platforms'
thresholds — but §8's rationale needs the Linux truncation case added, and it is the one that
belongs in the risk register.

## F6 — NEW: `display-message` never fails on a bad target

Not in any prior review. §7.4 uses `display-message -p -t '=name:' '#{pane_current_path}'`
to read a session's cwd.

| target | rc | stdout |
|---|---|---|
| `=build:` (exists) | 0 | `build\|/private/tmp` |
| `=bui:` (does not exist) | **0** | **empty** |
| `=bui` | **0** | **empty** |
| `nope:` | **0** | **empty** |

`display-message` returns **rc=0 with empty output** for every nonexistent target. Any code
reading cwd or liveness through it gets `""` and no error. Callers must treat empty output as
`not_found` explicitly — an rc check is worthless here.

## F7 — the corrected composition executes end to end, in both lanes

Using only the forms above: `new-session` (bare `-s`, no `window-size manual`) → `set-option
-g` → `set-option -t '=name:' @shellbox_incarnation` → `load-buffer -`/`paste-buffer -d`
→ `list-sessions -F` → `resize-window` → `kill-session` → `has-session`.

```
all_step_rcs [0,0,0,0,0,0,0,0]   any_step_failed false
delivered_bytes 11 / sent 11     incarnation_roundtrip_ok true
list_output  build  1785477220  /tmp  80x24  11660705-…  /tmp
resize_rc 0   kill_rc 0   has_session_after_kill_rc 1
```

Note the `-F` output uses a real `\t` separator and parses. r2's `#{q:;}` emits nothing
(`#{q:}` is tmux's *quote* modifier, not a literal) — confirmed, and avoided here.

## F8 — the plan's full §7.2 create chain, run verbatim, including the two elements the spike skipped

r3 correctly marked `default-terminal screen-256color` and `-e K=V` as *unverified inside the
chain*. Closed — the chain was run exactly as §7.2 writes it, in both lanes:

| assertion | 3.6b macOS | 3.4 Ubuntu |
|---|---|---|
| chain rc | **0** | **0** |
| pane `#{history_limit}` | **20000** | **20000** |
| `default-terminal` applied | `screen-256color` | `screen-256color` |
| `@shellbox_incarnation` round-trip | full uuid | full uuid |
| **second `new-session` (the F1 regression)** | **rc=0** | **rc=0** |
| sessions after | `build,other` | `build,other` |
| `-e 'FOO=a\nb'` / `-e 'BAR=x;y'` in the pane env | intact | intact |

So §7.2 is now verified end to end with no unmarked assertions.

**One apparent divergence was a test artifact, not a tmux difference** — worth recording so it
is not "rediscovered" as a finding. A `printenv FOO BAR` readback showed `BAR` missing on macOS
and present on Linux. Cause: **BSD `printenv` accepts only one variable name** and silently
ignores the rest. Re-tested with `sh -c 'echo "$BAR"'`, macOS returns `BAR=[x;y]` correctly.
Any `tests/tmux` env assertion must avoid `printenv` with multiple names.

## F9 — the *scope* is the variable, not the option. `window-size manual` has a safe form.

Refines F1. 10 trials per variant, **identical in both lanes**:

| variant | 2nd `new-session` fails | sessions surviving |
|---|---|---|
| option not set | 0/10 | 3 |
| **`set-option -g window-size manual`** | **10/10** | **1** — the server died and took the sessions with it |
| **`set-option -w -t '=<name>:' window-size manual`** | **0/10** | 3 |

So F1's causal model needs one word narrowed: **the global option is the cause**, not the option.
The per-window form is safe, and a third create is unaffected too.

**This turns R10 from "no Phase 2 mitigation" into a real one** — and it makes the obvious
handoff advice dangerous. "Set it lazily at first attach" is **fatal if implemented with `-g`**:
it detonates the shared tmux server on the next agent's `shell_create`, which is F1 with a
longer fuse. Any Phase 3/4 attach path must use `-w -t '=<name>:'` and never `-g`.

## F10 — the `strip()` trap: an unstamped session's 8 fields become 6

`list-sessions -F` with the plan's 8-field format, against a session that has **no**
`@shellbox_incarnation` (a foreign session, or one caught mid-create between `new-session` and
`set-option`), in **both lanes**:

| measurement | value |
|---|---|
| raw field count | **8** (last two empty) |
| field count **after `.strip()`** | **6** |
| incarnation field | `''` |

Two consequences, and they are layered rather than alternatives:

1. A field-count check does **not** detect a missing incarnation — the count is 8 either way.
   Only the "empty is never an incarnation match" rule catches it.
2. **Any parser that strips whitespace before splitting sees 6 fields and drops a legitimate
   record.** Trailing empty fields end the line in tabs and `.strip()` eats them. A
   "field count != 8 ⇒ drop" rule would then silently discard every unstamped session — which
   is exactly the set that matters for orphan reconciliation.

This spike hit the bug in its own harness first, which is how it was found.

---

## F11 — NEW in W2: `display-message` on a missing target emits the format's **literals**

F6 established "empty stdout is `not_found`". The obvious implementation of that rule is
wrong, and §7.4 states the rule in a form that invites it.

Only *placeholders* expand empty; **literals in the format still come through**. Measured,
both lanes, against a server holding only `build`:

| format | target | rc | stdout |
|---|---|---|---|
| `#{history_limit}` | `=nope:` | 0 | `""` — the F6 case |
| `#{history_limit}\t#{pane_dead}` | `=nope:` | **0** | **`"\t"`** — **NOT empty** |
| `#{session_name}\t#{history_limit}` | `=nope:` | 0 | `"\t"`, first field **empty** |
| `#{session_name}\t#{history_limit}` | `=build:` | 0 | `"build\t2000"` |

`tmux.py` reads several numeric fields in one invocation (cheaper than one subprocess per
field), which is exactly the shape that breaks a naive `if not stdout` check. **Every
`display-message` format in the adapter therefore LEADS with `#{session_name}`**, and
resolution is decided by that field alone — non-empty for any real session, empty for a
target that does not exist. `check_display_message_multifield` asserts both directions.

## F12 — NEW in W2: the N1 stderr table is **incomplete**, and the missing row is the cold start

Every signature `errors.py` classifies is now measured from a real tmux rather than
transcribed from prose. Six rows matched the plan's N1 table. **Two are new**, and they share
a prefix:

| condition | stderr | must classify as |
|---|---|---|
| socket file exists, no server behind it | `no server running on <path>` | `no_server` |
| **socket file does not exist yet** | **`error connecting to <path> (No such file or directory)`** | **`no_server`** |
| **socket path over the `sun_path` limit** | **`error connecting to <path> (File name too long)`** | **`tmux_error`** |

The cold start is the ordinary case — **no tmux server has ever run on this host** — and the
plan's N1 table has no row for it, so `shell_list` would have returned `tmux_error` instead of
an empty inventory on the very first call in every fresh sandbox.

**A signature is therefore a SET of required substrings, not one string.** Matching
`error connecting to` alone would classify a too-long socket path as `no_server` — i.e. report
a misconfiguration as a healthy empty inventory, which is precisely the "misconfigured process
poisons the registry" hazard (§9.2): that process would then mark every live session on the
host `orphaned`.

**One ambiguity is NOT resolved by this and must not be treated as solved:** a cold start
and a *wrong socket path* produce the identical message. Classification cannot tell them
apart, which is why §9.2's orphaning guard (compare the resolved socket against the `hosts`
row) is load-bearing rather than belt-and-braces.

Also measured while getting this right: `kill-server` **leaves the socket file behind**, so
the two "no server" states are both genuinely reachable. The spike's first draft of this check
inherited its socket teardown from the harness — which unlinks — and so measured the cold-start
message while asserting the `no server running` one. **The assertion caught it**, which is the
argument for the suite carrying assertions rather than emitting JSONL.

## F13 — the `sun_path` limit, measured per platform instead of assumed

`sockaddr_un.sun_path` is a fixed array whose size differs by platform, so one hardcoded
number is wrong on one of the two platforms shellbox ships to — and the symptom is
`File name too long` on *every* call with nothing naming the cause.

| lane | longest bindable path | `sun_path` (NUL included) |
|---|---|---|
| 3.6b / macOS | **103** | **104** |
| 3.4 / Ubuntu | **107** | **108** |

Measured by binding real `AF_UNIX` sockets at increasing lengths until the kernel refuses.
`naming.py` keys off `sys.platform`, and both the spike (`check_socket_path_limit`) and
`tests/unit/test_naming.py` re-measure and fail loudly if the table disagrees with the kernel
actually running.

## F14 — the forms W2's adapter adds beyond this spike's original set

New forms go into the spike first and into `tmux.py` second. Verified in both lanes:

| form | result |
|---|---|
| `display-message -p -t '=<name>:'` with a 6-field numeric group | 6 fields, leads with the name, pane `history_limit` = 20000 |
| `capture-pane -p -e -t '=<name>:' -S -<lines>` | rc=0 |
| `send-keys -t '=<name>:' -- <Key>` | rc=0, the key reaches the pane |
| `delete-buffer -b <name>` (the `paste-buffer` failure path) | rc=0, `list-buffers` empty afterwards |

`delete-buffer` is measured because `paste-buffer -d` only deletes on *success*: a failed paste
leaks the buffer, and `buffer-limit` is **50 server-wide** across all pooled agents, so a leak
both evicts other agents' buffers and retains arbitrary agent input.

## F15 — the TAB separator survives **only** under a UTF-8 ctype locale

The most consequential finding in W2, and structurally invisible to every earlier lane.

**When the invoking client's ctype locale is not UTF-8, tmux visually encodes the TAB in
format output as `_` — in `list-sessions -F` as well as `display-message`.** All eight fields
collapse into one:

```
build_1785477220_1785477230_80_24_0_11660705-…-eeee_/tmp
```

Measured, **identical in both lanes** (`check_locale_tab_dependence`), invoking tmux with a
controlled environment of `PATH`, `HOME`, `TERM`:

| client env | TAB in `list-sessions -F` | TAB in `display-message` |
|---|---|---|
| no locale variables | **`_` — 1 field** | **`_`** |
| `LC_CTYPE=C.UTF-8` | preserved, 8 fields | preserved |
| `LC_ALL=C.UTF-8` | preserved | preserved |
| **`LC_CTYPE=C.UTF-8` + `LC_ALL=C`** | **`_` — LC_ALL wins** | **`_`** |
| `LC_CTYPE=en_US.UTF-8` | preserved | preserved |

**Why nothing caught it before.** Every prior measurement — M1–M29, S1–S12, and this spike
until now — invoked tmux with the developer's **full environment**, which on a dev machine
carries `LANG=…UTF-8`. The variable was never varied, so it was never a variable. It surfaced
only when `tmux.py` began invoking tmux with a **reduced env allowlist** (`TERM`, `HOME`,
`PATH`, …) — which it does deliberately, because the MCP process's environment is where the
harness injects credentials and a tmux server passes its environment to every pane it spawns.
The hardening exposed the bug.

**Why it is severe rather than cosmetic.** A locale is normally **absent** in a container, a
systemd unit and a sandbox — i.e. exactly where shellbox runs. The failure chain:

1. every `list-sessions -F` record parses as 1 field, not 8;
2. every record is dropped as malformed;
3. `shell_list` reports an **empty inventory on a host full of live sessions**;
4. E5 orphan reconciliation marks **every live session `orphaned`**.

That is precisely the catastrophe §12's *"unknown stderr must never map to empty list"* rule
exists to prevent, arriving through a channel that rule does not cover. And on the read path
the same mangling made `display-message`'s `@shellbox_incarnation` read as **empty**, which
the "empty is never a match" rule then correctly but uselessly converted into `not_found` for
every send, resize and kill — a *working* session plane reporting that nothing exists.

**Two fixes, both applied:**

1. **`tmux.py` FORCES `LC_CTYPE=C.UTF-8`** on every invocation, exactly as it forces `TERM`.
   *Passing `LANG` through is not a fix* — if the parent has no locale there is nothing to
   pass. `LC_CTYPE` rather than `LC_ALL`, because forcing `LC_ALL` would also override
   collation and messages inside the user's shell; and `LC_ALL` is deliberately **not** in the
   pass-through allowlist, since it would override `LC_CTYPE` and silently reinstate the bug.
2. **`list_sessions` raises when tmux returned records and NONE parsed**, instead of returning
   `[]`. The forced locale should make that unreachable; if some other environment ever mangles
   the separator, shellbox now fails loudly instead of reporting a healthy empty inventory and
   orphaning every session on the strength of it.

`tests/tmux/test_tmux_adapter.py::test_the_record_parses_when_the_process_has_no_locale`
deletes `LANG`/`LC_ALL`/`LC_CTYPE` from the process — the sandbox's condition — and asserts the
adapter still parses, then asserts the same tmux on the same server **does** mangle without the
forced locale, so the reason for the fix stays visible.

**A note for W10 (sandbox verification):** §7's env rules force `TERM` and say nothing about
the locale. This finding means the sandbox's locale must be treated as **absent until measured**,
and any future code path that invokes tmux outside `TmuxAdapter` inherits this hazard.

---

## The suite now gates

`spike/tmux_spike.py` was upgraded after the iteration-3 architect review, which correctly
observed that it emitted JSONL and **returned 0 regardless of outcome** — so it could not gate
anything. It now:

- carries **assertions** and exits **non-zero** on any failure, with a `SUMMARY` record listing them;
- runs the plan's **verbatim §7.2 create chain** as one invocation, asserting the *pane's*
  `#{history_limit}` (reading the global is an invalid oracle) and that a **second** create succeeds;
- asserts the **8-field** `-F` format, raw and stripped, per F10;
- includes the **per-window `window-size manual`** variant per F9;
- **self-checks** that its own normative paths contain no bare `=name` target. The earlier version
  used `kill-session -t '=build'` — the very form §7.1 forbids — which is almost certainly where
  the plan's §9.2 race table inherited it. A regression suite must not violate the rule it guards.
  The check is deliberately scoped to the normative functions: `check_b2`/`check_b3` pass bare
  `=name` on purpose, because proving it unsafe is their job.

Verified: **exit 0 in both lanes** with all assertions passing, and **exit 1** with a listed
failure when an assertion is genuinely violated.

---

## What this says about the method

Four of these six findings are defects in **compositions**, not in individual commands. The
plan's measurement appendix tests fragments and each fragment was measured correctly; every
defect lived in how they were assembled. §7 should be transcribed from this spike's output,
and the spike kept as the W2 regression suite — it already is one.
