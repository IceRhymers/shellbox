.PHONY: install sync fmt lint test test-tmux migrate migration

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

# PLACEHOLDER (W2): the real tmux-backed regression suite (spike/tmux_spike.py adopted
# into tests/tmux, gated on tmux 3.4 in CI) lands in W2. Kept as a separate target because
# it needs a real tmux binary and must not silently no-op inside `make test`.
test-tmux:
	@echo "test-tmux: no tmux-backed tests yet (see W2, .omc/plans/phase-2-session-plane.md §4)"

# PLACEHOLDER (W6): alembic env.py + versions/0001_hosts_sessions.py land with
# shellbox-registry's models. alembic.ini is added at the repo root at that point too.
migrate:
	uv run alembic -c alembic.ini upgrade head

migration:
	uv run alembic -c alembic.ini revision --autogenerate -m "$(name)"
