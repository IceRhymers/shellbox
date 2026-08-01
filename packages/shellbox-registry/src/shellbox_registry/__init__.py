"""Shellbox registry package: SQLAlchemy models, Registry protocol, and alembic migrations.

Implements W6 (see .omc/plans/phase-2-session-plane.md §4, §10, §12, ADR-3): `hosts`/
`sessions` models, the `Registry` protocol, `NullRegistry`/`PostgresRegistry`, and the
alembic environment + migrations.

`lakebase.py` (OAuth-token-as-password) lives here too but is deliberately **not**
re-exported below: it is the only module needing the optional `databricks-sdk` extra, and
an unqualified export would make that dependency look mandatory. Import it explicitly:
``from shellbox_registry.lakebase import lakebase_registry``.
"""

from shellbox_registry.base import HostRecord, Registry, SessionRecord
from shellbox_registry.factory import create_registry
from shellbox_registry.null import NullRegistry
from shellbox_registry.postgres import PostgresRegistry

__version__ = "0.1.0"

__all__ = [
    "HostRecord",
    "NullRegistry",
    "PostgresRegistry",
    "Registry",
    "SessionRecord",
    "create_registry",
]
