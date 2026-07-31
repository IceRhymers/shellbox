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
