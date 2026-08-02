# Phase 3 Transport — Spike Findings

Issue: [IceRhymers/shellbox#3](https://github.com/IceRhymers/shellbox/issues/3) ·
Plan: `.omc/plans/phase-3-transport.md`

Workspace: `fevm-west` (`fevm-tanner-west.cloud.databricks.com`, workspace id
`7474657232613476`) · Profile: **`fevm-west`** — note a separate `tanner-west` profile exists in
`~/.databrickscfg` and is a *different* workspace; the `tanner-west` string inside this host name is
the workspace shard, not the profile.

Sandbox: `realistic-phoenix-2742` (pre-existing; the one Phase 1 drove), Databricks CLI **v1.7.0**
in-sandbox, v1.8.0 on the laptop.

Date: 2026-08-01. Method follows the `probe/` and `spike/` precedent: the exact commands, the raw
output, and an explicit split between what was measured and what is inferred.

---

## S-LOGIN (a) — Is there a browserless `databricks auth login` flow? **No.**

`OQ-G` answered. Measured on **both** CLI versions.

In-sandbox, v1.7.0:

```
$ databricks auth login --help | grep -iE "browser|device|headless|timeout"
This command authenticates via OAuth in the browser and saves the result
      --timeout duration       Timeout for completing login challenge in the browser (default 1h0m0s)
```

Laptop, v1.8.0: identical two lines. There is no `--device-code`, no `--no-browser`, and no headless
option on either version; every relevant flag is browser-oriented. v1.8.0 is the *newer* version and
also lacks it, so this is not a "not yet backported" situation.

**Consequences.**

* Decision E's option **E3 (fully automated, headless) does not exist.** It was scoped as a spike
  because the plan could not price it; the answer is that there is nothing to price.
* **`W20` keeps its full surface.** The `databricks sandbox ssh -- -L <port>:localhost:<port>`
  callback forward is *required*, not merely convenient — which is what makes omnigent's
  `login_app_oauth_in_sandbox` shape (`bootstrap.py:416`) the right one rather than one option
  among several.
* **A browser step is permanent in the credential chain**, not a temporary gap. `doctor` should say
  so plainly rather than implying the login is automatable.

Also relevant to (b), from the same help text: *"If a profile with the given name already exists,
**it is updated**. Otherwise a new profile is created."* So `auth login` mutates an existing
profile. The help does **not** say whether it preserves or clears an existing `token =`, which is
exactly what the `W20` ordering question turns on. See (b) below.

---

## S-LOGIN (b) — Does the login preserve an existing `token =`? **No. It replaces the stanza.**

WARNING: **Operator knowledge, not measured by this run.** Answered by @IceRhymers from prior
experience with the CLI, and recorded here so `W20` can be designed against it. It carries the same
status as the PID 1 environment claim in §3 below — *documented, not verified* — and wants an
in-sandbox confirmation on **v1.7.0** before `W20` is called done. It is written down now because
the answer resolves `W20`'s ordering, and a design blocked on a browser step should not also be
blocked on re-deriving something already known.

The answer: running `databricks auth login` against a profile that carries a `token =` **overwrites
that profile**, rewriting it as an OAuth profile with `auth_type = databricks-cli`. The PAT does not
survive. Combined with (a)'s help-text quote, the behavior is *update the matching profile in place*,
and the update is a replacement rather than a merge.

**Consequences — this is the good branch, and `W20` shrinks.**

* **The PAT removal is a side effect of the login, not a separate `reset_pat` call.** The `W20` row
  left both open and said the answer follows from (b). It is the former.
* **There is no credential-less window at all.** Removal and acquisition are the same write, so the
  ordering the plan kept trying to get right stops being a sequencing problem. This is the upside
  the `S-LOGIN` row named as possible; it landed.
* **`ADR-14`'s hazard dissolves rather than being mitigated.** Its trap was that `credential_less_cfg`
  writes a whole-file `[DEFAULT]`, so writing it *is* the removal. The resolution is to **never write
  it pre-login** for that stanza — the CLI produces the credential-less form itself, on success.
* **§0.6's chosen shape is confirmed by the tool rather than guessed.** `credential_less_cfg` is
  specified to write `auth_type = databricks-cli`, which is exactly what the CLI writes. That matters
  for `--restore-grant` and for `doctor`, which both have to recognise the state the CLI leaves.

**The residual question, and it is the one that decides whether `ADR-14`'s invariant survives.**

Does the rewrite happen **on success only**, or on **attempt**?

* *Success only* — a failed or abandoned login leaves the PAT intact. `ADR-14`'s invariant holds:
  "nothing is removed until a grant verifies, so a half-finished login leaves a working sandbox."
* *On attempt* — the credential-less window returns on the **worst** path. Walking away from the
  browser prompt would leave the sandbox with no PAT and no grant, and `ADR-14`'s invariant would
  have to be re-established by `W20` itself (back up the stanza, restore it if the login does not
  verify).

This is cheap and needs no sandbox: point `DATABRICKS_CONFIG_FILE` at a scratch file containing a
`[DEFAULT]` with a `token =`, start a login, **abandon it at the browser**, and read the file back.
Do it against a scratch path rather than the real `~/.databrickscfg` — the login rewrites whatever
config it resolves, and the laptop's copy holds the working profiles this file's header names.

**Also still unconfirmed, and both are about *where* rather than *what*.** The sandbox's
`~/.databrickscfg` is a **symlink into `/run/lakebox/`** (§1), so the login's write lands on a path
that is wiped between boots — the grant does not survive a reboot on the cfg side either, which is
`W21`'s problem and not solved by this answer. And §3 measured that an sshd-spawned shell inherits
neither config override, so which file the CLI resolves depends on how the shell was started;
`W20b`'s verification should assert the resolved path rather than assume `~/.databrickscfg`.

---

## Credential state on a freshly booted, never-reset sandbox

All read-only. Confirms four premises the plan depends on.

### 1. The baked creator PAT is present, and `[DEFAULT]` is the operative profile

```
$ ls -la ~/.databrickscfg
lrwxrwxrwx 1 sandbox-agent sandbox-agent 26 Aug  1 16:53 ~/.databrickscfg -> /run/lakebox/databrickscfg

[DEFAULT]
host = https://fevm-tanner-west.cloud.databricks.com
token = dkea...REDACTED
auth_type = pat
```

Matches Phase 1's finding (`dkea`-prefixed PAT, `auth_type = pat`). The profile is `[DEFAULT]`,
which is what makes the `W20` ordering hazard real: `credential_less_cfg` writes a whole-file
`[DEFAULT]`, so writing it before a grant is minted **is** the removal of the credential.

### 2. `~/.databricks/token-cache.json` is a **dangling** symlink

```
lrwxrwxrwx 1 sandbox-agent sandbox-agent 29 Aug  1 16:53 token-cache.json -> /run/lakebox/token-cache.json
target exists? NO (dangling)
```

**Measured live, on this boot.** This is the finding that makes `W21` necessary: the CLI's OAuth
token cache does not merely empty at boot, it ceases to exist. Combined with (1), a PAT-reset
sandbox that reboots has no workspace credential at all until the login is re-run.

### 3. An sshd-spawned shell inherits neither config override

```
$ env | grep -E "DATABRICKS_(CONFIG|TOKEN)"
(none inherited)
```

Confirms the measured claim in `docs/sandbox-environment.md` §3. **Caveat: PID 1's environment was
NOT verified here** — `sudo tr '\0' '\n' < /proc/1/environ` returned `Permission denied` (no
passwordless sudo over this ssh path). So the claim that PID 1 exports
`DATABRICKS_CONFIG_FILE=/run/lakebox/databrickscfg` and
`DATABRICKS_TOKEN_CACHE_FILE=/run/lakebox/token-cache.json` is **inherited from
`docs/sandbox-environment.md`, not re-measured by this run.** `W20b`'s write-everywhere rule still
needs it, so treat it as documented-not-verified until someone reads PID 1's environ successfully.

### 4. The `boot_id` / home-setup comparison works as a bootstrap check

```
boot_id: 47c0c27e-ac9e-46fa-9d21-3128dba7b136
marker:  47c0c27e-ac9e-46fa-9d21-3128dba7b136
```

They match, so `/etc/lakebox/setup-home-directory.sh` ran for *this* boot. This is the mechanism
the plan's §0.8 proposes for `W22` — it lets `doctor` state "the per-boot bootstrap has not run this
boot" as a fact rather than infer it from file states. **Confirmed available.**

### 5. The ambient PAT does reach the workspace API

`databricks current-user me` returned the creating user (`Tanner Wendland`, `active: true`). So the
PAT is valid for workspace REST; Phase 1's finding is specifically that the **Apps edge** 302s it.
Both remain true and they are not in tension.

---

## Unplanned finding — `OQ-A` may be wrong: the sandbox CAN read its own `sandbox_id`

`docs/sandbox-environment.md` §1 states, as "the single most consequential fact here", that six
independent searches found no sandbox identifier available from inside. `ADR-6` and `ADR-8` exist
because of it: `host_id` is self-assigned and `sandbox_id` is **injected from outside** via
`bootstrap --sandbox-id`.

`~/.databricks/sandbox.json` (regular file, mode `0600`, 250 bytes, mtime **Jul 31 19:41** — so it
survived the Aug 1 boot) contains both values:

```
/sandboxes/DEFAULT    = [{'id': 'realistic-phoenix-2742', 'name': 'realistic-phoenix-2742'}]
/gatewayHosts/DEFAULT = us-west-2.service-direct.cloud.databricks.com
```

**How it was missed is clear and not a reasoning error:** §1's disk search was
`grep -rl <id> /etc /run /var/lib /opt /usr/local`. It never searched `$HOME`. The file is also
absent from `TEMPLATED_PATHS`, so nothing in the boot-templating work would have surfaced it.

**This is NOT yet a correction to `OQ-A`, and the distinction is load-bearing.** The mtime falls
inside Phase 1's probe window, and this is the sandbox the probe drove heavily. The file is
plausibly written by the `databricks sandbox` CLI caching its default — in which case it exists only
in sandboxes where someone ran that CLI *from inside*, and depending on it would be a latent bug of
the worst shape: **present in every developed-in sandbox, absent in every fresh one.**

**What settles it:** read `~/.databricks/sandbox.json` on a freshly created sandbox that has never
had `databricks sandbox` run inside it. Until then:

* Do **not** change `ADR-6`/`ADR-8`. Injection from outside remains correct and is safe either way.
* If a fresh sandbox *does* carry the file, `OQ-A` and `docs/sandbox-environment.md` §1 both need
  correcting, and `bootstrap --sandbox-id` becomes a fallback rather than the only path.
* If it does not, record it here as an artifact so the next reader does not rediscover it and draw
  the wrong conclusion.

---

## Not yet run

| | What | Blocked on |
|---|---|---|
| `S-LOGIN` (b) **confirm** | The answer above is operator knowledge, not measured. Confirm on **v1.7.0 in-sandbox**, and record the **resolved config path** rather than assuming `~/.databrickscfg` (§1: it is a symlink into `/run/lakebox/`; §3: an ssh shell inherits no override) | A browser step (no headless flow exists) |
| `S-LOGIN` (b) **success-vs-attempt** | Does the stanza get rewritten **on success only**, or on **attempt**? Decides whether `ADR-14`'s "a half-finished login leaves a working sandbox" still holds, or whether `W20` must back up and restore the stanza itself | Nothing — laptop only, scratch `DATABRICKS_CONFIG_FILE`, ~2 min |
| `S-REFRESH` (a) | Mint a grant, copy the cache, force a refresh, try the copy — does a copied cache survive rotation? | `S-LOGIN` (b) |
| `S-REFRESH` (b) | **Restore a stale copy over a live grant — does the live grant survive?** This is the direction that can brick a sandbox, and `R23` is the plan's top risk | `S-REFRESH` (a); wants a throwaway sandbox |
| `OQ-A` re-check | `~/.databricks/sandbox.json` on a never-developed-in sandbox | A fresh sandbox |
