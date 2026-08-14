"""W39, `A21`/`A38` -- `Registry.touch_read` against real Postgres.

`A21`: a `touch_read` call advances `last_read_at` and leaves `last_activity_at` untouched --
the two columns advance independently, matching `test_upsert.py`'s split of the same two
columns (OQ5), now exercised through the READ primitive itself rather than through
`upsert_session`.

`A38`: `touch_read` called for a `session_id` with NO existing row matches zero rows, and the
table stays empty -- no row is created. This is the hazard `base.py`'s `touch_read` docstring
names: an upsert-shaped `touch_read` would create the row it failed to find, and every session
an agent reads on a no-database host would then acquire a registry row, defeating `ADR-36`'s
"a session with no registry row is never reaped" rule.

These live in the `registry` lane, not the unit lane, because both properties are Postgres's:
how many rows an `UPDATE ... WHERE` matches, and how `GREATEST` treats the resulting write. A
fake registry cannot fail either assertion.
"""

from __future__ import annotations

import datetime as dt

import pytest
from shellbox_mcp.enroll import reconcile_orphans
from shellbox_mcp.reaper import Reaper
from shellbox_mcp.tmux import SessionRecord as TmuxSessionRecord
from shellbox_registry.base import HostRecord, SessionRecord
from shellbox_registry.postgres import PostgresRegistry

pytestmark = pytest.mark.registry

T0 = dt.datetime(2026, 7, 31, 12, 0, 0, tzinfo=dt.UTC)
T1 = dt.datetime(2026, 7, 31, 12, 5, 0, tzinfo=dt.UTC)
NOW_FOR_SWEEP = T0 + dt.timedelta(hours=1)


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


def _session(registry: PostgresRegistry, **overrides: object) -> None:
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
    registry.upsert_session(SessionRecord(**fields))  # type: ignore[arg-type]


def test_touch_read_advances_last_read_at_and_leaves_last_activity_at_unchanged(
    registry: PostgresRegistry,
) -> None:
    """A21. `shell_read` calling `touch_read` must not make a read look like a send: the two
    columns are Phase 2's whole point (`base.py`'s `last_read_at` docstring), and a reaping
    predicate that cannot tell "watched" from "driven" is the thing this split exists to
    prevent."""
    _host(registry)
    _session(registry)

    registry.touch_read("h1:build", T1)

    row = registry.get_session("h1:build")
    assert row is not None
    assert row.last_read_at == T1
    assert row.last_activity_at == T0, "a read must not advance the SEND column"


def test_touch_read_never_moves_last_read_at_backwards(registry: PostgresRegistry) -> None:
    """The same GREATEST guarantee `upsert_session`'s columns get: a delayed `touch_read`
    call must not rewind the timestamp."""
    _host(registry)
    _session(registry, last_read_at=T1)

    registry.touch_read("h1:build", T0)

    row = registry.get_session("h1:build")
    assert row is not None
    assert row.last_read_at == T1


def test_touch_read_for_a_session_with_no_row_matches_zero_rows_and_creates_none(
    registry: PostgresRegistry,
) -> None:
    """A38. CRITICAL: this is the entire reason `touch_read` is an `UPDATE ... WHERE` and not
    an upsert -- see `base.py`'s docstring and the comment beside the statement in
    `postgres.py`. `sessions.host_id` is a NOT NULL foreign key, so a bare INSERT here would
    fail loudly; an upsert built from `shell_read`'s own knowledge would not -- it would
    succeed and create exactly the row this test proves does not exist.
    """
    registry.touch_read("no-such-session", T1)

    assert registry.get_session("no-such-session") is None
    assert registry.list_sessions() == [], "touch_read must never create a row"


# --------------------------------------------------------------------------------------
# `A26` (non-terminal half) -- `W41`'s reaper write, against real Postgres.
#
# The tmux side is a FAKE here on purpose: this half's claim is about the WRITE, which
# `sessions_status_chk` (`models.py:87-89`) and `upsert_session`'s `GREATEST` on
# `last_activity_at` (`postgres.py:147-149`) make real only against real Postgres -- no fake
# registry carries either property. The tmux-side evidence-gathering is already the tmux
# lane's job (`A35`, `A42`, `A43`, `A48`, `tests/tmux/test_reap_activity.py`).
# --------------------------------------------------------------------------------------


class _SingleSessionAdapter:
    """One unattached, aged candidate. No client attached, an old `window_activity_max`."""

    def __init__(self, tmux_name: str, *, window_activity: int) -> None:
        self.tmux_name = tmux_name
        self.window_activity = window_activity
        self.killed: list[str] = []

    def list_sessions(self) -> list[TmuxSessionRecord]:
        return [
            TmuxSessionRecord(
                tmux_name=self.tmux_name,
                created_at=0,
                last_activity_at=0,
                cols=80,
                rows=24,
                alive=True,
                incarnation=f"inc-{self.tmux_name}",
                cwd=None,
                stamped_cwd=None,
            )
        ]

    def session_attached(self, name: str) -> bool | None:
        return False

    def window_activity_max(self, name: str) -> int | None:
        return self.window_activity

    def kill(self, name: str) -> bool:
        self.killed.append(name)
        return True


def test_an_unattended_session_past_the_timeout_is_reaped_with_last_activity_at_unchanged(
    registry: PostgresRegistry,
) -> None:
    """A26 (non-terminal half). T-P5-REAP-UNATTENDED.

    A real sweep moves the row to `reaped`, and -- the point real Postgres proves -- the
    reaped row's `last_activity_at` is the value it held BEFORE the reap, unchanged, never
    `now`. `W41`'s own constraint 2: `upsert_session`'s `GREATEST` would make a `now` written
    here permanent, since it can only ever move a column up, never back down.
    """
    _host(registry)
    original_activity = T0
    _session(registry, last_activity_at=original_activity)

    adapter = _SingleSessionAdapter("build", window_activity=int(T0.timestamp()))
    reaper = Reaper(
        registry,
        lambda: adapter,
        host_id="h1",
        timeout=60,
        interval=9999,
        now=lambda: NOW_FOR_SWEEP,
    )
    reaper.sweep()

    assert adapter.killed == ["build"]
    row = registry.get_session("h1:build")
    assert row is not None
    assert row.status == "reaped"
    assert row.last_activity_at == original_activity, (
        "the reap must carry the STORED last_activity_at through unchanged, never write now()"
    )


# --------------------------------------------------------------------------------------
# `A26` (terminal half) -- `W48`'s widened guard, against real Postgres.
#
# The tmux side is a FAKE here for the same reason the non-terminal half above is: this
# claim is about `reconcile_orphans`'s SQL-adjacent behaviour (does the row change, per
# `sessions_status_chk`), not about tmux evidence-gathering.
# --------------------------------------------------------------------------------------


class _NoSessionsAdapter:
    """Nothing on the tmux server -- as if the session's server died, or the session was
    already reaped and is simply gone."""

    def list_sessions(self) -> list[TmuxSessionRecord]:
        return []


def test_a_reaped_row_is_left_reaped_by_reconciliation(registry: PostgresRegistry) -> None:
    """A26 (terminal half). T-P5-REAPED-TERMINAL.

    `R72`/`W48`: without the widened guard at `enroll.py:318`, the very next reconciliation
    pass after a reap finds the session absent from `live` -- the reaper just killed it -- and
    rewrites the row `orphaned`, erasing the one fact it was carrying: shellbox ended this
    session ON PURPOSE. `reaped` must be TERMINAL, exactly like `orphaned` already is.
    """
    _host(registry)
    _session(registry, status="reaped")

    changed = reconcile_orphans(
        registry,
        _NoSessionsAdapter(),
        host_id="h1",
        owner_email="a@example.com",
        expected_socket="irrelevant -- the hosts row here carries no tmux_socket",
    )

    assert changed == 0
    row = registry.get_session("h1:build")
    assert row is not None
    assert row.status == "reaped", "reconciliation must never relabel a reaped row orphaned"


# --------------------------------------------------------------------------------------
# `A47` -- `status` is not a predicate term, against real Postgres.
# --------------------------------------------------------------------------------------


def test_an_orphaned_row_past_the_timeout_is_reaped_like_any_other_status(
    registry: PostgresRegistry,
) -> None:
    """A47. T-P5-ORPHANED-ALIVE.

    `_candidates`'s own comment (`reaper.py:82-85`): `status` is not read as a filter term --
    a row reading `orphaned` is still a candidate if its session is still on tmux, and it is
    neither vetoed nor accelerated by that status. This plants a row reconciliation already
    marked `orphaned` while its (fake) tmux session is still alive and past the idle timeout;
    a real sweep reaps it exactly as it would a `live` row, and the write lands as `reaped` --
    proving the orphaned -> reaped transition is real against `sessions_status_chk`, not just
    permitted by a fake registry.
    """
    _host(registry)
    original_activity = T0
    _session(registry, status="orphaned", last_activity_at=original_activity)

    adapter = _SingleSessionAdapter("build", window_activity=int(T0.timestamp()))
    reaper = Reaper(
        registry,
        lambda: adapter,
        host_id="h1",
        timeout=60,
        interval=9999,
        now=lambda: NOW_FOR_SWEEP,
    )
    reaper.sweep()

    assert adapter.killed == ["build"], "an orphaned-but-still-alive session must still be reaped"
    row = registry.get_session("h1:build")
    assert row is not None
    assert row.status == "reaped"
