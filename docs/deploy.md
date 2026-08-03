# Deploying shellbox

`make deploy` provisions Lakebase and deploys the App. This file covers the two prerequisites
that are not `make` steps, and the one field nobody should add.

Everything here was checked against **Databricks CLI v1.8.0** on **2026-08-03**. Where a claim
was measured, it says so.

---

## 1. Two mechanisms, and why

`make deploy TARGET=<target>` runs two commands:

1. `databricks bundle deploy` reconciles the Lakebase project, branch, endpoint and database,
   plus the App **resource**.
2. [`scripts/deploy-app.sh`](../scripts/deploy-app.sh) deploys the App's **code**.

The second is not redundant. A `bundle deploy` does not create an app deployment, and the
Databricks docs state it verbatim: *"Deploying a bundle doesn't automatically deploy the app to
compute."* The code deployment is a separate API call. `deploy-app.sh` issues it, after
flattening the two uv workspace packages into one importable root — its header explains why that
flattening is not optional.

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

**Run this once per target, by hand, before that target's first deploy. It is not a `make`
step.**

```sh
databricks bundle deployment bind shellbox_app shellbox --auto-approve --profile fevm-west
```

**Why it is needed.** An unbound bundle plans `create` for an App that already exists, and the
deploy then fails with `409 RESOURCE_ALREADY_EXISTS`. The App named `shellbox` **does** exist —
created 2026-08-02, and confirmed running on 2026-08-03. So for the `prod` target this failure
is a certainty, not a risk. Skip the bind and the first deploy fails loudly.

The `dev` target names its App `shellbox-dev`, which does not exist yet, so `dev` needs no bind.

**Why it is not a `make` step.** `bundle deployment bind` prompts for confirmation unless
`--auto-approve` is passed. Inside a target it would therefore either block on an interactive
prompt, or carry `--auto-approve` and silently re-adopt whatever the name points at on every
run. Adoption is a one-time act and belongs in a runbook.

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

## 3. Prerequisite: the registry host must be set

`make deploy` and `make migrate` both refuse to run unless `SHELLBOX_PG_HOST` or
`SHELLBOX_DATABASE_URL` is set.

`dsn_from_env` in [`dsn.py`](../packages/shellbox-registry/src/shellbox_registry/dsn.py)
defaults the host to `localhost:55432` as soon as **any** `SHELLBOX_PG_*` variable is set. So a
half-configured environment makes `make migrate` migrate a laptop and report success. The guard
asserts the host is set; it does not assert that it is remote, because migrating a local
Postgres on purpose stays legitimate.

To point at the endpoint this bundle declares:

```sh
eval "$(scripts/bundle-vars.sh --target dev --profile fevm-west)"
export SHELLBOX_PG_HOST=$(databricks postgres get-endpoint "$SHELLBOX_PG_RESOURCE" \
  -p fevm-west -o json | python3 -c 'import json,sys;print(json.load(sys.stdin)["status"]["hosts"]["host"])')
export SHELLBOX_PG_PASSWORD=$(databricks postgres generate-database-credential \
  "$SHELLBOX_PG_RESOURCE" -p fevm-west -o json | python3 -c 'import json,sys;print(json.load(sys.stdin)["token"])')
export SHELLBOX_PG_USER=$(databricks current-user me -p fevm-west -o json \
  | python3 -c 'import json,sys;print(json.load(sys.stdin)["userName"])')
export SHELLBOX_PG_PORT=5432 SHELLBOX_PG_DB=shellbox SHELLBOX_PG_SSLMODE=require
```

`SHELLBOX_PG_SSLMODE` has no default and Lakebase requires TLS. Section 6 of
[`docs/lakebase-handoff.md`](lakebase-handoff.md) records why the token goes in its own variable
rather than into a URL.

### The first deploy of a target

On the very first deploy the endpoint does not exist, so its host cannot be resolved and the
guard has nothing to accept. **Do not set a placeholder.** Create the Lakebase resources first,
then resolve the host from what they made:

```sh
databricks bundle deploy -t dev --profile fevm-west   # creates project, branch, endpoint, database
eval "$(scripts/bundle-vars.sh --target dev --profile fevm-west)"
export SHELLBOX_PG_HOST=$(databricks postgres get-endpoint "$SHELLBOX_PG_RESOURCE" \
  -p fevm-west -o json | python3 -c 'import json,sys;print(json.load(sys.stdin)["status"]["hosts"]["host"])')
make deploy TARGET=dev
```

That `get-endpoint` reporting a host is also the only authoritative check that the bundle
provisioned Lakebase. Nothing before a deploy can tell you: `bundle validate` **does not check
resource references at all** — see section 7.

---

## 4. The endpoint path is constructed, never resolved

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

## 5. What `make lint` asserts about the bundle

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

## 6. Distinct names per target

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

## 7. What `bundle validate` does not check

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
