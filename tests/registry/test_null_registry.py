"""W6 acceptance criterion 7: with `SHELLBOX_DATABASE_URL` unset, the registry layer
must be fully usable and non-fatal. These tests need no database at all -- deliberately
NOT gated behind the `registry` marker (conftest's `pytest_collection_modifyitems` only
auto-marks tests, it does not skip them; these simply never request a DB fixture)."""

from __future__ import annotations

import datetime as dt

from shellbox_registry import NullRegistry, create_registry
from shellbox_registry.base import HostRecord, SessionRecord
from shellbox_registry.postgres import PostgresRegistry

NOW = dt.datetime(2026, 7, 31, tzinfo=dt.UTC)


def test_null_registry_accepts_every_call_and_does_nothing() -> None:
    reg = NullRegistry()

    # None of these may raise, regardless of what's passed.
    reg.upsert_host(
        HostRecord(
            host_id="h1",
            kind="lakebox",
            owner_email="a@example.com",
            last_seen_at=NOW,
            status="active",
        )
    )
    reg.upsert_session(
        SessionRecord(
            session_id="s1",
            host_id="h1",
            tmux_name="s1",
            owner_email="a@example.com",
            last_activity_at=NOW,
            status="live",
        )
    )

    assert reg.get_host("h1") is None
    assert reg.get_session("s1") is None
    assert reg.list_sessions_for_host("h1") == []


def test_null_registry_list_primitives_return_empty_and_never_raise() -> None:
    """`W28`'s two read primitives, held to the same contract as the rest of the class.

    The App runs on `NullRegistry` whenever no endpoint is configured, so "the inventory is
    empty" has to be a valid answer that renders. An exception here would take down a page
    that is meant to degrade to "terminals work, inventory is stale" (ADR-3).

    Both the filtered and the unfiltered call, because the filter is the parameter a caller
    is most likely to be exercising when the database turns out not to exist.
    """
    reg = NullRegistry()

    assert reg.list_hosts() == []
    assert reg.list_hosts("a@example.com") == []
    assert reg.list_sessions() == []
    assert reg.list_sessions("a@example.com") == []


def test_create_registry_unset_dsn_returns_null_registry() -> None:
    assert isinstance(create_registry(None), NullRegistry)
    assert isinstance(create_registry(""), NullRegistry)


def test_create_registry_postgres_dsn_returns_postgres_registry() -> None:
    # Credential-free on purpose: create_registry only dispatches on the scheme, it never
    # connects, so embedding a user:password pair here would buy nothing and trips
    # credential scanners.
    reg = create_registry("postgresql://example.invalid:5432/db")
    try:
        assert isinstance(reg, PostgresRegistry)
    finally:
        reg.dispose()  # never connects; safe to dispose immediately
