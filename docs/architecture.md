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
- **Delete the baked PAT** during enrolment; do not merely ignore it. Host-keyed
  credential resolution prefers a host-matching cfg entry and the edge 302s PATs,
  so the PAT *shadows* the OAuth grant. Reset `~/.databrickscfg` to exactly one
  **credential-less** `[DEFAULT]` naming the fronting workspace — the entry must
  exist or `databricks auth login` stalls on an interactive profile-name prompt.
- Identity stamping (D4) is confirmed viable: the ambient credential authenticates
  as the sandbox creator, and the Lakebox API exposes **no owner field**, so
  stamping host-side really is the only way to answer "whose sandbox is this."
- tmux 3.4 is already present in the image, so vendoring a static binary (D10) is
  belt-and-braces rather than required.

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

## Open scoping question

Before Phase 2 begins, see the [premise correction on
#9](https://github.com/IceRhymers/shellbox/issues/9#issuecomment-5137298161):
omnigent already implements this architecture on Databricks Apps — tmux PTY↔WS
bridge, xterm.js renderer, terminal CRUD, host tunnel, Lakebox launcher, and the
in-sandbox OAuth bootstrap drawn above. This diagram documents the design as
specified; whether to build it or extend omnigent is unresolved.
