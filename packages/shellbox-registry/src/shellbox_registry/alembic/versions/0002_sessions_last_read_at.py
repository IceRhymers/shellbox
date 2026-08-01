"""sessions.last_read_at

Splits "somebody is DRIVING this session" from "somebody is WATCHING it".
``last_activity_at`` advances on send only; this column advances on read.

Two columns rather than one, because one cannot express both of Phase 5's hazards and a
single column would force Phase 2 to pick Phase 5's reaping policy:

* count reads as activity -> an agent polling ``shell_read`` keeps a session alive forever
* ignore reads entirely   -> an agent watching a 40-minute build without sending gets its
                             session reaped mid-build

Recording both leaves the predicate to #5 (``last_activity_at`` alone, ``GREATEST(...)``, or
a different timeout per column).

**Why this is 0002 and not an edit to 0001.** It was briefly written into 0001 on the
reasoning that no database is deployed yet. That reasoning is wrong, and the way it is wrong
is silent: alembic records only the revision *id* in ``alembic_version`` and never
fingerprints the file's content. A developer whose local Postgres is already at 0001 -- which
is likely, since 0001 is merged -- would pull the amended file, get a schema with **no**
``last_read_at`` while ``alembic current`` reports up to date, and then see the first
``upsert_session`` fail with ``UndefinedColumn`` against a database alembic insists is
correct. An additive migration cannot do that to anyone. It is also the same
silent-schema-divergence class ``models.py`` already carries a comment to prevent for
``timestamptz``.

Nullable on purpose: a session that has never been read has no honest timestamp for this, and
defaulting it to ``created_at`` would read as "someone looked at this".

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-31

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("sessions", sa.Column("last_read_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("sessions", "last_read_at")
