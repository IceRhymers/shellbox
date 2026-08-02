#!/usr/bin/env bash
#
# Deploy `shellbox-app` to Databricks Apps.
#
# The whole reason this is a script and not three CLI calls in a runbook is the flattening
# step. `shellbox_transport` is a uv WORKSPACE package: `packages/shellbox-app/pyproject.toml`
# resolves it through `[tool.uv.sources] shellbox-transport = { workspace = true }`, and pip on
# the Apps runtime has no such notion -- it reads `requirements.txt`, sees no `shellbox
# transport` on any index, and the App boots into `ModuleNotFoundError`. Nothing in the repo's
# layout hints at this, so a hand-run deploy gets it wrong once per person.
#
# The fix is to copy both packages side by side into one root:
#
#     <root>/app.yaml  requirements.txt  shellbox_app/  shellbox_transport/
#
# `python -m shellbox_app` puts that root on `sys.path`, so `shellbox_transport` resolves as an
# ordinary sibling import -- no wheel, no editable install, no path manipulation in the app.
#
# Usage:  scripts/deploy-app.sh [--profile fevm-west] [--app shellbox] [--verify]
#
#   --verify  after deploying, dial the live edge and assert a real frame relays through it.
#             Needs a workspace OAuth token; the Apps edge 302s a PAT (Phase 1, `probe/`).

set -euo pipefail

PROFILE="${DATABRICKS_PROFILE:-fevm-west}"
APP_NAME="shellbox"
VERIFY=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --profile) PROFILE="$2"; shift 2 ;;
    --app)     APP_NAME="$2"; shift 2 ;;
    --verify)  VERIFY=1; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

echo "==> staging the deploy root"
cp -R "$REPO/packages/shellbox-app/src/shellbox_app" "$STAGE/"
cp -R "$REPO/packages/shellbox-transport/src/shellbox_transport" "$STAGE/"
cp "$REPO/packages/shellbox-app/src/app.yaml" "$REPO/packages/shellbox-app/src/requirements.txt" "$STAGE/"

# The self-containment check, run BEFORE upload because it is the failure this script exists to
# prevent and it costs a few seconds. `--no-project` keeps uv from resolving the workspace --
# without it the check would import the local editable install and pass no matter what got
# staged, which is precisely the false green that makes this worth asserting.
#
# `PYTHONDONTWRITEBYTECODE` is load-bearing, not hygiene: this check IMPORTS from the staged
# root, so without it the interpreter writes `__pycache__` into the very directory about to be
# uploaded. That is how the first run of this script shipped `cpython-311` bytecode -- the
# cleanup below used to run before the check, so it tidied the root and the check then dirtied
# it again. Bytecode from an interpreter that is not the runtime's is dead weight at best, and
# a stale `.pyc` beside an edited source is a confusing way to deploy yesterday's code.
echo "==> checking the root imports with no workspace packages installed"
if ! (cd "$STAGE" && PYTHONDONTWRITEBYTECODE=1 uv run --no-project --quiet \
        --with fastapi --with uvicorn --with websockets \
        python -c "import shellbox_app.server, shellbox_transport.codec" 2>/dev/null); then
  echo "ERROR: the staged root does not import on its own." >&2
  echo "  A package is missing from the flattening step above, or one of them grew a new" >&2
  echo "  dependency that neither the Apps runtime preinstalls nor requirements.txt declares." >&2
  exit 1
fi
echo "    ok"

# Belt and braces after the check, so that anything which did manage to write bytecode -- a
# future check, an editor, a stray import -- still cannot reach the upload.
find "$STAGE" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
find "$STAGE" -name '*.pyc' -delete 2>/dev/null || true

echo "==> the root that will be uploaded"
(cd "$STAGE" && find . -type f | sort | sed 's/^/    /')
if (cd "$STAGE" && find . -name '*.pyc' | grep -q .); then
  echo "ERROR: bytecode survived into the deploy root." >&2
  exit 1
fi

WS_PATH="/Workspace/Users/$(databricks current-user me --profile "$PROFILE" --output json | python3 -c 'import sys,json; print(json.load(sys.stdin)["userName"])')/${APP_NAME}-app"

if ! databricks apps get "$APP_NAME" --profile "$PROFILE" >/dev/null 2>&1; then
  echo "==> creating app $APP_NAME (provisions compute; takes a few minutes)"
  databricks apps create "$APP_NAME" --profile "$PROFILE" >/dev/null
fi

echo "==> syncing to $WS_PATH"
databricks workspace mkdirs "$WS_PATH" --profile "$PROFILE"
databricks sync "$STAGE" "$WS_PATH" --full --profile "$PROFILE"

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
