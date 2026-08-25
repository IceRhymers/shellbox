"""The release artifact's filename stem is model-visible API, and this guards it against drift.

`buzz-acp` spawns the artifact by path with no arguments and derives the MCP server name from the
file STEM, so the stem becomes the tool prefix the model sees (`mcp__shellbox__shell_create`, ...).
`docs/registration.md` documents that prefix, and `server.py` sets `SERVER_NAME = "shellbox"`.
So three things must agree, or the model's tool names change under it:

  * `server.py`'s `SERVER_NAME` -- the name the running server reports.
  * `scripts/build_artifact.sh`'s `ARTIFACT_NAME` -- the stem the published asset carries.
  * the literal `shellbox`.

This reads the first two FROM THEIR SOURCES rather than restating them, the technique
`tests/unit/test_runtime_python.py` uses to read `RUNTIME_PYTHON` out of `scripts/deploy-app.sh`:
a test that restated the value would pass while the two it is meant to tie together drifted apart.

`SERVER_NAME` is read with `ast`, not a regex, so an assignment written differently (a rename, a
computed value) is still found or fails loudly rather than silently matching nothing.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SERVER_PY = REPO / "packages" / "shellbox-mcp" / "src" / "shellbox_mcp" / "server.py"
BUILD_SCRIPT = REPO / "scripts" / "build_artifact.sh"

EXPECTED_STEM = "shellbox"


def _server_name() -> str:
    """`SERVER_NAME`'s value, parsed from server.py's AST.

    A module-level `SERVER_NAME = "..."` assignment to a string literal. Anything else -- a rename,
    a non-literal value -- raises here rather than passing vacuously, which is the point of reading
    the source instead of trusting a copy.
    """
    tree = ast.parse(SERVER_PY.read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "SERVER_NAME":
                    value = node.value
                    assert isinstance(value, ast.Constant) and isinstance(value.value, str), (
                        "SERVER_NAME is no longer a module-level string literal; this guard and "
                        "build_artifact.sh's ARTIFACT_NAME both need to be re-derived from it."
                    )
                    return value.value
    raise AssertionError(f"no module-level SERVER_NAME assignment found in {SERVER_PY}")


def _artifact_name() -> str:
    """`ARTIFACT_NAME`'s value, parsed from build_artifact.sh.

    A bare `ARTIFACT_NAME="..."` assignment. Read with an anchored regex so a commented-out or
    interpolated form does not match by accident.
    """
    text = BUILD_SCRIPT.read_text()
    matches = re.findall(r'^ARTIFACT_NAME="([^"]+)"$', text, re.MULTILINE)
    assert len(matches) == 1, (
        f"expected exactly one top-level ARTIFACT_NAME assignment in {BUILD_SCRIPT}, "
        f"found {len(matches)}"
    )
    return matches[0]


def test_server_name_is_the_expected_stem() -> None:
    assert _server_name() == EXPECTED_STEM


def test_artifact_name_matches_server_name() -> None:
    """The published asset's stem equals the running server's name, so the model-visible tool
    prefix (`mcp__shellbox__*`) stays put across a release."""
    assert _artifact_name() == _server_name()


def test_artifact_name_has_no_extension() -> None:
    """A dot in the stem would put a dot in the server name (a `.pyz` asset would make the prefix
    `shellbox.pyz`). The asset is extensionless on purpose."""
    assert "." not in _artifact_name()
