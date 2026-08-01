# shellbox — glossary

ASD-STE100 allows a project dictionary on top of its base vocabulary, for domain terms the
base list cannot cover. This is shellbox's.

[`docs/writing-style.md`](writing-style.md) requires a domain term to be defined once and
then used the same way everywhere. A term is in this file because it has a precise meaning
in this repo that a reader cannot get from ordinary English.

Where a term has a single authoritative definition in the code, the entry names that file.
Some terms are conventions rather than code, and those entries name no file.

---

## Sessions and identity

**session** — A tmux session on a host's tmux server, created by `shell_create` and
addressed by name. A session outlives the `shellbox-mcp` process that created it. It does
not outlive a sandbox restart. See "What survives what" in the [README](../README.md).

**session id** — `<host_id>:<tmux_name>`. Every tool returns one. A tool also accepts a bare
`tmux_name`. Authority: `naming.session_id` in
[`naming.py`](../packages/shellbox-mcp/src/shellbox_mcp/naming.py).

**incarnation** — A uuid4 that `shell_create` writes to the tmux user option
`@shellbox_incarnation`. It identifies **which** creation of a given session name this is,
so that reusing a name does not make two different sessions indistinguishable.

An incarnation is not a delivery receipt. It makes misdelivery detectable **after the
fact**. `shell_send` returns the incarnation it targeted, so a caller can tell later that a
send went to a session that has since been replaced.

The value is shape-checked when read back. A value carrying a TAB or a LF reads as
**absent**, not as valid. Authority: `INCARNATION_OPTION` and `_own_incarnation` in
[`tmux.py`](../packages/shellbox-mcp/src/shellbox_mcp/tmux.py).

**stamp** — The act of writing the incarnation onto a session. Also used for writing a
`host_id` onto a session, via `@shellbox_host_id`. That is how a host recovers its own
identity when its cache is missing. Both stamps are per **session**, not per server, so a
tmux server with no sessions carries no stamp. Authority: `stamp_host_id` in
[`tmux.py`](../packages/shellbox-mcp/src/shellbox_mcp/tmux.py).

**foreign** — A session on this host's tmux server that carries no readable incarnation.
shellbox cannot prove it owns the session. There are two causes and shellbox does not
distinguish them: something other than shellbox created the session, or the session was
observed between its creation and its stamp.

`shell_list` reports `foreign: true` with `incarnation: null`. `shell_send`, `shell_read`,
`shell_resize`, and `shell_kill` all refuse a foreign session with `not_found`. An empty
incarnation is **never** an incarnation match. Authority: the `foreign` field of
`SessionRecord` and `_resolve_owned` in
[`tmux.py`](../packages/shellbox-mcp/src/shellbox_mcp/tmux.py).

**host** — One machine running one tmux server, with one row in the `hosts` table.

**host_id** — A self-assigned uuid4, persisted to `$HOME/.shellbox/host.json`. The write
arbitrates with `os.link`, not with `O_CREAT|O_EXCL` on the final path. `os.link` is atomic
and fails with `EEXIST`, so exactly one process wins and the rest adopt the winner. `O_EXCL`
on the final path would publish the file before it had content. (`O_EXCL` is still used on
the staging file.)

A `host_id` is self-assigned because **a sandbox cannot learn its own `sandbox_id`** — not
from its environment, its disk, its hostname, or PID 1's environment.
Never derive a `host_id` from `/etc/machine-id`: that value is baked into the image, so it
is identical on every sandbox from that image. Authority:
[`identity.py`](../packages/shellbox-mcp/src/shellbox_mcp/identity.py), and section 1 and
section 2 of [`docs/sandbox-environment.md`](sandbox-environment.md).

**owner_email** — Who the host belongs to, resolved from the ambient credential, then the
identity cache, then `SHELLBOX_OWNER_EMAIL`. When none of those answer, enrollment is
**deferred**: shell tools keep working and the inventory waits. shellbox never invents an
owner. Authority: `resolve_owner_email` in
[`identity.py`](../packages/shellbox-mcp/src/shellbox_mcp/identity.py).

## The registry

**registry** — The Postgres or Lakebase database holding the `hosts` and `sessions` tables.
It is an inventory. It is not the source of truth.

**projection** — A registry write. tmux is the authority; the registry is a downstream copy
of what tmux already decided. A failed projection is a stale inventory, never a failed tool
call — a database outage must never stop an agent getting a shell. A failure returns a
`registry_warning` on an otherwise successful result.

Only `shell_create` and `shell_kill` carry a `registry_warning` field. `shell_send` also
projects a row, to advance `last_activity_at`, but it discards the outcome — a failed
projection on the send path is silent. Authority: the `project` function, and the
`CreateResult`, `KillResult`, and `SendResult` TypedDicts in
[`server.py`](../packages/shellbox-mcp/src/shellbox_mcp/server.py).

**enrollment** — The `E1`-`E7` sequence that puts a host in the inventory: resolve
`host_id`, resolve `owner_email`, cache it, write the `hosts` row, reconcile orphans, record
what is known about the host, then heartbeat. It runs on a background thread and is never
fatal. Authority: the module docstring of
[`enroll.py`](../packages/shellbox-mcp/src/shellbox_mcp/enroll.py), which lists all seven
steps.

**session status** — One of exactly four values, enforced by a check constraint:

| Status | Means |
|---|---|
| `live` | The session exists and is in use |
| `idle` | The session exists and has not been driven recently |
| `reaped` | shellbox killed the session |
| `orphaned` | The session's tmux counterpart is gone, so the row no longer describes anything real |

**host status** — One of `active`, `stale`, or `stopped`. Authority for both tables:
[`models.py`](../packages/shellbox-registry/src/shellbox_registry/models.py).

**orphan reconciliation** — Marking a session row `orphaned` when its tmux counterpart is
gone. It is **guarded**: a process may only do this when its own resolved tmux socket path
matches the one recorded for the host. Without that guard, a process looking at the wrong
socket would mark every live session on the host `orphaned`. Authority:
`reconcile_orphans` in [`enroll.py`](../packages/shellbox-mcp/src/shellbox_mcp/enroll.py).

## Delivery and limits

**delivery: unverified** — The only value `shell_send` returns for `delivery`. shellbox can
confirm that tmux accepted the input. It cannot confirm that the pane's process read it.
Read the session back to observe an effect.

**submitted_bytes** — Bytes handed to tmux. Not bytes the pane's process read.

**line-length boundary** — The limit on bytes since the last newline, enforced before tmux
is invoked. It is a **correctness** boundary, not a resource limit: the pty line discipline
drops an over-long line on macOS and **truncates** it on Linux, and a truncated command is a
different command that still runs. The limit is the first **rejected** length, so the
comparison is `>=`. Authority: `max_send_line_bytes` in
[`tmux.py`](../packages/shellbox-mcp/src/shellbox_mcp/tmux.py), measured as `F5` in
[`spike/FINDINGS.md`](../spike/FINDINGS.md).

**error taxonomy** — The closed set of error codes a tool may return. A failure that shellbox
classifies carries `{"error": {"code", "message", "session"}}`, so an agent branches on a code
instead of parsing prose. Each tool's docstring ends with its own codes.

Two failures fall outside the taxonomy and pass through as the SDK's own message: an unknown
tool name, and arguments that pydantic rejects. Neither describes a session or tmux, so
inventing a code for them would be worse. Authority:
[`errors.py`](../packages/shellbox-mcp/src/shellbox_mcp/errors.py), and
`_install_error_boundary` in
[`server.py`](../packages/shellbox-mcp/src/shellbox_mcp/server.py).

## The sandbox

**Lakebox / sandbox** — The Databricks agent host that `shellbox-mcp` runs on.

**boot-templated** — A `$HOME` file the platform re-points at a `/run/lakebox/` target on
**every** boot, with an unconditional `ln -sfn`. An edit to one is silently discarded. This
is why shellbox **replaces** such a file rather than writing through it.

There are four, and `TEMPLATED_PATHS` is the authoritative list:

| Path | Target |
|---|---|
| `~/.databrickscfg` | the workspace credential |
| `~/.codex/config.toml` | the Codex config |
| `~/.claude/settings.json` | the Claude Code settings |
| `~/.databricks/token-cache.json` | the OAuth token cache |

`~/.bashrc` and `~/.bash_profile` are also rewritten every boot, but by in-place templating
rather than by this symlink mechanism, so they are not in `TEMPLATED_PATHS`. Authority:
`TEMPLATED_PATHS` in
[`boot_templated.py`](../packages/shellbox-mcp/src/shellbox_mcp/boot_templated.py), and
section 3 of [`docs/sandbox-environment.md`](sandbox-environment.md).

**autostop** — A sandbox stops after an idle timeout, 600 s by default. A stopped sandbox
loses its tmux server, because the VM stops and the server is a process on it. The socket
file itself survives — it defaults to `$SHELLBOX_STATE_DIR/tmux.sock`, which is under `$HOME`
— but a socket file with no process listening on it is inert.

**no in-VM boot hook** — There is no `cron`, no user `systemd`, no `/etc/rc.local`, and
`~/.bashrc` is boot-templated. Nothing inside the sandbox can run at boot, so re-enrolment
must be driven from the agent side on next start. Authority: section 4 of
[`docs/sandbox-environment.md`](sandbox-environment.md).

---

## Terms to use consistently

The tool names are the verbs. Use them, and do not substitute a synonym.

| Use | Not |
|---|---|
| create a session | spawn, start, open, launch |
| send input | write, type, push, inject |
| read a session | capture, fetch, dump, scrape |
| list sessions | enumerate, inventory (as a verb), scan |
| resize a session | resize is the only verb; not reshape or set dimensions |
| kill a session | destroy, terminate, close, tear down |
| projection / project a row | sync, persist, save, mirror |
| foreign | unknown, unowned, alien, untrusted |
| orphaned | dead, lost, stale, dangling |
