.PHONY: install sync fmt lint test test-tmux test-registry test-integration migrate migration \
	migrate-roundtrip deploy grant require-pg-host require-deploy-principal

# The workspace, and the target inside the bundle. Both overridable on the command line:
#
#     make deploy TARGET=prod
#
# CRITICAL: the profile name is `fevm-west`. There is a second, similarly named workspace in
# this account, and it is a DIFFERENT workspace. `databricks.yml` pins the host for both targets
# so a wrong profile errors instead of deploying somewhere unexpected.
PROFILE ?= fevm-west
TARGET  ?= dev

# No `make` target may silently re-lock. UV_LOCKED is the environment form of `--locked`: uv
# checks `uv.lock` against the pyproject files and FAILS if it is stale, instead of quietly
# resolving a different dependency set in the middle of `make test`.
#
# UV_LOCKED, and NOT UV_FROZEN. `--frozen` skips the staleness check altogether, so a dependency
# that was added to a pyproject.toml and not re-locked installs the OLD set. That failure surfaces
# much later as an ImportError for a package the project declares, which is a confusing way to
# learn that a lockfile is stale.
#
# This is orthogonal to whether `uv.lock` is committed. It is not committed -- see the comment
# above `uv.lock` in `.gitignore` -- because this governs whether a target may REWRITE the
# lockfile, not where the lockfile comes from.
#
# Measured 2026-08-03 on uv 0.10.9, because two of the three behaviours are surprising:
#
#   UV_LOCKED=1, stale lockfile   -> "The lockfile at `uv.lock` needs to be updated, but
#                                    `UV_LOCKED=1` was provided." The guard this exists for.
#   UV_LOCKED=1, NO lockfile      -> "Unable to find lockfile at `uv.lock`". `uv.lock` is ignored,
#                                    so EVERY fresh clone starts in this state. That is why `sync`
#                                    is exempted below, and why it has to be.
#   UV_LOCKED= (empty)            -> hard error: "expected a boolish value". An exemption must
#                                    therefore set 0, never blank.
export UV_LOCKED := 1


install: sync

# The one target allowed to write the lockfile, and it must be. `uv.lock` is gitignored, so a
# fresh clone has none, and `uv sync` under UV_LOCKED=1 fails outright with "Unable to find
# lockfile". `make sync` is where a developer expects the lockfile to be created, and where
# adding a dependency is expected to update it.
#
# 0 rather than blank, because uv rejects an empty UV_LOCKED. See the measurement above.
sync: UV_LOCKED := 0
sync:
	uv sync

# Scoped to packages/ and tests/ deliberately: spike/ and probe/ are diagnostic scripts
# owned by other work items (they predate this lint config and legitimately print), not
# part of the shellbox-mcp / shellbox-registry packages this Makefile lints.
fmt:
	uv run ruff format packages tests
	uv run ruff check --fix packages tests

# `shellbox-app` is on this list because it is a DELIVERABLE, not a diagnostic script. The
# exclusion above is written for spike/ and probe/, and it does not extend to a package the
# integration lane exercises and a live run deploys.
lint:
	uv run ruff check packages tests
	uv run mypy packages/shellbox-mcp/src packages/shellbox-registry/src \
		packages/shellbox-transport/src packages/shellbox-app/src
	@# The bundle statics, and they are steps HERE rather than a target of their own or a
	@# pytest module. Three reasons, in order of how much they constrain the choice:
	@#
	@# 1. CI invokes a fixed list of make targets. A new target would be in none of them, and
	@#    a mitigation lane nothing invokes is a comment with a Makefile around it.
	@# 2. They assert on bundle files the Python test suite has no other reason to know about,
	@#    and a failure belongs in the lane a reader already reads as "static checks".
	@# 3. They must run on a checkout with NO Databricks credential, which they do -- they
	@#    parse files and never authenticate. Nothing here calls `bundle validate`.
	@#
	@# `--with pyyaml --no-project` rather than a dev dependency: this needs a YAML parser and
	@# nothing else in the repo does. The same idiom is in scripts/deploy-app.sh, and keeping
	@# it out of pyproject.toml keeps it out of the deployed requirement set.
	uv run --no-project --quiet --with pyyaml python scripts/check_bundle_statics.py

test:
	uv run pytest

# The tmux-backed lane, in the order that fails fastest and most informatively.
#
# The spike runs FIRST and is the oracle: §7 is transcribed from it, and if a tmux version
# behaves differently the spike says so in one line while tests/tmux would fail in a dozen
# confusing places. It also gates on its own exit code (it asserts; it does not merely emit
# JSONL), so it belongs in a target and not in a comment.
#
# tests/tmux also runs inside `make test` -- it skips when tmux is absent -- so this target
# is about the GATE: CI runs it in the tmux-3.4 container, and `-p no:randomly`-style
# surprises aside, a failure here invalidates §7 rather than one test.
test-tmux:
	python3 spike/tmux_spike.py
	uv run pytest tests/tmux -v
	@# A skip for a MISSING TMUX is a failure in this target, not a pass: a silently skipped
	@# gate is indistinguishable from a green one.
	@#
	@# It greps the skip REASON rather than counting skips. The count was a proxy for "no
	@# binary", and it stopped being one the first time a test skipped for a different
	@# reason -- W19b's claim cases, which need `/proc` and so cannot run on the macOS
	@# developer lane. Counting would have failed this target on a machine where the gate
	@# had in fact run, which trains people to ignore it. `-rs` prints the reasons.
	@uv run pytest tests/tmux -q -rs 2>&1 | tee /tmp/shellbox-tmux.txt | tail -1
	@if grep -q 'tmux binary not available' /tmp/shellbox-tmux.txt; then \
		echo "ERROR: tests/tmux SKIPPED -- no tmux binary on PATH (or SHELLBOX_TMUX_BIN unset)."; \
		exit 1; \
	fi
	@# And the anti-vacuous half, which the count did give for free: a lane that collected
	@# nothing must not report success.
	@if ! grep -qE '[0-9]+ passed' /tmp/shellbox-tmux.txt; then \
		echo "ERROR: tests/tmux ran no tests at all."; \
		exit 1; \
	fi

# The integration suite drives the server over real stdio against a real tmux server, so it
# is tmux-version-sensitive in the same way tests/tmux is and belongs in the 3.4 gate lane
# too. Separate target because it needs the mcp SDK, which `make test-tmux` deliberately
# does not (that target must be runnable with nothing but tmux and pytest).
test-integration:
	uv run pytest tests/integration -v

# The guard the donor project called its PGHOST guard. The variable names differ here on
# purpose: `dsn_from_env` in packages/shellbox-registry/src/shellbox_registry/dsn.py reads
# SHELLBOX_DATABASE_URL, then SHELLBOX_PG_USER / _PASSWORD / _HOST / _PORT / _DB. Guarding
# PGHOST would guard a name nothing in this repo consumes, which is a check that cannot fail.
#
# What it stops: `dsn_from_env` DEFAULTS the host to localhost:55432 as soon as ANY
# SHELLBOX_PG_* variable is set. So a developer with a half-configured local environment runs
# `make migrate`, migrates their laptop, and reads the green result as a production migration.
#
# It accepts EITHER a Lakebase endpoint (SHELLBOX_PG_RESOURCE) or a DSN, because those are the
# two things the migration and the grant can now connect from. The endpoint is the easy path
# and the message names it first: one variable, and the host, the Postgres role and the OAuth
# token are all derived from it in code. A DSN stays supported -- it is how a deliberate
# migration against a local Postgres works, and that is a thing developers legitimately do.
#
# NOTE: the name predates the resource path. It asserts "a database is configured", not "a host
# variable is set". Renaming it would mean editing the three files that cite it by name --
# scripts/check_deploy_principal.py, tests/unit/test_deploy_principal.py, and
# packages/shellbox-app/src/shellbox_app/config.py -- so the name stays and this note is here
# instead.
#
# NOTE on the FIRST deploy of a target: the endpoint does not exist yet, but its RESOURCE PATH
# is constructed from ids the bundle already declares, so `scripts/bundle-vars.sh` prints it
# before anything is provisioned and this guard has something to accept. `make deploy` runs
# `bundle deploy` first for that reason. Do NOT set a placeholder host -- that is how a guard
# becomes something people route around by reflex.
require-pg-host:
	@if [ -z "$${SHELLBOX_PG_RESOURCE:-}" ] && [ -z "$${SHELLBOX_DATABASE_URL:-}" ] \
			&& [ -z "$${SHELLBOX_PG_HOST:-}" ]; then \
		echo "ERROR: no database is configured for this command." >&2; \
		echo "  Without one the registry DSN silently defaults to localhost:55432, so this" >&2; \
		echo "  command would target a local Postgres and report success." >&2; \
		echo "  The easy path is the endpoint this bundle declares. It needs no credential of" >&2; \
		echo "  its own -- the host, the role and the token are derived from it:" >&2; \
		echo "    eval \"\$$(scripts/bundle-vars.sh -t $(TARGET) -p $(PROFILE))\"" >&2; \
		echo "    export SHELLBOX_PG_RESOURCE" >&2; \
		echo "  Or set SHELLBOX_DATABASE_URL, or SHELLBOX_PG_HOST and the credential parts." >&2; \
		echo "  See docs/deploy.md section 3." >&2; \
		exit 1; \
	fi

# `make deploy` is SIX ordered steps, and the order is the load-bearing part. They live in
# `scripts/deploy.sh` rather than here: a recipe is a list of commands with nowhere to record
# why a step is where it is, and no way to wait for the App to reach ACTIVE. Read that script's
# header for the order and the reason behind each position.
#
# No `require-pg-host` on this target, deliberately. Step 1 is `bundle deploy`, which reads no
# DSN at all, and step 2 derives SHELLBOX_PG_RESOURCE from the bundle and exports it for the
# steps that do. A guard here would demand a variable the pipeline is about to produce.
#
# The two deploy mechanisms coexist only because `resources/app.yml` declares no
# `lifecycle.started`. Read the CRITICAL comment there before changing anything on this path.
#
# BEFORE THE FIRST DEPLOY of a target whose App already exists, a human runs the one-time
# adoption step in docs/deploy.md section 2. It is not a step in the script: `bundle deployment
# bind` prompts unless it is passed --auto-approve, so inside the pipeline it would either block
# or silently re-adopt whatever the name points at on every run.
deploy:
	scripts/deploy.sh --target $(TARGET) --profile $(PROFILE)

# The second guard on this path, and it guards the IDENTITY rather than the destination.
#
# `alembic upgrade head` and the App SP's grant are both deploy-time actions, and both must run as
# the DEPLOYING PRINCIPAL. The App's service principal holds SELECT on hosts and sessions and
# nothing else, so a migration with its credential fails on a permission error -- and the tempting
# fix for that error is a wider grant, after which the serving principal holds DDL on a registry it
# is only supposed to read. That is a real path to a real outcome, and it arrives disguised as a fix
# for something else.
#
# `python3` and not `uv run`: the check imports nothing outside the standard library, so it works on
# a checkout with nothing synced, and it authenticates nowhere.
require-deploy-principal:
	@python3 scripts/check_deploy_principal.py "this command"

# DATABRICKS_CONFIG_PROFILE, and it is not decoration. With SHELLBOX_PG_RESOURCE set, alembic's
# env.py resolves the host and the Postgres role through the Databricks SDK -- and the SDK reads
# its profile from the environment, where an unset value resolves to DEFAULT. In this account
# DEFAULT is a DIFFERENT workspace with a confusingly similar name, so the migration would
# authenticate somewhere nobody chose. The DSN path ignores this variable entirely.
migrate: require-pg-host require-deploy-principal
	DATABRICKS_CONFIG_PROFILE=$(PROFILE) uv run alembic -c alembic.ini upgrade head

# The App's service principal gets SELECT on hosts and sessions, and nothing else.
#
# This is step 6 of `scripts/deploy.sh`, and it stays a target of its own because it is also the
# thing to re-run on its own after a migration adds a table the App reads. Its position in the
# pipeline is forced twice over: a grant needs its table, so the migration must have run, and the
# SP's Postgres role appears in pg_roles only after the App reaches ACTIVE.
#
# See scripts/grant-app-sp.sh for why this exists at all: everything verified before it
# authenticated as a workspace user, and the App authenticates as its service principal.
grant: require-pg-host require-deploy-principal
	scripts/grant-app-sp.sh --target $(TARGET) --profile $(PROFILE)

migration:
	uv run alembic -c alembic.ini revision --autogenerate -m "$(name)"

# A migration that cannot be reversed is not a migration you can deploy twice. CI runs
# this against a real Postgres rather than asserting the DDL by eye.
migrate-roundtrip:
	uv run alembic -c alembic.ini upgrade head
	uv run alembic -c alembic.ini downgrade base
	uv run alembic -c alembic.ini upgrade head

# Separate from `make test` because it needs a live Postgres. It SKIPS rather than fails
# when the DSN is unreachable, so a developer without a database still gets a green
# `make test` -- but CI provides one, so here the skips should not happen.
test-registry:
	uv run pytest tests/registry -v
