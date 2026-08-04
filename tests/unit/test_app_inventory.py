"""The inventory endpoints: the NULL ``sandbox_id`` label, and display-versus-authorization.

Two rules are asserted here, and both name a failure that reads as working.

1. **`T-P4-NULL-SANDBOX-ID`.** A host that was never bootstrapped carries a NULL
   ``sandbox_id`` by design -- a sandbox cannot learn its own id, section 1 of
   `docs/sandbox-environment.md`. ``host_id`` is an opaque uuid4, so the absent sandbox id is
   the only thing an inventory row can say about the primary failure mode: a stopped sandbox a
   human must go start. Rendered as an empty cell it looks like a formatting bug; rendered as
   the raw uuid it names something nobody can act on.
2. **`T-P4-DISPLAY-NOT-AUTHZ`.** ``X-Forwarded-Email`` is DISPLAY, never authorization --
   decision D5 of the epic, https://github.com/IceRhymers/shellbox/issues/9. The endpoints
   return the SAME rows whatever the header says, and differ only in what is labelled as the
   viewer's. The risk this guards is a silent authorization bypass built on a value the
   viewer's own proxy header supplies.

The routes are driven through a real ``TestClient`` rather than by calling the endpoint
functions, because the header binding is half of what rule 2 is about. ``TestClient`` is used
WITHOUT its context manager on purpose: entering it runs the lifespan handler, which starts the
30-minute prober, and no unit test wants a background task.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from shellbox_app.database import AppDatabase
from shellbox_app.inventory import (
    NOT_BOOTSTRAPPED_LABEL,
    host_payload,
    hosts_payload,
    sandbox_label,
    session_payload,
    sessions_payload,
)
from shellbox_app.ready import REASON_NO_DATABASE, REASON_QUERY_FAILED
from shellbox_app.server import build_app
from shellbox_registry import HostRecord, NullRegistry, SessionRecord

# Two owners, so "the same rows regardless of the header" has something to be wrong about. A
# test with one owner would pass on an implementation that filtered, because the filtered set
# and the full set would be equal.
OWNER = "owner@example.com"
OTHER = "someone-else@example.com"

SEEN = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)

# Both inventory routes and the collection each one returns. Every rule in this file applies to
# both, and parametrising is what stops one route quietly acquiring a different answer.
BOTH_ROUTES = [("/api/hosts", "hosts"), ("/api/sessions", "sessions")]

BOOTSTRAPPED = HostRecord(
    host_id="00000000-0000-4000-8000-000000000001",
    kind="databricks-sandbox",
    owner_email=OWNER,
    last_seen_at=SEEN,
    status="active",
    sandbox_id="sbx-bootstrapped",
)

# The harder half of `A7`, and debt 4's rendering case.
NEVER_BOOTSTRAPPED = HostRecord(
    host_id="00000000-0000-4000-8000-000000000002",
    kind="databricks-sandbox",
    owner_email=OTHER,
    last_seen_at=SEEN,
    status="stopped",
    sandbox_id=None,
)

A_SESSION = SessionRecord(
    session_id="00000000-0000-4000-8000-00000000000a",
    host_id=BOOTSTRAPPED.host_id,
    tmux_name="shellbox-demo",
    owner_email=OWNER,
    last_activity_at=SEEN,
    status="live",
    cols=120,
    rows=40,
)

ANOTHER_SESSION = SessionRecord(
    session_id="00000000-0000-4000-8000-00000000000b",
    host_id=NEVER_BOOTSTRAPPED.host_id,
    tmux_name="shellbox-other",
    owner_email=OTHER,
    last_activity_at=SEEN,
    status="idle",
)


class StubRegistry:
    """A registry over fixed rows that records the arguments the routes pass it.

    ``list_hosts`` and ``list_sessions`` record their ``owner_email`` argument, which is what
    lets `T-P4-DISPLAY-NOT-AUTHZ` assert the stronger property: not merely that the row sets
    match, but that the route never passed the header down as a filter at all. Comparing
    outputs alone would pass on an implementation that filtered and then re-added the missing
    rows.
    """

    def __init__(
        self, hosts: list[HostRecord] | None = None, sessions: list[SessionRecord] | None = None
    ) -> None:
        self.hosts = [] if hosts is None else hosts
        self.sessions = [] if sessions is None else sessions
        self.filters: list[str | None] = []

    def upsert_host(self, record: HostRecord) -> None:  # pragma: no cover - never called
        raise AssertionError("the App holds SELECT and nothing else")

    def upsert_session(self, record: SessionRecord) -> None:  # pragma: no cover - never called
        raise AssertionError("the App holds SELECT and nothing else")

    def get_host(self, host_id: str) -> HostRecord | None:
        return next((row for row in self.hosts if row.host_id == host_id), None)

    def get_session(self, session_id: str) -> SessionRecord | None:
        return next((row for row in self.sessions if row.session_id == session_id), None)

    def list_sessions_for_host(self, host_id: str) -> list[SessionRecord]:
        return [row for row in self.sessions if row.host_id == host_id]

    def list_hosts(self, owner_email: str | None = None) -> list[HostRecord]:
        self.filters.append(owner_email)
        return list(self.hosts)

    def list_sessions(self, owner_email: str | None = None) -> list[SessionRecord]:
        self.filters.append(owner_email)
        return list(self.sessions)


class BrokenRegistry(StubRegistry):
    """A registry whose reads raise, for the degraded-inventory assertions."""

    def list_hosts(self, owner_email: str | None = None) -> list[HostRecord]:
        raise RuntimeError("the endpoint is unreachable")

    def list_sessions(self, owner_email: str | None = None) -> list[SessionRecord]:
        raise RuntimeError("the endpoint is unreachable")


def _client(registry: object) -> TestClient:
    """An app over ``registry``, with NO lifespan run. See this module's docstring."""
    return TestClient(build_app(database=AppDatabase(registry=registry)))  # type: ignore[arg-type]


def _stocked() -> tuple[StubRegistry, TestClient]:
    registry = StubRegistry(
        hosts=[BOOTSTRAPPED, NEVER_BOOTSTRAPPED], sessions=[A_SESSION, ANOTHER_SESSION]
    )
    return registry, _client(registry)


# --------------------------------------------------------------------------------------
# T-P4-NULL-SANDBOX-ID
# --------------------------------------------------------------------------------------


def test_a_host_with_no_sandbox_id_renders_the_stated_label() -> None:
    """T-P4-NULL-SANDBOX-ID. Not an empty string, and not the bare ``host_id``.

    All three clauses are asserted, because the two wrong answers fail differently. An empty
    string is what a naive ``or ""`` produces and it renders as a blank cell. The ``host_id``
    is what a "show something" fallback produces, and it is an opaque uuid4 that names nothing
    a human can go and start.
    """
    label = sandbox_label(NEVER_BOOTSTRAPPED)

    assert label == NOT_BOOTSTRAPPED_LABEL
    assert label != ""
    assert label != NEVER_BOOTSTRAPPED.host_id
    assert NEVER_BOOTSTRAPPED.host_id not in label


def test_a_bootstrapped_host_renders_its_sandbox_id() -> None:
    """The negative control. A label that were always the stated one would pass the test above
    and tell every reader that no host is bootstrapped."""
    assert sandbox_label(BOOTSTRAPPED) == "sbx-bootstrapped"


def test_the_payload_carries_the_label_and_the_raw_null_together() -> None:
    """The client branches on ``sandbox_id`` and prints ``sandbox_label``.

    Both are present so the rule lives in one place. A browser deriving the label itself would
    be a second implementation of it, and the two would drift.
    """
    payload = host_payload(NEVER_BOOTSTRAPPED, viewer_email=None)

    assert payload["sandbox_id"] is None
    assert payload["sandbox_label"] == NOT_BOOTSTRAPPED_LABEL


def test_the_route_returns_both_hosts_with_the_non_bootstrapped_one_labelled() -> None:
    """`A7`, in the unit lane. One bootstrapped host and one that never was, through the route.

    This is the shape the live check against the provisioned endpoint asserts too. Here it
    costs milliseconds and needs no database.
    """
    _, client = _stocked()

    body = client.get("/api/hosts").json()

    labels = {row["host_id"]: row["sandbox_label"] for row in body["hosts"]}
    assert labels == {
        BOOTSTRAPPED.host_id: "sbx-bootstrapped",
        NEVER_BOOTSTRAPPED.host_id: NOT_BOOTSTRAPPED_LABEL,
    }
    assert body["stale"] is False


# --------------------------------------------------------------------------------------
# T-P4-DISPLAY-NOT-AUTHZ
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("path,collection", BOTH_ROUTES)
def test_the_rows_are_the_same_whatever_the_header_says(path: str, collection: str) -> None:
    """T-P4-DISPLAY-NOT-AUTHZ. The header labels rows. It never selects them.

    Three viewers: the owner of one row, the owner of the other, and no header at all. The row
    identifiers must be identical across all three. `R44` is a silent authorization bypass
    built on a value the viewer's own proxy header supplies, and it arrives by exactly the
    reasonable-looking step of passing the header into the query's ``owner_email`` parameter.
    """
    registry, client = _stocked()
    key = "host_id" if collection == "hosts" else "session_id"

    seen = {
        viewer: [row[key] for row in client.get(path, headers=headers).json()[collection]]
        for viewer, headers in (
            (OWNER, {"X-Forwarded-Email": OWNER}),
            (OTHER, {"X-Forwarded-Email": OTHER}),
            ("absent", {}),
        )
    }

    assert len(set(map(tuple, seen.values()))) == 1, (
        f"the rows changed with the viewer: {seen}. X-Forwarded-Email is display, never "
        "authorization -- see decision D5"
    )
    assert len(next(iter(seen.values()))) == 2, "the fixture must hold rows from two owners"
    assert registry.filters == [None, None, None], (
        f"the route passed {registry.filters} as the query's owner filter. That parameter "
        "filters a display and the routes must not pass the viewer's header into it"
    )


@pytest.mark.parametrize("path,collection", BOTH_ROUTES)
def test_only_the_mine_label_moves_with_the_header(path: str, collection: str) -> None:
    """The other half of the criterion: the endpoints DO differ, in the label and only there.

    Without this, the test above is satisfied by an implementation that ignores the header
    entirely -- which is a different bug and would leave a viewer unable to find their own
    rows.
    """
    _, client = _stocked()

    def mine_for(viewer: str) -> list[str]:
        body = client.get(path, headers={"X-Forwarded-Email": viewer}).json()
        assert body["viewer_email"] == viewer
        owner = "owner_email"
        return sorted(row[owner] for row in body[collection] if row["mine"])

    assert mine_for(OWNER) == [OWNER]
    assert mine_for(OTHER) == [OTHER]

    anonymous = client.get(path).json()
    assert anonymous["viewer_email"] is None
    assert [row for row in anonymous[collection] if row["mine"]] == []


def test_the_mine_label_ignores_the_case_the_edge_chose() -> None:
    """A viewer whose header differs only in case still finds their own rows.

    This repo has no measurement of how the edge cases an address, and an exact match would
    silently label a viewer's own rows as somebody else's -- the failure mode that reads as
    working.
    """
    _, client = _stocked()

    body = client.get("/api/hosts", headers={"X-Forwarded-Email": OWNER.upper()}).json()

    assert [row["owner_email"] for row in body["hosts"] if row["mine"]] == [OWNER]


# --------------------------------------------------------------------------------------
# The registry stays non-fatal -- ADR-3 and `R7`
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("path,collection", BOTH_ROUTES)
def test_a_failed_read_degrades_the_inventory_and_never_the_route(
    path: str, collection: str
) -> None:
    """A read that raises is a 200 carrying ``stale``, never a 500 and never an exception.

    A degraded inventory, never a degraded relay. The status stays 200 for the reason
    `packages/shellbox-app/src/shellbox_app/ready.py` gives: the Apps edge answers an
    unauthenticated request with HTTP 200 carrying an HTML login body, so a status code from
    this App is not a signal a caller can trust.
    """
    client = _client(BrokenRegistry())

    response = client.get(path)

    assert response.status_code == 200
    assert response.json() == {
        "viewer_email": None,
        collection: [],
        "stale": True,
        "reason": REASON_QUERY_FAILED,
    }


@pytest.mark.parametrize("path,collection", BOTH_ROUTES)
def test_an_unconfigured_app_reports_stale_rather_than_empty(path: str, collection: str) -> None:
    """The distinction an empty list alone cannot carry.

    ``NullRegistry`` returns ``[]`` and never raises, so an App that resolved no endpoint
    serves an inventory identical to a real registry holding no rows. That is the deploy
    failure `ready.py` exists to catch, arriving on a different route. ``stale`` is what
    separates "nothing to show" from "cannot see anything".
    """
    client = _client(NullRegistry())

    body = client.get(path).json()

    assert body[collection] == []
    assert body["stale"] is True
    assert body["reason"] == REASON_NO_DATABASE


def test_a_healthy_read_is_not_marked_stale() -> None:
    """The negative control for ``stale``. Always-true would satisfy both tests above."""
    registry, client = _stocked()

    assert client.get("/api/hosts").json()["stale"] is False
    assert client.get("/api/sessions").json()["stale"] is False
    assert "reason" not in client.get("/api/hosts").json()


# --------------------------------------------------------------------------------------
# The payload's shape
# --------------------------------------------------------------------------------------


def test_the_host_payload_is_the_display_subset_and_not_the_whole_row() -> None:
    """The payload is pinned to an exact key set, so a new column cannot arrive by reflection.

    Three columns are excluded for three different reasons, and all are in the docstring of
    `host_payload` in `packages/shellbox-app/src/shellbox_app/inventory.py`. ``tmux_socket`` is
    a path inside somebody's sandbox, ``gateway_host`` names an address this App measured
    itself unable to reach, and ``enrolled_at`` is bookkeeping no display sorts on.

    Asserted as equality rather than as three absences. The tempting way to build these
    payloads is to reflect every field of the record, and an absence test would still pass on a
    payload that grew a fourth column nobody decided to publish.
    """
    record = HostRecord(
        host_id=BOOTSTRAPPED.host_id,
        kind=BOOTSTRAPPED.kind,
        owner_email=OWNER,
        last_seen_at=SEEN,
        status="active",
        sandbox_id="sbx-bootstrapped",
        gateway_host="gateway.example.invalid",
        tmux_socket="/tmp/shellbox/default",  # noqa: S108 -- a fixture value, never a path used
        enrolled_at=SEEN,
    )

    assert set(host_payload(record, viewer_email=None)) == {
        "host_id",
        "sandbox_id",
        "sandbox_label",
        "kind",
        "owner_email",
        "mine",
        "status",
        "last_seen_at",
    }


def test_the_session_payload_is_the_display_subset_too() -> None:
    """``last_read_at`` is excluded, and that exclusion is the one worth a test.

    Which predicates may read that column is explicitly a Phase 5 semantic (section 5 of
    `docs/lakebase-handoff.md`), and `W28` recorded the trap: sorting on it would let the act
    of displaying a session reorder the display. Publishing it invites exactly that.
    """
    record = SessionRecord(
        session_id=A_SESSION.session_id,
        host_id=A_SESSION.host_id,
        tmux_name=A_SESSION.tmux_name,
        owner_email=OWNER,
        last_activity_at=SEEN,
        status="live",
        cwd="/home/agent",
        cols=120,
        rows=40,
        created_at=SEEN,
        last_read_at=SEEN,
    )

    assert set(session_payload(record, viewer_email=None)) == {
        "session_id",
        "host_id",
        "tmux_name",
        "owner_email",
        "mine",
        "status",
        "last_activity_at",
        "cwd",
        "cols",
        "rows",
    }


def test_the_payloads_are_json_serialisable_timestamps() -> None:
    """Timestamps cross the wire as ISO 8601 with the offset the ``timestamptz`` column kept."""
    database = AppDatabase(registry=StubRegistry(hosts=[BOOTSTRAPPED], sessions=[A_SESSION]))

    hosts = hosts_payload(database, viewer_email=None)
    sessions = sessions_payload(database, viewer_email=None)

    assert hosts["hosts"][0]["last_seen_at"] == SEEN.isoformat()  # type: ignore[index]
    assert sessions["sessions"][0]["last_activity_at"] == SEEN.isoformat()  # type: ignore[index]
