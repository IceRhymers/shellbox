"""``NullRegistry`` — the registry layer with `SHELLBOX_DATABASE_URL` unset.

Every method accepts its call and does nothing. This is a design choice, not a
fallback bug (§5): it makes the tool surface fully usable on a laptop with no database
at all, and it means a registry outage degrades shellbox to "shells work, inventory is
stale" rather than "shells are down." No method here ever raises.
"""

from __future__ import annotations

from shellbox_registry.base import HostRecord, Registry, SessionRecord


class NullRegistry(Registry):
    def upsert_host(self, record: HostRecord) -> None:
        return None

    def upsert_session(self, record: SessionRecord) -> None:
        return None

    def get_host(self, host_id: str) -> HostRecord | None:
        return None

    def get_session(self, session_id: str) -> SessionRecord | None:
        return None

    def list_sessions_for_host(self, host_id: str) -> list[SessionRecord]:
        return []

    def list_hosts(self, owner_email: str | None = None) -> list[HostRecord]:
        return []

    def list_sessions(self, owner_email: str | None = None) -> list[SessionRecord]:
        return []
