# shellbox

Agent shell sessions with browser rendering on Databricks.

Two halves:

- **`shellbox-mcp`** — a stdio MCP server that ships onto an agent host (a Databricks Sandbox / "Lakebox"). Gives the agent persistent, named, tmux-backed shell sessions and registers them centrally.
- **`shellbox-app`** — a Databricks App that renders those sessions as live, interactive browser terminals.

Sessions are created by agents, attached to by humans, and reaped on inactivity. Harness-agnostic (Claude Code, Codex, Buzz — anything speaking MCP).

Work in progress. Design and phased plan: [epic #9](https://github.com/IceRhymers/shellbox/issues/9).

---

## What survives what

This is the single most important thing to understand before relying on a shellbox session,
and it is not symmetric. Read it before filing a bug about a session that vanished.

| Event | Sessions survive? | Why |
|---|---|---|
| **`shellbox-mcp` restarts** (crash, `SIGKILL`, agent session rotation, MCP reconnect) | **YES** | A tmux client forks a server that *reparents away*. The tmux server is a **sibling** of the MCP process, never a descendant, so it is not killed with it. `shellbox-mcp` holds **zero session state in-process** — tmux is the authority — so a fresh process rediscovers every session. |
| **The sandbox stops and starts** (`sandbox stop`/`start`, or a 10-minute idle autostop) | **NO** | tmux's socket lives in `/tmp`, which is **wiped** on restart. The tmux server and every session go with it. `$HOME` files survive; a *running process* does not. |

The first row is a guarantee with a test behind it (`T-RESTART`: `SIGKILL` the MCP process, assert
the tmux server outlives it, the session is still usable, and its incarnation token is unchanged).
**If that test's assertion ever changes, this table is wrong by construction** — they are meant to
say the same thing.

The second row cannot be fixed in v1, and it is worth being explicit about why rather than leaving
it to be rediscovered. **There is no in-VM boot hook available:**

- no `cron` (`crontab` is not installed)
- no user `systemd` (`Failed to connect to bus: No medium found`; no `~/.config/systemd/user`)
- no `/etc/rc.local`
- `~/.bashrc` and `~/.bash_profile` exist but are **rewritten from a template on every boot** —
  confirmed by restarting a sandbox and observing a new mtime with an identical md5, so any edit
  there is silently clobbered

The only boot-time hooks are Databricks' own `setup-home-directory.sh` / `setup-user.sh`.
Consequently **re-enrolment and session re-creation must be driven from the agent side on next
start**, not by anything inside the VM. shellbox reports this honestly instead of hiding it: when a
session's tmux counterpart is gone, its registry row is marked `orphaned`, so the inventory stays
accurate rather than advertising sessions that no longer exist.

A newly created sandbox also defaults to `idleTimeout: 600s` with `noAutostop: false`, so it will
**autostop after 10 minutes idle** unless reconfigured. Keepalive is Phase 5's job
([#5](https://github.com/IceRhymers/shellbox/issues/5)); Phase 2 records the observed values at
enrolment and warns when autostop is enabled, so the condition is visible rather than mysterious.

## Corrections to issue #2

[Issue #2](https://github.com/IceRhymers/shellbox/issues/2) was written before the Phase 1 transport
probe ([#1](https://github.com/IceRhymers/shellbox/issues/1)) ran. Three of its statements are
**measured false**, and they are recorded here so a later reader does not follow the issue text into
a wrong assumption:

| Issue #2 says | Measured |
|---|---|
| *"tmux is NOT in the sandbox image … ship a static tmux binary into `$HOME`"* | **tmux 3.4 is present at `/usr/bin/tmux`** (and `screen` 4.09.01). Vendoring is optional, not required; shellbox resolves the binary from `$SHELLBOX_TMUX_BIN` and falls back to `PATH`. |
| *"do not assume sudo/apt (unprobed for uid 10086)"* | **Passwordless root** — `(ALL) NOPASSWD: ALL`. shellbox still does not *use* it, since depending on root would break the host-agnostic model. |
| *"`shell_send(session, text?, keys?)` — `text` → `tmux send-keys -l`"* | **Rejected.** `send-keys -l` **silently drops a lone `;`** (rc=0, the character never arrives, and `--` does not help) and errors on text starting with `-`. `text` is delivered through a tmux **buffer** (`load-buffer -` / `paste-buffer`); `send-keys` is used only for named keys from a strict allowlist. |

Full probe results: [`probe/FINDINGS.md`](probe/FINDINGS.md). tmux behaviour that `§7` of the plan
depends on is measured by an executable suite, [`spike/tmux_spike.py`](spike/tmux_spike.py), which
runs in two lanes — tmux 3.6b locally and **tmux 3.4 under `ubuntu:24.04`**, the sandbox's version —
and is a CI gate: if either lane fails, the tmux adapter's specification is not considered valid.
Findings: [`spike/FINDINGS.md`](spike/FINDINGS.md).
