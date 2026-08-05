"""The grant `make grant` issues, ENFORCED by a real Postgres rather than described.

`A15` is "the App cannot write to the registry", and until this file existed it rested on three
things, none of which was an enforced refusal:

* `tests/unit/test_grant_scope.py` asserts the SHAPE of the statements
  `scripts/grant_app_sp.py` builds. Static, credential-free, and it cannot say what Postgres does
  with them.
* `scripts/grant_app_sp.py`'s own read-back asks ``has_table_privilege``. That is the CATALOG's
  claim about what the grant records -- a different claim from what the server enforces.
* A hand-run measurement, recorded in prose in `docs/deploy.md` section 4 ("Measured 2026-08-03,
  against PostgreSQL 17.10 in a local container with a stand-in role"). Real evidence, run once,
  re-run by nothing.

This file is that third bullet turned into a lane. It creates a stand-in role, applies
`grant_statements` VERBATIM, and then asks the server -- which answers with an error code rather
than with a catalog row.

## What it proves, and what it still does not

It proves that **the statements the script actually issues produce a role Postgres refuses to
write with**. That is the enforcement claim, and it is what `test_grant_scope.py`'s docstring
defers to "a database, an App and a deploy".

CRITICAL: it needs a database. It does NOT need an App and it does not need a deploy, and that is
the whole reason this lane can exist. What is left for the deploy to prove is narrower than it
looks: that the REAL App SP's role on the REAL Lakebase endpoint received these statements and
nothing wider. `W37a`'s remaining bullet is that check, it is blocked on a
``service-principal-secrets-proxy`` permission, and nothing here closes it -- see `docs/deploy.md`
section 4, which also records that ``SET ROLE`` is not a way around it
(``pg_has_role(deploying_principal, app_sp, 'MEMBER') = False``, measured).

So: this file makes the grant's SHAPE enforced rather than merely asserted. The identity it is
enforced against is a stand-in.

## Why the incomplete row is the right probe, and not a shortcut

The writes below are deliberately malformed -- ``INSERT ... DEFAULT VALUES`` against a table with
six ``NOT NULL`` columns. That is safe, and it is load-bearing: **Postgres checks the privilege
before it evaluates the constraints**, so a role that lacked ``INSERT`` fails at the privilege
check and never reaches the constraint. A role that HELD ``INSERT`` would get past it and fail
with ``23502`` instead.

That makes the two codes a two-sided oracle rather than one assertion, which is why the test below
rejects ``23502`` and ``23503`` by name. `docs/deploy.md` section 4 tabulates the same four
outcomes, and this file is the executable form of that table.
"""

from __future__ import annotations

import importlib.util
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from sqlalchemy.engine import Engine

pytestmark = pytest.mark.registry

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "grant_app_sp.py"

_spec = importlib.util.spec_from_file_location("grant_app_sp", _SCRIPT)
assert _spec is not None and _spec.loader is not None, f"cannot load {_SCRIPT}"
_grant = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_grant)

# `insufficient_privilege`. THE code, and the criterion names it: a bare "refused" is satisfied by
# a constraint violation on a malformed row, which never reaches a privilege check. Asserted as the
# SQLSTATE and never as the message text, which is not stable across Postgres versions.
INSUFFICIENT_PRIVILEGE = "42501"

# The two codes that mean "the statement got PAST the privilege check". Seeing either one is a
# failure of this test's premise, not a pass -- see this module's docstring.
NOT_NULL_VIOLATION = "23502"
FOREIGN_KEY_VIOLATION = "23503"

# A password for the stand-in role. It exists only inside this test's transaction against a
# throwaway Postgres, and the role is dropped on the way out.
_STANDIN_PASSWORD = "w37b-standin-not-a-secret"  # noqa: S105 - throwaway, dropped in teardown


def _write_statement(table: str, privilege: str) -> str:
    """A minimal statement exercising ``privilege`` on ``table``.

    Each one is the cheapest statement that reaches a privilege check for its verb. They are
    written to be REFUSED, so none of them is guarded against succeeding -- if the grant is wider
    than it should be, an ``UPDATE`` or ``DELETE`` here really does run against the fixture's
    table. That is acceptable because the fixture drops the schema afterwards, and it is the point:
    a widened grant must produce a visible failure rather than a skipped assertion.
    """
    qualified = f"{_grant.SCHEMA}.{table}"
    if privilege == "INSERT":
        # DEFAULT VALUES on a table with NOT NULL columns -- see this module's docstring on why
        # the incomplete row is the right probe rather than a shortcut.
        return f"INSERT INTO {qualified} DEFAULT VALUES"
    if privilege == "UPDATE":
        return f"UPDATE {qualified} SET host_id = host_id"
    if privilege == "DELETE":
        return f"DELETE FROM {qualified}"
    if privilege == "TRUNCATE":
        return f"TRUNCATE {qualified}"
    raise AssertionError(
        f"scripts/grant_app_sp.py forbids {privilege!r}, and this test has no statement that "
        f"exercises it. Add one -- an unexercised forbidden privilege is an unchecked one."
    )


@pytest.fixture
def granted_role(pg_engine: Engine) -> Iterator[tuple[str, dict[str, Any]]]:
    """A stand-in role holding exactly what `make grant` grants, and its connection parameters.

    The role name is a dashed uuid because `grant_app_sp.quoted` REFUSES anything else -- every
    service principal client id has that shape, and the shape check is what makes interpolating an
    identifier into the SQL safe. So this fixture exercises the real identifier path rather than
    stepping around it.

    ``pg_engine`` is depended on for its schema: it creates `hosts` and `sessions` from the models
    and drops them afterwards, and the grant needs its tables to exist -- the same ordering
    `scripts/deploy.sh` obeys, where the grant comes after the migration.

    ## Why this revokes ``USAGE`` from ``PUBLIC`` first

    MEASURED 2026-08-05 on PostgreSQL 16.14: ``public``'s ACL is
    ``{pg_database_owner=UC/pg_database_owner,=U/pg_database_owner}``, so ``PUBLIC`` already holds
    ``USAGE`` and every fresh role inherits it. On such a server `grant_statements`'s explicit
    ``GRANT USAGE ON SCHEMA`` is INERT, and a test built on the stock defaults cannot tell whether
    it is there or not -- VERIFIED by mutation: deleting that statement left this whole file green.

    `grant_statements`'s docstring names the exact scenario the clause is for: "a Lakebase template
    that revoked the default would reproduce D-1 exactly -- a role that connects, holds a table
    grant, and still cannot read". So the fixture reproduces that template. After the revoke the
    role's ONLY route to schema usage is the explicit grant, which makes
    `test_the_granted_role_can_read_both_tables` a real guard on it rather than a restatement of
    Postgres's defaults.

    It is restored on teardown, and it is safe to do at all only because
    `tests/registry/conftest.py` refuses to run these fixtures against anything but a throwaway
    host.
    """
    role = str(uuid.uuid4())
    url = pg_engine.url

    def as_owner(statements: list[str]) -> None:
        # Autocommit: CREATE ROLE and DROP ROLE are not transactional in the way the rest of this
        # fixture would want, and a half-applied grant would leak into the next test.
        with psycopg.connect(_owner_params(url), autocommit=True) as connection:
            for statement in statements:
                connection.execute(statement)  # type: ignore[arg-type]

    as_owner(
        [
            # The hostile template, reproduced -- see this fixture's docstring. Without this the
            # explicit USAGE grant below is inert and untestable.
            f"REVOKE USAGE ON SCHEMA {_grant.SCHEMA} FROM PUBLIC",
            f'CREATE ROLE "{role}" LOGIN PASSWORD \'{_STANDIN_PASSWORD}\'',
            # VERBATIM from the script under test. Restating the statements here would make this
            # test assert against a copy, and a copy is exactly what stops tracking the original.
            *_grant.grant_statements(role),
        ]
    )
    try:
        yield role, {
            "host": url.host,
            "port": url.port,
            "dbname": url.database,
            "user": role,
            "password": _STANDIN_PASSWORD,
        }
    finally:
        # DROP OWNED BY first: a role holding privileges cannot be dropped, and the grants above
        # are exactly such privileges. Without this the role leaks and the next run's CREATE fails.
        #
        # The regrant to PUBLIC restores the server's stock default. It runs even when the body
        # failed, because leaving a shared dev Postgres with USAGE revoked would break every other
        # test that connects as a non-owner -- a teardown that only runs on success is how a test
        # failure becomes a suite failure.
        as_owner(
            [
                f'DROP OWNED BY "{role}"',
                f'DROP ROLE IF EXISTS "{role}"',
                f"GRANT USAGE ON SCHEMA {_grant.SCHEMA} TO PUBLIC",
            ]
        )


def _owner_params(url: Any) -> str:
    """A libpq connection string for the table owner, derived from the fixture's own engine.

    Derived rather than re-resolved from the environment, so this file cannot end up pointing at a
    different database from the one `pg_engine` created the schema in -- and so it inherits
    `tests/registry/conftest.py`'s refusal to run against anything but a throwaway host.
    """
    return (
        f"host={url.host} port={url.port} dbname={url.database} "
        f"user={url.username} password={url.password}"
    )


def test_the_granted_role_can_read_both_tables(
    granted_role: tuple[str, dict[str, Any]],
) -> None:
    """The non-vacuity guard, and it is the one that makes every refusal below meaningful.

    CRITICAL: without ``USAGE ON SCHEMA`` a ``SELECT`` is refused with ``42501`` too -- the same
    code the write tests assert. So a role that could not reach the schema at all, or could not log
    in, would make every one of those tests pass while proving nothing about the table privilege.

    This is also `D-1` in miniature: a role that connects, holds a table grant, and still cannot
    read is precisely the failure `grant_statements` states ``USAGE ON SCHEMA`` explicitly to
    prevent.
    """
    _, params = granted_role
    with psycopg.connect(**params) as connection:
        for table in _grant.GRANTED_TABLES:
            rows = connection.execute(f"SELECT * FROM {_grant.SCHEMA}.{table}").fetchall()
            assert rows == [], f"{table} should be empty in a fresh fixture, got {len(rows)} rows"


@pytest.mark.parametrize("table", _grant.GRANTED_TABLES)
@pytest.mark.parametrize("privilege", _grant.FORBIDDEN_PRIVILEGES)
def test_a_write_as_the_granted_role_is_refused_with_42501(
    granted_role: tuple[str, dict[str, Any]], table: str, privilege: str
) -> None:
    """`A15`'s enforced half. Parametrised off the script's own constants, so adding a privilege
    to ``FORBIDDEN_PRIVILEGES`` extends this test instead of silently leaving it behind."""
    _, params = granted_role
    statement = _write_statement(table, privilege)

    with psycopg.connect(**params) as connection, pytest.raises(psycopg.Error) as caught:
        connection.execute(statement)

    sqlstate = caught.value.sqlstate
    assert sqlstate not in (NOT_NULL_VIOLATION, FOREIGN_KEY_VIOLATION), (
        f"{statement!r} was refused with {sqlstate}, which is a CONSTRAINT violation -- the "
        f"statement reached the constraints, so it got PAST the privilege check. That means the "
        f"role holds {privilege} on {table}, and the grant is wider than "
        f"scripts/grant_app_sp.py says."
    )
    assert sqlstate == INSUFFICIENT_PRIVILEGE, (
        f"{statement!r} was refused with {sqlstate}, expected {INSUFFICIENT_PRIVILEGE} "
        f"(insufficient_privilege). See docs/deploy.md section 4 for what each code means."
    )


def test_the_grant_adds_no_create_capability(
    granted_role: tuple[str, dict[str, Any]],
) -> None:
    """The widening that would not show up as a table privilege: creating a table of your own.

    `grant_statements` states ``USAGE ON SCHEMA`` and two table ``SELECT``s, and its docstring is
    explicit that ``USAGE`` "is the right to look inside the schema, not to create in it". This is
    that sentence, enforced: after the grant the role still cannot create.

    ## What this does NOT say about the deployed App, and the distinction matters

    `scripts/grant_app_sp.py`'s docstring records that the App's Lakebase binding is
    ``CAN_CONNECT_AND_CREATE``, so the DEPLOYED role arrives holding a create capability the grant
    never gave it -- which is why `revoke_schema_create_statement` exists as an opt-in narrowing,
    UNVERIFIED against Lakebase and off by default.

    That capability comes from the BINDING, not from the grant and not from Postgres's defaults,
    and this lane cannot observe it: there is no binding here. So the two facts do not conflict,
    and a reader must not take this test as evidence that the deployed role cannot create.

    MEASURED here, 2026-08-05, PostgreSQL 16.14: ``public``'s ACL is
    ``{pg_database_owner=UC/pg_database_owner,=U/pg_database_owner}`` -- ``PUBLIC`` holds ``U``
    (usage) and not ``C`` (create). That is PostgreSQL 15's changed default, and it is why a fresh
    role gets no create capability from the schema either. On a server older than 15 this test
    would fail for that reason alone, which is worth knowing before reading its failure as a
    widened grant.
    """
    _, params = granted_role
    with psycopg.connect(**params, autocommit=True) as connection:
        own = f"w37b_own_{uuid.uuid4().hex[:8]}"
        with pytest.raises(psycopg.Error) as caught:
            connection.execute(f"CREATE TABLE {_grant.SCHEMA}.{own} (x int)")
        assert caught.value.sqlstate == INSUFFICIENT_PRIVILEGE, (
            f"CREATE TABLE was refused with {caught.value.sqlstate}, expected "
            f"{INSUFFICIENT_PRIVILEGE}. If it SUCCEEDED, grant_statements has gained a create "
            f"capability it does not document -- see its docstring on USAGE ON SCHEMA."
        )
