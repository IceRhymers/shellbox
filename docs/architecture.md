# shellbox — architecture

![shellbox architecture](architecture.png)

Source: [`architecture.dot`](architecture.dot) · regenerate with:

```sh
dot -Tpng docs/architecture.dot -o docs/architecture.png
dot -Tsvg docs/architecture.dot -o docs/architecture.svg
```

Annotations marked **[P1]** are measured results from the Phase 1 probe
([#1](https://github.com/IceRhymers/shellbox/issues/1)), not assumptions. Full raw
findings live in the probe's comment on that issue.

---

## The three paths

**1. Live data path** (bold + gold). The agent drives `shellbox-mcp` over stdio;
the MCP server controls a tmux server that is its *sibling*, not its child, so
sessions outlive MCP restarts and agent rotation. Frames go out over a single
WebSocket the sandbox dials. The App renders them in xterm.js and sends input back
down the same socket.

**2. Bootstrap path** (green, dashed — once per sandbox). Over SSH, which
[D1](https://github.com/IceRhymers/shellbox/issues/9) reserves for provisioning
only: install `shellbox-mcp` + tmux, **delete the image's baked PAT**, run the
login *inside* the sandbox over a PTY, and `ssh -L` the OAuth callback port in so
the local browser's redirect reaches the in-sandbox listener. The sandbox ends up
the sole holder of its own OAuth grant.

**3. Registry path** (purple). Lakebase holds `hosts` and `sessions` only. There is
no frame table — D7 is live-stream-only.

## Why the arrows point the way they do

The direction is forced, not chosen. **App → sandbox is impossible**: outbound TCP
from the App container to the sandbox gateway fails with `[Errno 113] No route to
host` in 1 ms. Every design where the App reaches in is dead, which is why the
sandbox dials out and why `App→SSH→tmux attach` is excluded twice over.

## What each remaining phase must do differently

### Phase 2 — session plane

Planned in detail at [`.omc/plans/phase-2-session-plane.md`](../.omc/plans/phase-2-session-plane.md)
(revision 4; Architect and Critic both APPROVE). §7 of that plan is **transcribed from an executable
spike**, [`spike/tmux_spike.py`](../spike/tmux_spike.py), after three rounds of prose review kept
finding new defects in freshly-rewritten command blocks.

- **The PAT is stamped first, then reset — and the reset belongs to the bootstrap path, not
  `serve`.** Enrolment order is fixed: resolve identity via `current_user.me()` → cache
  `owner_email`/`host_id` to `$HOME/.shellbox/host.json` → write the `hosts` row → *(bootstrap only)*
  reset `~/.databrickscfg`. Deleting the PAT inside `serve` would strand the sandbox with no
  workspace credential before Phase 3's OAuth login exists. Note `~/.databrickscfg` is a **symlink
  into tmpfs `/run`**, so the reset must remove the *symlink* and write a regular `$HOME` file, and it
  is inherently a **per-boot** operation rather than one-shot.
- Identity stamping (D4) is confirmed viable: the ambient credential authenticates
  as the sandbox creator, and the Lakebox API exposes **no owner field**, so
  stamping host-side really is the only way to answer "whose sandbox is this."
  The `$HOME` cache is **reconciled** against the credential whenever one is available (credential
  wins, mismatch logs loudly) rather than short-circuiting on it.
- **tmux 3.4 is already present at `/usr/bin/tmux`, so vendoring a static binary (D10) is optional,
  not required.** The binary is resolved from `$SHELLBOX_TMUX_BIN` before falling back to `PATH`, and
  the resolved path and version are recorded on the `hosts` row so an image bump is visible in the
  registry rather than surfacing as a mystery bug.
- **Four tmux behaviours are load-bearing and each is measured in two lanes** (3.6b/macOS and
  3.4/Ubuntu — identical results):
  - `-t <name>` **prefix- and fnmatch-matches**, so `has-session` is *not* an enforcement boundary and
    one agent could address another's session by naming a prefix. Every targeting verb uses the single
    anchored form **`=<name>:`**; `new-session -s` takes a **bare** name.
  - A **global** `window-size manual` **kills the tmux server on the next `new-session`** (15/15), so
    it appears nowhere in the create path. The **per-window** form is safe (0/15) and is how Phase 3/4
    should set it — never `-g`.
  - `history-limit` must be set **before** the first pane spawns (chained with `start-server`), or the
    pane keeps the 2000 default while `show-options` reports the new global.
  - The pty **line discipline** silently corrupts over-long lines — **dropped on macOS, truncated on
    Linux** — so a line-length ceiling is enforced before tmux is invoked.
- Sessions survive an **MCP restart** but not a **sandbox restart**, and there is no in-VM boot hook
  to fix that. See the README's "What survives what".

### Phase 3 — transport
- Ships `WSTransport` (B). `SSETransport` (C) is dead: cut at exactly 300.1 s.
- **Reconnect is the steady state**, roughly every 10–18 minutes, on a wall-clock
  global event that kills every open socket at once. Not periodic, not
  per-connection, keepalive-immune, and it sends **no close frame** — so detection
  must be a client-side liveness timer.
- `subscribe(session_id, from_seq)` **repaints from `capture-pane`**; it cannot
  gap-fill, because D7 means no frame log exists to replay from.
- Validate response *content*, not status codes. An unauthenticated POST returns
  HTTP 200 with an HTML login body.

### Phase 4 — renderer
- Drop the planned "App SP granted Lakebox sandbox visibility" item. It is not a
  permissions gap to be filled: the API is caller-scoped with no grant path, so the
  App SP sees only sandboxes it created, and the injected user OBO token carries
  scope `default` (`403 required scopes: all-apis`).
- Consequence to design around: **a stopped sandbox cannot be revived from the
  browser.** With a 600 s default autostop this will be common. Either accept
  "attach only works if the sandbox is already up" or drive revival from outside
  the App.

### Phase 5 — lifecycle
- The `idleTimeout` keepalive must run **host-side** (as specified) — the sandbox
  can manage its own autostop; the App cannot.
- Host staleness via `last_seen_at` is unaffected, since it reads Lakebase rather
  than the Lakebox API.
- **There is no in-VM boot hook.** `$HOME` survives a restart but tmux does not
  (its socket lives in wiped `/tmp`), `.bashrc` is rewritten from a template every
  boot, and there is no cron, no user systemd, and no `rc.local`. Re-enrolment has
  to be driven from the agent side on next start, not by anything in the VM.
- Lifecycle timings: `create` 1.8 s (warm pool, not a cold boot), `start` 25 s, and
  `stop` is **async with a premature success message** — poll `status` rather than
  trusting the CLI's exit.

---

## Scoping question — RESOLVED (2026-07-31): build

The [premise correction on
#9](https://github.com/IceRhymers/shellbox/issues/9#issuecomment-5137298161) established that
omnigent already implements this architecture on Databricks Apps — tmux PTY↔WS bridge, xterm.js
renderer, terminal CRUD, host tunnel, Lakebox launcher, and the in-sandbox OAuth bootstrap drawn
above. Three options were scored: **G1** build shellbox as specified, **G2** extend omnigent, **G3**
build the session plane and borrow omnigent's transport and renderer.

**Decision: G1 — build.** The deciding argument is structural rather than about code quality:
omnigent enforces launch-before-use through a **per-process in-memory registry**
(`omnigent/terminals/registry.py:104`), which is correct for omnigent, where one process owns the
terminals. shellbox runs **1–32 concurrent MCP processes** with session rotation, under which
in-process session state is wrong within a single turn — issue #2 declares that invariant mandatory.
"Extend omnigent" therefore means rewriting the layer that made omnigent attractive, inside a
codebase whose remainder assumes that layer's semantics.

G3 remains the right answer for **Phases 3 and 4**, and is not foreclosed: omnigent's
`ws_bridge.py:447` and `terminal_attach.py:130` already solve the transport and renderer problems
under this exact edge, including the ~10–18 minute ingress socket recycle the probe measured
(`omnigent/host/connect.py:1266`). Borrowing there is additive to a shellbox session plane.
