"""Placeholder test.

Without this, an empty ``tests/`` directory makes pytest exit 5 (NO_TESTS_COLLECTED),
which is indistinguishable from "green" in CI. Real coverage lands under tests/unit,
tests/tmux, tests/registry, tests/integration, and tests/sandbox as W2-W7 land (see
.omc/plans/phase-2-session-plane.md §4, §11).
"""

import shellbox_mcp
import shellbox_registry


def test_packages_import() -> None:
    assert shellbox_mcp.__version__
    assert shellbox_registry.__version__
