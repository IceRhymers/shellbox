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


# Hosts these fixtures may destroy without being asked twice.
#
# CRITICAL: The fixtures below are DESTRUCTIVE: they `drop_all` on teardown. Pointed at a shared
# or managed database they silently delete its schema — measured, not hypothetical. Running
# this suite against a real Lakebase endpoint dropped `hosts` and `sessions` while
# `alembic_version` was left claiming the migrations were applied, which is precisely the
# divergence migration 0002's docstring exists to warn about, arrived at from the other
# direction.
#
# So: a throwaway host runs unchanged (CI's service container and the dev docker instance
# are both localhost), and anything else demands an explicit opt-in. Deliberately running
# the suite against Lakebase is valuable — it is how ADR-3's "Lakebase is only a credential
# concern" got verified — it just must not happen by leaving an env var set.
_THROWAWAY_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "postgres", "db"})
_DESTRUCTIVE_OPT_IN = "SHELLBOX_ALLOW_DESTRUCTIVE_TESTS"

# The NARROW permission slip: one host, named exactly, written by
# `scripts/lakebase_branch.py up` for the disposable branch it just forked.
#
# It exists because `_DESTRUCTIVE_OPT_IN` is a blanket "=1" that authorises destroying WHATEVER
# host happens to be configured, and on Lakebase that is not a difference a human can see. A
# branch endpoint and the production endpoint are both
# `ep-<words>-<id>.database.<region>.cloud.databricks.com`, so a reviewer reading a shell cannot
# tell a fork from the registry the deployed App reads. Measured 2026-08-05: this project's
# production endpoint is `ep-orange-dawn-d1pfsv4t.database.us-west-2.cloud.databricks.com`, and
# a fork's differs only in the random segment.
#
# So `W37b` uses THIS rather than the blanket flag. The failure it removes is the ordinary one:
# a shell that still has `SHELLBOX_PG_RESOURCE` exported from a `make deploy`, plus an opt-in set
# an hour ago for a branch that has since been purged. Under `=1` that run drops the dev
# registry; under this variable the host does not match and it is refused.
_THROWAWAY_HOST_ENV = "SHELLBOX_THROWAWAY_PG_HOST"


def _refuse_to_destroy_a_real_database(host: str) -> None:
    """Fail unless ``host`` is one the destructive fixtures may drop tables in.

    Takes a HOST rather than a DSN because the Lakebase path never builds one -- see
    `_pg_engine_or_skip`, where the host comes from the resolved endpoint instead.
    """
    host = (host or "").lower()
    if host in _THROWAWAY_HOSTS:
        return
    if host and host == (os.environ.get(_THROWAWAY_HOST_ENV) or "").lower():
        return
    if os.environ.get(_DESTRUCTIVE_OPT_IN) == "1":
        return
    pytest.fail(
        f"REFUSING to run the destructive registry fixtures against {host!r}. These "
        f"fixtures drop `hosts` and `sessions` on teardown, so pointing them at a shared "
        f"or managed database (Lakebase, staging) deletes its schema — and leaves "
        f"alembic_version claiming the migrations are still applied.\n\n"
        f"For a Lakebase branch, fork a disposable one and let it name itself:\n"
        f"    eval \"$(make -s lakebase-branch-up BRANCH=w37b)\"\n"
        f"which exports {_THROWAWAY_HOST_ENV}={{that branch's host}} and nothing wider.\n\n"
        f"{_DESTRUCTIVE_OPT_IN}=1 also works and authorises ANY host, including production. "
        f"Prefer the branch."
    )


def _lakebase_engine_or_none() -> tuple[Engine, str] | None:
    """An engine on the Lakebase endpoint ``SHELLBOX_PG_RESOURCE`` names, and its host.

    ``None`` when no endpoint is configured, which is the ordinary case and takes the DSN path
    below.

    **A configured endpoint WINS over a DSN**, matching
    `packages/shellbox-registry/src/shellbox_registry/alembic/env.py`, which `W37a` established
    and `tests/unit/test_migration_target.py` asserts by which host was dialled. Two resolution
    orders for one pair of variables is how a command ends up migrating one database and testing
    another.

    ## Why this path exists at all, and what it is the ONLY way to reach

    `make test-registry` against `postgres:16-alpine` proves the registry code and nothing
    Lakebase-specific. The three things most likely to break are all absent from a Postgres
    image, and all of them are on this path:

    * the OAuth token minted **per connect** by `create_lakebase_engine`'s ``do_connect`` hook,
      rather than a password living in a DSN,
    * ``pool_pre_ping`` discarding a connection whose endpoint suspended under the pool,
    * the cold start after ``suspend_timeout_duration`` -- 300s on this project.

    That is `W37b`'s Lakebase half, and `scripts/lakebase_branch.py` is what makes it safe to
    run: it forks a disposable branch, so the ``drop_all`` below lands on a copy-on-write fork
    rather than on the registry the deployed App reads.

    NOTE: no credential is read from the environment here. The endpoint's host and role are
    resolved from the workspace and the token is minted per connect, so nothing in this file --
    and nothing a caller `eval`s -- carries a password.
    """
    resource = os.environ.get("SHELLBOX_PG_RESOURCE")
    if not resource:
        return None

    from shellbox_registry.lakebase import (
        DEFAULT_DATABASE,
        LakebaseCredentials,
        create_lakebase_engine,
        resolve_lakebase_endpoint,
        sdk_token_minter,
    )

    endpoint = resolve_lakebase_endpoint(
        resource, database=os.environ.get("SHELLBOX_PG_DB") or DEFAULT_DATABASE
    )
    # No `start_refresher()`: this suite runs in seconds and `LakebaseCredentials` already mints
    # on demand with a margin. The background refresher is the App's concern, where a process
    # lives for hours -- see `shellbox_app.database`.
    credentials = LakebaseCredentials(sdk_token_minter(resource))
    return create_lakebase_engine(endpoint, credentials), endpoint.host


@pytest.fixture(scope="session")
def _pg_engine_or_skip() -> Iterator[Engine]:
    lakebase = _lakebase_engine_or_none()
    if lakebase is not None:
        engine, host = lakebase
        label = f"Lakebase endpoint {os.environ['SHELLBOX_PG_RESOURCE']}"
    else:
        dsn = normalize_postgres_dsn(_test_dsn())
        engine, host = create_engine(dsn), _host_of(dsn)
        label = redact(_test_dsn())

    # AFTER the engine is built and BEFORE anything connects. On the Lakebase path the host is
    # not knowable until the endpoint has been resolved, and the guard needs the host it will
    # actually dial rather than the resource path that names it.
    try:
        _refuse_to_destroy_a_real_database(host)
    except BaseException:
        engine.dispose()
        raise

    try:
        with engine.connect():
            pass
    except sqlalchemy.exc.OperationalError as exc:
        engine.dispose()
        pytest.skip(f"no live Postgres reachable at {label}: {exc}")
    yield engine
    engine.dispose()


def _host_of(dsn: str) -> str:
    from urllib.parse import urlparse

    return urlparse(dsn).hostname or ""


def static_dsn_or_skip() -> str:
    """A DSN with a password in it, or skip.

    Some tests in this directory cannot use `pg_engine`: they build engines of their own with
    settings that engine does not carry -- `pool_pre_ping=False` for a negative control, or a
    per-test ``application_name`` so a test can terminate exactly its own backends. Those need a
    static connection string.

    **The Lakebase path has none, by design.** `create_lakebase_engine` mints a token per connect
    through a ``do_connect`` hook, so there is no password to put in a URL, and it exposes neither
    ``pool_pre_ping`` nor ``connect_args``. So these tests skip there rather than falling back --
    a fallback is what produced the failure that led here: with ``SHELLBOX_PG_DB`` set for
    Lakebase and the DSN defaulting to localhost, five tests failed with
    ``database "databricks_postgres" does not exist``, which reads as a Lakebase problem and is
    not one.

    WORTH KNOWING: this is the one gap left in `W37b`'s Lakebase half.
    `tests/registry/test_pool_resilience.py` is what would prove ``pool_pre_ping`` survives a
    real scale-to-zero suspend, and it is exactly the file that cannot run here. Closing it means
    teaching `create_lakebase_engine` to accept ``pool_pre_ping`` and ``connect_args``; whether
    Lakebase then permits ``pg_terminate_backend`` on one's own backends is UNMEASURED.
    """
    if os.environ.get("SHELLBOX_PG_RESOURCE"):
        pytest.skip(
            "this test needs a static DSN (a password in the URL) and the Lakebase path mints "
            "tokens per connect. Unset SHELLBOX_PG_RESOURCE to run it against a local Postgres."
        )
    return _test_dsn()


@pytest.fixture
def pg_engine(_pg_engine_or_skip: Engine) -> Generator[Engine, None, None]:
    """A live engine with the registry schema created fresh for this test and dropped
    afterward — cheaper than driving alembic per-test; alembic itself is exercised by
    ``test_migrations.py``.

    WARNING: Two things this fixture does NOT give you, both of which have already misled someone:

    * **It builds the schema from the MODELS, never from the migrations.** So a test passing here
      says nothing about whether a migration exists for what it asserts — deleting migration 0002
      left this whole directory green. `test_migrations.py` compares the migrated schema against
      the models for exactly that reason; keep that comparison, it is the only thing joining the
      two.
    * **`create_all` is `checkfirst=True`.** If a previous run left tables behind (a crashed test,
      an interrupted session, `test_migrations.py` failing before its cleanup), creation is
      **skipped silently** and you are testing against the OLD schema. A model change can then
      look verified when it was never applied. CI is unaffected — its database is new each run —
      but locally, drop the tables before trusting a schema-shaped result.
    """
    Base.metadata.create_all(_pg_engine_or_skip)
    try:
        yield _pg_engine_or_skip
    finally:
        Base.metadata.drop_all(_pg_engine_or_skip)


@pytest.fixture
def registry(pg_engine: Engine) -> PostgresRegistry:
    """A registry on the fixture's engine, whichever path built it.

    The first argument is IGNORED when ``engine`` is passed -- `PostgresRegistry.__init__` uses
    one or the other -- so it is a label here, not a connection. It must not be `_test_dsn()`:
    on the Lakebase path that call returns the localhost FALLBACK, which would read in a
    traceback as though the suite had been pointed at a local Postgres.
    """
    return PostgresRegistry("(engine supplied by the pg_engine fixture)", engine=pg_engine)
