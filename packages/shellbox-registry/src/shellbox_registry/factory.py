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

    Unset/empty -> `NullRegistry`. Anything else -> `PostgresRegistry`.

    This is **also** the Lakebase path when the DSN already carries a usable password —
    verified: the whole registry suite passes against a real Lakebase endpoint through
    exactly this function. Use `lakebase.lakebase_registry` instead when the password must
    be an OAuth token that expires, since that needs a per-connect hook rather than a DSN.
    """
    if not dsn:
        return NullRegistry()
    return PostgresRegistry(dsn)
