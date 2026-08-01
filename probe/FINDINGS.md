# Phase 1 Probe — Raw Findings

Issue: IceRhymers/shellbox#1 · Workspace: `fevm-west` (`fevm-tanner-west.cloud.databricks.com`)
App: `shellbox-probe` → `https://shellbox-probe-7474657232613476.aws.databricksapps.com`
Sandboxes: `realistic-phoenix-2742` (pre-existing) and `observant-leopard-8942`
(created fresh for the credential test) — Ubuntu 24.04.4, kernel 6.12.8, Python 3.12.3
Date: 2026-07-30

All transport measurements were driven **from inside the live sandbox**, holding a
workspace OAuth token minted with `databricks auth token -p fevm-west`.

A local baseline was captured first (uvicorn on 127.0.0.1) so that any deviation
observed through the edge is attributable to the edge and not to the probe app.
Local baseline: WS 101 + echo, SSE strictly incremental, POST 200.

---

## Verdict against the issue's outcome table

| Row in #1 | Result |
|---|---|
| WS returns 101 and holds → ship `WSTransport` (B) | **This is the outcome** — 101 confirmed, bidirectional, ~4 ms. But "holds" is qualified: sockets are killed every ~10–18 min by a global edge event. |
| WS fails, SSE survives → `SSETransport` (C) | Moot. WS works, and SSE would have failed anyway (hard 300 s cut). |
| Neither survives >5 min → escalate | Not triggered — WS survives well past 5 min. Would have been triggered if WS had failed, since SSE dies at exactly 300 s. |
| ttyd reachable via gateway → re-scope Phase 4 | **Not reachable.** Phase 4 scope unchanged. |

**Decision: Phase 3 ships `WSTransport` (option B)**, with mandatory reconnect.

Three constraints the issue did not anticipate, all load-bearing:

1. **The WS socket is killed every ~10–18 min** by a wall-clock global edge event,
   silently (no close frame), and keepalives do not prevent it.
2. **The sandbox's ambient credential is a PAT, which the Apps edge rejects**, and the
   sandbox cannot upgrade it to OAuth — so an OAuth token must be injected from
   outside.
3. **The App cannot manage the user's sandbox with any identity it is given.** The
   injected user OBO token lacks `all-apis` scope (403), and the App's own SP is
   caller-scoped to its own sandboxes. Sandbox lifecycle control needs a credential
   from outside the App.

---

## Headline: WebSocket works. Phase 3 ships `WSTransport` (option B).

The trace found zero user-code WS handlers in 64 repos, but that was absence of
evidence, not evidence of absence. Inbound WS to user code **does** work.

```
HTTP/1.1 101 Switching Protocols
connection: Upgrade
upgrade: websocket
server: databricks                      <-- the Databricks edge, not uvicorn
gap-auth: tanner.wendland@databricks.com <-- edge authenticated + injected identity
sec-websocket-accept: p052TXgNuaRcSn9jpM72LbTT8oo=
x-request-id: 8af69ad7-67c5-4db1-b7f7-f30578cf9c2d
```

Bidirectional echo, 5 frames, from inside the sandbox:

| metric | value |
|---|---|
| handshake status | **101** |
| round-trip latency | 3.4–4.3 ms |
| server→client one-way | 2.0–2.4 ms |
| frames delivered | 5/5, in order, incrementally |

### …but a global edge event kills every socket every ~10–18 min. Phase 3 MUST reconnect.

A 75-minute hold, pinging every 20 s, **died at 16.67 min** after 50 successful
frames:

```json
{"kind":"ws_hold","ok":false,"error":"ConnectionClosedError",
 "detail":"no close frame received or sent","died_at_min":16.67,"frames":50}
```

`no close frame received or sent` is an abrupt TCP-level termination — the edge did
not perform a WebSocket closing handshake, so a client gets no warning and no status
code to react to.

**This was not token expiry.** The OAuth token was minted at 20:14 UTC and valid
until 21:14; the socket died at ~20:54 — a full 20 minutes before expiry. Round-trip
latency was steady at 3.3 ms right up to the last successful frame, so there was no
degradation beforehand.

**It is not an idle timeout either.** Two concurrent holds with 4× different traffic
rates died together:

| run | ping interval | frames sent | last good frame | died at |
|---|---|---|---|---|
| capA | 20 s | 32 | 10.34 min | 10.67 min |
| capB | 5 s | 125 | 10.34 min | 10.43 min |

Same last-good-frame time to two decimals despite one connection being four times
chattier. A chattier socket did not live longer, so keepalive traffic does not help.

**And it is not a fixed per-connection lifetime**, since the first hold lasted
16.67 min while these lasted ~10.4 min.

**The app never restarted.** `/` reports a process-level `boot_epoch` that was
**unchanged** across all three deaths (`1785443728.98`, `uptime_s` 2007 s and
climbing), and the app log shows a single `Started server process [76]`. The app's own
log recorded the closes:

```
20:54:16  connection closed      <- first hold
21:07:59  connection closed      <- capA
21:07:59  connection closed      <- capB   (same second)
```

Two independent sockets closed in the **same second** while the app ran continuously.
The teardown therefore originates at the edge and hits all open connections at once,
not from any per-connection property. The staggered experiment below nails this down.

**Confirmed: it is a global edge event that kills every open socket simultaneously.**
Three holds opened 3 minutes apart all died in the *same second*:

| run | opened | died | lifetime | frames |
|---|---|---|---|---|
| stagA | 21:09:47 | **21:27:17** | 17.51 min | 70 |
| stagB | 21:12:47 | **21:27:17** | 14.50 min | 58 |
| stagC | 21:15:47 | **21:27:17** | 11.51 min | 46 |

The lifetimes differ by exactly the stagger offsets (17.51 − 14.50 = 3.01,
14.50 − 11.51 = 2.99), so connection age is irrelevant — what matters is the
wall-clock moment the event fires. All three raised `ConnectionClosedError` with no
close frame.

The event is **not on a fixed period**. Three kill times were observed:

```
20:54:16   ->  +13 m 43 s  ->  21:07:59   ->  +19 m 18 s  ->  21:27:17
```

Longest lifetime observed across all six holds: **17.51 min**. Shortest: **10.43 min**.

### What this means for Phase 3

`WSTransport` is confirmed as the transport — it is the only option that gets 101
through the edge and carries real bidirectional frames past the 5-minute mark. But
the design must assume:

1. **Reconnection is routine, not exceptional.** Sockets die every ~10–18 minutes.
2. **The timing is unpredictable.** No fixed period, and it is wall-clock-driven, so
   a freshly opened connection can be killed seconds later. Reconnect logic must not
   assume it gets a minimum useful lifetime.
3. **Keepalives do not help.** A 5 s ping interval died at the same instant as a 20 s
   one. Do not add traffic hoping to hold the socket open.
4. **Detection must be client-side.** The teardown sends no close frame, so a
   liveness timer is the only reliable signal.
5. **Session state must live outside the socket.** Because all connections die
   together, a reconnect cannot rely on any other live connection surviving to hand
   off state.
6. **Validate content, not status.** An auth failure returns HTTP 200 with an HTML
   login body (see the PAT section).

## SSE is capped at exactly 300 s — it fails the >5 min bar

The edge advertises its own limit in a request header to the app:

```
x-envoy-expected-rq-timeout-ms: 300000
```

The SSE hold was configured for 75 minutes. It ended at **`total_s = 300.1`** —
the 300 s Envoy route timeout, to within 100 ms.

**It died silently.** The client observed a clean EOF (`event: closed`), not an
error, not a reset. A naive consumer cannot distinguish "edge cut my stream at the
5-minute route timeout" from "the server finished sending." Any SSE-based design
would need an application-level end-of-stream sentinel to detect truncation at all.

Delivery quality up to the cut was otherwise good — buffering was **not** the
problem:

| metric | value |
|---|---|
| open status | 200, `text/event-stream` |
| `X-Accel-Buffering: no` | honored (echoed back) |
| time to first byte | 0.108 s |
| inter-frame gap | 15.000–15.002 s at a 15 s tick — strictly incremental |
| per-frame lag | 2.0–7.4 ms |
| frames before cut | 21 |
| **survival** | **300.1 s — cut at the route timeout** |

WS held past the same 5-minute mark uninterrupted (5.0 min, 3.7 ms RTT), so the
300 s ceiling is specific to the long-lived HTTP response, not to the connection.

Consequence for the issue's decision table: the "WS fails, SSE survives" row is
moot, and the "neither survives >5 min" escalation row would have been triggered
had WS not worked. **WS is the only viable transport**; SSE is not a usable
fallback without <5 min reconnect cycling.

## POST baseline

200, 132 ms round trip from inside the sandbox. Control behaved as expected.

---

## Riders

### App → sandbox gateway: impossible

From inside the App container, outbound TCP to the sandbox gateway
(`us-west-2.service-direct.cloud.databricks.com:2222`):

```json
{"ok": false, "error": "OSError", "detail": "[Errno 113] No route to host", "elapsed_s": 0.001}
```

Failure in **1 ms** with `No route to host` — no route exists at all; this is not a
timeout or a filtered drop. **App→SSH is off the table.**

### ttyd is NOT reachable via the gateway — renderer scope does not shrink

`ttyd` does run in every sandbox, as expected:

```
root 73 ttyd -i /ttyd/term/ttyd.sock -m 10 login -f sandbox-agent
```

But it is bound to a **Unix domain socket**, not a TCP port:
`srw-rw---- 1 root root /ttyd/term/ttyd.sock` (mode 660, root-owned). It appears
in no `ss -tlnp` listing.

And the gateway exposes no HTTP route to it. Gateway port probe from a laptop:

| port | result |
|---|---|
| 22 | timeout (24 s) |
| 443 | TCP opens, but **TLS fails: cert hostname mismatch** — no vhost for this name |
| 2222 | **open**, banner `SSH-2.0-russh_0.60.2` |

All HTTP path probes (`/`, `/ttyd`, `/term`, `/terminal`, `/lakebox/ttyd`,
`/sandbox/<id>/ttyd`) returned `000` (connection/TLS failure, consistent with the
cert mismatch above). The gateway speaks SSH on 2222 and nothing usable on 443.

**Phase 4 renderer scope is unchanged.** Reaching ttyd would require a
root-privileged local proxy from the UDS, which the sandbox does permit (see sudo
below) but which is net new work, not a free win.

### In-VM capabilities

| check | result |
|---|---|
| `tmux` | **present**, 3.4 (`/usr/bin/tmux`) |
| `screen` | present, 4.09.01 |
| `sudo -n true` | **passwordless root** — `(ALL) NOPASSWD: ALL` |
| user systemd | **unavailable** — `Failed to connect to bus: No medium found`; no `~/.config/systemd/user` |
| `crontab` | **absent** — `command not found` |
| `/etc/rc.local` | **absent** |
| shell rc files | `~/.bashrc`, `~/.bash_profile` present — but **rewritten from a template every boot** (confirmed by restart: new mtime, identical md5), so edits there are clobbered |
| boot hooks in `sandbox-daemon` | `/etc/lakebox/setup-home-directory.sh`, `setup-user.sh` ("provisioned sandbox user via setup-user.sh") |

No cron, no user systemd, no rc.local. The only boot-time hooks visible are
Databricks' own `setup-home-directory.sh` / `setup-user.sh`.

### Sandboxes DO ship with an auto-provisioned PAT — and the Apps edge rejects it

Measured on a **freshly created** sandbox (`observant-leopard-8942`). This is the
correct test; an earlier reading against a 5-day-old sandbox was wrong (see the
caveat at the end of this section).

`~/.databrickscfg` → `/run/lakebox/databrickscfg`, 124 bytes, mode 600, containing a
working profile:

```
Name     Host                                           Valid
DEFAULT  https://fevm-tanner-west.cloud.databricks.com  YES
```

| property | value |
|---|---|
| `auth_type` | **`pat`** |
| `host` | the workspace URL |
| token length | 36 chars |
| token prefix | **`dkea…`** (not `dapi…`) |

`databricks current-user me` works with **no flags** and returns the sandbox owner's
identity. So the sandbox is authenticated to the *workspace* out of the box.

**But that credential cannot reach a Databricks App.** Driven from the fresh sandbox:

| attempt | result |
|---|---|
| ambient PAT → `GET /` | **302** to OIDC authorize |
| ambient PAT → WS upgrade `/ws` | **302**, client raises `InvalidStatus` |
| ambient PAT → `POST /in` | "200" but the body is the Databricks **HTML login page** — a false positive, not app output |
| `databricks auth token` in-sandbox | **fails**: `databricks OAuth is not configured for this host. no cached credentials` |

This is the single most consequential constraint the probe found for Phase 3:

> The sandbox's ambient credential is a **PAT**, PATs are **rejected by the Apps
> edge**, and the sandbox **cannot upgrade its PAT into an OAuth token**. An
> in-sandbox shellbox component therefore cannot authenticate to the App from
> ambient state — it must be **handed a workspace OAuth token from outside**.

Note the `POST` false-positive: any client that trusts the HTTP status alone will
read an auth failure as success. Phase 3 must validate response *content*, not just
the status code.

**Provisioning can transiently fail — and a restart repairs it.** The 5-day-old
sandbox `realistic-phoenix-2742` had a **degenerate 61-byte comment-only stub** in
place of its config. A `stop` / `start` cycle fixed it outright:

| | before restart | after restart |
|---|---|---|
| `databricks auth profiles` | zero rows | `DEFAULT … Valid: YES` |
| cfg size | 61 B | 124 B |
| `[DEFAULT]` header | absent | present |
| `token` key | absent | present |

So the credential is written at boot, that sandbox's earlier boot produced a stub,
and restarting re-provisioned it correctly. **Operational takeaway: if a sandbox has
no usable credential, restart it.**

(For the record, my initial `dapi[0-9a-f]` content sweep could never have found this
token — real tokens here are `dkea`-prefixed. That grep returning nothing was a bad
test, not evidence of absence, and I compounded it by testing a sandbox that happened
to be in a broken state.)

### Sandbox egress

| target | result |
|---|---|
| `pypi.org` | 200 (2.2 s) |
| workspace control plane | 200 (0.045 s — internal path) |
| the Apps host | **302** (unauthenticated → OIDC) |

Preinstalled in-sandbox: `websockets 14.2`, `fastapi 0.139.2`, `uv`, `curl 8.5.0`,
Databricks CLI v1.7.0.

### Apps edge auth shape

Unauthenticated requests **302** to
`/oidc/oauth2/v2.0/authorize?...` — confirming the issue's warning (not a 401).

Headers the edge injects into the app (from `/whoami`):

- `x-forwarded-access-token` — an **on-behalf-of user token**, 824 chars
- `x-forwarded-email: tanner.wendland@databricks.com`
- `x-forwarded-user: 2352405730157715@7474657232613476`
- `x-forwarded-preferred-username: Tanner Wendland`
- `gap-auth: <email>` — present **on the WS 101 response too**, so identity
  forwarding survives the upgrade
- `x-envoy-expected-rq-timeout-ms: 300000` — the 300 s ceiling, self-reported
- app sees `host: localhost:8000` (edge terminates and proxies to localhost)

### O2 — ANSWERED. The endpoint is caller-scoped, and there is no owner field.

No service principal had to be created. **The probe app already *is* a second
principal** — Databricks Apps run as their own SP (`shellbox-probe`, client id
`69988b13-d8b9-4e7d-a017-b6c34435aa7e`) and the Apps runtime injects
`DATABRICKS_CLIENT_ID` / `DATABRICKS_CLIENT_SECRET` into the container, so a
client-credentials exchange yields a genuine second identity for free.

**No owner/creator field exists.** Complete key set per sandbox, identical for both
principals:

```
gatewayHost, idleTimeout, name, noAutostop, sandboxId, status
```

**Caller-scoped, proven in both directions.** Full lifecycle driven as the app SP:

| step (as app SP) | result |
|---|---|
| `GET /sandboxes` | 200, **0 sandboxes** (while the user owns 2) |
| `POST /sandboxes` `{"sandbox":{"name":"o2-sp-probe"}}` | **200** → `proven-kingfisher-7829` |
| `GET /sandboxes` | 200, **exactly 1** — its own only, *not* the user's 2 |
| `DELETE /sandboxes/{id}` | 200 (async; settles to removed) |
| `GET /sandboxes` | 200, back to 0 — no orphan |

And the mirror check, as the user: the SP's `proven-kingfisher-7829` was **never
visible**, count stayed at 2.

The create/delete round trip is what makes this conclusive rather than suggestive: it
proves the SP holds full lakebox entitlement, so its initial empty list was
caller-scoping and not an authorization failure in disguise. (A principal lacking
entitlement 403s; note the SP's first malformed create returned a **400
INVALID_PARAMETER_VALUE**, i.e. it reached body validation, which is past auth.)

> **Each principal sees exactly its own sandboxes and no others.** There is no owner
> field because none is needed — the caller identity *is* the filter.

### The App cannot act on the user's sandbox — a real Phase 3 gap

This falls directly out of O2 and is worth calling out separately, because it
constrains the architecture:

| identity available to the App | can it manage the user's sandbox? |
|---|---|
| `x-forwarded-access-token` (user OBO, injected by the edge) | **No** — `403 Invalid scope, required scopes: all-apis`. The OBO token carries scope `default` only, so it cannot call workspace REST APIs at all. |
| the App's own SP (M2M client credentials) | **No** — works against lakebox, but caller-scoping means it only ever sees *its own* sandboxes, never the user's. |

So a shellbox App **cannot list, start, or stop the user's sandbox on their behalf**
with anything the Apps runtime gives it. Neither available identity can reach the
user's sandboxes. If Phase 3 or later needs App-driven sandbox lifecycle control
(e.g. auto-starting a stopped sandbox when the user attaches), it needs a third
credential supplied from outside — the same conclusion the PAT section reached, now
independently confirmed from the App side.

### Sandbox listening ports

`2222` (sshd), `5557`, `7802`, `8443`, `8901` (localhost-only), `9090`, `9444`,
`12345`, `18767`. No ttyd TCP port.

---

### Persistence across `stop` / `start` — tmux dies, `$HOME` survives

Measured on `realistic-phoenix-2742` by staging markers, cycling the sandbox, and
diffing state. Boot time moved `20:31:40` → `21:11:36`, confirming a real restart.

| thing | survives restart? | evidence |
|---|---|---|
| **tmux session** | **NO** | `error connecting to /tmp/tmux-10086/default (No such file or directory)` — the socket lives in `/tmp`, which is wiped |
| `$HOME` files | **YES** | `~/persist-marker.txt` intact with its original 20:45:43 content |
| in-progress output | **YES** | `~/survivor-ticks.log` retained all 154 lines (last tick 21:11:14, moments before shutdown) |
| earlier probe artifacts | **YES** | all of `~/probe/` present |
| `~/.bashrc` edits | **NO** | mtime moved `20:31:48` → `21:11:39` while **md5 stayed identical** (`cf277e08…`) — it is rewritten from a template every boot, so any edit is clobbered |

So shellbox can persist **state on disk** in `$HOME`, but cannot persist a **running
process** — no tmux session, and (per the rider results) no cron, no user systemd, no
`rc.local`, and no usable `.bashrc` hook. **There is no in-VM boot hook available**
for restarting work after a sandbox restart; that has to be driven from outside.

### Sandbox lifecycle

`databricks sandbox create --name shellbox-probe-fresh` returned **Running in 1.8 s**
— far faster than the ~20.5 s the issue anticipated. The reason: the new sandbox
reported **42 minutes of uptime** immediately after creation, so `create` claims a
**pre-warmed microVM** from a pool rather than booting one. Plan for sub-2 s
acquisition, but do not assume a genuinely cold boot.

Defaults differ between created and long-lived sandboxes:

| sandbox | idleTimeout | noAutostop |
|---|---|---|
| `observant-leopard-8942` (fresh) | `600s` | `false` |
| `realistic-phoenix-2742` (old, hand-configured) | `0s` | `true` |

A newly created sandbox **will autostop after 10 minutes idle** unless reconfigured —
relevant to any shellbox session-persistence assumption.

Measured lifecycle timings:

| operation | wall time | note |
|---|---|---|
| `sandbox create` | **1.8 s** | warm pool, not a cold boot |
| `sandbox stop` (CLI returns) | 0.8 s | **misleading** — see below |
| `stop` → status `Stopped` | 7 s | actual settle time |
| `sandbox start` | **25.0 s** | matches the issue's ~20.5 s expectation |

**`stop` is asynchronous and its success message is premature.** The CLI printed
`Stopped realistic-phoenix-2742` after 0.8 s, but an immediate `start` was rejected
with `sandbox … is currently stopping; retry once it settles`. Anything scripting a
stop/start cycle must poll `status` rather than trust the CLI's exit.

---

## Pending

- **Direct boot-hook test.** Installing a systemd unit / `.bashrc` hook to confirm no
  boot hook fires was declined by a permission gate. The conclusion above rests on
  absence of cron/user-systemd/`rc.local` plus the demonstrated per-boot rewrite of
  `.bashrc`, which is strong but indirect.

Everything else in the issue, including all five riders, is answered above.

## Incidental confirmation

The fresh sandbox `observant-leopard-8942` was found **`Stopped`** on its own roughly
40 minutes after creation, with no stop command ever issued — empirically confirming
the `idleTimeout: 600s` / `noAutostop: false` default really does autostop an idle
sandbox after 10 minutes.
