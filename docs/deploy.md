# Deploying shellbox

`make deploy` provisions Lakebase, migrates the registry, deploys the App and grants its service
principal the reads it needs. This file covers the one prerequisite that is not a `make` step,
the order the pipeline runs in, the grant, and the one field nobody should add.

Everything here was checked against **Databricks CLI v1.8.0** on **2026-08-03**. Where a claim
was measured, it says so.

---

## 1. The pipeline, and the two deploy mechanisms inside it

`make deploy TARGET=<target>` runs [`scripts/deploy.sh`](../scripts/deploy.sh). That is six
ordered steps, and the order is the load-bearing part:

| | Step | Why it is here and not earlier |
|---|---|---|
| 1 | `databricks bundle deploy` | Provisions Lakebase and the App **resource**. Steps 2 and 3 address an endpoint that does not exist until it runs |
| 2 | [`scripts/bundle-vars.sh`](../scripts/bundle-vars.sh) | One `validate` call for the App name, its code root, and the endpoint resource path |
| 3 | `make migrate` | Before the code deploys. Running as the deploying principal makes that principal the **owner** of the tables, which is what lets step 6 grant reads on them |
| 4 | [`scripts/deploy-app.sh`](../scripts/deploy-app.sh) | Deploys the App's **code** and starts compute. The service principal and its Postgres role materialise here, not at step 1 |
| 5 | Wait for `compute_status.state == ACTIVE` | The SP's role appears in `pg_roles` some time **after** activation |
| 6 | `make grant` | Needs the tables from step 3 and the role from step 5 |

Steps 3 and 6 stay usable on their own — `make migrate` and `make grant` are targets, and the
grant is what to re-run after a migration adds a table the App reads.

Steps 1 and 4 are the two deploy mechanisms, and the second is not redundant. A `bundle deploy`
does not create an app deployment, and the Databricks docs state it verbatim: *"Deploying a
bundle doesn't automatically deploy the app to compute."* The code deployment is a separate API
call. `deploy-app.sh` issues it, after flattening the two uv workspace packages into one
importable root — its header explains why that flattening is not optional.

The two coexist because of one absence.

> **NORMATIVE: the App resource in [`resources/app.yml`](../resources/app.yml) declares no
> `lifecycle` and no `started`. Do not add either.**

With them omitted, `bundle deploy` creates no app deployment and therefore **cannot** revert the
script. With `started: true`, every `bundle deploy` re-deploys the App from the bundle's own
`source_code_path` and clobbers whatever the script deployed. `started: true` is the natural
thing to reach for when an App will not start, and it is the wrong lever — so `make lint`
asserts the absence rather than trusting the comment.

The bundle does **see** the script's deploys as drift. It reads remote state from the active
deployment specifically to detect out-of-band redeploys. It just does not act on that drift
without `started`.

---

## 2. Prerequisite: adopt an App that already exists

**Run this once per target, by hand, before that target's first deploy. It is not a `make` step
and it is not a step in [`scripts/deploy.sh`](../scripts/deploy.sh).**

```sh
databricks bundle deployment bind shellbox_app shellbox --auto-approve --profile fevm-west
```

**Why it is needed.** An unbound bundle plans `create` for an App that already exists, and the
deploy then fails with `409 RESOURCE_ALREADY_EXISTS`. The App named `shellbox` **does** exist —
created 2026-08-02, and confirmed running on 2026-08-03. So for the `prod` target this failure
is a certainty, not a risk. Skip the bind and the first deploy fails loudly.

The `dev` target names its App `shellbox-dev`, which does not exist yet, so `dev` needs no bind.

**Why it is not automated.** `bundle deployment bind` prompts for confirmation unless
`--auto-approve` is passed. Inside the pipeline it would therefore either block on an
interactive prompt, or carry `--auto-approve` and silently re-adopt whatever the name points at
on every run. Adoption is a one-time act and belongs in a runbook.

`shellbox_app` is the resource key in [`resources/app.yml`](../resources/app.yml). Renaming that
key changes this command, which is why `make lint` asserts the key still exists.

**One unverified detail.** The CLI help documents the second argument as *"RESOURCE_ID — The ID
of the existing resource in the workspace"*, and every example it gives is a numeric job or
pipeline id. Apps have no numeric id: the Apps API addresses them by name in the URL path, and
every other command in this repo passes `shellbox`. So the name is near-certainly right. If the
bind rejects it, the App also carries a uuid — `3337afac-b67b-41af-8996-828620bcc4a8`, read from
`databricks apps get shellbox -o json` on 2026-08-03 — and that is the only other candidate.
Confirm the bind worked by checking that the next `bundle deploy` plans `update` and not
`create`.

---

## 3. The connection is derived, not exported

**`make deploy` needs nothing exported.** Step 2 of the pipeline derives `SHELLBOX_PG_RESOURCE`
from the bundle and exports it for steps 3 and 6, so a deploy is one command:

```sh
make deploy TARGET=dev PROFILE=fevm-west
```

`SHELLBOX_PG_RESOURCE` is the endpoint's resource path,
`projects/<project>/branches/<branch>/endpoints/<endpoint>`. Everything else is resolved in code
by `resolve_lakebase_endpoint` in
[`lakebase.py`](../packages/shellbox-registry/src/shellbox_registry/lakebase.py):

| Value | Where it comes from |
|---|---|
| host | `get_endpoint(resource).status.hosts.host` |
| Postgres role | `current_user.me().user_name` — **whoever ran the command** |
| password | An OAuth token, minted per physical connection and never written into a URL |
| database | `SHELLBOX_PG_DB`, defaulting to the bundle's `pg_database` (`databricks_postgres`) |

**Deriving the role is a safety property, not a convenience.** The migration authenticates as
the deploying principal by construction, so it cannot silently run as the App's service
principal. Section 4 explains why that matters.

### Running `make migrate` or `make grant` on their own

Both need the same one variable, and
[`scripts/bundle-vars.sh`](../scripts/bundle-vars.sh) prints it:

```sh
eval "$(scripts/bundle-vars.sh --target dev --profile fevm-west)"
export SHELLBOX_PG_RESOURCE
make migrate TARGET=dev PROFILE=fevm-west
make grant   TARGET=dev PROFILE=fevm-west
```

`PROFILE` is not optional in spirit. Both targets pass it to the CLI **and** set
`DATABRICKS_CONFIG_PROFILE`, because the SDK reads its profile from the environment and an unset
value resolves to `DEFAULT` — which in this account is a different workspace with a confusingly
similar name.

### The first deploy of a target

The resource path is **constructed** from ids the bundle already declares, so it exists before
anything is provisioned — see section 5. There is no chicken-and-egg step any more, and no
reason to invent a placeholder host.

`databricks postgres get-endpoint "$SHELLBOX_PG_RESOURCE" -p fevm-west` reporting a host is
still the only authoritative check that the bundle provisioned Lakebase. Nothing before a deploy
can tell you: `bundle validate` **does not check resource references at all** — see section 8.

### The DSN path still exists

A pre-configured DSN keeps working, and it is how a deliberate migration against a local
Postgres runs. `dsn_from_env` in
[`dsn.py`](../packages/shellbox-registry/src/shellbox_registry/dsn.py) reads
`SHELLBOX_DATABASE_URL`, or the `SHELLBOX_PG_USER` / `_PASSWORD` / `_HOST` / `_PORT` / `_DB`
components. Section 6 of [`docs/lakebase-handoff.md`](lakebase-handoff.md) records the manual
recipe, including why the token goes in its own variable rather than into a URL.

> **A configured endpoint WINS over a DSN.** `dsn_from_env` defaults the host to
> `localhost:55432` as soon as **any** `SHELLBOX_PG_*` variable is set, so a developer who once
> exported one always has a DSN. If the DSN won, `make deploy` would migrate their laptop and
> report success while the deployed App read an unmigrated database.
> [`tests/unit/test_migration_target.py`](../tests/unit/test_migration_target.py) asserts the
> precedence by checking which host the migration actually dials.

`make migrate` and `make grant` still refuse to run with **neither** configured, because
`dsn_from_env` would otherwise silently target `localhost:55432`.

---

## 4. The service-principal grant

**`make deploy` runs the grant as its last step.** Run `make grant TARGET=<target>` on its own
after any migration that adds a table the App reads.

Everything verified before this authenticated as a **workspace user**. The App authenticates as
its **service principal**, and that service principal's Postgres role name is its client id.
Section 5 of [`docs/lakebase-handoff.md`](lakebase-handoff.md) names this as the most likely
thing to be missing on a first deploy, and nothing has exercised it.

Skipping the grant breaks the App in one specific way, and every part of that way is quiet:

- `GET /` stays green. It touches no database, by design.
- Terminals keep working. The relay is in memory.
- Every inventory call returns 500, and no automatic signal observes it.

That is why the grant is a target rather than a paragraph.

### The order, and why each step is where it is

Section 1 has the full pipeline. The three positions that matter for the grant:

| | Step | Runs as |
|---|---|---|
| 3 | `make migrate` — `alembic upgrade head` | you |
| 4 + 5 | `scripts/deploy-app.sh`, then the wait for `ACTIVE` | you |
| 6 | `make grant` — `SELECT` on `hosts` and `sessions` for the App's service principal | you |
| 7 | [`scripts/check_ready.py`](../scripts/check_ready.py) — the proof that step 6 landed | the App |

Step 3 comes before step 6 because `GRANT SELECT ON TABLE` needs the table. Postgres enforces
that ordering, and [`scripts/grant_app_sp.py`](../scripts/grant_app_sp.py) turns its error into
a sentence naming `make migrate`. Step 3 also makes the deploying principal the **owner** of
those tables, and a principal cannot grant a privilege it does not hold.

Steps 4 and 5 come before step 6 because the service principal's role appears in `pg_roles` some
time **after** the App is active. `make grant` waits for `compute_status.state` to read `ACTIVE`,
then retries the grant 5 times at 10-second intervals. Every statement it runs is idempotent,
which is what makes retrying safe rather than merely tolerable. The field names are **measured**:
on 2026-08-03, `databricks apps get shellbox -o json` reported `compute_status.state` as `ACTIVE`
and `service_principal_client_id` as a dashed uuid.

**Step 6 is green on everything the deploying principal can observe, and silent about the one
thing it cannot.** The grant reads itself back with `has_table_privilege`, which proves the
catalog agrees. It does not prove the App can read the database **as itself** — only the App can
exercise its own credential path. Step 7 is that proof, and it **fails the deploy**: a deploy
that reported success while the App could connect but not read is the state `/ready` exists to
catch. The curl below is the same check by hand, for a deploy you did not run.

### The migration runs as you, never as the App

`alembic upgrade head` is a deploy-time action. Granting DDL to the serving principal to save one
credential switch is how a read-only service acquires write access nobody decided to give it —
and it arrives disguised as the fix for a permission error. Three mechanisms enforce this, and
none of them is a comment:

- The Postgres role is **derived** from `current_user.me()`, so the connection authenticates as
  whoever ran the command. This is the structural half: there is no exported name to get wrong.
- `make migrate` and `make grant` both run
  [`scripts/check_deploy_principal.py`](../scripts/check_deploy_principal.py) first. It refuses a
  Postgres user whose name has the shape of a service principal role, which is a dashed uuid. A
  workspace user's role name is their email, so it cannot refuse a human by accident. On the
  derived path it has nothing to inspect, and the alembic environment in
  [`env.py`](../packages/shellbox-registry/src/shellbox_registry/alembic/env.py) applies the same
  shape rule to the **derived** name instead, before it opens a connection.
- [`scripts/grant_app_sp.py`](../scripts/grant_app_sp.py) refuses when the server reports
  `current_user` equal to the App's service principal. That is the same rule asserted against the
  identity that actually connected, rather than against the variables.

### What the grant contains

Three statements, and nothing else:

```sql
GRANT USAGE ON SCHEMA public TO "<service_principal_client_id>";
GRANT SELECT ON TABLE public.hosts TO "<service_principal_client_id>";
GRANT SELECT ON TABLE public.sessions TO "<service_principal_client_id>";
```

The App's only legitimate need is reads. Its writers are the 1 to 32 `shellbox-mcp` processes,
and each of those authenticates as the real user who runs it.

Deliberately absent, because each one hands the App every future table: `GRANT SELECT ON ALL
TABLES IN SCHEMA`, `ALTER DEFAULT PRIVILEGES`, and every write privilege.

After granting, the script reads its own work back with `has_table_privilege` and fails if
`SELECT` is missing or if `INSERT`, `UPDATE`, `DELETE` or `TRUNCATE` is present. Two tests hold
the same line with no credential:
[`tests/unit/test_grant_scope.py`](../tests/unit/test_grant_scope.py) asserts the statements are
reads on those two tables, and
[`tests/unit/test_no_app_writes.py`](../tests/unit/test_no_app_writes.py) asserts the
`shellbox_app` package calls no registry writer.

### "SELECT only" describes the grant, not the role

[`resources/app.yml`](../resources/app.yml) declares `permission: CAN_CONNECT_AND_CREATE` on the
App's Lakebase resource. **That is the only value the field accepts** — forced, not chosen. So the
service principal's role can create objects even with no grant on `hosts` or `sessions`, and no
grant written here narrows that.

What the create capability does **not** reach is the registry's rows. Creating a table is not a
privilege on another table, and the read-back above asserts the role holds no write privilege on
either of ours.

`scripts/grant_app_sp.py` therefore **reports** the capability on every run, as two measured
values rather than an assumption:

```
has_schema_privilege(role, 'public', 'CREATE')
has_database_privilege(role, current_database(), 'CREATE')
```

`make grant` does not fail on them. A script that failed on a consequence of a forced setting
would make the deploy unpassable.

**Can it be narrowed?** `scripts/grant_app_sp.py --revoke-schema-create` attempts
`REVOKE CREATE ON SCHEMA public FROM "<role>"`. **Measured 2026-08-03 against PostgreSQL 17.10 in
a local container, with a stand-in role:** the revoke is accepted, and `has_schema_privilege`
flips to false. **That measurement is not against Lakebase.** Two things are unverified: whether
the App's binding needs the privilege, and whether the next `bundle deploy` re-grants it while
reconciling the binding. The flag is opt-in for that reason, and the undo is
`GRANT CREATE ON SCHEMA public TO "<role>"`.

**Until a first deploy measures those two things, the create capability is an accepted
residual.** Record it as that, and do not describe the role as read-only. "SELECT only" is true
of the grant and false of the role.

### If the role never appears

`make grant` fails after its 5 attempts, and says so. There are two causes and they need
different fixes:

1. The App's Lakebase binding did not create the role. Confirm the last `bundle deploy`
   reconciled the App resource.
2. The role has to be created explicitly. `databricks postgres create-role --help` (CLI v1.8.0)
   documents a service-principal role spec, and the command is idempotent with
   `--replace-existing`. The failure message prints it with the ids filled in. Omit
   `membership_roles`: the role must start with default privileges, and the grant above is what
   gives it the two reads it needs.

**Which cause is real is unverified**, because no deploy has run. Whoever runs the first one
should record the answer here.

### Verifying the grant

Two checks. The first proves the App's own credential path; the second proves the grant is not
wider than it says. Neither is optional, and they fail differently.

#### 1. `/ready` returns a body saying `ready: true`

**Content, not status code.** An unauthenticated request to this edge returns HTTP 200 with an
HTML login body, so `curl -f` proves nothing at all here.

```sh
eval "$(scripts/bundle-vars.sh --target dev --profile fevm-west)"
APP_URL=$(databricks apps get "$SHELLBOX_APP_NAME" -p fevm-west -o json \
  | python3 -c 'import json,sys;print(json.load(sys.stdin)["url"])')
TOKEN=$(databricks auth token -p fevm-west \
  | python3 -c 'import json,sys;print(json.load(sys.stdin)["access_token"])')
curl -sS -H "Authorization: Bearer $TOKEN" "$APP_URL/ready" | python3 -c '
import json, sys
body = sys.stdin.read()
data = json.loads(body)
assert data.get("ready") is True, body
print("ready: true")
'
```

**A failure here means the App cannot read the database as itself.** The `json.loads` raising on
an HTML body is the same finding with a different shape: the token was refused, so the request
never reached the App. If `make grant` reported `ok` and this fails, the grant is not the suspect
— the App's own credential path is.

`make deploy` runs this as step 7, through
[`scripts/check_ready.py`](../scripts/check_ready.py), and fails the deploy on a false answer.
Run the curl by hand when you did not run the deploy, or when you want the answer now.

**The body is deliberately generic.** `{"ready": false, "reason": "no_database"}` means the App
resolved no Lakebase endpoint from its environment; `"query_failed"` means the read raised. The
route never names a host, a database, a schema or a relation, because every workspace user the
edge lets through can reach it. The driver error and its traceback go to the App's own log
instead: `databricks apps logs <app> --profile fevm-west`.

**The App also probes itself every 30 minutes** and logs a WARN line when the probe fails, which
is what shortens "the App has been broken for a week" to "there is a WARN line at the top of the
log". It is a recorded failure and **not** a notification — no alerting path is built. See
[`packages/shellbox-app/src/shellbox_app/ready.py`](../packages/shellbox-app/src/shellbox_app/ready.py).

#### 2. An `INSERT` as the service principal is refused with SQLSTATE `42501`

The grant is only tested by something the App's identity is refused.

```sh
# Authenticate as the App's service principal, not as yourself. `databricks postgres
# generate-database-credential` mints a credential for the CALLER and takes no principal
# argument, so there is no way to mint one for another identity.
export SHELLBOX_PG_USER=<service_principal_client_id>
export SHELLBOX_PG_PASSWORD=$(databricks postgres generate-database-credential \
  "$SHELLBOX_PG_RESOURCE" -p <sp-profile> -o json \
  | python3 -c 'import json,sys;print(json.load(sys.stdin)["token"])')

# CRITICAL: SHELLBOX_PG_HOST, and it is not optional. `dsn_from_env` defaults the host to
# localhost:55432 as soon as ANY SHELLBOX_PG_* variable is set, so a block that set the user
# and the token and not the host would run this whole check against a LOCAL Postgres and
# report a refusal that says nothing about Lakebase. That is the same trap `make migrate`
# guards with `require-pg-host`, arriving here through a hand-written DSN instead.
export SHELLBOX_PG_HOST=$(databricks postgres get-endpoint "$SHELLBOX_PG_RESOURCE" \
  -p fevm-west -o json \
  | python3 -c 'import json,sys;print(json.load(sys.stdin)["status"]["hosts"]["host"])')

# `databricks_postgres`, which is what `variables.pg_database` declares and what
# `scripts/bundle-vars.sh` prints as SHELLBOX_PG_DB. A wrong database name reaches a real
# endpoint and fails with `42P01`, which reads as "the migration never ran".
export SHELLBOX_PG_PORT=5432 SHELLBOX_PG_SSLMODE=require
export SHELLBOX_PG_DB="${SHELLBOX_PG_DB:-databricks_postgres}"

uv run python - <<'PY'
import psycopg
from shellbox_registry.dsn import dsn_from_env

with psycopg.connect(dsn_from_env(), autocommit=True) as connection, connection.cursor() as cursor:
    cursor.execute("SELECT count(*) FROM hosts")
    print("SELECT as the service principal:", cursor.fetchone()[0], "rows")
    try:
        cursor.execute(
            "INSERT INTO hosts (host_id) VALUES ('11111111-1111-1111-1111-111111111111')"
        )
    except psycopg.errors.Error as error:
        print("INSERT refused, SQLSTATE:", error.sqlstate)
    else:
        raise SystemExit("FAIL: the INSERT was accepted")
PY
```

`<sp-profile>` is a `~/.databrickscfg` profile holding an OAuth client id and secret for the
App's service principal, created with `databricks account service-principal-secrets create`.
**Whether an App-managed service principal accepts a client secret is unverified here**, and it
is the one part of this procedure nobody has run.

##### This procedure is still unrun, and `SET ROLE` is not a way around it

**Measured 2026-08-03** against the `dev` endpoint, PostgreSQL 17.10, as the deploying
principal:

```
pg_has_role('tanner.wendland@databricks.com', '<app_sp_client_id>', 'MEMBER') = False
```

So the deploying principal **cannot assume the App SP's role**, and `SET ROLE` followed by an
`INSERT` is not an alternative route to the refusal. That is the obvious shortcut and it is
closed. The block above, with a real client secret, remains the only known way to attempt a
write **as** the service principal.

**What is measured today, and what each half proves:**

| Claim | Evidence | Strength |
|---|---|---|
| The SP **can read** both granted tables | `/ready` on the deployed App returns `{"ready": true}`. That is the App minting a token as itself and reading both relations through the production path | Stronger than a laptop-minted credential. It exercises the path that actually runs |
| The SP **cannot write** | `has_table_privilege` reports `SELECT` true and `INSERT`, `UPDATE`, `DELETE`, `TRUNCATE` false on both tables | **Catalog evidence, NOT an attempted-write refusal.** The two are different claims. A catalog answer says what the grant records; only a refused statement says what Postgres enforces |

Do not read the second row as satisfying the `42501` requirement. It does not.

**Assert the SQLSTATE, never the message text**, which is not stable across Postgres versions.

**Measured 2026-08-03**, against PostgreSQL 17.10 in a local container with a stand-in role
granted exactly what `make grant` grants: the `SELECT` returns, and the `INSERT` is refused with
SQLSTATE `42501`. The deliberately incomplete row matters and it is safe: Postgres checks the
privilege **before** it evaluates the constraints, so the statement still reaches the privilege
check.

**What a wrong answer means:**

| Result | Meaning |
|---|---|
| The `INSERT` succeeds | The grant is wider than this document. Re-run `make grant` and read its `has_table_privilege` lines |
| `42501` | Correct. The role has no `INSERT` |
| `23502`, `23503` | A `NOT NULL` or foreign-key violation. The statement never reached a privilege check, so it proves nothing. Fix the row, not the grant |
| `42P01` | The table does not exist. `make migrate` has not run against this database |

### The seeded inventory rows on `dev`

The `dev` registry carries four rows whose ids all start with `w37a-`. They are **deliberate and
durable**, written **2026-08-03** by the deploying principal, and they are not test debris.

| Row | Id | Why it exists |
|---|---|---|
| host | `w37a-host-bootstrapped` | The ordinary case: a real `sandbox_id` (`w37a-sandbox-0001`) and a real `owner_email` |
| host | `w37a-host-not-bootstrapped` | `sandbox_id` is **NULL**, which a host that was never bootstrapped has by design — a sandbox cannot learn its own id, section 1 of [`docs/sandbox-environment.md`](sandbox-environment.md). It is the rendering case `NOT_BOOTSTRAPPED_LABEL` in [`inventory.py`](../packages/shellbox-app/src/shellbox_app/inventory.py) exists for |
| session | `w37a-session-on-bootstrapped` | A session under the bootstrapped host, carrying `cols` and `rows` |
| session | `w37a-session-on-not-bootstrapped` | A session under the host with no `sandbox_id`, which is what a renderer must not draw as a blank row |

The second host's `owner_email` is `w37a-other@example.invalid`. That is RFC 2606's reserved
TLD, so it can never be a real mailbox — and a second owner is what makes the `mine` label
observable: one row is the viewer's and one is not.

**Without these rows the inventory endpoints cannot be checked at all.** An empty registry makes
`GET /api/hosts` return an empty list, which is indistinguishable from a broken endpoint.

### Checking the inventory endpoints

The same token and URL as the `/ready` check above:

```sh
curl -sS -H "Authorization: Bearer $TOKEN" "$APP_URL/api/hosts" | python3 -c '
import json, sys
body = sys.stdin.read()
data = json.loads(body)
assert data["stale"] is False, body
labels = {row["host_id"]: row["sandbox_label"] for row in data["hosts"]}
assert labels["w37a-host-not-bootstrapped"] == "not bootstrapped", labels
print("hosts:", labels)
'
```

**Content, not status code**, for the reason the `/ready` check gives. Two fields carry the
answer:

- `stale` separates "there is nothing to show" from "the App cannot see anything".
  `NullRegistry` returns an empty list and never raises, so an empty `hosts` alone does not tell
  you which state you are in. When `stale` is true a `reason` accompanies it, from the same
  closed set `/ready` uses.
- `sandbox_label` is `not bootstrapped` for a host with no `sandbox_id`, never an empty string
  and never the bare `host_id`.

`X-Forwarded-Email` changes **only** the `viewer_email` field and the per-row `mine` flag. It
never changes which rows come back. That is decision D5, and
[`tests/unit/test_app_inventory.py`](../tests/unit/test_app_inventory.py) asserts it.

### Checking the renderer

The page is at **`$APP_URL/ui/`**, and the trailing slash matters — every asset it loads is
referenced relatively, so `/ui` without it would resolve `app.css` against the App root. The
mount redirects, so a browser is fine either way; a `curl` that does not follow redirects is not.

```sh
curl -sS -H "Authorization: Bearer $TOKEN" "$APP_URL/ui/" | head -5
curl -sS -o /dev/null -w '%{http_code} %{size_download}\n' \
  -H "Authorization: Bearer $TOKEN" "$APP_URL/ui/vendor/xterm.js"
```

`GET /` stays JSON and is still the smoke target — it must not become the page, because it is
the one check that has to answer when Lakebase is the thing that is broken. It carries a `ui`
field naming the path above, so a person who opens the App URL is told where to go.

**Everything under `/ui/` is same-origin and vendored**, xterm.js included. There is no CDN
reference and there must not be one: the Apps edge authenticates the request that fetched the
page, so a third-party host would be an unauthenticated dependency on its load path.
[`tests/unit/test_static_assets.py`](../tests/unit/test_static_assets.py) asserts the absence of
any absolute URL and checks the vendored bundles against the hashes recorded in
[`static/vendor/README.md`](../packages/shellbox-app/src/shellbox_app/static/vendor/README.md) —
browser assets have no lockfile, so that table is their only integrity record.

WARNING: **the renderer above the protocol layer has no automated gate at all.** ADR-23 tests
the browser client's protocol half in Python
([`tests/unit/test_client_protocol.py`](../tests/unit/test_client_protocol.py)) and compares the
JavaScript's declared constants against it
([`tests/unit/test_client_parity.py`](../tests/unit/test_client_parity.py)), and nothing
executes the JavaScript. Frame decode, reconnect, resume and the bounded `subscriber_conflict`
retry are covered; xterm.js wiring and every later change to it are covered by a human opening
this page. That is a standing cost of the decision, not a gap to be closed later.

---

## 5. The endpoint path is constructed, never resolved

[`scripts/bundle-vars.sh`](../scripts/bundle-vars.sh) prints three values for a target. Two are
read out of `databricks bundle validate -o json`; the third is built.

| Value | Where it comes from |
|---|---|
| `SHELLBOX_APP_NAME` | Read. `resources.apps.shellbox_app.name` resolves. |
| `SHELLBOX_APP_SOURCE_PATH` | Read. `resources.apps.shellbox_app.source_code_path` resolves. |
| `SHELLBOX_PG_RESOURCE` | **Built** from the three declared ids. |

The third is built because nothing resolves it. `${resources.postgres_projects.pg_project.id}`
stays **literal** in `bundle validate -o json`, in `bundle summary -o json` and in
`bundle plan -o json` — those are deploy-time outputs. Constructing the path from the ids the
bundle already declares is therefore the sanctioned route, not a shortcut. What is forbidden is
writing a project name into that string by hand, and `make lint` asserts both that no occurrence
does and that the construction exists at all.

---

## 6. What `make lint` asserts about the bundle

[`scripts/check_bundle_statics.py`](../scripts/check_bundle_statics.py) runs three checks as a
step of `make lint`. They parse files and never authenticate, so they run on a checkout with no
Databricks credential.

1. **No `lifecycle`, no `started`** on any App resource — section 1's normative rule.
2. **The `dev` and `prod` targets name different Apps**, and the App resource takes its name
   from a variable so those per-target values actually reach it.
3. **No Lakebase resource path is written out by hand**, and the constructed form exists.

Each check first asserts its input exists, then asserts it found the thing it is about to
inspect. That ordering is the point: before this bundle was written there was no
`databricks.yml`, and the obvious `grep`-and-fail form of every one of these checks passes on
that tree, because `grep` exits 2 on a missing file.

---

## 7. Distinct names per target

**`mode: development` does not prefix App names.** The `name_prefix` preset applies to jobs and
pipelines. A dev-target validate keeps the App name verbatim.

So two targets sharing one App name means `make deploy TARGET=dev` reconciles the **production**
App, and **nothing errors** — from the bundle's point of view it did what it was told. The same
hole exists for the Lakebase project, whose branch and endpoint carry `replace_existing: true`
and would be adopted rather than refused.

Both names are therefore declared explicitly per target:

| Target | App | Lakebase project |
|---|---|---|
| `dev` (default) | `shellbox-dev` | `shellbox-pg-dev` |
| `prod` | `shellbox` | `shellbox-pg` |

Verify the resolved names differ whenever the target definitions change:

```sh
for t in dev prod; do
  databricks bundle validate -t $t --profile fevm-west -o json \
    | python3 -c 'import json,sys;print(json.load(sys.stdin)["resources"]["apps"]["shellbox_app"]["name"])'
done
```

`make lint` compares the declarations, which is what gates every pull request. This command
compares the **resolved** values, and it covers the narrower case of names that differ in the
file and resolve to the same thing. It needs a workspace profile, so it cannot live in
`make lint`.

---

## 8. What `bundle validate` does not check

**`bundle validate` does not check resource references at all.** Do not read "Validation OK!" as
evidence that the resource graph is sound.

**Measured on 2026-08-03**, CLI v1.8.0, against this bundle. The branch's `parent` was changed
to reference a resource key that does not exist:

```
parent: ${resources.postgres_projects.nonexistent.id}
```

`databricks bundle validate --strict -t dev` printed **`Validation OK!`** and exited **0**.

The contrast is sharp, and it is worth knowing which half is checked. A nonexistent **variable**
reference is caught:

```
branch_id: ${var.no_such_variable}     ->  "Found 1 error", exit 1
```

So `${var.*}` is validated and `${resources.*}` is not. A typo in a resource reference surfaces
only at deploy time. That is why the check that the bundle provisioned Lakebase is a
**post-deploy** `databricks postgres get-endpoint`, in section 3, and never `validate` output.
