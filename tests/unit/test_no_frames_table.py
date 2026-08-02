"""T-NO-FRAMES-TABLE -- the transport adds no table and no migration.

D7 is live-stream-only. ``models.py``'s docstring records that a ``session_frames`` table "has
already been rejected once by review", and the transport is exactly the change that makes
adding one look reasonable again: ``subscribe(session_id, from_seq)`` promises resume, and a
frame log is the obvious way to keep that promise from any ``from_seq``.

It is not available. What the transport ships instead is two guarantees -- byte-exact
continuity while the publisher's in-memory ring holds ``from_seq``, and a declared
discontinuity carrying a ``capture-pane`` repaint otherwise. Neither one persists a frame, and
``FrameTransport.subscribe``'s docstring says so where a reader will see it.

So this file is the assertion that the promise did not quietly become a table. Two checks,
because the schema has two sources of truth that can disagree: the declarative metadata (used
by ``tests/registry/conftest.py`` through ``create_all``) and the alembic migrations (used by
every deployment).
"""

from __future__ import annotations

import ast
from pathlib import Path

from shellbox_registry.models import Base

_VERSIONS = (
    Path(__file__).resolve().parents[2]
    / "packages"
    / "shellbox-registry"
    / "src"
    / "shellbox_registry"
    / "alembic"
    / "versions"
)

EXPECTED_TABLES = {"hosts", "sessions"}


def test_the_metadata_holds_exactly_hosts_and_sessions() -> None:
    """A frame log would arrive here first, because a table needs a model before it needs a
    migration."""
    assert set(Base.metadata.tables) == EXPECTED_TABLES


def test_no_model_carries_a_frame_or_stream_column() -> None:
    """A frame log does not have to be a new table. Columns named for frames on ``sessions``
    would be the same rejected design at a smaller scale, so the names are checked too."""
    banned = ("frame", "seq", "epoch", "stream_")
    for table in Base.metadata.tables.values():
        for column in table.columns:
            assert not any(word in column.name for word in banned), (
                f"{table.name}.{column.name} looks like frame-log state. "
                "D7 is live-stream-only; the ring lives in the publisher's memory."
            )


def test_the_migrations_create_exactly_those_two_tables() -> None:
    """The second source of truth. A migration that creates a table the models do not declare
    would deploy a frame log that no model test could see.

    Structural over each migration's AST rather than a grep, and it counts what it validated:
    a structural check that silently matches nothing is the failure mode here.
    """
    created: set[str] = set()
    checked = 0
    for path in sorted(_VERSIONS.glob("[0-9]*.py")):
        checked += 1
        for node in ast.walk(ast.parse(path.read_text())):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "create_table"
                and node.args
                and isinstance(node.args[0], ast.Constant)
            ):
                created.add(str(node.args[0].value))

    assert checked >= 2, f"found {checked} migrations under {_VERSIONS}; expected at least 2"
    assert created == EXPECTED_TABLES
