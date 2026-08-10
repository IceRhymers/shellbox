#!/usr/bin/env bash
#
# Print the values a deploy needs, as shell assignments, from ONE `bundle validate` call.
#
# Usage:  eval "$(scripts/bundle-vars.sh --target dev --profile fevm-west)"
#
# Emits four variables:
#
#   SHELLBOX_APP_NAME         the App name resolved for that target
#   SHELLBOX_APP_SOURCE_PATH  the absolute /Workspace code root resolved for that target
#   SHELLBOX_PG_DB            the Postgres database name the target declares
#   SHELLBOX_PG_RESOURCE      the endpoint path, built from the three ids below
#
# Three of those are READ from the bundle and one is CONSTRUCTED. The difference is not a style
# choice, and it is the reason this script exists rather than four inline pipelines:
#
# - `resources.apps.<key>.name` and `.source_code_path` DO resolve in
#   `bundle validate -o json` (verified against Databricks CLI v1.8.0). Reading them keeps the
#   code root declared once, in `resources/app.yml`, instead of computed identically in two
#   places that can drift apart.
#
# - `variables.pg_database.value` resolves the same way, and it is read for the same reason.
#   `scripts/deploy-app.sh` stamps it into the App's environment and has NO default of its own,
#   so the database the App reads is the database `databricks.yml` declares. A default in the
#   script would be a second declaration, and a migration and an App reaching different
#   databases both report success.
#
# - The endpoint's resource path does NOT resolve, ever, before a deploy.
#   `${resources.postgres_projects.pg_project.id}` stays LITERAL in `bundle validate -o json`,
#   in `bundle summary -o json` and in `bundle plan -o json` -- those are deploy-time outputs.
#   Even after a successful deploy, the CLI's own acceptance test for postgres resources shows
#   the summary printing `URL: (not deployed)`. So there is nothing to read, and the path is
#   assembled from the three ids the bundle declares. The form is documented rather than
#   guessed: `databricks postgres --help` states that resources are identified by hierarchical
#   names of exactly this shape.
#
# Constructing it is therefore the sanctioned path, not a shortcut. What is forbidden is writing
# a project name into that string by hand. `make lint` asserts both halves: that every
# `projects/` here is followed by an expansion, and that the full constructed form exists at all
# -- because a rule about how a string is built proves nothing if nothing builds it.
#
# WARNING: this authenticates, because `bundle validate` does. It is on the deploy path only.
# `make lint` must stay runnable on a checkout with no Databricks credential, so nothing in the
# lint lane calls this script.

set -euo pipefail

TARGET="dev"
PROFILE="${DATABRICKS_PROFILE:-fevm-west}"
APP_KEY="shellbox_app"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --target|-t)  TARGET="$2"; shift 2 ;;
    --profile|-p) PROFILE="$2"; shift 2 ;;
    --app-key)    APP_KEY="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

# One `validate` call, read once. Each field is checked here rather than downstream, because a
# value that never resolved still LOOKS like a value: an id carrying a `${` assembles into a
# resource path the API rejects with a message pointing nowhere near the missing variable.
if ! ASSIGNMENTS="$(
  databricks bundle validate --target "$TARGET" --profile "$PROFILE" --output json \
    | APP_KEY="$APP_KEY" TARGET="$TARGET" python3 -c '
# NOTE: this block is a single-quoted shell argument, so it must contain no single quote of
# its own -- not even inside a Python string. One breaks the shell parse rather than the
# Python, which reports a confusing SyntaxError several lines from the real cause.
import json, os, shlex, sys

config = json.load(sys.stdin)
app_key, target = os.environ["APP_KEY"], os.environ["TARGET"]

apps = config.get("resources", {}).get("apps", {})
app = apps.get(app_key)
if app is None:
    sys.exit(f"bundle-vars: no app resource {app_key!r} in target {target!r}; "
             f"the bundle declares these app keys: {sorted(apps)}")

variables = config.get("variables", {})


def resolved(label: str, raw: object) -> str:
    if not raw:
        sys.exit(f"bundle-vars: {label} is empty in target {target!r}")
    if "${" in str(raw):
        sys.exit(f"bundle-vars: {label} did not resolve: {raw!r}")
    return str(raw)


emit = {
    "SHELLBOX_APP_NAME": resolved("the app name", app.get("name")),
    "SHELLBOX_APP_SOURCE_PATH": resolved(
        "the app source_code_path", app.get("source_code_path")
    ),
    "SHELLBOX_PG_DB": resolved(
        "variable pg_database", variables.get("pg_database", {}).get("value")
    ),
}
for key in ("pg_project_id", "pg_branch_id", "pg_endpoint_id"):
    emit[key.upper()] = resolved(f"variable {key!r}", variables.get(key, {}).get("value"))

for key, value in emit.items():
    print(f"{key}={shlex.quote(value)}")
'
)"; then
  exit 1
fi

eval "$ASSIGNMENTS"

# THE construction, and it is deliberately in shell rather than folded into the reader above.
# This one line is the whole of the endpoint-path derivation: no project name is written down,
# and every segment comes from a bundle variable. `databricks postgres get-endpoint` takes this
# string, and so does `generate-database-credential`.
SHELLBOX_PG_RESOURCE="projects/${PG_PROJECT_ID}/branches/${PG_BRANCH_ID}/endpoints/${PG_ENDPOINT_ID}"

printf 'SHELLBOX_APP_NAME=%q\n' "$SHELLBOX_APP_NAME"
printf 'SHELLBOX_APP_SOURCE_PATH=%q\n' "$SHELLBOX_APP_SOURCE_PATH"
printf 'SHELLBOX_PG_DB=%q\n' "$SHELLBOX_PG_DB"
printf 'SHELLBOX_PG_RESOURCE=%q\n' "$SHELLBOX_PG_RESOURCE"
