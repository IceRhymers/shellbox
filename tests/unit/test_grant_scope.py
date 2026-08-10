"""The App SP's grant is reads, on two tables, and nothing else.

``scripts/grant_app_sp.py`` verifies its own work at run time against a live Postgres, with
``has_table_privilege``. That check is the authoritative one and it needs a database, an App and a
deploy. This file asserts the part that can be asserted from a checkout: the STATEMENTS the script
builds.

It is a scope guard, and the failure it is written against is a plausible one-line widening --
``GRANT SELECT ON ALL TABLES IN SCHEMA public``, or an ``INSERT`` added to make some later feature
work. Either would pass the run-time verification only after that verification was also relaxed;
this file fails first, in ``make test``, with no credential.

Pairs with ``tests/unit/test_no_app_writes.py``: that file asserts the App does not CALL a writer,
this one asserts the database would not LET it.

Two Phase 4 plan labels appear below, and the plan is not committed, so both are glossed here as
``docs/plan-sections.md`` requires. ``A15`` is "the App cannot write to the registry". ``D-1`` is
"the first deploy with a database fails on the service principal's Postgres role".
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from shellbox_registry.models import Base

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "grant_app_sp.py"

_spec = importlib.util.spec_from_file_location("grant_app_sp", _SCRIPT)
assert _spec is not None and _spec.loader is not None, f"cannot load {_SCRIPT}"
_grant = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_grant)

# A real one, measured 2026-08-03: `databricks apps get shellbox -o json` reports this as
# `service_principal_client_id`. Using the real value rather than an invented uuid keeps the shape
# check honest about the shape it actually has to accept.
ROLE = "3337afac-b67b-41af-8996-828620bcc4a8"


def test_the_grant_is_three_statements_and_every_one_is_a_grant() -> None:
    statements = _grant.grant_statements(ROLE)
    assert len(statements) == 3, statements
    assert all(statement.startswith("GRANT ") for statement in statements), statements


def test_the_only_table_privilege_granted_is_select() -> None:
    """The whole point of the item. ``USAGE`` appears once and only on the schema, which is the
    right to look inside it rather than to create in it."""
    statements = _grant.grant_statements(ROLE)
    table_grants = [s for s in statements if " ON TABLE " in s]
    assert len(table_grants) == 2, statements
    assert all(s.startswith("GRANT SELECT ON TABLE ") for s in table_grants), table_grants

    schema_grants = [s for s in statements if " ON SCHEMA " in s]
    assert schema_grants == ["GRANT USAGE ON SCHEMA public TO " + f'"{ROLE}"'], schema_grants


def test_no_statement_grants_a_write_or_a_wildcard() -> None:
    """The forms a widening would actually take, each named so the failure message says which.

    ``ALL TABLES IN SCHEMA`` and ``ALTER DEFAULT PRIVILEGES`` are in this list because they are the
    convenient answers to "the App cannot read the new table", and both hand the App every future
    table -- including whichever one a later phase uses for a credential.
    """
    forbidden = (
        "INSERT",
        "UPDATE",
        "DELETE",
        "TRUNCATE",
        "ALL PRIVILEGES",
        "ALL TABLES",
        "ALTER DEFAULT PRIVILEGES",
        "CREATE",
    )
    for statement in _grant.grant_statements(ROLE):
        for word in forbidden:
            assert word not in statement, f"{statement!r} contains {word!r}"


def test_the_granted_tables_are_named_and_schema_qualified() -> None:
    statements = _grant.grant_statements(ROLE)
    assert "GRANT SELECT ON TABLE public.hosts TO " + f'"{ROLE}"' in statements
    assert "GRANT SELECT ON TABLE public.sessions TO " + f'"{ROLE}"' in statements


def test_every_granted_table_exists_in_the_registry_schema() -> None:
    """Non-vacuity in the other direction: a grant naming a table the schema does not declare
    would fail at deploy time with ``42P01``, several minutes after the deploy started.

    Deliberately a subset check and not an equality one. A table added to the registry is NOT
    granted to the App by this passing -- granting it takes an edit to ``GRANTED_TABLES``, which is
    the review this scoping exists to force.
    """
    assert set(_grant.GRANTED_TABLES) <= set(Base.metadata.tables), (
        f"{_grant.GRANTED_TABLES} against {sorted(Base.metadata.tables)}"
    )


def test_the_verification_asserts_the_writes_are_absent() -> None:
    """The run-time read-back is what proves the grant landed, so its expectations are pinned here
    too. Dropping ``INSERT`` from that list is how the live check would stop testing the thing
    ``A15`` is about."""
    assert {"INSERT", "UPDATE", "DELETE"} <= set(_grant.FORBIDDEN_PRIVILEGES)
    assert _grant.GRANTED_PRIVILEGE == "SELECT"


def test_a_role_name_that_is_not_a_uuid_is_refused() -> None:
    """The role name is interpolated into the SQL, because an identifier cannot be a parameter.

    So the shape check is the safety property, and it is asserted rather than trusted. The refused
    value here is a real injection attempt: without the check it would close the identifier and
    append a statement.
    """
    with pytest.raises(ValueError, match="dashed uuid"):
        _grant.quoted('x" TO postgres; DROP TABLE hosts; --')
    with pytest.raises(ValueError, match="dashed uuid"):
        _grant.quoted("tanner.wendland@databricks.com")
    assert _grant.quoted(ROLE) == f'"{ROLE}"'


def test_the_revoke_statement_touches_only_the_schema_create_privilege() -> None:
    """The opt-in narrowing of the create capability the App's binding forces. It must not turn
    into a general-purpose revoke: taking ``CONNECT`` or ``USAGE`` away from the role would produce
    D-1 again, from the mitigation for a different problem."""
    statement = _grant.revoke_schema_create_statement(ROLE)
    assert statement == f'REVOKE CREATE ON SCHEMA public FROM "{ROLE}"'
