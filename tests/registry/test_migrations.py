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
from shellbox_registry.models import Host, Session
from sqlalchemy import create_engine, inspect, text

from .conftest import static_dsn_or_skip

pytestmark = pytest.mark.registry

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def alembic_config(_pg_engine_or_skip) -> Config:
    # SHELLBOX_DATABASE_URL must be set for env.py to resolve a DSN; conftest's
    # _pg_engine_or_skip fixture already proved the target is reachable. Reuse
    # conftest's _test_dsn rather than repeating a default here -- two copies of a
    # connection string drift, and the literal form trips credential scanners.
    # `static_dsn_or_skip` SKIPS on the Lakebase path. alembic's own env.py does support
    # SHELLBOX_PG_RESOURCE (and it wins over a DSN), so migrating a branch works -- but this
    # file also DROPS `alembic_version` and both tables to force a clean slate, and it verifies
    # through an engine of its own. Pointing that at Lakebase needs the same passthrough
    # `test_pool_resilience.py` needs; until then, skipping is honest and a fallback is not.
    os.environ["SHELLBOX_DATABASE_URL"] = static_dsn_or_skip()
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


def test_the_migrated_schema_matches_the_models_column_for_column(
    alembic_config: Config, _pg_engine_or_skip
) -> None:
    """CRITICAL: The migrations were only ever checked by table NAME, so a whole column could go
    missing.

    Deleting `0002_sessions_last_read_at.py` outright passed both `make migrate-roundtrip` and
    `make test-registry`. Two reasons, and they compound: `tests/registry/conftest.py` builds the
    schema with `Base.metadata.create_all`, so every `last_read_at` assertion tests the **model**
    and never the migration; and the round-trip test asserted only that `hosts` and `sessions`
    exist. With 0002 gone, `head` is simply `0001` and up→down→up round-trips cleanly.

    That left the entire rationale in 0002's docstring — that alembic records a revision *id* and
    never fingerprints content, so an amended migration is invisible to a database already at it —
    defended by nothing.

    Asserted as equality against the models rather than as a list of expected names, so this also
    catches the general form: any future model change that ships without a migration, or migration
    without a model change.
    """
    engine = create_engine(normalize_postgres_dsn(os.environ["SHELLBOX_DATABASE_URL"]))
    _reset_to_a_clean_slate(engine)
    try:
        command.upgrade(alembic_config, "head")
        inspector = inspect(engine)
        for model in (Host, Session):
            migrated = {column["name"] for column in inspector.get_columns(model.__tablename__)}
            declared = {column.name for column in model.__table__.columns}
            assert migrated == declared, (
                f"{model.__tablename__}: the migrated schema and the model disagree. "
                f"Only in the migration: {sorted(migrated - declared)}. "
                f"Only in the model: {sorted(declared - migrated)}."
            )
    finally:
        _reset_to_a_clean_slate(engine)
        engine.dispose()


def test_last_read_at_arrives_in_0002_and_not_before(
    alembic_config: Config, _pg_engine_or_skip
) -> None:
    """Pins the 0001/0002 split itself, which is the thing OQ-G decided.

    `last_read_at` was briefly folded into the merged 0001. Stopping at 0001 and finding the
    column **absent** is what proves the split is real: an amended 0001 would show it here, and a
    developer whose database was already stamped 0001 would never receive it while `alembic
    current` reported them up to date.
    """
    engine = create_engine(normalize_postgres_dsn(os.environ["SHELLBOX_DATABASE_URL"]))
    _reset_to_a_clean_slate(engine)
    try:
        command.upgrade(alembic_config, "0001")
        columns = {c["name"] for c in inspect(engine).get_columns("sessions")}
        assert "last_activity_at" in columns, "0001 must still carry the send-activity column"
        assert "last_read_at" not in columns, (
            "last_read_at is present at revision 0001, so it was folded into a migration that is "
            "already applied in developers' databases — the exact failure OQ-G resolved"
        )

        command.upgrade(alembic_config, "head")
        assert "last_read_at" in {c["name"] for c in inspect(engine).get_columns("sessions")}

        # Reversible on its own, not only as part of a full downgrade to base.
        command.downgrade(alembic_config, "0001")
        assert "last_read_at" not in {c["name"] for c in inspect(engine).get_columns("sessions")}
    finally:
        _reset_to_a_clean_slate(engine)
        engine.dispose()
