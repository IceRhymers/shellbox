#!/usr/bin/env bash
#
# Deploy `shellbox-app` to Databricks Apps.
#
# The whole reason this is a script and not three CLI calls in a runbook is the flattening
# step. `shellbox_transport` and `shellbox_registry` are uv WORKSPACE packages:
# `packages/shellbox-app/pyproject.toml` resolves them through `[tool.uv.sources] ... =
# { workspace = true }`, and pip on the Apps runtime has no such notion -- it reads
# `requirements.txt`, sees no `shellbox-transport` on any index, and the App boots into
# `ModuleNotFoundError`. Nothing in the repo's layout hints at this, so a hand-run deploy gets
# it wrong once per person.
#
# The fix is to copy all three packages side by side into one root:
#
#     <root>/app.yaml  requirements.txt  shellbox_app/  shellbox_transport/  shellbox_registry/
#
# `python -m shellbox_app` puts that root on `sys.path`, so both siblings resolve as ordinary
# imports -- no wheel, no editable install, no path manipulation in the app.
#
# The second thing this script owns is the App's ENVIRONMENT. It writes the staged `app.yaml`
# rather than shipping the repo's copy unchanged, because the endpoint differs per bundle
# target and a literal endpoint path in the repo fails `make lint`. See the stamping step
# below, and the header of `packages/shellbox-app/src/app.yaml`.
#
# Usage:  scripts/deploy-app.sh [--profile fevm-west] [--app shellbox] [--verify]
#                               [--source-code-path /Workspace/...]
#                               (--pg-resource <path> --pg-database <name> | --no-database)
#
#   --verify  after deploying, dial the live edge and assert a real frame relays through it.
#             Needs a workspace OAuth token; the Apps edge 302s a PAT (Phase 1, `probe/`).
#
#   --source-code-path
#             the workspace directory to sync the staged root into, and to deploy from.
#             `make deploy` passes the value declared in `resources/app.yml` and resolved by
#             `scripts/bundle-vars.sh`, so the bundle and this script cannot disagree about
#             where the code lives. Omitted, the path is derived exactly as it was for the
#             verified 2026-08-02 run, which keeps this script usable on its own.
#
#   --pg-resource, --pg-database
#             the Lakebase endpoint's resource path and the Postgres database name.
#             `scripts/bundle-vars.sh` prints both, read back out of the bundle, and
#             `scripts/deploy.sh` passes them. NEITHER HAS A DEFAULT HERE, deliberately: a
#             default in this script would be a second declaration of a value the bundle
#             already owns, and the two could then disagree while both reported success.
#
#             The HOST is not passed. This script resolves it from the endpoint, so the caller
#             carries one endpoint value rather than two that can drift apart.
#
#   --no-database
#             deploy an App with NO database configured. It serves every terminal and reports
#             an empty inventory. This must be ASKED FOR, and it has a flag rather than being
#             what happens when nothing is passed, because an App with no inventory answers
#             `GET /` exactly like a healthy one.

set -euo pipefail

PROFILE="${DATABRICKS_PROFILE:-fevm-west}"
APP_NAME="shellbox"
SOURCE_CODE_PATH=""
PG_RESOURCE=""
PG_DATABASE=""
NO_DATABASE=0
VERIFY=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --profile)          PROFILE="$2"; shift 2 ;;
    --app)              APP_NAME="$2"; shift 2 ;;
    --source-code-path) SOURCE_CODE_PATH="$2"; shift 2 ;;
    --pg-resource)      PG_RESOURCE="$2"; shift 2 ;;
    --pg-database)      PG_DATABASE="$2"; shift 2 ;;
    --no-database)      NO_DATABASE=1; shift ;;
    --verify)           VERIFY=1; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STAGE="$(mktemp -d)"
EXPORTED="$(mktemp -d)"
trap 'rm -rf "$STAGE" "$EXPORTED"' EXIT

die() {
  echo "deploy-app: $*" >&2
  exit 1
}

echo "==> staging the deploy root"
cp -R "$REPO/packages/shellbox-app/src/shellbox_app" "$STAGE/"
cp -R "$REPO/packages/shellbox-transport/src/shellbox_transport" "$STAGE/"
cp -R "$REPO/packages/shellbox-registry/src/shellbox_registry" "$STAGE/"
cp "$REPO/packages/shellbox-app/src/app.yaml" "$REPO/packages/shellbox-app/src/requirements.txt" "$STAGE/"

# ---------------------------------------------------------------- the App's environment
#
# The staged `app.yaml` is the repo's file with a generated `env:` block APPENDED. Appending
# rather than rewriting is deliberate: the `command` that starts the App is then the repo's
# own bytes, and nothing here can reword it by re-serializing YAML.
#
# FAIL LOUDLY on a missing value. A stamped-but-empty SHELLBOX_PG_RESOURCE reproduces exactly
# the failure this step exists to remove: `open_registry` in
# `packages/shellbox-app/src/shellbox_app/database.py` treats an unresolvable environment as
# "no inventory" and never as an error, so the App would serve an empty inventory behind a
# green `GET /`. That is pre-mortem scenario 1 in the Phase 4 plan.

# yaml_value <string>: a scalar safe to write into the generated file, or a fatal error.
#
# The generated file is machine-written and hand-read, so a value that needs escaping is a
# value that is wrong. None of the three can legitimately contain a quote or a newline.
yaml_value() {
  case "$1" in
    *'"'*|*'\'*|*$'\n'*) die "refusing to stamp a value containing a quote, a backslash or a newline: $1" ;;
  esac
  printf '%s' "$1"
}

# The host is RESOLVED here rather than passed in, so the caller carries one endpoint value and
# not two that can disagree. `resolve_lakebase_endpoint` in
# `packages/shellbox-registry/src/shellbox_registry/lakebase.py` reads the same field through
# the SDK, and `docs/deploy.md` section 3 names it as the authority.
#
# CRITICAL: the host is a DIFFERENT string from the resource path. The resource path mints
# tokens; the host is what psycopg dials. Conflating them is the first mistake to make here.
pg_host() {
  databricks postgres get-endpoint "$1" --profile "$PROFILE" --output json 2>/dev/null \
    | python3 -c '
import json, sys
endpoint = json.load(sys.stdin) or {}
status = endpoint.get("status") or {}
hosts = status.get("hosts") or {}
print(hosts.get("host") or "")
' 2>/dev/null || true
}

if [[ "$NO_DATABASE" -eq 1 ]]; then
  if [[ -n "$PG_RESOURCE" ]]; then
    die "--no-database and --pg-resource contradict each other; pass one"
  fi
  echo "==> stamping app.yaml with NO database (--no-database)"
  echo "    WARNING: this App will serve terminals and report an EMPTY inventory."
  {
    printf '\n'
    printf '# --- GENERATED by scripts/deploy-app.sh. Everything above is the repo template. ---\n'
    printf '#\n'
    printf '# No env block: this deploy passed --no-database. The App reports an empty inventory.\n'
  } >> "$STAGE/app.yaml"
else
  [[ -n "$PG_RESOURCE" ]] || die "no Lakebase endpoint was given.
  Pass --pg-resource, which scripts/bundle-vars.sh prints as SHELLBOX_PG_RESOURCE, or pass
  --no-database to deploy an App with no inventory on purpose. There is no default, because
  an App with no database answers GET / exactly like a healthy one."
  [[ -n "$PG_DATABASE" ]] || die "no Postgres database name was given.
  Pass --pg-database. scripts/bundle-vars.sh prints it as SHELLBOX_PG_DB, read back out of the
  bundle's pg_database variable, which is the one declaration of it. This script defaults it
  nowhere on purpose: a default here could disagree with the database the migration reached,
  and both halves would report success."

  echo "==> resolving the endpoint host"
  PG_HOST="$(pg_host "$PG_RESOURCE")"
  [[ -n "$PG_HOST" ]] || die "Lakebase reported no host for the endpoint '$PG_RESOURCE'.
  The endpoint may not be provisioned yet: run \`databricks bundle deploy\` for this target
  first, then \`databricks postgres get-endpoint '$PG_RESOURCE' --profile $PROFILE\` to see
  what it reports. Deploying now would stamp an empty host, and the App would serve an empty
  inventory while GET / stayed green."

  echo "==> stamping app.yaml with the App's environment"
  {
    printf '\n'
    printf '# --- GENERATED by scripts/deploy-app.sh. Everything above is the repo template. ---\n'
    printf '#\n'
    printf '# Do NOT edit this copy. It lives in a staging directory that is deleted on exit.\n'
    printf '# The template is packages/shellbox-app/src/app.yaml, whose header says why these\n'
    printf '# three values are stamped at deploy time rather than committed.\n'
    printf 'env:\n'
    printf '  - name: SHELLBOX_PG_RESOURCE\n    value: "%s"\n' "$(yaml_value "$PG_RESOURCE")"
    printf '  - name: SHELLBOX_PG_HOST\n    value: "%s"\n' "$(yaml_value "$PG_HOST")"
    printf '  - name: SHELLBOX_PG_DB\n    value: "%s"\n' "$(yaml_value "$PG_DATABASE")"
  } >> "$STAGE/app.yaml"
  echo "    SHELLBOX_PG_RESOURCE=$PG_RESOURCE"
  echo "    SHELLBOX_PG_HOST=$PG_HOST"
  echo "    SHELLBOX_PG_DB=$PG_DATABASE"
fi

# ------------------------------------------------------------------ the self-containment check
#
# Run BEFORE upload because it is the failure this script exists to prevent and it costs a few
# seconds. `--no-project` keeps uv from resolving the workspace -- without it the check would
# import the local editable install and pass no matter what got staged, which is precisely the
# false green that makes this worth asserting. It is also why this check CANNOT pass on a
# local editable install, and that property is what caught `shellbox_registry` missing from
# the staged root.
#
# THE DEPENDENCY LIST COMES FROM THE STAGED `requirements.txt` AND FROM NOTHING ELSE. That is
# the whole of `R36`'s mitigation. The Apps runtime installs `requirements.txt` PLUS whatever
# it preinstalls, so this file is a strict SUBSET of what the runtime has -- and a check that
# passes under a strict subset cannot fail at runtime for a missing dependency. A hand-kept
# second list is how the two diverge, which they already had before this line existed.
echo "==> reading the staged requirements as the check's dependency list"
WITH_ARGS=()
while IFS= read -r requirement; do
  requirement="${requirement%%#*}"
  requirement="$(printf '%s' "$requirement" | tr -d '[:space:]')"
  [[ -n "$requirement" ]] || continue
  WITH_ARGS+=(--with "$requirement")
  echo "    $requirement"
done < "$STAGE/requirements.txt"
# A parse that silently produced nothing would make the check below assert almost nothing, and
# it would still print "ok".
[[ "${#WITH_ARGS[@]}" -gt 0 ]] || die "no requirements were parsed out of the staged requirements.txt"

# `PYTHONDONTWRITEBYTECODE` is load-bearing, not hygiene: this check IMPORTS from the staged
# root, so without it the interpreter writes `__pycache__` into the very directory about to be
# uploaded. That is how the first run of this script shipped `cpython-311` bytecode -- the
# cleanup below used to run before the check, so it tidied the root and the check then dirtied
# it again. Bytecode from an interpreter that is not the runtime's is dead weight at best, and
# a stale `.pyc` beside an edited source is a confusing way to deploy yesterday's code.
#
# WARNING: `databricks-sdk` can NEVER be verified by this check, and no import-time check can
# verify it. `shellbox_registry.lakebase` imports the SDK LAZILY by design -- section 3 of
# `docs/lakebase-handoff.md` states it: "importing lakebase does not need the SDK; minting a
# token does". So `import shellbox_registry.lakebase` below succeeds on a runtime with no SDK
# installed at all, and this check would print "ok". The SDK is verified by a LIVE call at
# deploy time -- the first token mint against the endpoint -- and by nothing here. This is
# written down because a reader will otherwise believe the check covers the riskiest new
# dependency in `requirements.txt`.
echo "==> checking the root imports with no workspace packages installed"
if ! (cd "$STAGE" && PYTHONDONTWRITEBYTECODE=1 uv run --no-project --quiet \
        "${WITH_ARGS[@]}" \
        python -c "import shellbox_app.server, shellbox_transport.codec, shellbox_registry.postgres, shellbox_registry.lakebase" 2>/dev/null); then
  echo "ERROR: the staged root does not import on its own." >&2
  echo "  A package is missing from the flattening step above, or one of them grew a new" >&2
  echo "  dependency that requirements.txt does not declare. Adding it to this check is NOT" >&2
  echo "  the fix -- the check has no list of its own. Declare it in" >&2
  echo "  packages/shellbox-app/src/requirements.txt, pinned, and the check picks it up." >&2
  exit 1
fi
echo "    ok"

# Belt and braces after the check, so that anything which did manage to write bytecode -- a
# future check, an editor, a stray import -- still cannot reach the upload.
find "$STAGE" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
find "$STAGE" -name '*.pyc' -delete 2>/dev/null || true

# CAPTURED HERE, BEFORE THE SYNC, and reused by the destination assertion below. It must not be
# recomputed after the sync, and this is not a style preference:
#
# `databricks sync` writes `.databricks/sync-snapshots/<hash>.json` INTO ITS SOURCE ROOT while
# it runs, and then correctly declines to upload it. So a staged list computed afterwards holds
# one file the destination will never have, and the assertion reports a stale-file failure on a
# perfectly clean deploy -- with a remedy that says to delete the deployed root.
#
# MEASURED on the first real run, 2026-08-03: the deploy reached the assertion having uploaded
# all 22 files correctly, and failed on `./.databricks/sync-snapshots/c2c6837e3653b878.json`.
# The hazard was already recorded for the build stamp; it reaches anything that reads `$STAGE`
# after the sync.
STAGED_FILES="$(cd "$STAGE" && find . -type f | sort)"

echo "==> the root that will be uploaded"
printf '%s\n' "$STAGED_FILES" | sed 's/^/    /'
if (cd "$STAGE" && find . -name '*.pyc' | grep -q .); then
  echo "ERROR: bytecode survived into the deploy root." >&2
  exit 1
fi

# The bundle's value wins when it is given. The fallback below is the same expression the
# 2026-08-02 run used, and `resources/app.yml` declares the same shape, so a bundle-driven
# deploy and a bare one land in the same place for the same app name. That is deliberate: it
# means adding the bundle did not move the live code root.
if [[ -n "$SOURCE_CODE_PATH" ]]; then
  WS_PATH="$SOURCE_CODE_PATH"
else
  WS_PATH="/Workspace/Users/$(databricks current-user me --profile "$PROFILE" --output json | python3 -c 'import sys,json; print(json.load(sys.stdin)["userName"])')/${APP_NAME}-app"
fi

if ! databricks apps get "$APP_NAME" --profile "$PROFILE" >/dev/null 2>&1; then
  echo "==> creating app $APP_NAME (provisions compute; takes a few minutes)"
  databricks apps create "$APP_NAME" --profile "$PROFILE" >/dev/null
fi

echo "==> syncing to $WS_PATH"
databricks workspace mkdirs "$WS_PATH" --profile "$PROFILE"
databricks sync "$STAGE" "$WS_PATH" --full --profile "$PROFILE"

# ------------------------------------------------------- the destination assertion, `R45`
#
# THIS SYNC NEVER REMOVES ANYTHING, for two INDEPENDENT reasons, and removing either one alone
# fixes nothing:
#
#   1. `$STAGE` is a fresh `mktemp -d` every run, so the sync snapshot never exists and every
#      sync is therefore a full sync from empty.
#   2. `--full` above starts from an empty snapshot whether or not one exists. This is the
#      stronger of the two, because it holds even if the staging directory became stable.
#
# App deployments are `mode: SNAPSHOT`, so each deploy ships the ACCUMULATED directory. A
# module that was renamed or deleted in the repo therefore stays importable in the deployed
# artifact, indefinitely, and nothing reports it. So the destination is asserted instead.
#
# WHY `export-dir` AND NOT `workspace list`. `databricks workspace list` has NO recursive flag
# on CLI v1.8.0 -- `-r` returns "unknown shorthand flag" -- and its non-recursive form returned
# 4 top-level entries against 8 deployed files when this was measured on 2026-08-02.
# Accumulation happens INSIDE package directories, so a top-level listing is structurally blind
# to the exact failure this assertion is the only guard against.
#
# The comparison is over FULL RELATIVE PATHS, never basenames. Two modules in different
# packages can share a basename, and `shellbox_app/` and `shellbox_registry/` both carry a
# `config`-shaped module today.
#
# PRECONDITION, stated because it is the one way this comparison can report a false failure:
# `export-dir` appends a language extension (`.py`, `.scala`, `.sql`, `.r`) to anything the
# workspace classifies as a NOTEBOOK. `databricks sync` does not produce notebooks for these
# `.py` files -- the 2026-08-02 export round-tripped all 8 deployed files with exact relative
# paths. If that ever changes, the failure below is the extension and not a stray file.
echo "==> asserting the deployed root is exactly the staged root"
databricks workspace export-dir "$WS_PATH" "$EXPORTED" --overwrite --profile "$PROFILE" >/dev/null
# `|| true` because diff exits 1 on a difference, which `set -e` would otherwise turn into a
# bare exit with none of the explanation below.
ROOT_DIFF="$(diff -u \
  <(printf '%s\n' "$STAGED_FILES") \
  <(cd "$EXPORTED" && find . -type f | sort) || true)"
if [[ -n "$ROOT_DIFF" ]]; then
  echo "ERROR: the deployed root does not match the staged root." >&2
  echo "  A '-' line is staged and NOT deployed. A '+' line is deployed and NOT staged, which" >&2
  echo "  is a stale file from an earlier deploy: this sync removes nothing, and app" >&2
  echo "  deployments are mode: SNAPSHOT, so it would ship in the artifact and stay" >&2
  echo "  importable." >&2
  printf '%s\n' "$ROOT_DIFF" | sed 's/^/    /' >&2
  echo "  Remedy: databricks workspace rm -r '$WS_PATH' --profile $PROFILE, then re-run." >&2
  exit 1
fi
echo "    ok"

# THE COMPUTE MUST BE RUNNING BEFORE THE CODE CAN DEPLOY, and this is measured rather than
# assumed. `databricks apps deploy` against a stopped app fails:
#
#     Error: Cannot deploy app shellbox-dev as it is not in RUNNING state.
#            Please start the app first.
#
# and `apps get` says the same thing from the other side: "Start the app compute to deploy the
# app." Measured 2026-08-03, CLI v1.8.0, on the first real `make deploy`.
#
# WHY THE APP IS STOPPED AT ALL, and why that is correct. `resources/app.yml` declares no
# `lifecycle.started`, so `bundle deploy` creates the App RESOURCE and no deployment -- which is
# the single line that stops the bundle from clobbering what this script uploads. The cost of
# that choice is exactly this: nobody starts the compute, so this script must.
#
# CORRECTION: `resources/app.yml` claimed "an app deployment starts the compute". It does not.
# The dependency runs the other way, and the comment there now says so.
#
# `apps start` BLOCKS until ACTIVE by default (20m timeout), so no bespoke wait loop is needed
# here. It is a no-op on an already-running app, which keeps this script idempotent.
APP_COMPUTE="$(databricks apps get "$APP_NAME" --profile "$PROFILE" --output json \
  | python3 -c 'import sys,json; print(json.load(sys.stdin).get("compute_status",{}).get("state") or "")')"
if [[ "$APP_COMPUTE" != "ACTIVE" ]]; then
  echo "==> starting the app compute (state: ${APP_COMPUTE:-unknown}; provisioning takes a few minutes)"
  databricks apps start "$APP_NAME" --profile "$PROFILE" >/dev/null
fi

echo "==> deploying"
databricks apps deploy "$APP_NAME" --source-code-path "$WS_PATH" --profile "$PROFILE"

APP_URL="$(databricks apps get "$APP_NAME" --profile "$PROFILE" --output json \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["url"])')"
echo "==> deployed: $APP_URL"

if [[ "$VERIFY" -eq 1 ]]; then
  echo "==> verifying against the live edge"
  SHELLBOX_APP_URL="$APP_URL" \
  SHELLBOX_EDGE_TOKEN="$(databricks auth token --profile "$PROFILE" \
    | python3 -c 'import sys,json; print(json.load(sys.stdin)["access_token"])')" \
    uv run --project "$REPO" python "$REPO/scripts/verify_app.py"
fi
