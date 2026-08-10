"""The App's registry, its credential refresher, and the rule that neither may be fatal.

This module is the whole of what the App adds to `shellbox-registry`. The registry class is
the same one the local-Postgres suite exercises; only the credential path differs, which is
what "Lakebase is a credential concern, not a second registry" means in practice. See section
1 of `docs/lakebase-handoff.md`, which verified that claim against a real endpoint.

## CRITICAL: opening the registry may NEVER be fatal

A Lakebase outage degrades the App to "terminals work, the inventory is stale". It must never
degrade it to "terminals are down". The relay holds no database dependency at all -- the data
path is a WebSocket against an in-memory `Relay` -- so a database that cannot be reached has
nothing to do with whether a browser can attach.

`open_registry` therefore mirrors `_open_registry` in
`packages/shellbox-mcp/src/shellbox_mcp/server.py`, which makes the same promise for the MCP
half. Both catch at engine construction, because that is where a bad DSN, a missing driver or
a half-configured environment fails, and letting it propagate would take the process down
before it served anything.

`tests/unit/test_app_database.py` asserts the property from the other side: with a registry
that raises on every call, the accept path still binds a publisher and a subscriber.

## Why the refresher runs here and nowhere else

`LakebaseCredentials` refreshes lazily by default, and that default is right for
`shellbox-mcp`: it is spawned per agent session, 1 to 32 concurrent and short-lived, so a
background thread there would be 32 threads that never fire once. The App is the opposite
case. It is one long-lived process, so it calls `start_refresher()` at startup and `stop()` at
shutdown -- debt 2 of section 5 of `docs/lakebase-handoff.md`, which names the App as the case
the refresher exists for.

Both calls live in the FastAPI lifespan handler rather than at import. A module imported by a
test suite must not start a thread, and `shellbox_app.server` is imported by every test in the
App lane.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from shellbox_registry import NullRegistry, Registry
from shellbox_registry.lakebase import LakebaseCredentials, lakebase_registry

from shellbox_app.config import (
    MAX_OVERFLOW,
    POOL_SIZE,
    POOL_TIMEOUT_SECONDS,
    DatabaseSettings,
)

logger = logging.getLogger(__name__)

__all__ = ["AppDatabase", "RegistryBuilder", "open_registry"]


RegistryBuilder = Callable[..., tuple[Registry, LakebaseCredentials]]
"""How `open_registry` builds a registry. `lakebase_registry` is the real one.

A seam, for the same reason `LakebaseCredentials` takes a `TokenMinter` and a clock: the unit
lane must be able to drive a fake minter, and it must be able to make construction fail on
purpose. There is no provisioned endpoint to point a test at."""


@dataclass
class AppDatabase:
    """The registry the App reads, plus the credentials behind it when there are any.

    ``credentials`` is ``None`` for a `NullRegistry`, which is both the unconfigured case and
    the degraded one. A caller therefore asks this object to start and stop rather than
    branching on which registry it got.
    """

    registry: Registry
    credentials: LakebaseCredentials | None = None

    refresh_interval: float | None = None
    """How often the refresher wakes, in seconds. ``None`` keeps `start_refresher`'s own
    default, so the production interval stays declared in one place -- restating the number
    here would be a second value to keep in step. A test lowers it to make the refresher's
    work observable inside a bounded poll."""

    starts: int = 0
    """How many times `start` ran. Counted rather than flagged so a second start is visible to
    a test instead of merely harmless."""

    @classmethod
    def disabled(cls) -> AppDatabase:
        """No inventory, no credentials, and every terminal still works."""
        return cls(registry=NullRegistry())

    def start(self) -> None:
        """Start the background credential refresher, if there is a credential to refresh.

        Idempotent: `start_refresher` already returns early on a second call, and the counter
        here makes a double start visible to a test rather than merely harmless.
        """
        self.starts += 1
        if self.credentials is None:
            return
        if self.refresh_interval is None:
            self.credentials.start_refresher()
        else:
            self.credentials.start_refresher(interval=self.refresh_interval)

    def stop(self) -> None:
        """Stop the refresher and dispose the pool. NEVER raises.

        A shutdown handler that raises leaves the other half of the shutdown undone, and
        uvicorn reports it as a failed lifespan rather than as a failed dispose. Both halves
        are attempted, and each failure is a log line.
        """
        if self.credentials is not None:
            try:
                self.credentials.stop()
            except Exception:
                logger.warning("could not stop the Lakebase refresher", exc_info=True)

        # `Registry` deliberately declares no `dispose`: `NullRegistry` has no engine and the
        # protocol is the set of primitives a caller needs, not a lifecycle. So this asks.
        dispose = getattr(self.registry, "dispose", None)
        if callable(dispose):
            try:
                dispose()
            except Exception:
                logger.warning("could not dispose the registry engine", exc_info=True)


def open_registry(
    environ: Mapping[str, str] | None = None,
    *,
    build: RegistryBuilder | None = None,
) -> AppDatabase:
    """Resolve the environment and open the registry. NEVER fatal -- see the module docstring.

    Three outcomes, and only the first one has a database:

    * A resolvable endpoint, and an engine that constructs: a `PostgresRegistry` over a
      Lakebase engine, plus the credentials the lifespan handler refreshes.
    * Nothing configured: a `NullRegistry`, logged at INFO. This is the laptop and the
      integration lane, and it is a supported state rather than a misconfiguration.
    * Anything else: a `NullRegistry`, logged as a WARNING with the traceback. The App serves
      terminals and reports a stale inventory.

    The pool settings come from `shellbox_app.config` and are passed explicitly rather than
    inherited. `pool_timeout` is passable only because `create_lakebase_engine` declares the
    parameter; before it did, this exact call raised `TypeError` rather than ignoring the
    value, and `tests/unit/test_lakebase.py` keeps that regression test.
    """
    builder: RegistryBuilder = lakebase_registry if build is None else build

    try:
        settings = DatabaseSettings.from_env(environ)
    except Exception:
        logger.warning(
            "could not resolve the Lakebase endpoint from the environment; continuing with "
            "no inventory (terminals still work, the inventory will be stale)",
            exc_info=True,
        )
        return AppDatabase.disabled()

    if settings is None:
        logger.info(
            "no Lakebase endpoint is configured (SHELLBOX_PG_RESOURCE and SHELLBOX_PG_HOST "
            "are unset); serving terminals with no inventory"
        )
        return AppDatabase.disabled()

    try:
        registry, credentials = builder(
            settings.endpoint(),
            pool_size=POOL_SIZE,
            max_overflow=MAX_OVERFLOW,
            pool_timeout=POOL_TIMEOUT_SECONDS,
        )
    except Exception:
        logger.warning(
            "could not open the Lakebase registry at %s; continuing with no inventory "
            "(terminals still work, the inventory will be stale)",
            settings.host,
            exc_info=True,
        )
        return AppDatabase.disabled()

    logger.info(
        "opened the Lakebase registry at %s/%s as %s",
        settings.host,
        settings.database,
        settings.user,
    )
    return AppDatabase(registry=registry, credentials=credentials)
