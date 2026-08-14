"""T-P4-NO-APP-WRITES -- the App package calls no registry writer.

The App's service principal is granted ``SELECT`` on ``hosts`` and ``sessions`` and nothing else
(``scripts/grant_app_sp.py``). This file is the static half of that decision, and the two halves
fail in opposite places: a write path added to the App fails HERE, in ``make test``, rather than in
production as a permission error on a route nobody exercised.

The rule is a scope rule rather than a taste one. Under D6 the App is reachable by every workspace
user. Its writers are the 1 to 32 ``shellbox-mcp`` processes, and each of those authenticates as
the real user who runs it. A write from the App would be a write by a principal that stands for
everybody, into rows that record who owns what.

**This passes on today's tree, and that is what it is for.** No route in ``shellbox_app`` touches
the registry at all yet. It is a regression guard: it fails the moment someone adds a write path,
which is exactly the moment a SELECT-only grant starts returning permission errors in production.
So the non-vacuity is proved differently -- ``test_the_scan_sees_a_planted_write`` plants each
shape of call the scan must catch and asserts it catches them. Without that case this file would be
a check whose only evidence is that it found nothing.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
APP_PACKAGE = REPO / "packages" / "shellbox-app" / "src" / "shellbox_app"

# The registry's writers, named in `shellbox_registry.base.Registry`. The read primitives
# (`list_hosts`, `list_sessions`, `get_host`, ...) are what the App is for and are not listed here.
# `touch_read` (W39) is a writer too -- an UPDATE, not an upsert, but still a write the App SP's
# SELECT-only grant does not permit.
WRITERS = ("upsert_host", "upsert_session", "touch_read")


def writer_references(source: str) -> list[str]:
    """Every reference to a registry writer in ``source``, as ``name:lineno`` strings.

    Four shapes, because an attribute check alone is evadable by accident rather than by malice:

    - ``registry.upsert_host(...)``     an attribute, which is how the App would call it
    - ``upsert_host(...)``              a bare name, after ``from ... import upsert_host``
    - ``getattr(registry, "upsert_host")``  the name as a string, which no attribute check sees
    - ``from ... import upsert_host``   the import, which is a reference even unused

    Matching the string constant is what makes the last two work, and it is deliberately blunt: a
    string equal to a writer's name has no other business in this package.
    """
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Attribute) and node.attr in WRITERS:
            found.append(f"{node.attr}:{node.lineno}")
        elif isinstance(node, ast.Name) and node.id in WRITERS:
            found.append(f"{node.id}:{node.lineno}")
        elif isinstance(node, ast.Constant) and node.value in WRITERS:
            found.append(f"{node.value}:{node.lineno}")
        elif isinstance(node, ast.ImportFrom):
            found += [
                f"{alias.name}:{node.lineno}" for alias in node.names if alias.name in WRITERS
            ]
    return sorted(found)


def _app_sources() -> list[Path]:
    sources = sorted(APP_PACKAGE.rglob("*.py"))
    assert sources, f"no App sources found under {APP_PACKAGE}"
    return sources


def test_the_app_package_calls_no_registry_writer() -> None:
    """The assertion itself. It scans the package rather than one module, so a new module in
    ``shellbox_app`` inherits the rule instead of escaping it."""
    offenders = {
        str(path.relative_to(REPO)): writer_references(path.read_text()) for path in _app_sources()
    }
    offenders = {path: hits for path, hits in offenders.items() if hits}
    assert offenders == {}, (
        f"the App package references a registry writer: {offenders}. The App SP holds SELECT on "
        f"hosts and sessions and nothing else, so this write fails in production with a "
        f"permission error. The writers are the shellbox-mcp processes, which authenticate as the "
        f"real user -- see scripts/grant_app_sp.py."
    )


def test_the_scan_sees_a_planted_write() -> None:
    """The positive witness, and the reason the test above is not vacuous.

    Each planted line is a shape the scan claims to catch. Deliberately synthetic: it plants the
    calls in a string rather than in the package, because a guard proved by temporarily breaking
    the thing it guards is a guard that leaves the tree broken when the run is interrupted.
    """
    planted = "\n".join(
        (
            "def render(registry, record):",
            "    registry.upsert_host(record)",
            "    writer = getattr(registry, 'upsert_session')",
            "    writer(record)",
            "    registry.touch_read(record.session_id, now)",
        )
    )
    hits = writer_references(planted)
    # `writer_references` returns `sorted(found)`, so the order is alphabetical by name, not
    # by appearance -- "touch_read" sorts before "upsert_host"/"upsert_session".
    assert [hit.split(":")[0] for hit in hits] == [
        "touch_read",
        "upsert_host",
        "upsert_session",
    ], hits


def test_the_scan_ignores_the_reads_the_app_exists_for() -> None:
    """The negative control. A scan that flagged ``list_hosts`` would fail the App's own purpose,
    and a rule that cannot distinguish a read from a write is not the rule this file claims."""
    reads = "\n".join(
        (
            "def render(registry):",
            "    return registry.list_hosts(), registry.list_sessions()",
        )
    )
    assert writer_references(reads) == []
