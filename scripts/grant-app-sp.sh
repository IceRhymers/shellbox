#!/usr/bin/env bash
#
# Grant the App's service principal the reads it needs, and FAIL if the grant does not land.
#
# Usage:  scripts/grant-app-sp.sh [--profile fevm-west] [--target dev] [--app shellbox-dev]
#                                 [--revoke-schema-create]
#
#   --app     the App to read the service principal out of. Omitted, it is resolved from the
#             bundle for --target, so the name is declared once in `resources/app.yml`.
#
#   --revoke-schema-create
#             passed through to `scripts/grant_app_sp.py`. Opt-in and UNVERIFIED -- read that
#             script's header before using it.
#
# WHY THIS EXISTS, and it is worth the paragraph. Everything verified before this authenticated as
# a WORKSPACE USER. The App authenticates as its service principal, whose Postgres role name is
# the SP client id, and that path has never run. If the role has no grant, the App is broken in
# one specific way: `GET /` stays green because it touches no database, the terminals keep working
# because the relay is in memory, and every inventory call 500s. Nothing automatic observes that.
# So the failure is silent, and this script is what stops it from starting.
#
# THE ORDER MATTERS, and two of the three steps are load-bearing:
#
#   1. `alembic upgrade head` runs FIRST, and as the DEPLOYING PRINCIPAL. `make migrate` does it.
#      Migrations are a deploy-time action. Granting DDL to the serving principal to save one
#      credential switch is how a read-only service acquires write access nobody decided to give
#      it, so this script refuses to run as the App SP -- see `scripts/check_deploy_principal.py`,
#      which it runs below, and the `current_user` assertion in `scripts/grant_app_sp.py`.
#      `GRANT SELECT ON TABLE` also needs the table, so Postgres enforces the ordering too.
#   2. The App must reach ACTIVE before the grant, because the SP's role appears in `pg_roles`
#      some time after activation rather than at deploy.
#   3. `retry 5 10` absorbs the remainder of that lag. Every statement the grant runs is
#      idempotent, which is what makes retrying it safe rather than merely tolerable.
#
# WARNING: this authenticates and it writes to the registry. It is on the deploy path only.
# `make lint` must stay runnable with no Databricks credential, so nothing in the lint lane calls
# it.

set -euo pipefail

PROFILE="${DATABRICKS_PROFILE:-fevm-west}"
TARGET="dev"
APP_NAME=""
REVOKE_ARGS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --profile|-p)            PROFILE="$2"; shift 2 ;;
    --target|-t)             TARGET="$2"; shift 2 ;;
    --app)                   APP_NAME="$2"; shift 2 ;;
    --revoke-schema-create)  REVOKE_ARGS+=("--revoke-schema-create"); shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# The retry idiom, and the shape is deliberate: attempts, delay, then the command. The plan
# specifies `retry 5 10 grant_attempt`, so the numbers are the caller's and the budget is 40
# seconds of waiting across 5 attempts.
#
# It retries on a NON-ZERO exit and nothing cleverer, so the decision about what is retryable
# belongs to the command. `grant_attempt` below makes that decision from an exit code, because
# most ways the grant can fail are not waiting problems and retrying them wastes 40 seconds before
# printing the same message.
retry() {
  local attempts="$1" delay="$2"
  shift 2
  local attempt=1
  until "$@"; do
    if (( attempt >= attempts )); then
      echo "    gave up after $attempts attempts" >&2
      return 1
    fi
    echo "    attempt $attempt of $attempts did not succeed; waiting ${delay}s"
    sleep "$delay"
    attempt=$(( attempt + 1 ))
  done
}

# Field names measured 2026-08-03 against the `shellbox` App, CLI v1.8.0:
# `compute_status.state` reads ACTIVE, `app_status.state` reads RUNNING, and
# `service_principal_client_id` reads a dashed uuid equal to the App's own `id`.
app_field() {
  databricks apps get "$APP_NAME" --profile "$PROFILE" --output json \
    | FIELD="$1" python3 -c '
import json, os, sys
app = json.load(sys.stdin)
path = os.environ["FIELD"].split(".")
node = app
for key in path:
    node = (node or {}).get(key)
print(node or "")
'
}

app_is_active() {
  local state
  state="$(app_field compute_status.state)" || return 1
  echo "    compute_status.state=${state:-<empty>}"
  [[ "$state" == "ACTIVE" ]]
}

grant_attempt() {
  local status=0
  uv run --project "$REPO" python "$REPO/scripts/grant_app_sp.py" \
    --role "$SP_CLIENT_ID" "${REVOKE_ARGS[@]+"${REVOKE_ARGS[@]}"}" || status=$?
  case "$status" in
    0) return 0 ;;
    # 3 is the ONE retryable outcome: the role is not in `pg_roles` yet. That is the lag this
    # whole retry exists for.
    3) return 1 ;;
    *)
      # Loud, and immediate. `exit` from inside the retry loop is intentional: waiting cannot fix
      # a missing migration or a refused grant, and 40 seconds of retries before the same message
      # teaches an operator to ignore this script's output.
      echo "ERROR: the grant failed for a reason retrying cannot fix (exit $status)." >&2
      exit 1
      ;;
  esac
}

# The bundle is read only when it has to be. With `--app` given, this script needs no bundle at
# all, which keeps it usable against an App that no target declares -- and `bundle validate`
# authenticates, so not calling it is also one fewer thing to fail.
PG_BRANCH=""
if [[ -z "$APP_NAME" ]]; then
  echo "==> resolving the App and the Lakebase branch from the bundle"
  eval "$("$REPO/scripts/bundle-vars.sh" --target "$TARGET" --profile "$PROFILE")"
  APP_NAME="$SHELLBOX_APP_NAME"
  PG_BRANCH="${SHELLBOX_PG_RESOURCE%/endpoints/*}"
fi
echo "    app=$APP_NAME target=$TARGET profile=$PROFILE"

echo "==> checking the credential is the deploying principal and not the App SP"
python3 "$REPO/scripts/check_deploy_principal.py" "the grant"

echo "==> waiting for the App to reach ACTIVE"
# 30 attempts at 10 s is 5 minutes, and the budget is separate from the grant's `retry 5 10` on
# purpose: this one waits for COMPUTE, which `databricks apps create` warns takes minutes, while
# the grant's retry waits for a catalog row.
if ! retry 30 10 app_is_active; then
  echo "ERROR: the App $APP_NAME never reached ACTIVE." >&2
  echo "  The grant needs the App running: its service principal's Postgres role appears after" >&2
  echo "  activation. Check the App's own logs before re-running this." >&2
  exit 1
fi

SP_CLIENT_ID="$(app_field service_principal_client_id)"
if [[ -z "$SP_CLIENT_ID" ]]; then
  echo "ERROR: $APP_NAME reports no service_principal_client_id." >&2
  echo "  That field is the App SP's Postgres role name, so there is nothing to grant to." >&2
  exit 1
fi
echo "    service principal: $SP_CLIENT_ID"

# Informational, and the distinction it draws is the one that makes a failure below diagnosable:
# the control plane's roles list answers "does Databricks think this role exists", and `pg_roles`
# answers "can Postgres see it". Those two can disagree, and only the second one lets a grant run.
#
# A SUBSTRING match on the raw JSON, deliberately. The response's field layout for a
# service-principal-backed role is unverified here -- nothing has listed roles on a real branch
# yet -- and a substring match cannot be wrong about a uuid appearing. It is not load-bearing: the
# authority is `pg_roles`, checked by `scripts/grant_app_sp.py`.
if [[ -n "$PG_BRANCH" ]]; then
  echo "==> asking the control plane whether a role exists for that principal"
  if databricks postgres list-roles "$PG_BRANCH" --profile "$PROFILE" --output json 2>/dev/null \
      | grep -q "$SP_CLIENT_ID"; then
    echo "    the branch lists a role naming $SP_CLIENT_ID"
  else
    echo "    the branch lists NO role naming $SP_CLIENT_ID (see the failure note below if the"
    echo "    grant now times out)"
  fi
fi

echo "==> granting reads on the registry tables"
if ! retry 5 10 grant_attempt; then
  echo "ERROR: the App SP's Postgres role never appeared in pg_roles." >&2
  echo "  This is the first-deploy failure the plan calls D-1 -- the App's service principal" >&2
  echo "  has no usable Postgres role -- and it is now loud instead of silent. Two causes," >&2
  echo "  in the order to check them:" >&2
  echo "" >&2
  echo "  1. The App's Lakebase binding did not create the role. The binding is declared in" >&2
  echo "     resources/app.yml; confirm the last 'databricks bundle deploy' reconciled it." >&2
  echo "  2. The role has to be created explicitly. The CLI has an API for exactly this shape" >&2
  echo "     (databricks postgres create-role --help, v1.8.0), and it is idempotent with" >&2
  echo "     --replace-existing:" >&2
  echo "" >&2
  echo "       databricks postgres create-role ${PG_BRANCH:-<the branch resource path>} \\" >&2
  echo "         --replace-existing \\" >&2
  echo "         --role-id $SP_CLIENT_ID --profile $PROFILE \\" >&2
  echo "         --json '{\"spec\": {\"identity_type\": \"SERVICE_PRINCIPAL\"," >&2
  echo "                            \"postgres_role\": \"$SP_CLIENT_ID\"," >&2
  echo "                            \"auth_method\": \"LAKEBASE_OAUTH_V1\"}}'" >&2
  echo "" >&2
  echo "     Omit 'membership_roles'. The role must start with default privileges only; this" >&2
  echo "     script is what gives it the two reads it needs. Then re-run this script." >&2
  echo "     WARNING: which of these two causes is real is UNVERIFIED -- no deploy has run." >&2
  echo "     See docs/deploy.md section 4." >&2
  exit 1
fi

echo "==> granted"
