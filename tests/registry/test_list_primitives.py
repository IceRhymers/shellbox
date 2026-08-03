"""The inventory read primitives (`W28`): `list_hosts` and `list_sessions`.

These are what the App's `/api/hosts` and `/api/sessions` render, and two properties are
worth more than the round-trip:

1. **`owner_email` filters a display, never authorization.** Omitting it returns every
   row, including rows belonging to other owners. That is not a leak, it is the design:
   the App is open to every workspace user, and decision D5 of the epic
   (https://github.com/IceRhymers/shellbox/issues/9) forbids deriving a permission from a
   viewer's email. The test asserting the *unfiltered* call returns other owners' rows is
   what fails if someone later "fixes" the default to be owner-scoped. That change would
   make the parameter look like an access control, and invite a caller to use it as one.
2. **A NULL `sandbox_id` is a first-class row.** A host enrolled but never bootstrapped
   has one, and `sandbox_id` is the only human-meaningful label a `hosts` row carries
   (`host_id` is an opaque uuid4). A list primitive that dropped or choked on those rows
   would hide exactly the hosts an operator most needs to see.

Ordering is asserted because a display refreshes: an unordered query lets Postgres return
rows in physical order, which reshuffles the list between refreshes.
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

ALICE = "alice@example.com"
BOB = "bob@example.com"


def _host(registry: PostgresRegistry, host_id: str, owner: str, **overrides: object) -> None:
    fields: dict[str, object] = {
        "host_id": host_id,
        "kind": "lakebox",
        "owner_email": owner,
        "last_seen_at": T0,
        "status": "active",
        "enrolled_at": T0,
    }
    fields.update(overrides)
    registry.upsert_host(HostRecord(**fields))  # type: ignore[arg-type]


def _session(
    registry: PostgresRegistry, session_id: str, host_id: str, owner: str, **overrides: object
) -> None:
    fields: dict[str, object] = {
        "session_id": session_id,
        "host_id": host_id,
        "tmux_name": session_id,
        "owner_email": owner,
        "last_activity_at": T0,
        "status": "live",
        "created_at": T0,
    }
    fields.update(overrides)
    registry.upsert_session(SessionRecord(**fields))  # type: ignore[arg-type]


# --------------------------------------------------------------------------------------
# list_hosts
# --------------------------------------------------------------------------------------
def test_list_hosts_on_an_empty_table_returns_empty(registry: PostgresRegistry) -> None:
    assert registry.list_hosts() == []
    assert registry.list_hosts(ALICE) == []


def test_list_hosts_unfiltered_returns_every_owners_rows(registry: PostgresRegistry) -> None:
    """CRITICAL: The default is DEFAULT-OPEN, and that is the design, not an oversight.

    If this ever fails because the default became owner-scoped, the parameter has turned
    into something that looks like an access control. That is the precise failure the
    display-only rule exists to prevent: the next caller reads it as one, and builds a
    permission check on a value the viewer's own proxy header supplies. See the module
    docstring for the epic decision this holds.
    """
    _host(registry, "h-alice", ALICE)
    _host(registry, "h-bob", BOB)

    assert {r.host_id for r in registry.list_hosts()} == {"h-alice", "h-bob"}


def test_list_hosts_filters_to_one_owner(registry: PostgresRegistry) -> None:
    _host(registry, "h-alice-1", ALICE)
    _host(registry, "h-alice-2", ALICE)
    _host(registry, "h-bob", BOB)

    assert {r.host_id for r in registry.list_hosts(ALICE)} == {"h-alice-1", "h-alice-2"}
    assert {r.host_id for r in registry.list_hosts(BOB)} == {"h-bob"}


def test_list_hosts_filter_takes_no_caller_identity(registry: PostgresRegistry) -> None:
    """The filter is a value, not a subject: asking for another owner's rows returns them.

    Nothing in this layer knows who is calling, so there is nothing here that *could*
    authorize. The assertion is the observable half of that: `BOB` is returned to a query
    naming `BOB` regardless of who ran it.
    """
    _host(registry, "h-bob", BOB)

    rows = registry.list_hosts(BOB)
    assert [r.host_id for r in rows] == ["h-bob"]
    assert rows[0].owner_email == BOB


def test_list_hosts_orders_by_last_seen_at_newest_first(registry: PostgresRegistry) -> None:
    _host(registry, "h-oldest", ALICE, last_seen_at=T0)
    _host(registry, "h-newest", ALICE, last_seen_at=T2)
    _host(registry, "h-middle", ALICE, last_seen_at=T1)

    assert [r.host_id for r in registry.list_hosts()] == ["h-newest", "h-middle", "h-oldest"]


def test_list_hosts_returns_a_host_with_a_null_sandbox_id(registry: PostgresRegistry) -> None:
    """A host enrolled but never bootstrapped has no `sandbox_id`. It is still a host.

    First-class, not an edge case: `sandbox_id` is the only human-meaningful label on a
    `hosts` row, so a primitive that dropped these rows would hide the hosts whose state
    most needs looking at. `None` must arrive as `None` -- never as the string "None",
    which is what a display would then render.
    """
    _host(registry, "h-never-bootstrapped", ALICE, sandbox_id=None)
    _host(registry, "h-bootstrapped", ALICE, sandbox_id="sbx-1", last_seen_at=T1)

    by_id = {r.host_id: r for r in registry.list_hosts()}
    assert set(by_id) == {"h-never-bootstrapped", "h-bootstrapped"}
    assert by_id["h-never-bootstrapped"].sandbox_id is None
    assert by_id["h-bootstrapped"].sandbox_id == "sbx-1"


def test_list_hosts_null_sandbox_id_survives_the_owner_filter(registry: PostgresRegistry) -> None:
    """The filter is on `owner_email`; a NULL in an unrelated column must not exclude a row.

    Worth its own test rather than folding into the one above: a filtered query is a
    different statement, and this is the one an "only mine" display runs.
    """
    _host(registry, "h-never-bootstrapped", ALICE, sandbox_id=None)

    rows = registry.list_hosts(ALICE)
    assert [r.host_id for r in rows] == ["h-never-bootstrapped"]
    assert rows[0].sandbox_id is None


# --------------------------------------------------------------------------------------
# list_sessions
# --------------------------------------------------------------------------------------
def test_list_sessions_on_an_empty_table_returns_empty(registry: PostgresRegistry) -> None:
    assert registry.list_sessions() == []
    assert registry.list_sessions(ALICE) == []


def test_list_sessions_is_flat_across_hosts(registry: PostgresRegistry) -> None:
    """What `list_sessions_for_host` cannot answer, and why this primitive exists."""
    _host(registry, "h1", ALICE)
    _host(registry, "h2", ALICE)
    _session(registry, "h1:build", "h1", ALICE)
    _session(registry, "h2:test", "h2", ALICE)

    assert {r.session_id for r in registry.list_sessions()} == {"h1:build", "h2:test"}
    assert {r.session_id for r in registry.list_sessions_for_host("h1")} == {"h1:build"}


def test_list_sessions_unfiltered_returns_every_owners_rows(registry: PostgresRegistry) -> None:
    """The same default-open property as `list_hosts`. See that test for why it matters."""
    _host(registry, "h1", ALICE)
    _session(registry, "h1:alice", "h1", ALICE)
    _session(registry, "h1:bob", "h1", BOB)

    assert {r.session_id for r in registry.list_sessions()} == {"h1:alice", "h1:bob"}


def test_list_sessions_filters_on_the_sessions_own_owner_not_the_hosts(
    registry: PostgresRegistry,
) -> None:
    """`sessions.owner_email` is its own column, and the two owners can differ.

    A session on Alice's host belonging to Bob is a real row: `sessions` carries an owner
    independently of `hosts`. Filtering on the host's owner instead would show Bob a
    session he does not own and hide it from the one he does.
    """
    _host(registry, "h-alice", ALICE)
    _session(registry, "h-alice:bobs-work", "h-alice", BOB)
    _session(registry, "h-alice:alices-work", "h-alice", ALICE)

    assert {r.session_id for r in registry.list_sessions(BOB)} == {"h-alice:bobs-work"}
    assert {r.session_id for r in registry.list_sessions(ALICE)} == {"h-alice:alices-work"}


def test_list_sessions_orders_by_last_activity_at_newest_first(
    registry: PostgresRegistry,
) -> None:
    _host(registry, "h1", ALICE)
    _session(registry, "h1:oldest", "h1", ALICE, last_activity_at=T0)
    _session(registry, "h1:newest", "h1", ALICE, last_activity_at=T2)
    _session(registry, "h1:middle", "h1", ALICE, last_activity_at=T1)

    assert [r.session_id for r in registry.list_sessions()] == [
        "h1:newest",
        "h1:middle",
        "h1:oldest",
    ]


def test_list_sessions_ordering_ignores_last_read_at(registry: PostgresRegistry) -> None:
    """Ordering is on `last_activity_at`, which advances on SEND only.

    The two columns are deliberately separate (`models.py`), so a primitive sorting on the
    wrong one is a silent behaviour change rather than an error. A row with a newer
    `last_read_at` and an older `last_activity_at` is what tells them apart.
    """
    _host(registry, "h1", ALICE)
    _session(registry, "h1:driven", "h1", ALICE, last_activity_at=T2, last_read_at=None)
    _session(registry, "h1:watched", "h1", ALICE, last_activity_at=T0, last_read_at=T2)

    assert [r.session_id for r in registry.list_sessions()] == ["h1:driven", "h1:watched"]


def test_list_sessions_round_trips_the_columns_a_display_needs(
    registry: PostgresRegistry,
) -> None:
    """`cwd`/`cols`/`rows` size a terminal, and a NULL in any of them is legal.

    A session created without them renders at the client's default rather than failing, so
    an unwritten column surfaces as a mis-sized pane instead of an error.
    """
    _host(registry, "h1", ALICE)
    _session(registry, "h1:sized", "h1", ALICE, cwd="/w", cols=120, rows=40, last_activity_at=T2)
    _session(registry, "h1:unsized", "h1", ALICE)

    by_id = {r.session_id: r for r in registry.list_sessions()}
    assert (by_id["h1:sized"].cwd, by_id["h1:sized"].cols, by_id["h1:sized"].rows) == (
        "/w",
        120,
        40,
    )
    assert (by_id["h1:unsized"].cwd, by_id["h1:unsized"].cols, by_id["h1:unsized"].rows) == (
        None,
        None,
        None,
    )
