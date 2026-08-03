"""What the App reads from its environment to reach the Lakebase registry.

The App resolves an endpoint; it never discovers one. Every value below is declared once in
the bundle and handed to the container as an environment variable, so nothing here contacts
a control plane to learn where the database is.

## CRITICAL: the Postgres role is the service principal's client id, and NOT `current_user.me()`

Lakebase maps an OAuth principal onto a Postgres role of the same name. A Databricks App
authenticates as its own service principal, whose role name is the SP's **client id** --
`docs/lakebase-handoff.md` section 5, debt 3, states this and records it as untested.
`DATABRICKS_CLIENT_ID` carries that id, and the Apps runtime injects it: the Phase 1 probe
measured it in the container and recorded the value it saw in `probe/FINDINGS.md` under O2.

So the user comes from an environment variable, and **no code here calls
`current_user.me()`**. That call would look correct and would be wrong. Without an
on-behalf-of token the SDK resolves the *App's own* service principal, so a viewer's
inventory would silently be filtered by whatever principal the App runs as, and the App
would still work. A failure that reads as working is the one worth a rule, so
`tests/unit/test_app_database.py` asserts that no module in this package names
`current_user`.

## The variable names, and why they are the ones the deploy path already prints

`scripts/bundle-vars.sh` emits `SHELLBOX_PG_RESOURCE`, built from the three bundle ids. The
`SHELLBOX_PG_*` component names are the ones `dsn_from_env` in
`packages/shellbox-registry/src/shellbox_registry/dsn.py` already reads, and
`make require-pg-host` already guards. Choosing a fourth spelling would mean an operator who
exported the variables for `make migrate` still has nothing the App can read.

WARNING: **Nothing in this repo yet SETS these variables inside the App container.** The
bundle declares the database as an app resource (`resources/app.yml`), and this repo has
never deployed it, so what the Apps runtime injects for such a resource is unmeasured here.
Until that is measured, the values arrive through `app.yaml`'s environment or through the
app's configuration, and `open_registry` treats their absence as "no inventory" rather than
as a failure -- see `packages/shellbox-app/src/shellbox_app/database.py`.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

from shellbox_registry.lakebase import LakebaseEndpoint

__all__ = [
    "DEFAULT_DATABASE",
    "DEFAULT_PORT",
    "MAX_OVERFLOW",
    "POOL_SIZE",
    "POOL_TIMEOUT_SECONDS",
    "DatabaseSettings",
]

# The three pool settings the App overrides. Every one of them is derived in
# `create_lakebase_engine`'s docstring, in
# `packages/shellbox-registry/src/shellbox_registry/lakebase.py`, rather than here. Read that
# docstring before changing a number: it carries the arithmetic, including the measured 1.4 s
# first connect to a suspended endpoint.
#
# The short version, so a reader knows what the numbers are for. The App is the only caller
# with Starlette's 40-thread threadpool in front of this pool. A synchronized edge kill
# reconnects every browser at once, and the storm that follows parks requests on the pool.
POOL_SIZE = 5

# A deliberate RAISE from `create_lakebase_engine`'s default of 5, not an inherited value.
# 5 plus 10 is the 15 connections that docstring's arithmetic assumes. `shellbox-mcp` keeps
# the narrower default because it has no threadpool in front of its pool.
MAX_OVERFLOW = 10

# Seconds a checkout waits for a free connection. This is NOT the TCP connect bound, which is
# `PostgresRegistry.CONNECT_TIMEOUT_SECONDS`.
#
# It is passed explicitly because SQLAlchemy's own default is 30 s, and 30 s turns a
# reconnect storm into a hang that also delays the only automatic detector of a database
# problem. `create_lakebase_engine` gained this parameter for this caller, and
# `tests/unit/test_lakebase.py` holds the regression test for the `TypeError` the call raised
# before it existed.
POOL_TIMEOUT_SECONDS = 5

# The Postgres port, and the database `alembic upgrade head` migrates.
#
# `DEFAULT_DATABASE` mirrors the `pg_database` bundle variable's default and `dsn_from_env`'s
# component default, which are already the same value. A default is safe for a database NAME
# and would not be safe for a HOST: the host decides which server is reached, which is why
# `SHELLBOX_PG_HOST` below has no default at all.
DEFAULT_PORT = 5432
DEFAULT_DATABASE = "shellbox"


@dataclass(frozen=True, slots=True)
class DatabaseSettings:
    """The resolved Lakebase endpoint, as the App reads it from the environment."""

    resource_name: str
    """The API path ``projects/{project}/branches/{branch}/endpoints/{endpoint}``. It mints
    tokens, and it is NOT what psycopg dials. `scripts/bundle-vars.sh` constructs it from the
    three ids the bundle declares, and prints it as ``SHELLBOX_PG_RESOURCE``."""

    host: str
    """What psycopg dials, read back from the endpoint after a deploy. Separate from
    ``resource_name`` because the two come from different calls, and conflating them is the
    first mistake to make here."""

    database: str
    user: str
    """The Postgres role. The App's service principal client id -- see the module docstring
    for why this is never resolved with a workspace API call."""

    port: int = DEFAULT_PORT

    def endpoint(self) -> LakebaseEndpoint:
        """The `shellbox-registry` value this settings object describes."""
        return LakebaseEndpoint(
            resource_name=self.resource_name,
            host=self.host,
            database=self.database,
            user=self.user,
            port=self.port,
        )

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> DatabaseSettings | None:
        """Resolve the endpoint, or ``None`` when no database is configured.

        ``None`` means "this App has no inventory", which is a supported state rather than an
        error: the relay needs no database, so an App with nothing configured still serves
        every terminal. `open_registry` in
        `packages/shellbox-app/src/shellbox_app/database.py` turns it into a `NullRegistry`.

        A HALF-configured environment raises instead. One of the two required variables set
        and the other missing is a deploy that meant to reach a database, so reporting "no
        inventory" would hide it. The caller logs the failure and degrades, so the raise
        costs an operator a log line rather than an outage.
        """
        env = os.environ if environ is None else environ
        resource_name = (env.get("SHELLBOX_PG_RESOURCE") or "").strip()
        host = (env.get("SHELLBOX_PG_HOST") or "").strip()
        if not resource_name and not host:
            return None
        if not resource_name or not host:
            raise ValueError(
                "the App's database is half configured: SHELLBOX_PG_RESOURCE is "
                f"{'set' if resource_name else 'unset'} and SHELLBOX_PG_HOST is "
                f"{'set' if host else 'unset'}. Both are needed -- one mints the credential "
                "and the other is dialled. Run scripts/bundle-vars.sh for the resource path."
            )

        # `SHELLBOX_PG_USER` first, so a laptop run as a workspace user needs no code change.
        # `DATABRICKS_CLIENT_ID` is the deployed case, and it is what the Apps runtime injects.
        user = (env.get("SHELLBOX_PG_USER") or env.get("DATABRICKS_CLIENT_ID") or "").strip()
        if not user:
            raise ValueError(
                "the App has no Postgres role to connect as: neither SHELLBOX_PG_USER nor "
                "DATABRICKS_CLIENT_ID is set. The deployed App uses DATABRICKS_CLIENT_ID, "
                "which is its service principal's client id. Do NOT resolve this with "
                "current_user.me() -- see the module docstring in config.py."
            )

        raw_port = (env.get("SHELLBOX_PG_PORT") or "").strip()
        try:
            port = DEFAULT_PORT if not raw_port else int(raw_port)
        except ValueError as exc:
            raise ValueError(f"SHELLBOX_PG_PORT={raw_port!r} is not an integer") from exc

        return cls(
            resource_name=resource_name,
            host=host,
            database=(env.get("SHELLBOX_PG_DB") or "").strip() or DEFAULT_DATABASE,
            user=user,
            port=port,
        )
