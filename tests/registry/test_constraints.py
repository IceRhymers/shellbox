"""§10: CHECK constraints on `status` are stated explicitly, not just documented in a
comment — prove it by attempting an invalid insert and asserting the failure
(W6 acceptance criterion 4).

Each invalid-insert test wraps the whole `engine.begin()` block in `pytest.raises`, not
just the `execute()` call, so the failed statement's IntegrityError propagates out of
the block and SQLAlchemy rolls the transaction back in `begin()`'s `__exit__` — catching
it earlier would leave the connection in Postgres's aborted-transaction state for the
`begin()` block's own (would-be) COMMIT.
"""

from __future__ import annotations

import datetime as dt

import pytest
from shellbox_registry.models import Host, Session
from sqlalchemy import insert
from sqlalchemy.exc import IntegrityError

pytestmark = pytest.mark.registry

NOW = dt.datetime(2026, 7, 31, tzinfo=dt.UTC)


def _insert_host(conn, **overrides) -> None:
    values = dict(
        host_id="h1",
        kind="lakebox",
        owner_email="a@example.com",
        enrolled_at=NOW,
        last_seen_at=NOW,
        status="active",
    )
    values.update(overrides)
    conn.execute(insert(Host).values(**values))


def test_hosts_status_check_rejects_invalid_value(registry) -> None:
    with pytest.raises(IntegrityError):
        with registry._engine.begin() as conn:
            _insert_host(conn, host_id="h-bad", status="definitely-not-a-real-status")


def test_hosts_status_check_accepts_every_documented_value(registry) -> None:
    for i, status in enumerate(("active", "stale", "stopped")):
        with registry._engine.begin() as conn:
            _insert_host(conn, host_id=f"h-ok-{i}", status=status)


def test_sessions_status_check_rejects_invalid_value(registry) -> None:
    with registry._engine.begin() as conn:
        _insert_host(conn)

    with pytest.raises(IntegrityError):
        with registry._engine.begin() as conn:
            conn.execute(
                insert(Session).values(
                    session_id="s-bad",
                    host_id="h1",
                    tmux_name="s-bad",
                    owner_email="a@example.com",
                    created_at=NOW,
                    last_activity_at=NOW,
                    status="definitely-not-a-real-status",
                )
            )


def test_sessions_status_check_accepts_every_documented_value(registry) -> None:
    with registry._engine.begin() as conn:
        _insert_host(conn)

    for i, status in enumerate(("live", "idle", "reaped", "orphaned")):
        with registry._engine.begin() as conn:
            conn.execute(
                insert(Session).values(
                    session_id=f"s-ok-{i}",
                    host_id="h1",
                    tmux_name=f"s-ok-{i}",
                    owner_email="a@example.com",
                    created_at=NOW,
                    last_activity_at=NOW,
                    status=status,
                )
            )


def test_hosts_owner_email_not_null(registry) -> None:
    with pytest.raises(IntegrityError):
        with registry._engine.begin() as conn:
            conn.execute(
                insert(Host).values(
                    host_id="h-no-owner",
                    kind="lakebox",
                    enrolled_at=NOW,
                    last_seen_at=NOW,
                    status="active",
                )
            )


def test_sessions_owner_email_not_null(registry) -> None:
    with registry._engine.begin() as conn:
        _insert_host(conn)

    with pytest.raises(IntegrityError):
        with registry._engine.begin() as conn:
            conn.execute(
                insert(Session).values(
                    session_id="s-no-owner",
                    host_id="h1",
                    tmux_name="s-no-owner",
                    created_at=NOW,
                    last_activity_at=NOW,
                    status="live",
                )
            )
