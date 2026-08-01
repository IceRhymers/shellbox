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
            sandbox_id="sbx-1",
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
            sandbox_id="sbx-2",
            gateway_host="gw-2",
            tmux_socket="/b.sock",
        )
    )
    row = registry.get_host("h1")
    assert row is not None
    assert row.status == "stale"
    # `sandbox_id` was droppable from both the values and the conflict set with nothing failing,
    # and ADR-8's entire story funnels into this column: it is the only human-meaningful label a
    # `hosts` row has, since `host_id` is an opaque uuid4 and the sandbox cannot learn its own id.
    assert row.sandbox_id == "sbx-2"
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


# --------------------------------------------------------------------------------------
# `last_read_at` -- the two-column activity split (plan OQ5). `last_activity_at` advances
# on SEND, `last_read_at` on READ, so #5 can choose a reaping predicate that Phase 2 does
# not pre-decide. These live here rather than in the unit lane because the property being
# asserted is Postgres's, not Python's: GREATEST ignores NULLs.
# --------------------------------------------------------------------------------------
def _session(**overrides: object) -> SessionRecord:
    fields: dict[str, object] = {
        "session_id": "h1:build",
        "host_id": "h1",
        "tmux_name": "build",
        "owner_email": "a@example.com",
        "last_activity_at": T0,
        "status": "live",
        "created_at": T0,
    }
    fields.update(overrides)
    return SessionRecord(**fields)  # type: ignore[arg-type]


def _host(registry: PostgresRegistry) -> None:
    """The FK parent. `sessions.host_id` REFERENCES hosts, so this is not optional."""
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


def test_upsert_session_round_trips_the_columns_phase_4_renders(
    registry: PostgresRegistry,
) -> None:
    """`cwd`/`cols`/`rows` were droppable from `upsert_session` undetected.

    Lower stakes than `sandbox_id`, but they are what Phase 4 needs to size a terminal, so a
    silently-unwritten column surfaces as a mis-rendered pane rather than as an error.
    """
    _host(registry)
    registry.upsert_session(_session(cwd="/w", cols=120, rows=40))
    row = registry.get_session("h1:build")
    assert row is not None
    assert (row.cwd, row.cols, row.rows) == ("/w", 120, 40)

    registry.upsert_session(_session(cwd="/other", cols=80, rows=24))
    row = registry.get_session("h1:build")
    assert row is not None
    assert (row.cwd, row.cols, row.rows) == ("/other", 80, 24), "the conflict set must update too"


def test_last_read_at_is_null_until_a_read_happens(registry: PostgresRegistry) -> None:
    """ "Never read" has no honest timestamp. It must NOT default to created_at, which would
    read as "someone looked at this"."""
    _host(registry)
    registry.upsert_session(_session())
    row = registry.get_session("h1:build")
    assert row is not None
    assert row.last_read_at is None


def test_a_send_only_upsert_preserves_an_existing_last_read_at(
    registry: PostgresRegistry,
) -> None:
    """🔴 The subtle one, and the reason `last_read_at` needs no special-casing in callers.

    Postgres's GREATEST *ignores* NULLs -- returning NULL only when every argument is NULL --
    so a send-only upsert carrying last_read_at=None leaves the stored read timestamp alone
    instead of clearing it. That is the opposite of how NULL behaves in most expressions, and
    it is a property of the database rather than of this code, so it is asserted against a
    real Postgres. If it were ever wrong, every `shell_send` would silently erase the
    evidence that a session is being watched -- and #5 would then reap sessions mid-build.
    """
    _host(registry)
    registry.upsert_session(_session(last_read_at=T1))
    # A subsequent SEND: advances last_activity_at, says nothing about reads.
    registry.upsert_session(_session(last_activity_at=T2, last_read_at=None))

    row = registry.get_session("h1:build")
    assert row is not None
    assert row.last_activity_at == T2, "a send must advance last_activity_at"
    assert row.last_read_at == T1, (
        "a send carried last_read_at=None and CLEARED the read timestamp; GREATEST must "
        "ignore the NULL and preserve T1"
    )


def test_last_read_at_never_moves_backwards(registry: PostgresRegistry) -> None:
    """Same GREATEST guarantee the other timestamps get: a delayed write cannot rewind it."""
    _host(registry)
    registry.upsert_session(_session(last_read_at=T2))
    registry.upsert_session(_session(last_read_at=T0))
    row = registry.get_session("h1:build")
    assert row is not None
    assert row.last_read_at == T2


def test_the_two_activity_columns_advance_independently(registry: PostgresRegistry) -> None:
    """The whole point of two columns: a session being READ but not DRIVEN is distinguishable
    from one being driven, which is the distinction #5 needs and one column cannot express."""
    _host(registry)
    registry.upsert_session(_session(last_activity_at=T0, last_read_at=None))
    registry.upsert_session(_session(last_activity_at=T0, last_read_at=T2))

    row = registry.get_session("h1:build")
    assert row is not None
    assert (row.last_activity_at, row.last_read_at) == (T0, T2), (
        "reads advanced while sends did not -- a watched-but-idle session"
    )
