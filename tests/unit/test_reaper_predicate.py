"""W39, `A22` -- `touch_read`'s own contract, at the registry-primitive level.

W41 builds the actual reaping predicate on top of this column; this file is not that. It
asserts the narrower, permanent thing `touch_read` itself must guarantee: it is an
``UPDATE ... WHERE``, never an upsert/insert-shaped call, and the mechanism it uses
(``GREATEST``) is the one that lets a NULL ``last_read_at`` stay an "absent term" -- not
``now()`` and not epoch -- until a real read happens.

The Postgres-specific half of that (a zero-row match creates no row; ``GREATEST`` ignores
NULLs) is Postgres's own behavior and belongs in the ``registry`` lane against real Postgres
(``tests/registry/test_reaper_registry.py``, `A21`/`A38`). This file needs no database: it
captures the statement `PostgresRegistry.touch_read` builds, without executing it against
anything, which is enough to prove the statement is UPDATE-shaped and never INSERT-shaped.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from shellbox_registry.null import NullRegistry
from shellbox_registry.postgres import PostgresRegistry
from sqlalchemy import Insert, Update
from sqlalchemy.dialects import postgresql

NOW = dt.datetime(2026, 7, 31, tzinfo=dt.UTC)


class _CapturingConnection:
    """Stands in for the `with self._engine.begin() as conn:` block in `touch_read`, so the
    statement it builds can be inspected without a real database behind it."""

    def __init__(self) -> None:
        self.executed: list[Any] = []

    def execute(self, stmt: Any) -> None:
        self.executed.append(stmt)

    def __enter__(self) -> _CapturingConnection:
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False


class _CapturingEngine:
    def __init__(self) -> None:
        self.conn = _CapturingConnection()

    def begin(self) -> _CapturingConnection:
        return self.conn


def _registry_with_capture() -> tuple[PostgresRegistry, _CapturingEngine]:
    engine = _CapturingEngine()
    # `dsn` is never used when `engine` is supplied -- see `PostgresRegistry.__init__` -- so
    # this connects to nothing, the same trick `test_null_registry.py`'s
    # `test_create_registry_postgres_dsn_returns_postgres_registry` relies on.
    registry = PostgresRegistry("postgresql://example.invalid/db", engine=engine)  # type: ignore[arg-type]
    return registry, engine


def test_touch_read_issues_exactly_one_update_never_an_insert() -> None:
    """The hazard `base.py`'s `touch_read` docstring names: an INSERT here would create the
    row it failed to find, and every read-only session on a no-database host would then
    acquire a registry row -- silently defeating ADR-36's "no row, never reaped" rule."""
    registry, engine = _registry_with_capture()

    registry.touch_read("s1", NOW)

    assert len(engine.conn.executed) == 1
    stmt = engine.conn.executed[0]
    assert isinstance(stmt, Update)
    assert not isinstance(stmt, Insert)


def test_touch_read_where_clause_names_the_session_and_nothing_else() -> None:
    """An UPDATE with no WHERE clause would touch every row on the table; the statement must
    be scoped to exactly the session it was called for."""
    registry, engine = _registry_with_capture()

    registry.touch_read("s1", NOW)

    compiled = str(engine.conn.executed[0].compile(dialect=postgresql.dialect()))
    assert compiled.startswith("UPDATE sessions")
    assert "WHERE sessions.session_id" in compiled


def test_touch_read_writes_last_read_at_through_greatest() -> None:
    """The mechanism that makes a NULL `last_read_at` an "absent term" rather than `now()` or
    epoch: Postgres's `GREATEST` ignores NULLs, so the first read stores `when` and a NULL
    column is never treated as a real, comparable timestamp by SQL arithmetic elsewhere.
    Also the same mechanism `upsert_session` uses for `last_activity_at` (`postgres.py`), so a
    delayed `touch_read` call can never move the timestamp backwards either.
    """
    registry, engine = _registry_with_capture()

    registry.touch_read("s1", NOW)

    compiled = str(engine.conn.executed[0].compile(dialect=postgresql.dialect()))
    assert "greatest(" in compiled.lower()
    assert "last_read_at" in compiled


def test_null_registry_touch_read_never_raises_and_creates_no_row() -> None:
    """`NullRegistry` matches every other method here (`null.py`): it accepts the call and
    does nothing, so `touch_read` is safe to call unconditionally on a no-database host --
    the whole point of ADR-3's degrade-not-fail design."""
    reg = NullRegistry()

    assert reg.touch_read("s1", NOW) is None
    assert reg.get_session("s1") is None
