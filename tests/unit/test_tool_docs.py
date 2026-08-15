"""W39, `A52` -- the two tool docstrings that are the only mitigation the long-redirected-build
residual has (`ADR-28`). An agent cannot read this repository's plan; it can only read what
`shell_read` and `shell_send` say about themselves. So the tokens below are pinned as a
mechanical test rather than left to a review note, and each is checked against the ACTUAL
docstring of the ACTUAL tool function -- parsed out of `server.py`'s own source, the same
technique `tests/unit/test_no_keepalive.py` uses via `inspect.getsourcefile` -- so this fails the
moment either docstring drifts from what it must say.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

from shellbox_mcp import server as server_module

_SOURCE = Path(inspect.getsourcefile(server_module) or "").read_text(encoding="utf-8")
_TREE = ast.parse(_SOURCE)


def _tool_docstring(name: str) -> str:
    """The docstring of the nested tool function `name` (e.g. `shell_read`), defined inside
    `build_server`. There is exactly one function of this name in the module, so the first
    match is unambiguous."""
    for node in ast.walk(_TREE):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            doc = ast.get_docstring(node)
            assert doc is not None, f"{name} has no docstring"
            return doc
    raise AssertionError(
        f"no function named {name!r} found in {inspect.getsourcefile(server_module)}"
    )


def test_shell_read_docstring_says_a_read_is_recorded_and_names_the_reap_window() -> None:
    doc = _tool_docstring("shell_read")

    assert "records the read" in doc
    assert "reaped" in doc
    assert "25 minutes" in doc


def test_shell_read_docstring_names_no_environment_key() -> None:
    """An agent cannot resolve `SHELLBOX_IDLE_TIMEOUT_SECONDS`, and an operator may have
    changed it -- the docstring must say the default in minutes, not the variable name."""
    doc = _tool_docstring("shell_read")

    assert "SHELLBOX_IDLE_TIMEOUT_SECONDS" not in doc


def test_shell_send_docstring_says_a_send_counts_as_activity() -> None:
    """The half that stops an agent from polling a session it is already driving."""
    doc = _tool_docstring("shell_send")

    assert "counts as activity" in doc


def test_shell_send_docstring_names_no_environment_key() -> None:
    """Scoped to `SHELLBOX_IDLE_TIMEOUT_SECONDS` specifically, not to environment keys in
    general: `shell_send`'s docstring already legitimately names
    `SHELLBOX_MAX_SEND_LINE_BYTES` for an unrelated reason (a refusal an agent must diagnose,
    not a duration it must act on), and that is fine and expected."""
    doc = _tool_docstring("shell_send")

    assert "SHELLBOX_IDLE_TIMEOUT_SECONDS" not in doc
    assert "SHELLBOX_MAX_SEND_LINE_BYTES" in doc
