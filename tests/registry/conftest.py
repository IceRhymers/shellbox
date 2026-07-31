"""Fixtures for tests that need a live Postgres (the ``registry`` marker, W6 acceptance
criterion 8): reachable at ``SHELLBOX_DATABASE_URL`` (default: the local docker instance
used in dev). When it is unreachable, tests using ``_pg_engine_or_skip`` (directly, or
via ``pg_engine``/``registry``) are SKIPPED, not failed, so ``make test`` stays green
with no database at all. Files that exercise these fixtures set
``pytestmark = pytest.mark.registry`` themselves — this module does not auto-mark by
directory, because tests/registry/ also holds DB-free tests (NullRegistry, the
create_registry factory) that must run unconditionally, not be excluded by
``pytest -m "not registry"``.
"""

from __future__ import annotations

import os
from collections.abc import Generator, Iterator

import pytest
import sqlalchemy
from shellbox_registry.dsn import dsn_from_env, normalize_postgres_dsn, redact
from shellbox_registry.models import Base
from shellbox_registry.postgres import PostgresRegistry
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine


def _test_dsn() -> str:
    """The DSN under test.

    Delegates to ``dsn_from_env`` so assembly lives in exactly one place; see its
    docstring for why that matters. Falls back to the package defaults (the local dev
    instance) when the environment configures nothing, which is what makes
    ``make test-registry`` work with no setup on a developer machine.
    """
    return dsn_from_env() or _local_dev_dsn()


def _local_dev_dsn() -> str:
    # Force component resolution to its defaults by naming the one variable that always
    # has a default; dsn_from_env only returns None when NOTHING is configured.
    os.environ.setdefault("SHELLBOX_PG_HOST", "localhost")
    resolved = dsn_from_env()
    assert resolved is not None  # setdefault above guarantees it
    return resolved


@pytest.fixture(scope="session")
def _pg_engine_or_skip() -> Iterator[Engine]:
    dsn = normalize_postgres_dsn(_test_dsn())
    engine = create_engine(dsn)
    try:
        with engine.connect():
            pass
    except sqlalchemy.exc.OperationalError as exc:
        engine.dispose()
        pytest.skip(f"no live Postgres reachable at {redact(_test_dsn())}: {exc}")
    yield engine
    engine.dispose()


@pytest.fixture
def pg_engine(_pg_engine_or_skip: Engine) -> Generator[Engine, None, None]:
    """A live engine with the registry schema created fresh for this test and dropped
    afterward — cheaper than driving alembic per-test; alembic itself is exercised by
    ``test_migrations.py``."""
    Base.metadata.create_all(_pg_engine_or_skip)
    try:
        yield _pg_engine_or_skip
    finally:
        Base.metadata.drop_all(_pg_engine_or_skip)


@pytest.fixture
def registry(pg_engine: Engine) -> PostgresRegistry:
    return PostgresRegistry(_test_dsn(), engine=pg_engine)
