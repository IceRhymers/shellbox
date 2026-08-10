#!/usr/bin/env bash
#
# The ordered pipeline behind `make deploy`.
#
# Usage:  scripts/deploy.sh [--target dev] [--profile fevm-west]
#
# WHY A SCRIPT, and not a Makefile recipe. Seven steps, each of which can fail, and the ORDER is
# the load-bearing part of the arrangement. A recipe is a list of commands with nowhere to
# record why a step is where it is, and no way to wait for a state to arrive.
# `databricks-code-search/scripts/deploy.sh` reached the same conclusion for the same pipeline,
# and this mirrors its shape.
#
# THE ORDER. Every step needs the one before it, and none of them is arbitrary:
#
#   1. `bundle deploy`   provisions Lakebase and the App RESOURCE. Nothing can precede it:
#                        steps 2 and 3 address an endpoint that does not exist until it runs.
#   2. bundle-vars       ONE `validate` call for the App name, the App's code root, and the
#                        endpoint resource path. Read back rather than recomputed, so each
#                        value stays declared once in the bundle.
#   3. `make migrate`    BEFORE the App's code deploys. It needs only the endpoint, and running
#                        it as the DEPLOYING PRINCIPAL is what makes that principal the OWNER
#                        of hosts and sessions -- which is what lets step 6 grant reads on them
#                        at all. A principal cannot grant a privilege it does not hold.
#   4. `deploy-app.sh`   ships the code and starts compute. The App's service principal, and
#                        its Postgres role, materialise here and not at step 1.
#   5. wait for ACTIVE   the SP's role appears in `pg_roles` some time AFTER activation, so
#                        until this passes the grant has no role to grant to.
#   6. `make grant`      needs the tables from step 3 and the role from step 5.
#   7. `/ready`          proves the grant landed, and it is LAST because it is the only step
#                        that exercises the App's OWN credential path. Step 6 reads its grant
#                        back with `has_table_privilege`, which proves the catalog agrees and
#                        proves nothing about the App connecting as itself. A deploy that
#                        reported success while the App could connect but not read is the
#                        state this step exists to fail.
#
# WHAT THIS DOES NOT DO. The one-time `bundle deployment bind` for a target whose App already
# exists is not a step here -- it prompts unless it is passed --auto-approve, so inside this
# script it would either block or silently re-adopt whatever the name points at on every run.
# It is a runbook step; see docs/deploy.md section 2.
#
# The retry helper the donor's script defines is not duplicated here. The one retryable
# condition on this path is the App SP's role appearing in `pg_roles`, and
# `scripts/grant-app-sp.sh` owns that with its own `retry 5 10` -- see its header for why only
# one exit code is treated as retryable.
#
# WARNING: this authenticates, deploys, and writes to the registry. Nothing in `make lint`
# calls it.

set -euo pipefail

TARGET="dev"
PROFILE="${DATABRICKS_PROFILE:-fevm-west}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --target|-t)  TARGET="$2"; shift 2 ;;
    --profile|-p) PROFILE="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# The CLI calls below pass --profile; the SDK reads the environment. Both must name the SAME
# workspace, and this line is what guarantees it: `alembic` and the grant resolve their endpoint
# through the SDK, and an unset DATABRICKS_CONFIG_PROFILE resolves to DEFAULT -- which in this
# account is a DIFFERENT workspace with a confusingly similar name.
export DATABRICKS_CONFIG_PROFILE="$PROFILE"

die() {
  echo "deploy: $*" >&2
  exit 1
}

# req <value> <human name>: fail loudly on an empty derived value, else echo it back.
#
# The net under the `eval` below. `eval "$(cmd)"` reports the status of `eval`, not of `cmd`, so
# a failed derivation produces an empty string that `set -e` never sees.
req() {
  [[ -n "$1" ]] || die "could not derive $2 (empty)"
  printf '%s' "$1"
}

# Field name measured 2026-08-03 against the `shellbox` App, CLI v1.8.0: `compute_status.state`
# reads ACTIVE. `app_status.state` reads RUNNING and is a different question.
#
# python3 rather than jq, which is the idiom every other script here uses and one fewer thing a
# contributor has to install. NOTE: the block is a single-quoted shell argument, so it contains
# no single quote of its own -- one would break the shell parse, not the Python.
app_state() {
  databricks apps get "$1" --profile "$PROFILE" --output json 2>/dev/null \
    | python3 -c '
import json, sys
app = json.load(sys.stdin) or {}
print((app.get("compute_status") or {}).get("state") or "")
' 2>/dev/null || true
}

# 30 attempts at 10 s is 5 minutes, the same budget `scripts/grant-app-sp.sh` uses for the same
# wait. `databricks apps create` warns that provisioning compute takes minutes.
wait_active() {
  local app="$1" state="" i
  for i in $(seq 1 30); do
    state="$(app_state "$app")"
    if [[ "$state" == "ACTIVE" ]]; then
      echo "    compute_status.state=ACTIVE"
      return 0
    fi
    echo "    compute_status.state=${state:-<empty>}; waiting (${i}/30)"
    sleep 10
  done
  echo "ERROR: the App $app never reached ACTIVE." >&2
  echo "  Last observed compute_status.state: ${state:-<empty>}." >&2
  echo "  The grant below needs the App running: its service principal's Postgres role appears" >&2
  echo "  after activation. Check the App's own logs before re-running." >&2
  exit 1
}

echo "==> [1/7] bundle deploy -t $TARGET (Lakebase and the App resource)"
databricks bundle deploy -t "$TARGET" --profile "$PROFILE"

echo "==> [2/7] reading the App and the Lakebase endpoint from the bundle"
if ! BUNDLE_VARS="$("$REPO/scripts/bundle-vars.sh" --target "$TARGET" --profile "$PROFILE")"; then
  die "could not read the bundle for target '$TARGET'; see the message above"
fi
eval "$BUNDLE_VARS"
APP_NAME="$(req "${SHELLBOX_APP_NAME:-}" "the App name")"
SOURCE_CODE_PATH="$(req "${SHELLBOX_APP_SOURCE_PATH:-}" "the App source_code_path")"
SHELLBOX_PG_RESOURCE="$(req "${SHELLBOX_PG_RESOURCE:-}" "the Lakebase endpoint resource path")"
SHELLBOX_PG_DB="$(req "${SHELLBOX_PG_DB:-}" "the Postgres database name")"
# EXPORTED, because steps 3 and 6 run child processes that read it from the environment to
# derive the host, the Postgres role and the OAuth token. Unexported, both would fall back to
# `dsn_from_env` -- which on a machine with a stale SHELLBOX_PG_HOST means migrating and
# granting on a local Postgres, and reporting success.
export SHELLBOX_PG_RESOURCE
# EXPORTED for the same reason, and it is what makes step 3, step 6 and the App provably reach
# the SAME database. `alembic/env.py` and `scripts/grant_app_sp.py` both read SHELLBOX_PG_DB
# and fall back to the registry's `DEFAULT_DATABASE` when it is unset; step 4 stamps the App's
# copy from the same value. So the bundle's `pg_database` is the one declaration, and no step
# here relies on a fallback agreeing with it.
export SHELLBOX_PG_DB
echo "    app=$APP_NAME"
echo "    source=$SOURCE_CODE_PATH"
echo "    endpoint=$SHELLBOX_PG_RESOURCE"
echo "    database=$SHELLBOX_PG_DB"

echo "==> [3/7] make migrate (alembic upgrade head, as the deploying principal)"
make -C "$REPO" migrate TARGET="$TARGET" PROFILE="$PROFILE"

echo "==> [4/7] deploying the App's code and starting compute"
# The endpoint and the database name are PASSED rather than inherited from the environment.
# `deploy-app.sh` stamps them into the staged `app.yaml`, which is the App's only source of
# them -- nothing in this shell reaches the container. Passing them makes the flag list the
# contract, so a bare run of that script cannot silently pick up an operator's exported
# variables and stamp a different endpoint than this pipeline migrated.
"$REPO/scripts/deploy-app.sh" --profile "$PROFILE" --app "$APP_NAME" \
  --source-code-path "$SOURCE_CODE_PATH" \
  --pg-resource "$SHELLBOX_PG_RESOURCE" --pg-database "$SHELLBOX_PG_DB"

echo "==> [5/7] waiting for the App to reach ACTIVE"
wait_active "$APP_NAME"

echo "==> [6/7] granting the App's service principal its reads"
make -C "$REPO" grant TARGET="$TARGET" PROFILE="$PROFILE"

APP_URL="$(databricks apps get "$APP_NAME" --profile "$PROFILE" --output json \
  | python3 -c 'import json, sys; print((json.load(sys.stdin) or {}).get("url") or "")')"
APP_URL="$(req "$APP_URL" "the App url")"

echo "==> [7/7] proving the App can read the registry as itself (GET /ready)"
# This is the step that closes the gap step 6 leaves open. `make grant` proves the CATALOG
# agrees; only the App can exercise its own credential path, so only the App can answer this.
#
# The token is minted here rather than earlier because the steps above can take minutes, and a
# workspace token has a lifetime. `scripts/deploy-app.sh --verify` mints its own the same way.
#
# CRITICAL: `scripts/check_ready.py` asserts on the response BODY, never on the status code.
# An unauthenticated request to this edge returns HTTP 200 with an HTML login page, measured by
# the Phase 1 probe, so a status check reads an auth failure as success. `set -e` turns a
# non-zero exit here into a FAILED DEPLOY, which is the point: a deploy that reports success
# while the App can connect and not read is what this whole step exists to prevent.
#
# python3 rather than `uv run`: the check imports nothing outside the standard library, which
# is the same reasoning the Makefile records for `scripts/check_lockfile.py`.
SHELLBOX_APP_URL="$APP_URL" \
SHELLBOX_EDGE_TOKEN="$(databricks auth token --profile "$PROFILE" \
  | python3 -c 'import json, sys; print(json.load(sys.stdin)["access_token"])')" \
  python3 "$REPO/scripts/check_ready.py"

echo "==> deployed: $APP_URL"
