"""``create_registry`` — the one place that picks `NullRegistry` vs `PostgresRegistry`.

Per §5/ADR-3: `SHELLBOX_DATABASE_URL` unset means the registry layer must be fully
usable and non-fatal. This is a design choice, not a fallback bug — a Lakebase outage
degrades shellbox to "shells work, inventory is stale," never "shells are down."
"""

from __future__ import annotations

from shellbox_registry.base import Registry
from shellbox_registry.null import NullRegistry
from shellbox_registry.postgres import PostgresRegistry


def create_registry(dsn: str | None) -> Registry:
    """Select a `Registry` implementation from a DSN (typically `SHELLBOX_DATABASE_URL`).

    Unset/empty -> `NullRegistry`. Anything else -> `PostgresRegistry` (this is also the
    Lakebase path once W9's `lakebase.py` credential hook is wired in; the DSN, not this
    function, is what changes).
    """
    if not dsn:
        return NullRegistry()
    return PostgresRegistry(dsn)
