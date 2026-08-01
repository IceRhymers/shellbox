# Lakebase hand-off — what is proven, and what each later phase still owes

`shellbox-registry` can talk to Lakebase today. This is the evidence for that claim, and
the list of things it deliberately does **not** settle, addressed to the phases that own
them: [#3](https://github.com/IceRhymers/shellbox/issues/3) (transport / OAuth login) and
[#4](https://github.com/IceRhymers/shellbox/issues/4) (App + provisioning).

Everything below was measured on **2026-07-31** against a real Autoscaling Lakebase
project (`projects/shellbox`, `fevm-west`, us-west-2, Postgres 17.10), not inferred.

---

## 1. ADR-3 holds: Lakebase is a credential concern, not a second registry

The claim was that `PostgresRegistry` connects to a DSN and does not care who minted the
password, so Lakebase adds a credential path and nothing else. That is now verified rather
than asserted:

| Evidence | Result |
|---|---|
| **The entire registry suite, run against Lakebase instead of local Postgres** | **30 passed**, unchanged code — including the `sessions→hosts` FK, both `GREATEST` timestamp semantics, the `CHECK` constraints, and the migration tests |
| `alembic upgrade head` against Lakebase | Clean, both `0001` and `0002`; `last_read_at` present |
| A `hosts` + `sessions` row written through `lakebase_registry()` | Landed with a real `owner_email` (`current_user.me()`), `sandbox_id`, `cwd`, dimensions |
| `pool_pre_ping` under total connection loss | Survives every backend being terminated — **with a negative control** proving the same test fails without the flag |

**So the DoD sentence "rows land in Lakebase with a correct `owner_email`" is proven.** The
remaining half of #2's clause is *"from inside a Lakebox sandbox"*, which is W10c — and
§4 below removes the mechanism that was blocking it.

## 2. Measured facts about the credential — three of which contradict the obvious guess

Each of these was wrong in the first draft of `lakebase.py` and corrected by calling the
real API. None of them could have been caught by a unit test, because a fake minter never
touches the SDK.

| Fact | Measured | Why it matters |
|---|---|---|
| **Token lifetime** | **3600 s** by default (`iat`/`exp` on the returned JWT) | The API *documents* a 300 s floor, and a first draft used that as the assumed lifetime — **12× more minting than necessary** |
| **`expire_time` type** | `google.protobuf.timestamp_pb2.Timestamp`, **not** a datetime or an ISO string | A parser handling only the latter two returns `None` silently and falls through to the "no stated expiry" path. `ToDatetime()` also returns a **naive** datetime, which raises `TypeError` against an aware `now()` |
| **`ttl` parameter type** | `google.protobuf.duration_pb2.Duration` | Passing the `"900s"` string the docs show raises `AttributeError: 'str' object has no attribute 'ToJsonString'` — the SDK calls `.ToJsonString()` on whatever it is given |
| **Requested short TTLs are honoured** | `ttl=900s` → a 899 s token | Usable for deliberately testing expiry against the real API |
| **Token shape** | A ~848-char JWT, `eyJr…` prefix, claims `aud client_id exp iat iss jti scope sub` | The `exp` claim is authoritative and is what `expire_time` mirrors |
| **First connect to an `IDLE` endpoint** | **1.4 s** | Scale-to-zero wake is fast enough not to need special handling, but it is why `pool_pre_ping` is mandatory |

## 3. What `lakebase.py` decides, and why the obvious alternative is wrong

- **Refresh is lazy by default; the background refresher is opt-in.** `shellbox-mcp` is
  spawned **per agent session**, 1–32 concurrent and short-lived. A 45-minute background
  refresher in that process means 32 threads that never fire once, a token minted per
  process on the path enrollment promises is non-blocking, and — if non-daemon — a hung
  process exit after stdio closes, invisible to an MCP client except as a child that never
  reaps. **Phase 4's App is the opposite case and should call `start_refresher()`.**
- **The server's stated expiry is trusted, never a hardcoded TTL.** See §2; a policy change
  then costs nothing.
- **`pool_recycle = 1800 s`**, half the *measured* lifetime — not derived from the
  documented floor, which would recycle every 240 s and discard good connections ~15× more
  often than needed. A caller requesting a short `ttl` must lower it; the minter **warns**
  when it observes a token shorter than the recycle, so the mismatch is observable.
- **The token is injected per-connect via `do_connect`, never baked into the DSN.** The URL
  outlives any single token, and a DSN in a log or a `repr` then carries no credential.
- **`databricks-sdk` is an optional extra** (`shellbox-registry[lakebase]`). The package's
  three real dependencies are alembic, psycopg and SQLAlchemy — deliberately SDK-free,
  which is what lets local-Postgres CI and `NullRegistry` run with no Databricks install.
  Importing `lakebase` does not need the SDK; minting a token does, and says so.

## 4. 🟢 For #4 — the reverse tunnel is not needed

The plan assumed a sandbox could not reach Lakebase directly, because sandbox egress was
measured only to pypi, the workspace control plane and the Apps host — arbitrary outbound
TCP to 5432 was **unmeasured**. It is now measured, from inside the sandbox:

```
ep-broad-grass-….database.us-west-2.cloud.databricks.com -> 192.168.200.30
TCP 5432 CONNECTED in 0.00s
```

It resolves to a **private** address and connects instantly, i.e. it is routed internally
rather than over the public internet. **W10c needs no `ssh -R` tunnel**, and the "what if
`-R` is not passed through" contingency is moot.

## 5. What each phase still owes

### #4 (App + provisioning)
1. **Provision the endpoint via DAB** rather than the CLI. `projects/shellbox` here was
   created by hand for verification and is **not** infrastructure-as-code.
2. **Call `start_refresher()`** — the App is long-lived and is the case the background
   refresher exists for. Call `stop()` on shutdown.
3. **Grant the App's service principal a Postgres role.** Everything above authenticated as
   a *workspace user*; the App authenticates as its SP, whose role name is the SP client id.
   Untested here, and it is the most likely thing to be missing on first deploy.
4. **Render a NULL `sandbox_id`.** A host that was never bootstrapped has one, by design
   (ADR-6) — a sandbox cannot learn its own id, so it is injected from outside or absent.
   With `host_id` an opaque uuid4, `sandbox_id` is the only human-meaningful label a `hosts`
   row carries, so its absence is a real rendering case and not an edge case.
5. **Re-run §1's row assertions** against the provisioned endpoint. That is a re-run, not an
   open question.

### #3 (transport / OAuth login)
🔴 **A PAT-reset sandbox has no workspace credential at all after a reboot.**
`~/.databricks/token-cache.json` is a boot-templated symlink into `/run`, which is wiped
between boots — and it is currently *dangling*, so the cache does not merely empty, it
ceases to exist. Combined with the per-boot PAT reset, a rebooted sandbox can reach neither
the workspace API nor anything needing that identity until the OAuth login is re-run. #3
owns that login. See [`docs/sandbox-environment.md`](sandbox-environment.md) §3.

### #5 (lifecycle)
**`last_read_at` is recorded but no predicate uses it.** `last_activity_at` advances on
**send**, `last_read_at` on **read** — two columns because one cannot express both hazards
(a polling agent keeping a session alive forever; a watched session reaped mid-build).
⚠️ The **read** side is not yet written by `server.py`: it needs a value for the `NOT NULL`
`last_activity_at`, and every option is a Phase 5 semantic. That choice is #5's.

---

## 6. Operational notes

⚠️ **The registry test suite is destructive.** Its fixtures `drop_all` on teardown, so
running it against a shared database deletes `hosts` and `sessions` — and can leave
`alembic_version` claiming the migrations are still applied. That happened here. The
fixtures now **refuse** any host that is not obviously throwaway unless
`SHELLBOX_ALLOW_DESTRUCTIVE_TESTS=1` is set.

**Reaching Lakebase without putting a credential in a URL.** `dsn_from_env` assembles from
`SHELLBOX_PG_USER` / `_PASSWORD` / `_HOST` / `_PORT` / `_DB`, plus `SHELLBOX_PG_SSLMODE`
(no default — a local Postgres has no TLS, and Lakebase requires it). The OAuth token goes
in `_PASSWORD` and never appears inside a URL string.

```sh
R=projects/shellbox/branches/production/endpoints/primary
export SHELLBOX_PG_HOST=$(databricks postgres get-endpoint "$R" -p fevm-west -o json \
  | python3 -c 'import json,sys;print(json.load(sys.stdin)["status"]["hosts"]["host"])')
export SHELLBOX_PG_PASSWORD=$(databricks postgres generate-database-credential "$R" \
  -p fevm-west -o json | python3 -c 'import json,sys;print(json.load(sys.stdin)["token"])')
export SHELLBOX_PG_USER=$(databricks current-user me -p fevm-west -o json \
  | python3 -c 'import json,sys;print(json.load(sys.stdin)["userName"])')
export SHELLBOX_PG_PORT=5432 SHELLBOX_PG_DB=shellbox SHELLBOX_PG_SSLMODE=require
```

**The verification project.** `projects/shellbox` in `fevm-west`, 0.5–1 CU, scale-to-zero
at 300 s, holding a `shellbox` database. It costs ~$0.06/CU-hour while awake and nothing
while suspended. Delete with
`databricks postgres delete-project projects/shellbox -p fevm-west` — it is a verification
artifact, not infrastructure, and #4 replaces it with a DAB-managed one.
