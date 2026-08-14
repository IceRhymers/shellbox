"""SQLAlchemy models for the registry: ``hosts`` and ``sessions``.

Transcribed field-for-field from `.omc/plans/phase-2-session-plane.md` §10. Do not add a
``session_frames`` table here — D7 is live-stream-only (Phase 2 §3's explicit "Out" list),
and re-adding one has already been rejected once by review.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# §10's schema is `timestamptz` throughout. SQLAlchemy's default type inference for
# `Mapped[datetime]` maps to `DateTime()` WITHOUT a timezone, which would silently
# create `timestamp without time zone` columns that strip tzinfo on read — a mismatch
# against the alembic migration's explicit `DateTime(timezone=True)`. Every datetime
# column below states this type explicitly so `Base.metadata.create_all` (used by
# tests/registry/conftest.py) and migration 0001 create byte-identical schemas.
_TIMESTAMPTZ = DateTime(timezone=True)


class Base(DeclarativeBase):
    pass


class Host(Base):
    """A row per enrolled host (§10). ``tmux_socket`` is the E5 orphaning guard (§9.2,
    Critic 9): it lets `identity.py` (W7) refuse to orphan every session on a host when
    *this* process resolved the wrong tmux socket, rather than treating `no server
    running` and `wrong socket` as the same signal. Do not drop it as cosmetic."""

    __tablename__ = "hosts"

    host_id: Mapped[str] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(nullable=False)
    sandbox_id: Mapped[str | None] = mapped_column(default=None)
    gateway_host: Mapped[str | None] = mapped_column(default=None)
    owner_email: Mapped[str] = mapped_column(nullable=False)
    enrolled_at: Mapped[datetime] = mapped_column(_TIMESTAMPTZ, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(_TIMESTAMPTZ, nullable=False)
    status: Mapped[str] = mapped_column(nullable=False)
    tmux_socket: Mapped[str | None] = mapped_column(default=None)

    __table_args__ = (
        # ADR-30: `status` does not track heartbeat staleness. The App holds only SELECT on
        # this table (`scripts/grant_app_sp.py:99-104`) and cannot write `'stale'` here even
        # for a host whose heartbeat has gone quiet -- `status` keeps reading `'active'` on a
        # dead host, and that is a documented property, not a bug. `heartbeat_stale` in the
        # App's inventory payload (`shellbox_app/inventory.py`'s `host_payload`) is what
        # answers "is this heartbeat old", derived at read time rather than stored here.
        CheckConstraint("status IN ('active','stale','stopped')", name="hosts_status_chk"),
        Index("hosts_owner_email_idx", "owner_email"),
    )


class Session(Base):
    """A row per tmux session shellbox knows about (§10). No ``session_frames`` table —
    see the module docstring."""

    __tablename__ = "sessions"

    session_id: Mapped[str] = mapped_column(primary_key=True)
    host_id: Mapped[str] = mapped_column(ForeignKey("hosts.host_id"), nullable=False)
    tmux_name: Mapped[str] = mapped_column(nullable=False)
    owner_email: Mapped[str] = mapped_column(nullable=False)
    cwd: Mapped[str | None] = mapped_column(default=None)
    cols: Mapped[int | None] = mapped_column(default=None)
    rows: Mapped[int | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(_TIMESTAMPTZ, nullable=False)
    last_activity_at: Mapped[datetime] = mapped_column(_TIMESTAMPTZ, nullable=False)
    # `last_activity_at` advances on SEND only; `last_read_at` on READ. Two columns, because
    # one cannot express both of Phase 5's hazards and a single column would force Phase 2 to
    # pick Phase 5's reaping policy:
    #
    #   * count reads as activity  -> an agent polling `shell_read` keeps a session alive forever
    #   * ignore reads entirely    -> an agent watching a 40-minute build without sending gets
    #                                 its session reaped mid-build
    #
    # Recording both leaves the predicate to #5 (`last_activity_at` alone, `GREATEST(...)`, or a
    # different timeout per column). Added by migration 0002 rather than folded into the merged
    # 0001: alembic records only a revision *id* and never fingerprints content, so amending a
    # migration a developer has already applied gives them a schema missing this column while
    # `alembic current` reports up to date -- the same silent divergence the `_TIMESTAMPTZ`
    # comment above exists to prevent.
    # Nullable because a session that has never been read has no honest value for it — do NOT
    # default it to `created_at`, which would read as "someone looked at this".
    last_read_at: Mapped[datetime | None] = mapped_column(_TIMESTAMPTZ, default=None)
    status: Mapped[str] = mapped_column(nullable=False)

    __table_args__ = (
        CheckConstraint(
            "status IN ('live','idle','reaped','orphaned')", name="sessions_status_chk"
        ),
        Index("sessions_host_id_idx", "host_id"),
        Index("sessions_owner_email_idx", "owner_email"),
    )
