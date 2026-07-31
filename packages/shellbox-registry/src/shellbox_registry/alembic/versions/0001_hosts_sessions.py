"""hosts and sessions

Transcribed field-for-field from .omc/plans/phase-2-session-plane.md §10. No
session_frames table (D7 — live-stream-only). owner_email is NOT NULL on both tables
from this first migration, so ACL enforcement (#7) is a WHERE clause, never a migration.

Revision ID: 0001
Revises:
Create Date: 2026-07-31

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: str | None = None
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "hosts",
        sa.Column("host_id", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("sandbox_id", sa.Text(), nullable=True),
        sa.Column("gateway_host", sa.Text(), nullable=True),
        sa.Column("owner_email", sa.Text(), nullable=False),
        sa.Column("enrolled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        # NEW in r4: the E5 orphaning guard (§9.2, Critic 9). Not cosmetic — without it,
        # a process with a misconfigured tmux socket path cannot tell "no server
        # running" apart from "wrong socket", and would orphan every live session on the
        # host. Do not drop this column.
        sa.Column("tmux_socket", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("host_id"),
        sa.CheckConstraint("status IN ('active','stale','stopped')", name="hosts_status_chk"),
    )
    op.create_index("hosts_owner_email_idx", "hosts", ["owner_email"])

    op.create_table(
        "sessions",
        sa.Column("session_id", sa.Text(), nullable=False),
        sa.Column("host_id", sa.Text(), nullable=False),
        sa.Column("tmux_name", sa.Text(), nullable=False),
        sa.Column("owner_email", sa.Text(), nullable=False),
        sa.Column("cwd", sa.Text(), nullable=True),
        sa.Column("cols", sa.Integer(), nullable=True),
        sa.Column("rows", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_activity_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("session_id"),
        sa.ForeignKeyConstraint(["host_id"], ["hosts.host_id"]),
        sa.CheckConstraint(
            "status IN ('live','idle','reaped','orphaned')", name="sessions_status_chk"
        ),
    )
    op.create_index("sessions_host_id_idx", "sessions", ["host_id"])
    op.create_index("sessions_owner_email_idx", "sessions", ["owner_email"])


def downgrade() -> None:
    op.drop_index("sessions_owner_email_idx", table_name="sessions")
    op.drop_index("sessions_host_id_idx", table_name="sessions")
    op.drop_table("sessions")

    op.drop_index("hosts_owner_email_idx", table_name="hosts")
    op.drop_table("hosts")
