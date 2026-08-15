# Registering `shellbox-mcp` with a harness

`shellbox-mcp` is a stdio MCP server (plan §6; plan section numbers are indexed in
[`docs/plan-sections.md`](plan-sections.md)). Every setting comes from the environment
(plan §5). The entrypoint takes **zero CLI arguments** — `{"command":
"shellbox-mcp", "args": []}` is a complete, correct registration in any harness. This
is not a convenience. It is a hard constraint (see [Zero-args / env-only](#zero-args--env-only-the-buzz-constraint-6)
below).

This document is the product of real testing, not inference. The tests launched
Claude Code and Codex CLI against a real, built `shellbox-mcp`, and called tools over
the real stdio transport. They did not infer registration syntax from `--help` output.
Both clients were installed on the machine this was verified on — `claude` 2.1.220,
`codex` 0.142.0-alpha.1. Where a step could not be completed, the text says so plainly
rather than writing it up as if it passed. See
[Codex: what could not be verified](#what-could-not-be-verified).

## Quick reference

| | Claude Code | Codex |
|---|---|---|
| Config file | `~/.claude.json` (`mcpServers`, per-scope — see below) | `~/.codex/config.toml` (`[mcp_servers.<id>]`) |
| Negotiated MCP protocol version | **`2025-11-25`** (measured) | **`2025-06-18`** (measured) |
| `initialize`/`tools/list` handshake | Verified | Verified |
| Live `tools/call` round trip | Verified (`shell_create`→`shell_send`→`shell_read`→`shell_kill`) | **Not verified** — see gap below |
| Server stderr destination | Discarded unless the harness is run with `--debug`/`--debug-file`; then written verbatim | **Not determined** — see gap below |

Both measured protocol versions fall inside what the installed SDK (`mcp==1.28.1`)
supports. That SDK matches the `mcp>=1.2,<2` bound in
`packages/shellbox-mcp/pyproject.toml`. This is the direct, measured answer to
**OQ-B**: what MCP protocol version Claude Code and Codex negotiate, and whether
`mcp>=1.2` satisfies both. (See [`docs/plan-sections.md`](plan-sections.md) for what the
plan's identifiers refer to.) The upper bound is not too tight for either harness observed
here.

## Claude Code

### Registering

```sh
claude mcp add --scope local shellbox -- /absolute/path/to/.venv/bin/shellbox-mcp
```

`--scope` controls **where** `claude mcp add` writes, all inside `~/.claude.json`:

- `local` (default) — private to you, scoped to the current project directory. Lands
  under that project's entry in `~/.claude.json`, not the top-level `mcpServers` key.
- `user` — global, applies to every project you open with Claude Code. Lands in the
  top-level `mcpServers` object.
- `project` — shared with anyone who clones the repo, via a `.mcp.json` file at the
  repo root (not `~/.claude.json` at all).

For a single agent host running `shellbox-mcp` for one user, `user` scope is the
closest match to "register this server for me everywhere." That is also what the
acceptance criterion means by "via `~/.claude.json` `mcpServers`". This doc's
verification used `--scope local` instead. That choice kept the test from touching
the global `mcpServers` entry that this machine's normal Claude Code configuration
uses for unrelated servers. The write path (`~/.claude.json`) and format are
identical between scopes — only the JSON location differs.

Verify:

```sh
$ claude mcp get shellbox
shellbox:
  Scope: Local config (private to you in this project)
  Status: Connected
  Type: stdio
  Command: /absolute/path/to/.venv/bin/shellbox-mcp
```

`Connected` means Claude Code actually spawned the process and completed the MCP
`initialize` handshake — it is not just a config-file syntax check.

### Calling a tool

This example drives the call non-interactively with `claude -p` and an explicit tool
allowlist. Claude Code exposes MCP tools as `mcp__<server-name>__<tool-name>`:

```sh
claude -p "Use the shellbox MCP server's shell_create tool to create a session named
w11-claude-test with cwd=/tmp, then shell_send text='echo hello-from-claude' and
keys=[\"Enter\"], then shell_read, then shell_kill. Report the raw JSON of each call." \
  --allowedTools "mcp__shellbox__shell_create,mcp__shellbox__shell_send,mcp__shellbox__shell_read,mcp__shellbox__shell_kill" \
  --output-format json
```

This ran a real create → send → read → kill sequence against a real tmux server and
returned the real tool payloads, e.g.:

```json
{"session":"3f9c1e70-...:w11-claude-test","tmux_name":"w11-claude-test","cwd":"/private/tmp",
 "cols":80,"rows":24,"created":true,"incarnation":"a1c6ecf0-...","host_id":"3f9c1e70-...",
 "registry_warning":null}
```

(`host_id` is a self-assigned uuid4 here because `SHELLBOX_HOST_ID` was intentionally
left unset for this test. See plan §10 and W7 (enrollment). This is not a defect.)

### Protocol version

The verification wrapped the real binary in a one-line `tee` shim (`tee stdin.log |
shellbox-mcp | tee stdout.log`). That let it inspect the literal JSON-RPC frames.
Claude Code's own `--debug` output reports connection *capabilities*, not the
negotiated `protocolVersion` string. So the shim was the only way to get a *measured*
answer rather than an inferred one. The client's `initialize` request and the
server's response both carried:

```json
{"method":"initialize","params":{"protocolVersion":"2025-11-25", ...
{"jsonrpc":"2.0","id":0,"result":{"protocolVersion":"2025-11-25", ...
```

### Where server stderr goes

**Discarded by default.** A plain `claude -p` run's session transcript
(`~/.claude/projects/<project>/<session>.jsonl`) contains no trace of the MCP
server's stderr anywhere. The verification confirmed this by grepping a full
transcript for `stderr` after a tool call. That search found only unrelated hook I/O,
always empty strings for MCP.

**Captured only in debug mode.** Passing `--debug` with **any category**, or
`--debug-file <path>`, makes Claude Code write a full debug transcript. (`mcp` was the
category used here, but the behavior is not gated on that specific filter.) The transcript
goes either to a
session-scoped `~/.claude/debug/<sessionId>.txt` or to the path given to
`--debug-file`. Inside it, every line the server writes to stderr appears verbatim,
at a forced `[ERROR]` level regardless of actual severity:

```
2026-07-31T16:37:55.268Z [ERROR] "MCP server \"shellbox\" Server stderr: 2026-07-31 09:37:55,257 WARNING shellbox_mcp.config: SHELLBOX_HOST_ID is unset ...\n2026-07-31 09:37:55,257 INFO shellbox_mcp.server: shellbox-mcp serving on stdio: tmux=... registry=none\n"
```

**Practical consequence for `SHELLBOX_LOG_LEVEL` (plan §5, §6 review item 25):** on
Claude Code, a user only ever sees shellbox's stderr if they proactively launch
Claude Code with `--debug`/`--debug-file`. There is no ambient log file it lands in
otherwise. Tell anyone debugging a Claude Code + shellbox-mcp session to re-run with
`--debug` first.

## Codex

> **Inside a Lakebox sandbox, `~/.codex/config.toml` is a symlink into `/run/lakebox/`
> and the platform re-points it on EVERY boot.** So the platform destroys a
> registration written by `codex mcp add` — or by hand — at the next start. Writing
> *through* the symlink instead puts the file in `/run`, which is wiped every boot
> too. This was measured from the platform's own boot script. See section 3 of
> [`docs/sandbox-environment.md`](sandbox-environment.md).
>
> In a sandbox, register with the command that knows this, **once per boot**:
>
> ```sh
> shellbox-mcp bootstrap --register-codex
> ```
>
> It replaces the *symlink* with a regular 0600 file and **merges** shellbox in. That
> preserves the `model_provider`/`model_providers` keys Codex needs to reach its model
> at all — a wholesale overwrite would discard them. Running it twice is a no-op. This
> was verified against a live sandbox's real template.
>
> `~/.claude.json` is **not** affected: it is a genuine file in persistent `$HOME`, so
> Claude Code registration is durable and needs no per-boot step.

### Registering

```sh
codex mcp add shellbox --env SHELLBOX_LOG_LEVEL=DEBUG -- /absolute/path/to/.venv/bin/shellbox-mcp
```

(Note the argument order: `--env` must come *before* the `--` that introduces the
command. `codex mcp add --env K=V shellbox -- ...` fails with `missing required
argument 'commandOrUrl'` because it isn't yet at `--`.)

This writes directly to `~/.codex/config.toml`:

```toml
[mcp_servers.shellbox]
command = "/absolute/path/to/.venv/bin/shellbox-mcp"

[mcp_servers.shellbox.env]
SHELLBOX_LOG_LEVEL = "DEBUG"
```

**Observed side effect, worth knowing before running this on a machine with other
MCP servers already configured:** `codex mcp add` rewrites the *entire*
`config.toml`, not just the new stanza. On this test machine that reordered
unrelated keys and normalized at least one value's type (`startup_timeout_sec = 120`
→ `120.0`). Harmless here, but back up `~/.codex/config.toml` before running this
against a config you care about.

Verify:

```sh
$ codex mcp get shellbox
shellbox
  enabled: true
  transport: stdio
  command: /absolute/path/to/.venv/bin/shellbox-mcp
```

### Protocol version

This used the same tee-shim technique as Claude Code (Codex's `exec --json` event
stream does not surface the raw `initialize` frame either). The client's request and
the server's response both carried:

```json
{"jsonrpc":"2.0","id":0,"method":"initialize","params":{"protocolVersion":"2025-06-18","clientInfo":{"name":"codex-mcp-client","title":"Codex","version":"0.142.0-alpha.1"}}}
{"jsonrpc":"2.0","id":0,"result":{"protocolVersion":"2025-06-18","serverInfo":{"name":"shellbox", ...
```

Codex negotiates an **older** protocol version than Claude Code (`2025-06-18` vs.
`2025-11-25`). Both are inside what `mcp==1.28.1` supports server-side — the SDK
answered with whichever version each client asked for, rather than insisting on one
fixed version. This is the second half of closing OQ-B: the `mcp>=1.2,<2` bound
covers both harnesses actually measured, on both ends of their version range.

The handshake also confirms `tools/list` end-to-end: all six tools (`shell_create`,
`shell_send`, `shell_read`, `shell_list`, `shell_resize`, `shell_kill`) came back
with their full schemas. Those schemas match the FastMCP-derived schemas Claude Code
saw.

### What could not be verified

**This test could not complete a live `tools/call` round trip against Codex in this
environment.** This document reports that gap rather than papering over it.

What was tried, non-interactively via `codex exec`:

1. Plain `codex exec "...call shell_create..."` — every MCP tool call came back
   `{"error":{"message":"user cancelled MCP tool call"}}` immediately. The wire-tap
   confirms the `tools/call` request **never reached the server at all** (only
   `initialize`/`tools/list` frames appear in the captured stdin log). So this is
   Codex's own approval gate declining the call client-side, not a shellbox-mcp
   failure.
2. Marking the working directory as a trusted project
   (`[projects."<path>"] trust_level = "trusted"` in `config.toml`, the same key
   Codex's own interactive trust prompt persists). Same result: still cancelled.
3. `codex exec --dangerously-bypass-approvals-and-sandbox` and
   `codex exec -c approval_policy=never` are both documented CLI options for this
   case ("never ask for approval" / "skip all confirmation prompts"). But this test
   environment's own sandboxing layer, unrelated to Codex, blocked the shell command
   attempting either. That block happened before Codex itself ever ran. It could not
   be determined whether either flag would have let the call through.

So: **registration, connection, and the full MCP handshake with Codex are verified
facts. A completed tool invocation against Codex is not.** File this as a follow-up
for whoever next has an unrestricted terminal. Do not file it as "Codex doesn't
support tool calls." That claim is very unlikely to be true, and it is not what was
measured here.

### Where server stderr goes

**Not determined — an explicit gap, not a fabricated answer.** What was checked and
came up empty:

- The `codex exec --json` event stream: zero occurrences of `stderr`, the
  `shellbox_mcp` logger name, or the server's own startup log lines. That holds
  across the full captured output of every run.
- `~/.codex/log/` — exists, but was empty throughout testing.
- `~/.codex/sessions/` contains dated subdirectories, but nothing that looked like a
  plaintext transcript for these runs. The rest of this Codex install's state sits in
  SQLite databases (`state_*.sqlite`, `logs_*.sqlite`, ...). This task's scope did not
  include querying those further.

Best-supported conclusion from what was checked: this Codex installation does not
surface `shellbox-mcp`'s stderr anywhere a user would see it by default. That is
distinct from "Codex discards it" (not proven) and from "Codex logs it somewhere"
(not found). If this becomes a real debugging question, say so as a known gap, and
check the SQLite state files directly first.

(Also worth noting: the `codex` install used for this doc is a heavily customized,
ChatGPT-desktop-bundled build, not a vanilla `codex-cli` checkout. It carries plugins
for browser/computer-use/node_repl, a Databricks model provider, and an internal
"Omnigent" policy layer. During testing, that policy layer intercepted an unrelated
config change. A stock open-source Codex CLI may behave differently for both the
tool-call approval gate and the stderr question above.)

## Zero-args / env-only: the Buzz constraint (#6)

`shellbox-mcp`'s CLI (`cli.py`) accepts exactly one subcommand, `serve`, which is
also the default. It takes **no flags** — `config.py` reads every setting from the
environment (plan §5). This is deliberate, not an oversight, and the reason is a
different harness entirely: **Buzz**.

`buzz-acp`'s `build_mcp_servers` returns **at most one** `McpServer` for the
`buzz-agent` runtime, constructed with `args: vec![]`. There is no way to hand it
additional CLI arguments. `BUZZ_ACP_MCP_COMMAND` is also a reserved configuration
key. For that runtime, `buzz-dev-mcp` already occupies the single MCP server slot.
Consequently:

- Any design for `shellbox-mcp` that required a CLI flag to configure could **never**
  register under Buzz, regardless of how that flag was spelled. The slot doesn't
  exist to put it in.
- Even with a zero-args design, `shellbox-mcp` **cannot occupy that one slot today**,
  because `buzz-dev-mcp` already does. Getting shellbox-mcp running under
  `buzz-agent` is **out of scope here and deferred to
  [issue #6](https://github.com/IceRhymers/shellbox/issues/6)**. It needs a change on
  the Buzz side — either a second slot, or composing multiple MCP servers behind one
  process. `shellbox-mcp` cannot make that change unilaterally.
- The env-only design is what keeps that a Buzz-side problem instead of a shellbox
  problem: `{"command": "shellbox-mcp", "args": []}` is already exactly the shape
  `buzz-acp` can express. The day Buzz can offer shellbox-mcp a slot, no changes are
  needed here — only environment variables need to be threaded through.

No Buzz installation was available to test against directly. The constraint above is
sourced from `buzz-acp`'s own source (`crates/buzz-acp/src/lib.rs`, per the plan §4).
It is documented rather than exercised.

## Configuration reference

Every environment variable `shellbox-mcp` reads, transcribed from `config.py` (and
`shellbox_registry/dsn.py` for the database options) — nothing here is invented:

| Var | Default | Notes |
|---|---|---|
| `SHELLBOX_TMUX_BIN` | `shutil.which("tmux")`, else the bare string `"tmux"` | Resolved once, at process start. |
| `SHELLBOX_TMUX_SOCKET` | `$SHELLBOX_STATE_DIR/tmux.sock` | Private tmux server socket. |
| `SHELLBOX_STATE_DIR` | `~/.shellbox` | Created (mode `0700`) at `serve` startup if absent. |
| `SHELLBOX_HISTORY_LIMIT` | `20000` | Must be a positive integer or `serve` refuses to start (`ConfigError`). |
| `SHELLBOX_DEFAULT_COLS` | `80` | ″ |
| `SHELLBOX_DEFAULT_ROWS` | `24` | ″ |
| `SHELLBOX_MAX_SEND_BYTES` | `1048576` (1 MiB) | tmux-server memory guard only. |
| `SHELLBOX_MAX_SEND_LINE_BYTES` | `1000` | The real correctness boundary — see plan §8 and H4 (the pty line-discipline hazard). |
| `SHELLBOX_LOG_LEVEL` | `INFO` | One of `DEBUG`/`INFO`/`WARNING`/`ERROR`/`CRITICAL`. An unrecognised value **warns and falls back to `INFO`** instead of failing startup. stderr is the only diagnostic channel, so refusing to start would be worse. |
| `SHELLBOX_DATABASE_URL` | unset | A full `postgresql://...` DSN. Wins over the `SHELLBOX_PG_*` parts below if both are set. |
| `SHELLBOX_PG_USER` / `_PASSWORD` / `_HOST` / `_PORT` / `_DB` | `shellbox` / `shellbox` / `localhost` / `55432` / `shellbox` | These assemble a DSN only if `SHELLBOX_DATABASE_URL` is unset **and** at least one of these is set. |
| `SHELLBOX_OWNER_EMAIL` | unset | An **override**, honoured but deliberately **not cached** — symmetrically with `SHELLBOX_HOST_ID`. Debugging one process must not permanently change what the host claims about itself. With no owner resolvable from anywhere, `shellbox-mcp` **skips** the inventory write rather than writing a placeholder, and it says so. |
| `SHELLBOX_HOST_ID` | unset | An **override**, not cached. Left unset, the host **self-assigns a uuid4** and remembers it in `$SHELLBOX_STATE_DIR/host.json`. There is no `unknown:<machine-id>` fallback any more. `/etc/machine-id` is baked into the sandbox image, so it was identical on every sandbox. That would have collapsed the whole fleet into one `hosts` row. See section 2 of [`docs/sandbox-environment.md`](sandbox-environment.md). |
| *(the sandbox id)* | n/a | **Not an environment variable.** A sandbox cannot learn which sandbox it is. The bootstrap path injects the id once per boot, from outside, using `shellbox-mcp bootstrap --sandbox-id <id>`. `shellbox-mcp doctor` reports `sandbox_id: NULL` when that has not happened. |
| `SHELLBOX_IDLE_TIMEOUT_SECONDS` | `1800` (30 minutes) | Must be an integer in `[60, 86400]` or `serve` refuses to start (`ConfigError`). Assumed, not measured — a conservative starting point with no usage data behind it. |
| `SHELLBOX_REAP_INTERVAL_SECONDS` | `60` | Must be an integer in `[10, 3600]` or `serve` refuses to start (`ConfigError`). Matches `HEARTBEAT_SECONDS = 60.0` so the two threads share one cadence. The sweep only samples the idle predicate periodically, so the effective timeout an operator gets is `[SHELLBOX_IDLE_TIMEOUT_SECONDS, SHELLBOX_IDLE_TIMEOUT_SECONDS + SHELLBOX_REAP_INTERVAL_SECONDS)` — the configured timeout rounded up to the next sweep. |

**No database configuration at all ⇒ `NullRegistry`.** This is the documented design
(plan §5), not a missing feature. Every
`upsert_host`/`upsert_session`/`get_host`/`get_session`/`list_sessions_for_host` call
on `NullRegistry` is a no-op that returns successfully (`None` or `[]`). Every tool
call still works, because tmux is the authority on sessions regardless of the
registry (plan §9). A registry outage or an entirely unconfigured registry degrades
shellbox to "shells work, the inventory is stale," never to "shells don't work."

This was confirmed directly. Both the Claude Code and the Codex test runs above ran
with no `SHELLBOX_DATABASE_URL` set. `shell_create`/`shell_send`/`shell_read`/`shell_kill`
all succeeded — `registry_warning` came back `null` because there was no registry
configured to warn about.
