"""``PostgresRegistry`` — the `Registry` implementation backed by real Postgres.

Works against local Postgres and, later, Lakebase unchanged (ADR-3): Lakebase reduces to
a credential concern owned by W9's `lakebase.py` (OAuth-token-as-password, refresh),
which is out of scope here. This module only ever receives a DSN and connects.
"""

from __future__ import annotations

from sqlalchemy import create_engine, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session as OrmSession

from shellbox_registry.base import HostRecord, Registry, SessionRecord
from shellbox_registry.dsn import normalize_postgres_dsn
from shellbox_registry.models import Host
from shellbox_registry.models import Session as SessionModel


def _host_to_record(row: Host) -> HostRecord:
    return HostRecord(
        host_id=row.host_id,
        kind=row.kind,
        owner_email=row.owner_email,
        last_seen_at=row.last_seen_at,
        status=row.status,
        sandbox_id=row.sandbox_id,
        gateway_host=row.gateway_host,
        tmux_socket=row.tmux_socket,
        enrolled_at=row.enrolled_at,
    )


def _session_to_record(row: SessionModel) -> SessionRecord:
    return SessionRecord(
        session_id=row.session_id,
        host_id=row.host_id,
        tmux_name=row.tmux_name,
        owner_email=row.owner_email,
        last_activity_at=row.last_activity_at,
        status=row.status,
        cwd=row.cwd,
        cols=row.cols,
        rows=row.rows,
        created_at=row.created_at,
    )


class PostgresRegistry(Registry):
    """Real Postgres implementation. Every method raises on failure — callers (W7) are
    responsible for catching that and degrading to a `registry_warning` rather than
    failing the tool call (§9)."""

    def __init__(
        self,
        dsn: str,
        *,
        pool_size: int = 3,
        pool_pre_ping: bool = True,
        engine: Engine | None = None,
    ) -> None:
        self._engine = engine or create_engine(
            normalize_postgres_dsn(dsn),
            pool_size=pool_size,
            pool_pre_ping=pool_pre_ping,
        )

    def dispose(self) -> None:
        self._engine.dispose()

    def upsert_host(self, record: HostRecord) -> None:
        enrolled_at = record.enrolled_at or record.last_seen_at
        stmt = pg_insert(Host).values(
            host_id=record.host_id,
            kind=record.kind,
            sandbox_id=record.sandbox_id,
            gateway_host=record.gateway_host,
            owner_email=record.owner_email,
            enrolled_at=enrolled_at,
            last_seen_at=record.last_seen_at,
            status=record.status,
            tmux_socket=record.tmux_socket,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[Host.host_id],
            set_={
                "kind": stmt.excluded.kind,
                "sandbox_id": stmt.excluded.sandbox_id,
                "gateway_host": stmt.excluded.gateway_host,
                "owner_email": stmt.excluded.owner_email,
                # enrolled_at intentionally NOT set here -> the existing row's value
                # is preserved on conflict ("first enrollment wins", E4).
                "last_seen_at": func.greatest(stmt.excluded.last_seen_at, Host.last_seen_at),
                "status": stmt.excluded.status,
                "tmux_socket": stmt.excluded.tmux_socket,
            },
        )
        with self._engine.begin() as conn:
            conn.execute(stmt)

    def upsert_session(self, record: SessionRecord) -> None:
        created_at = record.created_at or record.last_activity_at
        stmt = pg_insert(SessionModel).values(
            session_id=record.session_id,
            host_id=record.host_id,
            tmux_name=record.tmux_name,
            owner_email=record.owner_email,
            cwd=record.cwd,
            cols=record.cols,
            rows=record.rows,
            created_at=created_at,
            last_activity_at=record.last_activity_at,
            status=record.status,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[SessionModel.session_id],
            set_={
                "host_id": stmt.excluded.host_id,
                "tmux_name": stmt.excluded.tmux_name,
                "owner_email": stmt.excluded.owner_email,
                "cwd": stmt.excluded.cwd,
                "cols": stmt.excluded.cols,
                "rows": stmt.excluded.rows,
                # created_at intentionally NOT set here -> preserved on conflict.
                "last_activity_at": func.greatest(
                    stmt.excluded.last_activity_at, SessionModel.last_activity_at
                ),
                "status": stmt.excluded.status,
            },
        )
        with self._engine.begin() as conn:
            conn.execute(stmt)

    def get_host(self, host_id: str) -> HostRecord | None:
        # scalar_one_or_none() over an ORM entity requires an orm.Session (not a bare
        # Core Connection.execute()) to hydrate rows into Host instances, not tuples.
        with OrmSession(self._engine) as sess:
            row = sess.execute(select(Host).where(Host.host_id == host_id)).scalar_one_or_none()
        return _host_to_record(row) if row is not None else None

    def get_session(self, session_id: str) -> SessionRecord | None:
        with OrmSession(self._engine) as sess:
            row = sess.execute(
                select(SessionModel).where(SessionModel.session_id == session_id)
            ).scalar_one_or_none()
        return _session_to_record(row) if row is not None else None

    def list_sessions_for_host(self, host_id: str) -> list[SessionRecord]:
        with OrmSession(self._engine) as sess:
            rows = (
                sess.execute(select(SessionModel).where(SessionModel.host_id == host_id))
                .scalars()
                .all()
            )
        return [_session_to_record(row) for row in rows]
