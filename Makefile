.PHONY: install sync fmt lint test test-tmux test-registry test-integration migrate migration migrate-roundtrip


install: sync

sync:
	uv sync

# Scoped to packages/ and tests/ deliberately: spike/ and probe/ are diagnostic scripts
# owned by other work items (they predate this lint config and legitimately print), not
# part of the shellbox-mcp / shellbox-registry packages this Makefile lints.
fmt:
	uv run ruff format packages tests
	uv run ruff check --fix packages tests

lint:
	uv run ruff check packages tests
	uv run mypy packages/shellbox-mcp/src packages/shellbox-registry/src

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
	@# A skip here means no tmux binary, which in this target is a failure, not a pass:
	@# a silently skipped gate is indistinguishable from a green one.
	@uv run pytest tests/tmux -q 2>&1 | tee /tmp/shellbox-tmux.txt | tail -1
	@if grep -qE '[0-9]+ skipped' /tmp/shellbox-tmux.txt; then \
		echo "ERROR: tests/tmux SKIPPED -- no tmux binary on PATH (or SHELLBOX_TMUX_BIN unset)."; \
		exit 1; \
	fi

# The integration suite drives the server over real stdio against a real tmux server, so it
# is tmux-version-sensitive in the same way tests/tmux is and belongs in the 3.4 gate lane
# too. Separate target because it needs the mcp SDK, which `make test-tmux` deliberately
# does not (that target must be runnable with nothing but tmux and pytest).
test-integration:
	uv run pytest tests/integration -v

migrate:
	uv run alembic -c alembic.ini upgrade head

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
