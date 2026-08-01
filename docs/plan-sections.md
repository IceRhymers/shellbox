# Reference map — what the section numbers and identifiers mean

The code and docs in this repo cite short identifiers: `§7.2`, `W4`, `ADR-8`, `F12`, `H4`,
`E5`, `T-RESTART`. They are useful shorthand and they are worth keeping. But many of them
point at the Phase 2 plans, which live under `.omc/` and are **`.gitignore`d**. A reader with
only a clone cannot follow those.

This file makes them resolvable. For each identifier it gives what the identifier means and
the **committed** file that is authoritative for it.

[`docs/writing-style.md`](writing-style.md) requires every cross-reference to resolve from a
clone alone. This file is how the existing shorthand satisfies that rule.

> The section titles below are transcribed from the plan headings, not inferred from the code
> that cites them. Where the plan and the committed code disagree, **the committed code
> wins** — it is what actually runs, and it is what a reader can check.

## Read this first: `§` is overloaded

The section symbol refers to **three different documents**, and the qualifier is often
dropped.

| Written as | Means |
|---|---|
| `§1`, `§2`, `§3`, `§5` **when a nearby path names it** | A numbered section of [`docs/sandbox-environment.md`](sandbox-environment.md), which is committed |
| `plan §N`, or a bare `§N` in package code | A section of `.omc/plans/phase-2-session-plane.md`, which is not committed |
| `§0`, `§0.1`-`§0.6` | A section of `.omc/plans/phase-2-completion.md`, which is not committed |

A bare `§5` therefore means the configuration table in package code, and "what the ambient
credential proves" in `enroll.py`, which names `docs/sandbox-environment.md` explicitly. When
you write a new reference, name the document. Never write a bare `§N`.

---

## Session-plane plan sections

From `.omc/plans/phase-2-session-plane.md`. Not committed.

| Section | Plan heading | Committed authority |
|---|---|---|
| `§0` | GATE — build vs extend vs borrow | The "Scoping question" section of [`docs/architecture.md`](architecture.md) |
| `§2` | Reconciling issue #2 against what Phase 1 measured | "Corrections to issue #2" in the [README](../README.md) |
| `§4` | Repo skeleton | The workspace layout: [`pyproject.toml`](../pyproject.toml), [`Makefile`](../Makefile), [`packages/`](../packages/) |
| `§5` | Configuration | [`config.py`](../packages/shellbox-mcp/src/shellbox_mcp/config.py); the table is reproduced in [`docs/registration.md`](registration.md) |
| `§6` | MCP server shape, including the tool schemas | [`server.py`](../packages/shellbox-mcp/src/shellbox_mcp/server.py), [`errors.py`](../packages/shellbox-mcp/src/shellbox_mcp/errors.py) |
| `§7` | The tmux adapter | [`tmux.py`](../packages/shellbox-mcp/src/shellbox_mcp/tmux.py), and [`spike/tmux_spike.py`](../spike/tmux_spike.py), which `§7` was transcribed from |
| `§7.1` | Targeting — `-t` prefix-matches, and it is a security boundary | [`target.py`](../packages/shellbox-mcp/src/shellbox_mcp/target.py) |
| `§7.2` | Create — transcribed from the spike's verified composition | `TmuxAdapter.create` in [`tmux.py`](../packages/shellbox-mcp/src/shellbox_mcp/tmux.py); run verbatim by the spike as `F8` |
| `§7.3` | `history-limit` — it must reach the pane | `F4` in [`spike/FINDINGS.md`](../spike/FINDINGS.md) |
| `§7.4` | Remaining operations, including the `list-sessions -F` field order | The `NORMATIVE FIELD ORDER` comment in [`tmux.py`](../packages/shellbox-mcp/src/shellbox_mcp/tmux.py) |
| `§7.5` | `session_id` and `tmux_name` | [`naming.py`](../packages/shellbox-mcp/src/shellbox_mcp/naming.py) |
| `§8` | Input escaping, injection, and the size ceiling | `max_send_line_bytes` in [`tmux.py`](../packages/shellbox-mcp/src/shellbox_mcp/tmux.py) is the correctness boundary. `max_send_bytes` is a separate tmux-server memory guard and does NOT protect delivery. Also [`keys.py`](../packages/shellbox-mcp/src/shellbox_mcp/keys.py) |
| `§9` | Concurrency, incarnations, and the residual race | [`tmux.py`](../packages/shellbox-mcp/src/shellbox_mcp/tmux.py), and the `project` function in [`server.py`](../packages/shellbox-mcp/src/shellbox_mcp/server.py) |
| `§9.1` | Incarnation tracking via tmux user options | `INCARNATION_OPTION` in [`tmux.py`](../packages/shellbox-mcp/src/shellbox_mcp/tmux.py), `NotFound` in [`errors.py`](../packages/shellbox-mcp/src/shellbox_mcp/errors.py) |
| `§9.2` | Race table. The orphaning guard is two of its rows | [`tests/integration/test_concurrency.py`](../tests/integration/test_concurrency.py); the guard is `reconcile_orphans` in [`enroll.py`](../packages/shellbox-mcp/src/shellbox_mcp/enroll.py) |
| `§10` | Enrollment, identity, and the registry, including the schema and E1-E7 | [`models.py`](../packages/shellbox-registry/src/shellbox_registry/models.py), transcribed field for field; [`enroll.py`](../packages/shellbox-mcp/src/shellbox_mcp/enroll.py) |
| `§11` | Testing strategy | [`tests/conftest.py`](../tests/conftest.py) |
| `§11.1` | Test synchronization — required infrastructure, not a detail | The wait helpers in [`tests/conftest.py`](../tests/conftest.py) |
| `§11.2` | Lanes | [`.github/workflows/ci.yml`](../.github/workflows/ci.yml) |
| `§11.3` | The H4 oracle — split, because a single one is impossible. Two halves: the limit as the first **rejected** length, and byte-exactness | [`tests/unit/test_send_input_delivery.py`](../tests/unit/test_send_input_delivery.py) |
| `§11.4` | The DoD test, `T-RESTART` | [`tests/integration/test_restart_survival.py`](../tests/integration/test_restart_survival.py) |
| `§12` | Work items — the `W` table. `N1` is a subsection of it, a named `errors.py` deliverable in W2 | The `N1` table is `STDERR_SIGNATURES` in [`errors.py`](../packages/shellbox-mcp/src/shellbox_mcp/errors.py) |
| `§13` | Blockers and risks — the `R` table | — |
| `§14` | Open questions — the `OQ` table | — |
| `§15` | ADRs 1 through 5 | — |
| Appendix | Measurements `M1`-`M29`, and spike measurements `S1`-`S12` | [`spike/FINDINGS.md`](../spike/FINDINGS.md) for the spike half |

## Completion-plan sections

From `.omc/plans/phase-2-completion.md`. Not committed.

| Section | Plan heading | Committed authority |
|---|---|---|
| `§0` | Nine inherited premises that were wrong, missing, or overclaimed | [`probe/probe_identity.py`](../probe/probe_identity.py) |
| `§0.1` | `OQ-A` answered: the sandbox cannot learn its own `sandbox_id` | Section 1 of [`docs/sandbox-environment.md`](sandbox-environment.md) |
| `§0.2` | `/etc/machine-id` is image-baked, so a fleet-wide merge is possible | Section 2 of [`docs/sandbox-environment.md`](sandbox-environment.md) |
| `§0.3` | `OQ1` answered from the platform's own source: per-boot and unconditional | Section 3 of [`docs/sandbox-environment.md`](sandbox-environment.md) |
| `§0.4` | FOUR boot-templated files, not three | `TEMPLATED_PATHS` in [`boot_templated.py`](../packages/shellbox-mcp/src/shellbox_mcp/boot_templated.py) |
| `§0.5` | Riders | Section 6 of [`docs/sandbox-environment.md`](sandbox-environment.md) |
| `§0.6` | `DATABRICKS_CONFIG_FILE` can make the PAT reset a silent no-op | `config_file_overrides` in [`boot_templated.py`](../packages/shellbox-mcp/src/shellbox_mcp/boot_templated.py), [`doctor.py`](../packages/shellbox-mcp/src/shellbox_mcp/doctor.py) |
| `§2` | ADRs 6, 7, and 8 | — |

---

## Identifier families

### Families that already resolve

Cite these freely. Their authority is committed.

| Family | Means | Defined in |
|---|---|---|
| `F1`-`F15` | A numbered finding from the tmux spike | [`spike/FINDINGS.md`](../spike/FINDINGS.md), one heading per finding |
| `E1`-`E7` | A step of the enrollment sequence | The module docstring of [`enroll.py`](../packages/shellbox-mcp/src/shellbox_mcp/enroll.py), which lists all seven |
| `N1` | The tmux-stderr classification table | `STDERR_SIGNATURES` in [`errors.py`](../packages/shellbox-mcp/src/shellbox_mcp/errors.py) |
| `T-RESTART`, `T-CONC-1`-`T-CONC-3` | A named acceptance test | [`tests/integration/`](../tests/integration/) |
| `D1`-`D10` | A design decision from the epic | [Issue #9](https://github.com/IceRhymers/shellbox/issues/9) |

WARNING: `F1`, `F12`, and `F13` are also tmux function-key names in the send allowlist. In
[`keys.py`](../packages/shellbox-mcp/src/shellbox_mcp/keys.py) and
[`tests/unit/test_keys.py`](../tests/unit/test_keys.py) they are keys, not findings. Read for
context.

### Families whose authority is not committed

Each of these points into `.omc/`. Where one appears, say what it means on first use in the
file, or cite the committed proxy below.

| Family | Means | Nearest committed authority |
|---|---|---|
| `W1`-`W13`, with letter suffixes such as `W7f` and `W10c` | A unit of Phase 2 work, from the `§12` table. `W2` is the tmux adapter, `target.py`, `errors.py`, `naming.py`, and `keys.py`. `W4` is the MCP server: stdio wiring, the six tools, the error taxonomy. `W6` is `shellbox-registry`. `W7` is `identity.py` and `enroll.py`. `W8` is the PAT reset and `doctor`. `W9` is Lakebase wiring. `W10` is the sandbox verification run, and `W10c` is its live path — a real token against a real endpoint from inside a sandbox | The module each one produced. `W10c` is described in the module docstring of [`tests/unit/test_lakebase.py`](../tests/unit/test_lakebase.py) |
| `ADR-1`-`ADR-8` | An architecture decision. `ADR-1` resolves one tmux binary from configuration. `ADR-2` stamps identity first. `ADR-3` makes Lakebase a credential concern, not a second registry. `ADR-4` delivers `shell_send` text through a tmux buffer. `ADR-5` makes tmux the session authority. `ADR-6` self-assigns `host_id` and injects `sandbox_id`. `ADR-7` is one symlink-aware merging writer for every boot-templated file. `ADR-8` stamps `sandbox_id` from the per-boot bootstrap | `ADR-1` in [`config.py`](../packages/shellbox-mcp/src/shellbox_mcp/config.py); `ADR-3` in [`docs/lakebase-handoff.md`](lakebase-handoff.md); `ADR-5` and `ADR-6` in [`identity.py`](../packages/shellbox-mcp/src/shellbox_mcp/identity.py); `ADR-7` in [`boot_templated.py`](../packages/shellbox-mcp/src/shellbox_mcp/boot_templated.py) |
| `H1`-`H4` | A hazard from `§8`. `H4` is the pty line-discipline hazard, and it is the send path's correctness boundary | `F5` in [`spike/FINDINGS.md`](../spike/FINDINGS.md), which measured it on both platforms |
| `M1`-`M29` | A numbered measurement from the plan's appendix. `M18` is the guarantee that `shell_send` delivers `text` before `keys`. `M13` is the platform `sun_path` limit on the socket path | `M18` is asserted by [`tests/unit/test_adapter_argv.py`](../tests/unit/test_adapter_argv.py); `M13` is `SocketPathTooLong` in [`errors.py`](../packages/shellbox-mcp/src/shellbox_mcp/errors.py) and `validate_socket_path` in [`naming.py`](../packages/shellbox-mcp/src/shellbox_mcp/naming.py) |
| `R3`-`R22` | A risk from the `§13` table. `R7` is "a Lakebase outage blocks agents getting shells", answered by making registry failure non-fatal. `R22` is the same risk for LATENCY rather than errors, answered by a libpq connect timeout | `R7` is the `project` function in [`server.py`](../packages/shellbox-mcp/src/shellbox_mcp/server.py); `R22` is `CONNECT_TIMEOUT_SECONDS` in [`postgres.py`](../packages/shellbox-registry/src/shellbox_registry/postgres.py) |
| `OQ-A`-`OQ-E`, `OQ1`-`OQ5` | An open question from the `§14` table. `OQ-A` asks what `sandbox_id` derives from inside the sandbox, and is answered: nothing. `OQ-B` asks what MCP protocol version Claude Code and Codex negotiate, and whether `mcp>=1.2` satisfies both. `OQ1` asks whether the boot script recreates the `~/.databrickscfg` symlink. `OQ5` decides that `last_activity_at` advances on send only, not on read | `OQ-A` in [`probe/probe_identity.py`](../probe/probe_identity.py); `OQ-B` in [`docs/registration.md`](registration.md); `OQ5` in [`server.py`](../packages/shellbox-mcp/src/shellbox_mcp/server.py) |
| `S1`-`S12` | A numbered spike measurement. The table itself is in the plan's appendix, so a bare `S11` does not resolve from a clone | The measurements are in [`spike/FINDINGS.md`](../spike/FINDINGS.md) and the suite is [`spike/tmux_spike.py`](../spike/tmux_spike.py), but neither carries `S<n>` labels. Say what the measurement was |

## Writing a new reference

- Prefer a repo-relative path and a symbol over an identifier.
- If you use an identifier from the first table, no gloss is needed.
- If you use one from the second table, say what it means on first use in that file.
- Never write a bare `§N`. Name the document.
