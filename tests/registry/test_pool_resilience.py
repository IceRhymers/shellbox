"""`pool_pre_ping` against a real Postgres — the setting Lakebase's scale-to-zero needs.

Lakebase suspends compute after an idle timeout, which **kills every pooled connection**.
Without `pool_pre_ping` the next checkout hands back a dead connection and the failure
surfaces on whatever query happened to be next, rather than at connect where it belongs.

WARNING: **This file exists because the obvious version of the test proves nothing.** Killing
*one* backend and asserting the next query works passes with `pool_pre_ping=False` — the
pool simply hands back one of the other live connections. So every test here terminates
**all** backends matching a per-test `application_name`, and the central one carries a
**negative control** asserting it genuinely fails when the flag is off. A resilience test
that passes against the unprotected configuration is not a resilience test.
"""

from __future__ import annotations

import uuid

import pytest
from shellbox_registry.dsn import normalize_postgres_dsn
from sqlalchemy import Engine, create_engine, text

from .conftest import static_dsn_or_skip

pytestmark = pytest.mark.registry


def _engine(*, pre_ping: bool, app_name: str, pool_size: int = 3) -> Engine:
    """An engine whose connections are identifiable, so a test can kill exactly its own.

    Built from a STATIC DSN, which is why this whole file skips on the Lakebase path: it needs
    `pool_pre_ping=False` for its negative control and a per-test ``application_name`` to kill
    only its own backends, and `create_lakebase_engine` exposes neither. See
    `static_dsn_or_skip`, which also records what closing that gap would take -- this file is
    the one that would prove `pool_pre_ping` against a REAL suspend rather than a simulated one.
    """
    return create_engine(
        normalize_postgres_dsn(static_dsn_or_skip()),
        pool_size=pool_size,
        max_overflow=0,
        pool_pre_ping=pre_ping,
        connect_args={"application_name": app_name},
    )


def _fill_pool(engine: Engine, count: int) -> None:
    """Open and return `count` connections, so the pool holds that many idle ones."""
    held = [engine.connect() for _ in range(count)]
    for connection in held:
        connection.close()


def _kill_all_backends(killer: Engine, app_name: str) -> int:
    """Terminate every backend with this `application_name`. Returns how many were killed.

    This is what a scale-to-zero suspend does to a pool: not one connection, all of them.
    """
    with killer.begin() as conn:
        rows = conn.execute(
            text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE application_name = :name AND pid <> pg_backend_pid()"
            ),
            {"name": app_name},
        ).fetchall()
    return len(rows)


def test_pre_ping_survives_every_pooled_connection_being_killed(registry) -> None:  # noqa: ANN001
    """The property, and its negative control in the same test.

    Both halves matter. Without the control, this test would pass against an engine with
    `pool_pre_ping=False` and we would have "verified" nothing — which is exactly how a
    scale-to-zero outage reaches production having been tested.
    """
    killer = registry._engine  # a separate engine, so it survives the massacre

    protected_name = f"sbx-pre-ping-{uuid.uuid4().hex[:8]}"
    protected = _engine(pre_ping=True, app_name=protected_name)
    try:
        _fill_pool(protected, 3)
        killed = _kill_all_backends(killer, protected_name)
        assert killed >= 1, "no backends were terminated, so nothing was actually tested"

        with protected.connect() as conn:
            assert conn.execute(text("SELECT 1")).scalar() == 1
    finally:
        protected.dispose()

    # ---- negative control -------------------------------------------------------------
    unprotected_name = f"sbx-no-ping-{uuid.uuid4().hex[:8]}"
    unprotected = _engine(pre_ping=False, app_name=unprotected_name)
    try:
        _fill_pool(unprotected, 3)
        assert _kill_all_backends(killer, unprotected_name) >= 1

        with pytest.raises(Exception) as caught:  # noqa: PT011 - driver-specific type
            with unprotected.connect() as conn:
                conn.execute(text("SELECT 1"))
        assert caught.value is not None, (
            "an engine WITHOUT pool_pre_ping survived every connection being killed, so "
            "the positive case above proves nothing about pre_ping"
        )
    finally:
        unprotected.dispose()


def test_a_killed_pool_recovers_for_real_work_not_just_select_1(registry) -> None:  # noqa: ANN001
    """`SELECT 1` can pass while a real statement fails, so this asserts on a real table.

    Uses the registry's own schema rather than a scratch table: the thing that must survive
    a scale-to-zero suspend is the inventory write, not an arbitrary query.
    """
    app_name = f"sbx-recover-{uuid.uuid4().hex[:8]}"
    engine = _engine(pre_ping=True, app_name=app_name)
    try:
        _fill_pool(engine, 2)
        assert _kill_all_backends(registry._engine, app_name) >= 1

        with engine.connect() as conn:
            count = conn.execute(text("SELECT count(*) FROM hosts")).scalar()
        assert count is not None
    finally:
        engine.dispose()
