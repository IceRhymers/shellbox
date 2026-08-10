"""The ``Registry`` protocol.

Every write is an UPSERT keyed on a natural id (`host_id` / `session_id`), because
enrollment (W7's `identity.py`/`enroll.py`) runs this on every start and reconciliation
pass, not once. `NullRegistry` (`null.py`) and `PostgresRegistry` (`postgres.py`) both
satisfy this protocol; callers must be able to swap one for the other without changing
behavior beyond "the inventory is/isn't persisted" (ADR-3).

Registry failures must never break a caller: a shell tool call succeeds whether or not
the registry write behind it succeeded (§9 "Registry write fails while tmux succeeds").
`PostgresRegistry` methods raise on failure so a caller can log a `registry_warning`;
`NullRegistry` methods never raise.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True, slots=True)
class HostRecord:
    host_id: str
    kind: str
    owner_email: str
    last_seen_at: datetime
    status: str
    sandbox_id: str | None = None
    gateway_host: str | None = None
    tmux_socket: str | None = None
    enrolled_at: datetime | None = None
    """Only used on first insert; an existing row's ``enrolled_at`` is preserved on
    conflict (E4: "first enrollment wins"). Defaults to ``last_seen_at`` if omitted."""


@dataclass(frozen=True, slots=True)
class SessionRecord:
    session_id: str
    host_id: str
    tmux_name: str
    owner_email: str
    last_activity_at: datetime
    status: str
    cwd: str | None = None
    cols: int | None = None
    rows: int | None = None
    created_at: datetime | None = None
    """Only used on first insert; an existing row's ``created_at`` is preserved on
    conflict, mirroring hosts.enrolled_at. Defaults to ``last_activity_at`` if omitted."""
    last_read_at: datetime | None = None
    """When the pane was last *read*. ``last_activity_at`` advances on send only, so the two
    together let #5 choose a reaping predicate that Phase 2 does not pre-decide (see
    ``models.py``).

    ``None`` means "this write is not a read" — **not** "never read". An `upsert_session`
    carrying ``None`` must therefore leave an existing timestamp alone rather than clearing
    it, which is what `PostgresRegistry` relies on ``GREATEST`` for."""


class Registry(Protocol):
    """Everything a caller needs from the registry layer. Deliberately narrow: this is
    the set of primitives W6 owns. Enrollment sequencing (E1-E7) and orphan
    reconciliation policy belong to W7's `identity.py`/`enroll.py`, built on top of
    these primitives."""

    def upsert_host(self, record: HostRecord) -> None:
        """Insert or update a `hosts` row.

        On conflict: ``last_seen_at`` is set to ``GREATEST(excluded.last_seen_at,
        hosts.last_seen_at)`` so a delayed/stale heartbeat can never move the timestamp
        backwards; ``enrolled_at`` is preserved from the existing row.
        """
        ...

    def upsert_session(self, record: SessionRecord) -> None:
        """Insert or update a `sessions` row.

        On conflict: ``last_activity_at`` is set to ``GREATEST(excluded.last_activity_at,
        sessions.last_activity_at)`` for the same reason as ``upsert_host``;
        ``created_at`` is preserved from the existing row.
        """
        ...

    def get_host(self, host_id: str) -> HostRecord | None:
        """Return the current `hosts` row, or ``None`` if it does not exist."""
        ...

    def get_session(self, session_id: str) -> SessionRecord | None:
        """Return the current `sessions` row, or ``None`` if it does not exist."""
        ...

    def list_sessions_for_host(self, host_id: str) -> list[SessionRecord]:
        """Return every `sessions` row for a host, in no particular order."""
        ...

    # -- inventory reads -------------------------------------------------------------------
    #
    # CRITICAL: `owner_email` on both primitives below is a DISPLAY FILTER, never an
    # authorization decision. The rule is stated on each method too, because a reader who
    # meets one of them without the other must still get the warning.

    def list_hosts(self, owner_email: str | None = None) -> list[HostRecord]:
        """Return `hosts` rows, newest heartbeat first. All of them when ``owner_email``
        is ``None``.

        CRITICAL: ``owner_email`` FILTERS A DISPLAY. It is NEVER an authorization
        decision. That is decision D5 of the epic,
        https://github.com/IceRhymers/shellbox/issues/9, and ``docs/architecture.dot``
        labels the same edge "display only (D5)".
        A caller that omits the filter gets every host, and that is correct. The App is
        open to every workspace user by design, so the inventory is not a per-viewer
        secret. The parameter exists so a UI can offer "mine" next to "all".

        The rule is spelled out because an email parameter on a query looks like an access
        control. A caller that reads it as one then builds a permission check on a value
        the viewer's own proxy header supplies.

        Ordering is by ``last_seen_at`` descending, and it is part of the contract. A
        display refreshes repeatedly. An unordered query lets Postgres return rows in
        whatever physical order it likes, which reshuffles the list on every refresh.
        """
        ...

    def list_sessions(self, owner_email: str | None = None) -> list[SessionRecord]:
        """Return `sessions` rows, most recent activity first. All of them when
        ``owner_email`` is ``None``.

        CRITICAL: ``owner_email`` FILTERS A DISPLAY. It is NEVER an authorization
        decision. See `list_hosts` for the full reasoning; it applies here unchanged.

        The filter reads ``sessions.owner_email``, which is its own column: a session's
        owner and its host's owner can differ.

        Ordering is by ``last_activity_at`` descending, for the same reason `list_hosts`
        orders by ``last_seen_at``. It is ``last_activity_at`` and NOT ``last_read_at``.
        Sorting on reads would let the act of displaying a session reorder the display, if
        a viewer's read ever advanced that column.
        """
        ...
