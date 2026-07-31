"""UPSERT semantics (W6 acceptance criterion 6): `PostgresRegistry.upsert_host` and
`upsert_session` must behave like E4's UPSERT — GREATEST(last_seen_at) so a delayed or
out-of-order heartbeat can never move the timestamp backwards, and the first-insert
value of enrolled_at/created_at is preserved across later conflicts ("first enrollment
wins", §10 E4).
"""

from __future__ import annotations

import datetime as dt

import pytest
from shellbox_registry.base import HostRecord, SessionRecord
from shellbox_registry.postgres import PostgresRegistry

pytestmark = pytest.mark.registry

T0 = dt.datetime(2026, 7, 31, 12, 0, 0, tzinfo=dt.UTC)
T1 = dt.datetime(2026, 7, 31, 12, 5, 0, tzinfo=dt.UTC)
T2 = dt.datetime(2026, 7, 31, 12, 10, 0, tzinfo=dt.UTC)


def test_upsert_host_inserts_new_row(registry: PostgresRegistry) -> None:
    registry.upsert_host(
        HostRecord(
            host_id="h1",
            kind="lakebox",
            owner_email="a@example.com",
            last_seen_at=T0,
            status="active",
            enrolled_at=T0,
        )
    )
    row = registry.get_host("h1")
    assert row is not None
    assert row.last_seen_at == T0
    assert row.enrolled_at == T0
    assert row.status == "active"


def test_upsert_host_advances_last_seen_at_on_newer_heartbeat(registry: PostgresRegistry) -> None:
    registry.upsert_host(
        HostRecord(
            host_id="h1",
            kind="lakebox",
            owner_email="a@example.com",
            last_seen_at=T0,
            status="active",
            enrolled_at=T0,
        )
    )
    registry.upsert_host(
        HostRecord(
            host_id="h1",
            kind="lakebox",
            owner_email="a@example.com",
            last_seen_at=T1,
            status="active",
        )
    )
    row = registry.get_host("h1")
    assert row is not None
    assert row.last_seen_at == T1


def test_upsert_host_out_of_order_heartbeat_does_not_move_last_seen_at_backwards(
    registry: PostgresRegistry,
) -> None:
    """The core GREATEST(last_seen_at) guarantee: a delayed/stale heartbeat carrying an
    earlier timestamp than what's already stored must be a no-op on last_seen_at."""
    registry.upsert_host(
        HostRecord(
            host_id="h1",
            kind="lakebox",
            owner_email="a@example.com",
            last_seen_at=T2,
            status="active",
            enrolled_at=T0,
        )
    )
    # A heartbeat that arrives late, carrying an OLDER timestamp than the row already has.
    registry.upsert_host(
        HostRecord(
            host_id="h1",
            kind="lakebox",
            owner_email="a@example.com",
            last_seen_at=T0,
            status="active",
        )
    )
    row = registry.get_host("h1")
    assert row is not None
    assert row.last_seen_at == T2, "a stale heartbeat must not move last_seen_at backwards"


def test_upsert_host_preserves_enrolled_at_across_conflict(registry: PostgresRegistry) -> None:
    registry.upsert_host(
        HostRecord(
            host_id="h1",
            kind="lakebox",
            owner_email="a@example.com",
            last_seen_at=T0,
            status="active",
            enrolled_at=T0,
        )
    )
    # A later upsert with a DIFFERENT enrolled_at must not overwrite the original.
    registry.upsert_host(
        HostRecord(
            host_id="h1",
            kind="lakebox",
            owner_email="a@example.com",
            last_seen_at=T1,
            status="active",
            enrolled_at=T2,
        )
    )
    row = registry.get_host("h1")
    assert row is not None
    assert row.enrolled_at == T0, "first enrollment wins (E4)"


def test_upsert_host_other_fields_still_update_on_conflict(registry: PostgresRegistry) -> None:
    registry.upsert_host(
        HostRecord(
            host_id="h1",
            kind="lakebox",
            owner_email="a@example.com",
            last_seen_at=T0,
            status="active",
            enrolled_at=T0,
            gateway_host="gw-1",
            tmux_socket="/a.sock",
        )
    )
    registry.upsert_host(
        HostRecord(
            host_id="h1",
            kind="lakebox",
            owner_email="a@example.com",
            last_seen_at=T1,
            status="stale",
            gateway_host="gw-2",
            tmux_socket="/b.sock",
        )
    )
    row = registry.get_host("h1")
    assert row is not None
    assert row.status == "stale"
    assert row.gateway_host == "gw-2"
    assert row.tmux_socket == "/b.sock"


def _seed_host(registry: PostgresRegistry) -> None:
    registry.upsert_host(
        HostRecord(
            host_id="h1",
            kind="lakebox",
            owner_email="a@example.com",
            last_seen_at=T0,
            status="active",
            enrolled_at=T0,
        )
    )


def test_upsert_session_inserts_new_row(registry: PostgresRegistry) -> None:
    _seed_host(registry)
    registry.upsert_session(
        SessionRecord(
            session_id="s1",
            host_id="h1",
            tmux_name="s1",
            owner_email="a@example.com",
            last_activity_at=T0,
            status="live",
            created_at=T0,
        )
    )
    row = registry.get_session("s1")
    assert row is not None
    assert row.last_activity_at == T0
    assert row.created_at == T0


def test_upsert_session_out_of_order_write_does_not_move_last_activity_at_backwards(
    registry: PostgresRegistry,
) -> None:
    _seed_host(registry)
    registry.upsert_session(
        SessionRecord(
            session_id="s1",
            host_id="h1",
            tmux_name="s1",
            owner_email="a@example.com",
            last_activity_at=T2,
            status="live",
            created_at=T0,
        )
    )
    # An out-of-order write carrying an OLDER last_activity_at than the row already has.
    registry.upsert_session(
        SessionRecord(
            session_id="s1",
            host_id="h1",
            tmux_name="s1",
            owner_email="a@example.com",
            last_activity_at=T0,
            status="idle",
        )
    )
    row = registry.get_session("s1")
    assert row is not None
    assert row.last_activity_at == T2, "an out-of-order write must not move last_activity_at back"
    assert row.status == "idle", "non-timestamp fields still update on conflict"


def test_upsert_session_preserves_created_at_across_conflict(registry: PostgresRegistry) -> None:
    _seed_host(registry)
    registry.upsert_session(
        SessionRecord(
            session_id="s1",
            host_id="h1",
            tmux_name="s1",
            owner_email="a@example.com",
            last_activity_at=T0,
            status="live",
            created_at=T0,
        )
    )
    registry.upsert_session(
        SessionRecord(
            session_id="s1",
            host_id="h1",
            tmux_name="s1",
            owner_email="a@example.com",
            last_activity_at=T1,
            status="live",
            created_at=T2,
        )
    )
    row = registry.get_session("s1")
    assert row is not None
    assert row.created_at == T0


def test_list_sessions_for_host(registry: PostgresRegistry) -> None:
    _seed_host(registry)
    registry.upsert_session(
        SessionRecord(
            session_id="s1",
            host_id="h1",
            tmux_name="s1",
            owner_email="a@example.com",
            last_activity_at=T0,
            status="live",
        )
    )
    registry.upsert_session(
        SessionRecord(
            session_id="s2",
            host_id="h1",
            tmux_name="s2",
            owner_email="a@example.com",
            last_activity_at=T0,
            status="live",
        )
    )
    rows = registry.list_sessions_for_host("h1")
    assert {r.session_id for r in rows} == {"s1", "s2"}


def test_get_missing_rows_return_none(registry: PostgresRegistry) -> None:
    assert registry.get_host("nope") is None
    assert registry.get_session("nope") is None
    assert registry.list_sessions_for_host("nope") == []
