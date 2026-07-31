"""W6 acceptance criterion 1: `upgrade head -> downgrade base -> upgrade head` round-trips
cleanly against a real Postgres. Driven through the alembic Config API (not a subprocess)
so it participates in `make test` like any other pytest test; the literal CLI invocation
is also run manually and pasted into the W6 report, since that is the acceptance
criterion's exact wording.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from shellbox_registry.dsn import normalize_postgres_dsn
from sqlalchemy import create_engine, inspect, text

from .conftest import _test_dsn

pytestmark = pytest.mark.registry

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def alembic_config(_pg_engine_or_skip) -> Config:
    # SHELLBOX_DATABASE_URL must be set for env.py to resolve a DSN; conftest's
    # _pg_engine_or_skip fixture already proved the target is reachable. Reuse
    # conftest's _test_dsn rather than repeating a default here -- two copies of a
    # connection string drift, and the literal form trips credential scanners.
    os.environ["SHELLBOX_DATABASE_URL"] = _test_dsn()
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / cfg.get_main_option("script_location")))
    return cfg


def _reset_to_a_clean_slate(engine) -> None:
    """Drop everything this migration touches, including `alembic_version` itself.

    Other fixtures in this directory (`pg_engine`) create/drop `hosts`/`sessions` via
    `Base.metadata` directly, without ever touching `alembic_version` -- so alembic can
    be left believing it's "already at head" while the tables it tracks don't exist.
    This test owns alembic's own bookkeeping, so it forces a known-clean starting point
    instead of trusting whatever state other tests left behind in the same database.
    """
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS sessions CASCADE"))
        conn.execute(text("DROP TABLE IF EXISTS hosts CASCADE"))
        conn.execute(text("DROP TABLE IF EXISTS alembic_version CASCADE"))


def test_upgrade_head_downgrade_base_upgrade_head_round_trips(
    alembic_config: Config, _pg_engine_or_skip
) -> None:
    engine = create_engine(normalize_postgres_dsn(os.environ["SHELLBOX_DATABASE_URL"]))
    _reset_to_a_clean_slate(engine)
    try:
        command.upgrade(alembic_config, "head")
        inspector = inspect(engine)
        assert {"hosts", "sessions"} <= set(inspector.get_table_names())

        command.downgrade(alembic_config, "base")
        inspector = inspect(engine)
        assert "hosts" not in inspector.get_table_names()
        assert "sessions" not in inspector.get_table_names()

        command.upgrade(alembic_config, "head")
        inspector = inspect(engine)
        assert {"hosts", "sessions"} <= set(inspector.get_table_names())
    finally:
        # Leave the database clean for the next test module/run, regardless of whether
        # the round trip above succeeded or raised partway through.
        _reset_to_a_clean_slate(engine)
        engine.dispose()
