# tmux adapter spike — findings

Settles B1–B4 from the iteration-2 architect review of `.omc/plans/phase-2-session-plane.md`
by executing the compositions §7 prescribes, rather than reasoning about them. F16–F21 continue
that for Phase 3's attach path (`W14` of
[`phase-3-transport.md`](../.omc/plans/phase-3-transport.md)), under the same rule: a new tmux
form goes into the spike first and into a module second.

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

# Phase 3 — W14. The attach path (`S-ATTACH`, `S-PANE-DEAD`, `S-PIPE`, `S-CLAIM`)

Measurements for [`.omc/plans/phase-3-transport.md`](../.omc/plans/phase-3-transport.md) `W14`,
run in the same two lanes. Every result below is **identical across both lanes** except F18's
loss shape and F21's `/proc` availability — the two places §10 already predicts a platform split.

The reason these are here and not in a module: `W14` is a gate. `R24` (High) recorded that
per-window `window-size manual` holding a window's size under a *live* attached client was
**inferred** from F1/F9 and had no upstream precedent — omnigent sets no `window-size` option at
all and accepts the reflow (`omnigent/terminals/ws_bridge.py:485-487`, HEAD `fddb9b07`). A
negative result would have flipped Decision A from an attached PTY to a `pipe-pane` sink and
rewritten `W15`, `W19`, `ADR-9` and `ADR-10`.

## F16 — `R24` is CLOSED, positive: per-window `window-size manual` holds under a live attached client

An 80x24 session, a live 120x40 attach client, the size sampled continuously for 1.5 s while the
client was attached. `#{session_attached}` was asserted non-zero before every size result, because
"the size held" and "nothing ever attached" are otherwise the same observation.

| placement | sizes seen while attached | held? | samples (3.4 / 3.6b) |
|---|---|---|---|
| **no option** — the control | `120x40` | **no — it reflows** | 1744 / 191 |
| **`at_create`** — inside the create chain | `80x24` only | **yes** | 1767 / 173 |
| **`before_attach`** — set on the existing window just before the client spawns | `80x24` only | **yes** | 1714 / 204 |
| **`after_attach`** — set once the client is live and the window already reflowed | `80x24` only | **yes**, and see below | 1749 / 198 |

Four consequences, and the third was not predicted by the plan:

1. **Decision A stands.** `ADR-9`'s attached PTY is viable on tmux 3.4 and `A2` stays a
   fallback nobody has to take. `ADR-10`'s per-window scope is confirmed under the condition
   F9 could not test.
2. **The control reflows, so PM3 is real on 3.4** — a viewer at 120x40 does move an 80x24
   agent window. The mitigation is not defensive programming against a hypothetical.
3. **The `after_attach` exposure window is transient and self-healing.** The window *does*
   reflow to `120x40` first (recorded as `reflowed_before_option_was_set`), and then setting
   per-window `window-size manual` **restores `80x24` on its own** — no `resize-window`
   required. `ADR-10` priced the attach-time placement as costing "one reflow that can still
   fire"; measured, it costs one reflow that then reverts. That materially strengthens the
   attach-time placement, which is the one that **freezes the agent create path** — the chain
   the plan itself calls transcribed-from-the-spike and paid for over three review rounds.
4. **`resize-window` still works while `manual` is in force**, rc=0 and the window follows to
   90x28 in every variant. This had to be checked: `shell_resize` is a shipped tool built on
   `resize-window`, and an option that silently froze it would be a regression introduced by a
   mitigation. Note the size does **not** revert when the last client detaches (it stays at
   90x28), which is what `manual` means.

5. **One `set-option` protects every later viewer.** Set once before the first attach, the
   window held `80x24` through a 120x40 client, that client's detach, and a **second client at
   100x50** — 825 samples on the second attach, both lanes. `ADR-10`'s attach-time placement
   rests on this and does not state it: if the option were client-scoped, or cleared when the
   last client left, setting it at attach time would protect only the first viewer and the
   second would reflow the window — which is what "do not set it and size the PTY to the
   session" was already rejected for.

And the composition, separately, because F1's whole lesson is that these defects live in
compositions rather than in commands: the per-window `set-option` **inside the shipped create
chain** produced **0/15 second-create failures in both lanes**. F9 measured the same option as a
standalone call; this measures the form `W15` would ship at the create-time placement.

### Which placement `W15` should use — the decision `ADR-10` delegates to this measurement

`ADR-10` states the rule: the create chain is the default, and "the attach-time placement
[is] preferred if its exposure window measures empty." The measurement separates two orderings
the ADR's prose runs together, and the distinction decides it:

- **Set the option, then spawn the attach client** (`before_attach`) — the ordering a publisher
  would actually use, because the window already exists the moment the session does. **Exposure
  window: empty.** No reflow observed in 1714 samples, and per (5) it holds for later viewers too.
- **Spawn the attach client, then set the option** (`after_attach`) — not an ordering any
  publisher needs. One reflow fires and then reverts.

So by `ADR-10`'s own criterion the **attach-time placement wins**, and it wins on the ADR's own
stated benefit: it **freezes the agent create path** — the chain the plan calls
transcribed-from-the-spike and paid for over three review rounds — so the 1-32 agents who never
open a browser pay nothing, and the blast radius of a wrong measurement is one attach rather than
every `shell_create`. The create-chain form is measured safe as well (0/15), so this is a choice
between two working options rather than a fallback.

## F17 — the attach client must be spawned with `forkpty`. `Popen` attaches fine and then silently ignores every resize

`ADR-16` lists `os.openpty()` + `subprocess.Popen(start_new_session=True)` as an alternative to
`os.forkpty()`, and routes one question here: does `tmux attach` tolerate a slave that is not the
child's controlling terminal, since `subprocess` performs no `TIOCSCTTY`?

**That is the wrong question. The answer is yes, and the mechanism still loses.**

| | `forkpty` | `openpty` + `Popen(start_new_session=True)` |
|---|---|---|
| controlling terminal | yes | **no** |
| client attaches (`#{session_attached}`) | 1 | **1 — it works** |
| initial repaint arrives on the master fd | yes | yes |
| `DeprecationWarning` in a threaded process | **yes** | none |
| **`TIOCSWINSZ` on the master reaches tmux** | **yes — window follows to 100x30** | **NO — window stays 120x40** |

The kernel delivers `SIGWINCH` to the pty's **foreground process group**, and a child with no
controlling terminal is not in one. So the `Popen` route produces a client that comes up, streams
correctly, and then discards every viewer resize — which is a **worse** outcome than an attach
that fails, because nothing reports it. `W19` applies `TIOCSWINSZ` on the master for a resize
control frame, so this is not a corner: it is one of the four things `W19` owes.

**Action.** `W19` uses `os.forkpty()` and **silences the `DeprecationWarning` deliberately**
(`ADR-15`'s decision 1 — `os.execve`, not `execvpe`, so the child allocates nothing before
`exec` — is the mitigation, and it is upstream's). `preexec_fn` is not an escape route: it
reinstates running Python in the child after a fork, which is the hazard that made `Popen`
attractive in the first place. Both directions of the table are asserted, so a kernel or tmux
change that rehabilitates `Popen` will say so rather than pass quietly.

Two of `ADR-15`'s transcribed decisions were measured here rather than left as citations:

- **`TERM` describes the far end.** `tmux attach` with `TERM=dumb` is refused outright —
  `open terminal failed: terminal does not support clear`, child exits, zero clients attached, in
  both lanes. So `attach_argv`'s forced `TERM` is load-bearing, not hygiene.
- **The initial repaint is free, in-band, and ordered by tmux.** Decision A's strongest argument
  was argued from the *absence* of a `capture-pane` call in omnigent's bridge, and absence of a
  call is not presence of a repaint. Measured directly: a sentinel printed into the pane **before
  any client existed** arrived in the attach master's first ~730 bytes with **no `capture-pane`
  issued at all**.

## F18 — H4 applies to the attach input path too, and Linux truncates at 4096 there as well

The correction this validates is load-bearing and inverts a claimed advantage. An earlier plan
revision held that an attached PTY *escapes* H4. It does not: H4 is the **receiving pane's tty in
canonical mode** (F5's table reads `raw (both) ok` at every length), and tmux forwards an attach
client's keystrokes to that same pty.

Measured through a live attach client into a canonical-mode `cat > file`:

| written to the attach master | delivered, 3.4 / Linux | delivered, 3.6b / macOS |
|---|---|---|
| 26 bytes + LF | **26 — byte-exact** | 26 |
| 8192 bytes + LF | **4096 — silently truncated** | **0 — discarded** |

Exactly F5's platform split, on the new path. Linux's truncation is the worse half, per F5's own
verdict: *a truncated command is a different, still-executable command.*

**Action.** `W19`'s `send_input` owes a per-line ceiling, and it must be the one the repo already
ships — `max_send_line_bytes` (default 1000, `SHELLBOX_MAX_SEND_LINE_BYTES`) raising
`LineTooLong`, which `errors.py` calls "the real boundary" — not a new number at 4096. The
ceiling is now a **measured** requirement rather than an inferred one, and `T-INPUT-LINE-CEILING`
asserts rejection, which is the only implementable branch: canonical-mode truncation is the
kernel's `MAX_CANON` buffer, so chunking does not help, and no tmux format exposes the pane pty's
termios, so the publisher cannot know which mode the pane is in.

## F19 — `S-PANE-DEAD`: both directions hold, and the attach client OUTLIVES the pane's process

Read through the shipped `display-message` path (`#{session_name}\t#{pane_dead}`), not
`list-panes`, and with a live attach client present in both directions. Identical in both lanes.

| direction | `pane_dead` | `has-session` | clients attached |
|---|---|---|---|
| the attach client is killed (**detach**) | `0` while attached, **`0` after** | rc=0 | 1 → 0 |
| the pane's **process exits** under `remain-on-exit on` | **`1`** | **rc=0 — the session survives** | **1 — still attached** |

This validates an assumption already load-bearing in shipped code rather than gating new code:
`tmux.py` derives `alive` from `#{pane_dead}` and calls it the single source of truth for
liveness, so it runs whether or not `W19` proceeds.

The third column is the finding the earlier plan revision did not have. **The attach client stays
attached after the pane it is showing dies**, so a publisher can read `#{pane_dead}` and emit
`4404 terminal-gone` *on a socket that is still up*, rather than inferring it from its own client
going away. That is what makes the 4404/4405 split implementable as `ws_bridge.py:71-80`
describes it — and a detach misread as terminal-gone would stop the client's reconnect loop and
tear the session down.

## F20 — `pipe-pane` is worse than the plan feared, and `-o` is the dangerous spelling

`OQ-J` asked whether tmux stores one pipe per pane. It does, and the two spellings fail
differently. Identical in both lanes: open a pipe to sink 1, send output, open a second pipe to
sink 2, send more.

| form | sink 1 after | sink 2 | verdict |
|---|---|---|---|
| `pipe-pane -t <t> 'cat >> f'` | frozen at 7 B | 7 B | **the second pipe REPLACED the first** |
| `pipe-pane -o -t <t> 'cat >> f'` | frozen at 7 B | **0 B** | **the second call TOGGLED piping OFF — neither sink receives it** |

Both are stream-stealing failures under D6's multi-viewer expectation, and **the `-o` form —
the one §2's `A2` sketch is written with — is the worse of the two**: a second viewer does not
take over the stream, it silently stops the first viewer's *and* gets nothing itself. `-o` means
"only open a pipe if none exists", so a second call toggles rather than replaces.

This is priced now rather than when the fallback is reached for, per `ADR-10`'s rollback
paragraph. F16 means nobody has to take it; if `R24` ever reopens, `A2` needs publisher-side
arbitration of its own before it can serve two viewers, which is cost the plan's A2 analysis did
not carry.

## F21 — `S-CLAIM`: the claim round-trips, and the read path that distinguishes "no claim" from "error" is the shipped one

Four measurements behind `ADR-16`'s claim protocol, none of which had been run.

**(a) Round trip, including from another process.** A claim of the shape
`<pid>:<tid>:<starttime>` — digits and colons only, so it cannot corrupt a TAB group the way
`@shellbox_cwd` can (`check_cwd_injection` in [`tmux_spike.py`](tmux_spike.py) measures that
hazard) — written with `set-option -t '=<name>:'` and read back:

| read path | present | **absent** |
|---|---|---|
| `display-message -p -F '#{session_name}\t#{@shellbox_publisher}'` | rc=0, exact value | **rc=0, empty field** |
| `show-options -t '=<name>:' -v @shellbox_publisher` | rc=0, exact value | **rc=1, `invalid option: @…`** |
| the same `display-message`, run by a **separate Python process** | rc=0, exact value | — |
| `set-option -u` then re-read | — | rc=0, empty field |

**`W19b` must read the claim through `display-message`, not `show-options -v`.** An absent claim
is the *ordinary* case — it is what a first publisher sees — and `show-options -v` reports it as
an error indistinguishable from a real failure, while the shipped path reports it as an empty
field at rc=0. This is F11's rule paying off a second time: lead the format with
`#{session_name}` and an empty first field means unresolved, so "no claim" and "no session" stay
distinct too.

**(b) Racing writers.** Twelve trials per lane, two separate processes released by a shared
trigger file, each doing `set-option` then read-back:

| outcome | 3.4 | 3.6b |
|---|---|---|
| one racer reads its own claim, the other reads a foreign one (**the protocol works**) | **12/12** | **12/12** |
| both read their own (`R33`'s interleaving — both would attach) | 0/12 | 0/12 |
| a stored value belonging to neither writer (**torn**) | 0/12 | 0/12 |

Stated honestly: **`own+own` was not observed, which bounds `R33` rather than excluding it.** The
protocol is detection, not mutual exclusion — tmux has no compare-and-swap, the same limit
`_resolve_owned` documents for the send path (R12) — and 24 trials of a millisecond-scale
interleaving is weak evidence about a microsecond-scale window. What *is* established is that
last-writer-wins converges and never leaves a torn value, which is what makes the loser's
read-back meaningful.

**(c) `/proc` per-thread identity, on Linux.** Field 22 of `/proc/<pid>/task/<tid>/stat` is
**per-thread**, measured rather than taken from the documentation:

| thread | native tid | field 22 (ticks, `SC_CLK_TCK` = 100) |
|---|---|---|
| main | 2577 | 115514216 |
| first (started at T) | 13257 | 115518130 |
| second (started at T+0.4 s) | 13258 | **115518170 — 40 ticks later** |

40 ticks at 100 Hz is the 0.4 s the threads were staggered by, so the field tracks *thread* start
and not process start. And the property the whole design rests on: **the `/proc/<pid>/task/<tid>`
entry disappears when the thread dies while the process keeps running** — verified with the
process's own main-thread entry still present in the same instant. That is what makes the claim
self-clearing, which is what lets a design with no shutdown path be correct anyway, and it is why
a `tid` works where a publisher-uuid cannot: any of the 1-32 processes can evaluate it.

**(d) The degraded predicate, for the lane with no `/proc`.** On macOS the only liveness probe
available is `os.kill(pid, 0)`, and the spike emits it with the limitation attached rather than
behaving differently in silence: pid-liveness **reinstates the exact hazard the `tid` was
introduced for** — a publisher thread that died inside a still-running process leaves a claim
naming a live pid, so no publisher serves that session again for the rest of that process's life.
That is acceptable only because macOS is a developer lane and the sandbox is Ubuntu 24.04. It
would not be acceptable in production, and `T-ATTACH-CLAIM`'s case 3 is the test that would
otherwise pass there while the production predicate deadlocked.

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

`W14` extended it along the same lines, and two of the additions are worth naming because they
are the kind of assertion that is easy to write vacuously:

- **Every size result asserts `#{session_attached}` first.** "The size held" and "nothing ever
  attached" are otherwise the same observation, and the second one passes.
- **F17's mechanism table is asserted in BOTH directions** — `forkpty` propagates a resize and
  `Popen` does not. Asserting only the half that ships would let a platform change quietly
  rehabilitate the rejected alternative; asserting only the other half would let the shipped
  mechanism regress unnoticed. F16's `no_option` control and F18's over-long line are asserted
  the same way: the **hazard** is asserted, so its mitigation can never come to look unnecessary.

`check_self`'s normative list grew to cover the W14 checks, including `Attach.__init__` — the
attach argv itself. That is the single place the `=<name>:` rule is most likely to be broken by
copying, because omnigent's own attach passes an **unanchored** `-t` (`ws_bridge.py:492`).

---

## What this says about the method

Four of these six findings are defects in **compositions**, not in individual commands. The
plan's measurement appendix tests fragments and each fragment was measured correctly; every
defect lived in how they were assembled. §7 should be transcribed from this spike's output,
and the spike kept as the W2 regression suite — it already is one.
