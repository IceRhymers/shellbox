#!/usr/bin/env python3
"""Grant the App's service principal ``SELECT`` on ``hosts`` and ``sessions``, and nothing else.

The SQL half of the grant. ``scripts/grant-app-sp.sh`` resolves the identities, waits for the App
to reach ACTIVE and retries this script; everything that needs a Postgres connection is here.

## Why the scope is reads only

The App is reachable by every workspace user, and its only legitimate need is reads. The writers
are the 1 to 32 ``shellbox-mcp`` processes, and each of those authenticates as the real user who
runs it -- never as the App. ``tests/unit/test_no_app_writes.py`` asserts the App package calls no
registry writer, which is the static half of the same rule. This file is the half Postgres
enforces.

## What "SELECT only" does and does not describe

It describes the GRANT. It does not describe everything the App SP's role can do, and the
difference has to be stated plainly, because the App's database binding forces it.

``resources/app.yml`` declares ``permission: CAN_CONNECT_AND_CREATE`` on the Lakebase resource.
That is the ONLY value the field accepts -- forced, not chosen. It gets the service principal a
Postgres role that can connect, and table privileges are a separate ``GRANT``, which is this
script. But the name of that permission is not decorative: the role may also be able to CREATE
objects, and no grant on ``hosts`` or ``sessions`` changes that.

So this script REPORTS the role's create capability on every run, as two measured facts rather
than an assumption::

    has_schema_privilege(role, 'public', 'CREATE')
    has_database_privilege(role, current_database(), 'CREATE')

MEASURED 2026-08-05, authenticated as the deployed ``dev`` App SP against the deployed ``dev``
endpoint: **``CREATE TABLE`` in ``public`` was refused with ``42501``.** So on this endpoint the
paragraph above overstates the binding -- its create capability does not reach ``public``.
Lakebase runs PostgreSQL 17, where ``PUBLIC`` holds ``USAGE`` and not ``CREATE`` on ``public`` by
default, and that default is what actually decides it.

Read that narrowly. It was measured on ``public`` only, so the binding may still confer ``CREATE``
elsewhere -- which is why this script keeps reporting ``has_database_privilege`` too -- and it is
a property of the Lakebase template rather than of anything this repo controls.

``--revoke-schema-create`` attempts to narrow the schema privilege. It stays opt-in and remains
UNVERIFIED, because the measurement above means there has been nothing to narrow: nothing has
established whether the platform re-grants it on the next ``bundle deploy``, or whether the
binding needs it. It is the remedy if a future template changes that default. Section 4 of
``docs/deploy.md`` records the run.

What the create capability does NOT put at risk is the registry's existing rows. Creating a table
is not a privilege on another table, and the verification below asserts the role holds no
``INSERT``, ``UPDATE``, ``DELETE`` or ``TRUNCATE`` on either of them.

## The identifiers this file cites

The Phase 4 plan is not committed, so each label is glossed here rather than left to resolve.
``docs/plan-sections.md`` records why that is the rule.

- ``D-1``: the first deploy with a database fails on the service principal's Postgres role.
  Named as the most likely first-deploy failure, and this script is its mitigation.
- ``A15``: the App cannot write to the registry.
- ``W37a``: the non-destructive verification run against the provisioned endpoint, which owns
  the behavioural half of ``A15``.

## Exit codes

The caller retries on one of them and only one, so they are part of this script's contract::

    0  the grant landed and verified
    2  usage error
    3  the role is not in ``pg_roles`` yet. RETRYABLE -- this is the lag the caller absorbs
    4  fatal. Retrying cannot help: the migration has not run, the credential is the App SP's own,
       the grant was refused, or the read-back disagreed with the grant
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

# `scripts/` is on `sys.path` already when this file is run as a script, but not when a test loads
# it by path. One line here keeps the role-name rule declared once, in the guard that `make
# migrate` also runs, instead of copied into two files that can drift apart.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from check_deploy_principal import is_service_principal_role  # noqa: E402

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_ROLE_ABSENT = 3
EXIT_FATAL = 4

SCHEMA = "public"

# The two tables the App reads, and the one privilege it gets on them. Widening either of these is
# a decision, not a commit: `tests/unit/test_grant_scope.py` asserts that the statements this
# module builds are reads, on exactly these two tables, and nothing else.
GRANTED_TABLES = ("hosts", "sessions")
GRANTED_PRIVILEGE = "SELECT"

# Asserted ABSENT after the grant. These are the privileges whose presence would mean the App can
# write the registry, which is the failure `A15` exists to catch.
FORBIDDEN_PRIVILEGES = ("INSERT", "UPDATE", "DELETE", "TRUNCATE")


def quoted(role: str) -> str:
    """The role name as a quoted SQL identifier. Refuses anything that is not a uuid.

    An identifier cannot be a query parameter, so the role name is interpolated into the SQL
    below. This function is what makes that safe, and it is a shape check rather than an escape: a
    canonical dashed uuid contains no quote, no semicolon and no whitespace, so there is nothing
    left to escape once the shape holds. Every service principal client id has that shape.
    """
    if not is_service_principal_role(role):
        raise ValueError(
            f"refusing to build SQL for role {role!r}: a service principal role name is a "
            f"dashed uuid, and this is not one"
        )
    return f'"{role}"'


def grant_statements(role: str) -> list[str]:
    """Every statement the grant runs, in order.

    ``USAGE ON SCHEMA`` is stated explicitly even though Postgres grants it to ``PUBLIC`` by
    default. Two reasons, and the first is the one that matters: without schema usage the table
    grant is inert, so a Lakebase template that revoked the default would reproduce D-1 exactly --
    a role that connects, holds a table grant, and still cannot read. The second is that schema
    usage is not a widening. It is the right to look inside the schema, not to create in it.

    Every statement is idempotent, which is what makes the caller's retry safe.

    Deliberately NOT here: ``GRANT SELECT ON ALL TABLES IN SCHEMA`` and
    ``ALTER DEFAULT PRIVILEGES``. Both extend the App's reach to tables nobody has reviewed it
    against, and a future table is exactly where a Phase 5 credential or token would land.
    """
    role_sql = quoted(role)
    statements = [f"GRANT USAGE ON SCHEMA {SCHEMA} TO {role_sql}"]
    statements += [
        f"GRANT {GRANTED_PRIVILEGE} ON TABLE {SCHEMA}.{table} TO {role_sql}"
        for table in GRANTED_TABLES
    ]
    return statements


def revoke_schema_create_statement(role: str) -> str:
    """The opt-in narrowing of the create capability the binding forces.

    See the module docstring: it is UNVERIFIED against Lakebase, and it is off by default.
    """
    return f"REVOKE CREATE ON SCHEMA {SCHEMA} FROM {quoted(role)}"


def _fail(message: str) -> int:
    print(f"ERROR: {message}", file=sys.stderr)
    return EXIT_FATAL


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Grant the App SP reads on the registry tables.")
    parser.add_argument("--role", required=True, help="the App's service_principal_client_id")
    parser.add_argument(
        "--revoke-schema-create",
        action="store_true",
        help="also attempt to revoke CREATE on the schema from the role. UNVERIFIED",
    )
    args = parser.parse_args(argv)

    if not is_service_principal_role(args.role):
        print(
            f"ERROR: --role {args.role!r} is not a dashed uuid.\n"
            f"  The Postgres role name of a service principal IS its client id. Read it from\n"
            f"  `databricks apps get <app> -o json`, field `service_principal_client_id`.",
            file=sys.stderr,
        )
        return EXIT_USAGE

    # Imported here rather than at module scope so that `grant_statements` stays importable with no
    # driver and no synced workspace: `tests/unit/test_grant_scope.py` asserts on the SQL this
    # module builds, and that test must not need psycopg to run.
    import psycopg
    from shellbox_registry.dsn import dsn_from_env

    # A configured endpoint WINS over a DSN, for the reason the alembic env.py docstring gives:
    # `dsn_from_env` defaults the host to localhost as soon as ANY SHELLBOX_PG_* variable is set,
    # so a stale variable in the operator's shell must not be able to redirect a deploy-time grant
    # onto their laptop.
    resource = (os.environ.get("SHELLBOX_PG_RESOURCE") or "").strip()
    dsn = dsn_from_env()
    if not resource and dsn is None:
        return _fail(
            "no registry connection is configured, so there is nothing to grant on.\n"
            "  Set SHELLBOX_PG_RESOURCE to the endpoint this bundle declares -- it is the\n"
            '  easy path, and it needs no credential of its own:\n'
            '    eval "$(scripts/bundle-vars.sh --target dev --profile fevm-west)"\n'
            "  Or set SHELLBOX_PG_HOST and the credential parts. Both routes are in\n"
            "  docs/deploy.md section 3."
        )

    try:
        connection = _connect_lakebase(resource) if resource else _connect_dsn(str(dsn))
    except psycopg.Error as error:
        return _fail(f"cannot connect to the registry: {error}")

    with connection:
        return _grant(connection, args.role, revoke_schema_create=args.revoke_schema_create)


def _connect_lakebase(resource_name: str) -> Any:
    """Connect to the Lakebase endpoint named by ``resource_name``, as the deploying principal.

    Connection PARAMETERS rather than a URI, and that is not a style choice: the OAuth token is
    the password, so a URI would carry a live credential in a string that an exception, a
    ``repr`` or a log line could reproduce. ``redact`` exists on the DSN path for that reason;
    here the token never enters a string at all.

    The Postgres role is DERIVED from ``current_user.me()`` unless ``SHELLBOX_PG_USER`` overrides
    it -- see ``resolve_lakebase_endpoint``. That is what makes this grant run as the deploying
    principal by construction rather than by an operator remembering to export the right name.
    ``_grant`` still asserts it against what the server reports, which is the only authority on
    which identity actually connected.
    """
    import psycopg
    from shellbox_registry.lakebase import (
        DEFAULT_DATABASE,
        resolve_lakebase_endpoint,
        sdk_token_minter,
    )

    endpoint = resolve_lakebase_endpoint(
        resource_name,
        database=(os.environ.get("SHELLBOX_PG_DB") or "").strip() or DEFAULT_DATABASE,
        user=(os.environ.get("SHELLBOX_PG_USER") or "").strip() or None,
    )
    print(f"    connecting to {endpoint.host}/{endpoint.database} as {endpoint.user}")
    return psycopg.connect(
        host=endpoint.host,
        port=endpoint.port,
        dbname=endpoint.database,
        user=endpoint.user,
        password=sdk_token_minter(resource_name)().token,
        # Lakebase demands TLS, and libpq's default is `prefer`, which would silently accept
        # a downgrade rather than fail.
        sslmode="require",
        autocommit=True,
    )


def _connect_dsn(dsn: str) -> Any:
    """Connect to an explicitly configured DSN. The local-Postgres and pre-minted-token path."""
    import psycopg
    from shellbox_registry.dsn import redact

    # NOT `normalize_postgres_dsn`. That rewrite names the SQLAlchemy driver, and libpq rejects the
    # `postgresql+psycopg://` scheme it produces. `dsn_from_env` returns a plain URI, which is what
    # psycopg wants.
    print(f"    connecting to {redact(dsn)}")
    return psycopg.connect(dsn, autocommit=True)


def _grant(connection: Any, role: str, *, revoke_schema_create: bool) -> int:
    import psycopg

    with connection.cursor() as cursor:
        cursor.execute("SELECT current_user, current_database()")
        current_user, database = cursor.fetchone()
        print(f"    connected as {current_user} to {database}")

        # The structural half of "a deploy-time action runs as the deploying principal".
        # `make grant` runs `scripts/check_deploy_principal.py` first, on the configured
        # variables; this is the same rule asserted against what the server says it got, which is
        # the only authority on which identity actually connected.
        if current_user == role:
            return _fail(
                f"this connection IS the App service principal ({role}).\n"
                f"  The grant, like `alembic upgrade head`, runs as the deploying principal. A\n"
                f"  principal cannot grant itself a privilege it does not already hold, so this\n"
                f"  would fail anyway. It fails here instead, with a message that says why."
            )

        cursor.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,))
        if cursor.fetchone() is None:
            # RETRYABLE, and the only retryable outcome. The caller's `retry 5 10` exists for the
            # lag between the App reaching ACTIVE and this row appearing.
            print(f"    the role {role} is not in pg_roles yet")
            return EXIT_ROLE_ABSENT

        missing = [table for table in GRANTED_TABLES if _regclass(cursor, table) is None]
        if missing:
            return _fail(
                f"the registry tables {missing} do not exist in {database}.\n"
                f"  The grant comes AFTER the migration, because `GRANT SELECT ON TABLE` needs\n"
                f"  the table to exist. Run `make migrate` as the deploying principal, then\n"
                f"  re-run this."
            )

        for statement in grant_statements(role):
            print(f"    {statement}")
            try:
                cursor.execute(statement)
            except psycopg.Error as error:
                return _fail(
                    f"the grant was refused: {error}\n"
                    f"  The deploying principal must own the tables, or hold GRANT OPTION on\n"
                    f"  them, to grant reads on them. `alembic upgrade head` run as this same\n"
                    f"  principal is what makes it the owner."
                )

        verdict = _verify(cursor, role)
        _report_create_capability(cursor, role, database)

        if revoke_schema_create:
            _attempt_revoke(cursor, role)
            _report_create_capability(cursor, role, database)

        return verdict


def _regclass(cursor: Any, table: str) -> Any:
    cursor.execute("SELECT to_regclass(%s)", (f"{SCHEMA}.{table}",))
    return cursor.fetchone()[0]


def _verify(cursor: Any, role: str) -> int:
    """Read the grant back, and assert it is neither missing nor wider than intended.

    ``has_table_privilege`` is the check rather than a scan of ``information_schema``, because it
    answers the effective question: a privilege the role holds through membership in another role,
    or through a grant to ``PUBLIC``, shows up here. A catalog scan for rows naming this role
    would miss both.

    This is NOT `A15`'s witness. `A15` needs an ``INSERT`` attempted with the SP's own credential
    and refused with SQLSTATE ``42501`` -- a behavioural refusal, not a catalog answer -- and that
    is `W37a`'s to run. This is the cheaper check that runs on every grant, and it fails loudly
    the moment anything widens the role.
    """
    failures: list[str] = []
    for table in GRANTED_TABLES:
        qualified = f"{SCHEMA}.{table}"
        for privilege in (GRANTED_PRIVILEGE, *FORBIDDEN_PRIVILEGES):
            cursor.execute("SELECT has_table_privilege(%s, %s, %s)", (role, qualified, privilege))
            held = cursor.fetchone()[0]
            expected = privilege == GRANTED_PRIVILEGE
            print(f"    has_table_privilege({table}, {privilege}) = {held}")
            if held != expected:
                failures.append(f"{qualified}: {privilege} is {held}, expected {expected}")

    if failures:
        return _fail(
            "the grant did not land as written:\n"
            + "".join(f"    - {line}\n" for line in failures)
            + "  A missing SELECT means the App 500s on every inventory call. A present write\n"
            "  privilege means the registry is writable by a service every workspace user can\n"
            "  reach."
        )

    print(f"    ok: {role} holds {GRANTED_PRIVILEGE} on {', '.join(GRANTED_TABLES)} and no writes")
    return EXIT_OK


def _report_create_capability(cursor: Any, role: str, database: str) -> None:
    """Print the create capability the binding forces, as a measurement rather than a claim.

    This does NOT fail the grant. ``permission: CAN_CONNECT_AND_CREATE`` is the only value the
    App's Lakebase binding accepts, so a script that failed on its consequence would make the
    deploy unpassable. It is reported on every run so the residual is observed, and so the first
    deploy answers a question the plan could only assume.
    """
    cursor.execute(
        "SELECT has_schema_privilege(%s, %s, 'CREATE'), has_database_privilege(%s, %s, 'CREATE')",
        (role, SCHEMA, role, database),
    )
    on_schema, on_database = cursor.fetchone()
    print(f"    has_schema_privilege({SCHEMA}, CREATE) = {on_schema}")
    print(f"    has_database_privilege({database}, CREATE) = {on_database}")
    if on_schema or on_database:
        print(
            "    NOTE: the role can create objects. That is the forced CAN_CONNECT_AND_CREATE "
            "binding, not this grant, and it reaches no existing row. See docs/deploy.md "
            "section 4."
        )


def _attempt_revoke(cursor: Any, role: str) -> None:
    """Try the narrowing revoke, and say what happened either way.

    Deliberately non-fatal in both directions. A refusal means the platform owns the privilege and
    the residual stands. A success means it was revocable at this instant, which is not yet the
    same claim as revocable durably: the next ``bundle deploy`` reconciles the binding.
    """
    import psycopg

    statement = revoke_schema_create_statement(role)
    print(f"    {statement}")
    try:
        cursor.execute(statement)
    except psycopg.Error as error:
        print(f"    WARNING: the revoke was refused: {error}")
        print("    The create capability stands. Record it as the residual it is.")
        return
    print("    the revoke was accepted. Re-run this script after the next `bundle deploy`:")
    print("    if the capability is back, the platform re-grants it and the residual stands.")


if __name__ == "__main__":
    sys.exit(main())
