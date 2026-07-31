# The Lakebox sandbox environment — measured

What a Databricks Sandbox ("Lakebox") actually provides, as measured rather than assumed.
`shellbox-mcp` runs inside one, so several of its design decisions are forced by the facts
below — and three earlier design premises were **wrong** until they were measured.

**Reproduce everything here** with [`probe/probe_identity.py`](../probe/probe_identity.py); its
complete output is committed as
[`probe/sandbox-identity-results.jsonl`](../probe/sandbox-identity-results.jsonl):

```sh
databricks sandbox start <id> --profile <profile>
B64=$(base64 -i probe/probe_identity.py | tr -d '\n')
databricks sandbox ssh <id> --profile <profile> -- \
  "printf %s '$B64' | base64 -d > /tmp/probe_identity.py && \
   PROBE_SANDBOX_ID=<id> python3 /tmp/probe_identity.py"
```

Measured 2026-07-31 on `realistic-phoenix-2742` (`fevm-west`, us-west-2, tmux 3.4,
Python 3.12.3, Databricks CLI v1.7.0 in-image, 8274 MB / 4 cores).

> ⚠️ **The probe must never print a credential.** It runs against four files holding live
> secrets and reports only key names, lengths and 4-character prefixes. Its output is
> committed to this repo — keep it that way.

---

## 1. A sandbox cannot learn which sandbox it is

**This is the single most consequential fact here.** Six independent searches found no
sandbox identifier available from inside:

| Where | Result |
|---|---|
| Process environment (exec) | **12 variables**, none identifying |
| Process environment (login shell) | adds only `DATABRICKS_CONFIG_PROFILE=DEFAULT` |
| Disk — `grep -rl <id> /etc /run /var/lib /opt /usr/local` | **zero files** |
| Host naming | `hostname`=`databricks` (from `vminit_hostname=` in `/proc/cmdline`), `/etc/hostname`=`sandbox`, `getfqdn()`=`localhost` — image constants, identical everywhere |
| `/run/lakebox/` | only the templated credential files (§3) |
| **PID 1's environment** (`sudo cat /proc/1/environ`) | 746 bytes, fully enumerated: **no sandbox id.** `LOCATION` is *region- and cluster*-scoped (`kubernetes-cluster:prod/aws/public/us-west-2/s1/nb78se`), shared by many sandboxes |

The **only** source that knows the id is the workspace API, and
`GET /api/2.0/lakebox/sandboxes` is **caller-scoped**: it returns every sandbox the caller
owns, with **no locally-readable field to match "which of these am I."**

### ⚠️ `~/.databricks/sandbox.json` is a trap, not an answer

It is a *regular* file in persistent `$HOME` and it *does* contain this sandbox's id. It is
still **not** a platform fact:

- its mtime is **two minutes after boot**, exactly when the probe ran `databricks sandbox list`;
- the boot script never references it (`grep -c` → 0);
- it is the **CLI's own client-side cache** of the *caller-scoped* list, keyed by profile;
- it contains the right id **only because this user owns exactly one sandbox**.

A reader who finds this file will reasonably conclude the id is available locally. It is not.

### What a sandbox *can* prove locally

That it **is** a Lakebox — PID 1 is `sandbox-daemon --enable-sshd --uid 10086`, and
`/etc/lakebox/` exists. Never **which** one. That asymmetry is why `hosts.kind` can be
populated and `hosts.sandbox_id` cannot: `sandbox_id` must be **injected** by the bootstrap
path, which runs from outside and does know the id.

**Consequence for shellbox:** `host_id` is a self-assigned uuid4 persisted to
`$HOME/.shellbox/host.json`, created under `O_CREAT|O_EXCL` so that concurrent MCP processes
adopt one winner instead of minting one identity each.

## 2. `/etc/machine-id` is baked into the image — never use it as a host identity

```
/ is overlay:   lowerdir=/containers/images/driver, upperdir=/containers/overlays/driver/upper
/etc/machine-id mtime 2026-07-27 23:18:25   ← image build date (/etc/hostname is 23:22 same day)
/etc is not separately mounted
```

Dated to the **image build**, never to a boot, served from the overlay's read-only lower
layer ⇒ **the same string on every sandbox launched from that image**. Use
`/proc/sys/kernel/random/boot_id` when you want a value that changes per boot; use neither
as a host identity.

> A host id derived from `machine-id` does not degrade to "unhelpful but distinct" — it
> collapses **the entire fleet into one `hosts` row**, each host overwriting the others'
> `owner_email`, `gateway_host` and `last_seen_at`.

*Status: the mechanism is established; the direct cross-sandbox comparison is still to run.*

## 3. Four `$HOME` files are re-templated on **every** boot

`/etc/lakebox/setup-home-directory.sh` (root, mode 555, on the read-only image layer) is the
authority. It runs **once per boot** — `~/.home-setup-complete` stores the current
`boot_id`, and the run is serialized on `/run/lakebox-home-setup.lock` — and it does this:

```sh
write_placeholder() { if [[ ! -e "$target" ]]; then printf '%s' "$content" > "$target"; ... }
link()             { ... ln -sfn "$target" "$linkpath"; ... }   # UNCONDITIONAL, every boot
```

| Symlink | Target | Placeholder content |
|---|---|---|
| `~/.databrickscfg` | `/run/lakebox/databrickscfg` | a comment-only stub |
| `~/.codex/config.toml` | `/run/lakebox/codex-config.toml` | a comment-only stub |
| `~/.claude/settings.json` | `/run/lakebox/claude-settings.json` | `{"env":{}}` |
| `~/.databricks/token-cache.json` | `/run/lakebox/token-cache.json` | `{"version":1,"tokens":{}}` |

Four facts follow, each of which changed a design decision:

1. **`ln -sfn` is unconditional**, so a regular file written at any of these paths is
   **replaced by a symlink at the next boot**. Any reset of these files is therefore a
   **per-boot** operation, not once-per-sandbox.
2. **`/run` is wiped between boots.** Proven rather than assumed: `write_placeholder` writes
   *only if absent*, and the placeholder's mtime is the current boot's, so it was absent.
   (`/proc/mounts` in this namespace lists only the overlay root, so whether `/run` is
   *tmpfs* is unverifiable from inside — and irrelevant. "Wiped between boots" is the fact
   the code needs.)
3. **Never write *through* the symlink.** The target is in `/run`: non-durable, and
   re-provisioned. Unlink the **symlink** and write a regular file in its place.
4. **Two of the four must be *merged*, not replaced** — their templates carry keys the
   harness needs:

   | File | Keys | Clobbering them breaks |
   |---|---|---|
   | `claude-settings.json` | `apiKeyHelper`, `env` | how Claude Code authenticates |
   | `codex-config.toml` | `model_provider`, `model_providers` (**no** `mcp_servers`) | how Codex reaches its model |

`~/.claude` and `~/.codex` are **real directories** (not templated), so a regular file
written inside them persists. `~/.claude.json` is a genuine 35 KB regular file in persistent
`$HOME` holding Claude Code's own state (`numStartups`, `projects`, an existing `mcpServers`)
and is **absent from the boot script** — so MCP registration there is durable, and it must
**not** be routed through the symlink-aware writer.

### 🔴 The OAuth token cache does not survive a boot — and is currently absent entirely

`~/.databricks/token-cache.json` is a **dangling symlink**: the link exists, the target does
not. So the token cache is not merely emptied at boot; right now there is no file at all.

This breaks a premise elsewhere in the design. "Reset the PAT, then rely on the CLI's OAuth
token cache" works **within one boot** and not across one: after a reboot a PAT-reset sandbox
has **no workspace credential at all** until the OAuth login is re-run.

### 🔴 `DATABRICKS_CONFIG_FILE` can make a PAT reset a silent no-op

PID 1 exports:

```
DATABRICKS_CONFIG_FILE=/run/lakebox/databrickscfg
DATABRICKS_TOKEN_CACHE_FILE=/run/lakebox/token-cache.json
```

Both **override the default `~/` paths** for the CLI and SDK. An sshd-spawned shell does
*not* inherit them (measured), but processes descending from the daemon by another route may.
Where they are inherited, writing a credential-less `~/.databrickscfg` **changes nothing** —
the baked PAT at `/run/lakebox/databrickscfg` stays in use. Any reset must therefore account
for these variables, and a health check should report them, or a security-relevant operation
reports success while having no effect.

### The "degenerate config" mystery, explained

A 61-byte comment-only `~/.databrickscfg` with zero profiles was previously observed and
attributed to mis-provisioning. It is **this script's placeholder**. The correct diagnosis is
*"home setup ran and credential provisioning never landed on top of the placeholder"* — and
the fix is still to restart the sandbox.

## 4. Nothing you control runs at boot

| Hook | Status |
|---|---|
| system systemd | **absent** — "System has not been booted with systemd as init system (PID 1)" |
| user systemd | absent — "Failed to connect to bus: No medium found" |
| `crontab` | **not installed** |
| `/etc/rc.local` | absent |
| `sudo -n` | **passwordless root** (`(ALL) NOPASSWD: ALL`) |
| the only boot hook | Databricks' own `setup-home-directory.sh`, root-owned mode 555 on a read-only image path |

So **tmux sessions cannot survive a sandbox restart**, and no in-VM mechanism can re-create
them. Re-enrolment has to be driven from the agent side on next start. `$HOME` persists
across `stop`/`start`; everything else, including `/tmp` (where tmux's socket lives) and
`/run`, does not.

## 5. What the ambient credential proves

`current_user.me()` against the baked PAT resolves the sandbox's **creator** — measured, with
`groups: [admins, users]`. The Lakebox API exposes no owner field, so this is the only way to
answer "whose sandbox is this."

⚠️ It is a **confused deputy**: any agent in the sandbox can act as that user, and here that
user is a **workspace admin**. Reading identity from it is acceptable only while access is
default-open; before any ACL is enforced it must be replaced by a per-host enrollment token.

## 6. Riders

| | Measured |
|---|---|
| tmux | **3.4 at `/usr/bin/tmux`** — present in the image; vendoring a static binary is optional |
| terminfo | `tmux-256color`, `screen-256color`, `xterm-256color` all **present** — so `tmux-256color` is a safe `default-terminal` |
| `TERM` | **unset** under a non-tty exec, which is why it must be forced before invoking tmux |
| Resources | 8274 MB total / 7664 available, 4 cores — a 20 000-line `history-limit` (~285 MB est.) is ≈3.7 % of available |
| Egress | `pypi.org` 200 · workspace control plane 200 · Apps host 302. **Arbitrary outbound TCP is unmeasured** — do not assume a database port is reachable |

---

## Operational footgun

**Do not delete `$HOME/.shellbox/host.json`.** It is the sandbox's only durable host
identity. Deleting it while sessions are live re-keys every `session_id` (they are
`f"{host_id}:{tmux_name}"`), leaves the old rows `live` forever, and makes every session id
already handed to an agent permanently unaddressable while its tmux session still runs.
`shellbox-mcp` mitigates this by also stamping `@shellbox_host_id` as a tmux user option and
recovering from it, but the file is the primary record.
