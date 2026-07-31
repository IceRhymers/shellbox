"""Alembic environment.

The DSN comes from `SHELLBOX_DATABASE_URL`, never from `alembic.ini` (no credentials
committed to the repo). `NullRegistry`'s unset-DSN case has nothing to migrate, so a
missing env var is a hard error here rather than a silent no-op.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from shellbox_registry.dsn import dsn_from_env, normalize_postgres_dsn
from shellbox_registry.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _resolve_url() -> str:
    # dsn_from_env accepts either SHELLBOX_DATABASE_URL or the SHELLBOX_PG_* components,
    # so CI can configure a service container without writing an assembled,
    # credential-bearing URL into its config.
    url = dsn_from_env()
    if not url:
        raise RuntimeError(
            "No database is configured, so alembic has nothing to migrate against "
            "(NullRegistry has no schema). Set SHELLBOX_DATABASE_URL, or the "
            "SHELLBOX_PG_USER / _PASSWORD / _HOST / _PORT / _DB components. For a local "
            "instance, see the docker run command in README.md."
        )
    return normalize_postgres_dsn(url)


def run_migrations_offline() -> None:
    context.configure(
        url=_resolve_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
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
