"""Alembic environment.

Two ways to reach a database, and **a configured Lakebase endpoint WINS**.

* ``SHELLBOX_PG_RESOURCE`` set -> Lakebase. The host, the Postgres role and the OAuth token
  are all derived from that one resource path, so a migration needs no exported DSN.
* Nothing set -> ``dsn_from_env()``, exactly as before. That is `SHELLBOX_DATABASE_URL` or the
  ``SHELLBOX_PG_*`` components, and it is the path CI's ``registry`` job takes against its
  ``postgres:16-alpine`` service. That job sets no resource, so nothing here changes for it.

## Why the endpoint's PRESENCE is the selector, and not the DSN's absence

The obvious rule -- "use Lakebase when no DSN is configured" -- fails in the one place it
matters. ``dsn_from_env`` DEFAULTS the host to ``localhost:55432`` as soon as **any**
``SHELLBOX_PG_*`` variable is set, so a developer who once exported ``SHELLBOX_PG_HOST`` for a
local Postgres always has a DSN. Under that rule ``scripts/deploy.sh`` would migrate their
laptop, report success, and leave the deployed App reading an unmigrated database.

So the resource path wins whenever it is set. ``scripts/deploy.sh`` sets it from
``scripts/bundle-vars.sh``, which makes the deploy path immune to a half-configured shell.
`app/db/client.py` in ``databricks-code-search`` reaches the same rule from the other
direction: there, a deployed App's Lakebase binding injects a ``PGHOST`` that is not paired
with a usable password.

## No credentials in ``alembic.ini``

Neither path reads ``sqlalchemy.url`` from the config file. On the Lakebase path there is no
URL to write down at all -- the token is injected per physical connect.
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool
from sqlalchemy.engine import Engine

from shellbox_registry.dsn import dsn_from_env, normalize_postgres_dsn
from shellbox_registry.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# The one variable that selects the Lakebase path. `scripts/bundle-vars.sh` prints it, and
# `packages/shellbox-app/src/shellbox_app/config.py` reads the same name -- one spelling for
# the endpoint, everywhere.
RESOURCE_ENV = "SHELLBOX_PG_RESOURCE"


def _configured_resource() -> str | None:
    """The Lakebase endpoint resource path, or ``None`` when the DSN path applies."""
    return (os.environ.get(RESOURCE_ENV) or "").strip() or None


def _resolve_url() -> str:
    # dsn_from_env accepts either SHELLBOX_DATABASE_URL or the SHELLBOX_PG_* components,
    # so CI can configure a service container without writing an assembled,
    # credential-bearing URL into its config.
    url = dsn_from_env()
    if not url:
        raise RuntimeError(
            "No database is configured, so alembic has nothing to migrate against "
            f"(NullRegistry has no schema). Set {RESOURCE_ENV} to the Lakebase endpoint "
            "this bundle declares -- `eval \"$(scripts/bundle-vars.sh -t dev -p fevm-west)\"` "
            "prints it -- or set SHELLBOX_DATABASE_URL, or the SHELLBOX_PG_USER / _PASSWORD / "
            "_HOST / _PORT / _DB components. For a local instance, see the docker run command "
            "in README.md."
        )
    return normalize_postgres_dsn(url)


def _lakebase_engine(resource_name: str) -> Engine:
    """An engine for the Lakebase endpoint named by ``resource_name``.

    NOT ``engine_from_config``. The token is injected by a per-connect ``do_connect`` hook
    that only `create_lakebase_engine` installs, so an engine built from a URL here would
    connect with no password at all.

    The pool is deliberately one connection with no overflow: a migration is a single
    session, and `alembic` holds it for the whole run.
    """
    from shellbox_registry.lakebase import (
        DEFAULT_DATABASE,
        LakebaseCredentials,
        create_lakebase_engine,
        is_service_principal_role,
        resolve_lakebase_endpoint,
        sdk_token_minter,
    )

    database = (os.environ.get("SHELLBOX_PG_DB") or "").strip() or DEFAULT_DATABASE
    endpoint = resolve_lakebase_endpoint(
        resource_name,
        database=database,
        # Unset, the role is derived from `current_user.me()`, which is the point -- see
        # `resolve_lakebase_endpoint`. The override exists for a deploy that must name a role
        # the caller is not, and it is the same variable `dsn_from_env` reads.
        user=(os.environ.get("SHELLBOX_PG_USER") or "").strip() or None,
    )

    # The same rule `scripts/check_deploy_principal.py` enforces before `make migrate` runs,
    # asserted here against the DERIVED role. That guard reads the environment, so on this path
    # it has nothing to inspect: nobody exported a user. Without this line the resource path
    # would be the one way to migrate as a service principal with no guard in front of it.
    if is_service_principal_role(endpoint.user):
        raise RuntimeError(
            f"refusing to migrate as {endpoint.user}, which is a service principal role.\n"
            "  `alembic upgrade head` is a deploy-time action and must run as the DEPLOYING\n"
            "  PRINCIPAL. The App's service principal holds SELECT on hosts and sessions and\n"
            "  nothing else, so this migration fails on a permission error -- and widening the\n"
            "  grant to make it pass gives the serving principal DDL on a registry it only\n"
            "  reads. Authenticate as yourself and re-run. See docs/deploy.md section 4."
        )

    print(f"alembic: migrating {endpoint.host}/{endpoint.database} as {endpoint.user}")
    credentials = LakebaseCredentials(sdk_token_minter(resource_name))
    return create_lakebase_engine(endpoint, credentials, pool_size=1, max_overflow=0)


def run_migrations_offline() -> None:
    resource = _configured_resource()
    if resource is not None:
        # Offline mode emits SQL from a URL and never connects. A Lakebase URL carries no
        # usable password -- the token is minted per connect -- and baking one into a script
        # would write a live credential to stdout, which is worse than not supporting this.
        raise RuntimeError(
            f"offline mode cannot use a Lakebase endpoint ({RESOURCE_ENV}="
            f"{resource}).\n"
            "  There is no password to put in the URL: the OAuth token is minted per\n"
            "  connection, and baking one into generated SQL would leak a live credential.\n"
            "  Unset the variable and use the DSN path (SHELLBOX_DATABASE_URL, or the\n"
            "  SHELLBOX_PG_* components) to generate offline SQL."
        )
    context.configure(
        url=_resolve_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    resource = _configured_resource()
    if resource is not None:
        connectable = _lakebase_engine(resource)
    else:
        configuration = config.get_section(config.config_ini_section) or {}
        configuration["sqlalchemy.url"] = _resolve_url()
        connectable = engine_from_config(
            configuration,
            prefix="sqlalchemy.",
            poolclass=pool.NullPool,
        )

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
