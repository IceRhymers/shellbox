"""``target.py`` is the only source of ``-t`` values, asserted over ``tmux.py``'s AST.

A behavioral test cannot prove this one: it is a statement about every ``-t`` the adapter
could ever build, including the branches a given test does not reach. So it is checked
structurally, and the check counts what it validated -- a structural test that silently
matches nothing is the failure mode to guard against here.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest
from shellbox_mcp import target as target_module
from shellbox_mcp import tmux
from shellbox_mcp.target import new_session_name, target

TMUX_SOURCE = Path(inspect.getsourcefile(tmux) or "").read_text()
TMUX_TREE = ast.parse(TMUX_SOURCE)


def test_target_module_exposes_exactly_two_functions() -> None:
    """Two classes of name, not three: an anchored target, and a bare new-session name."""
    public = {
        name
        for name, value in vars(target_module).items()
        if callable(value) and not name.startswith("_")
    }
    assert public == {"target", "new_session_name"}
    assert set(target_module.__all__) == {"target", "new_session_name"}


def test_target_is_the_fully_anchored_form() -> None:
    # `=name:` -- the one form correct for all six targeting verbs, and the only one that
    # rejects a nonexistent `=bui:` everywhere. NOT `=name`, which resize-window resolves
    # by prefix anyway, and NOT `name`, which prefix- and fnmatch-matches.
    assert target("build") == "=build:"
    assert target("bui") == "=bui:"


def test_new_session_name_is_never_anchored() -> None:
    # `-s` takes a NAME. Anchoring it creates a session literally named `=build`, which
    # `target()` can then never address.
    assert new_session_name("build") == "build"
    assert not new_session_name("build").startswith("=")


def _sequences(tree: ast.AST) -> list[list[ast.expr]]:
    """Every argument list and literal sequence in the module, as ordered element lists."""
    sequences: list[list[ast.expr]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.List | ast.Tuple):
            sequences.append(list(node.elts))
        elif isinstance(node, ast.Call):
            sequences.append(list(node.args))
    return sequences


def _is_call_to(node: ast.expr, function_name: str) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == function_name
    )


def test_every_dash_t_argument_in_tmux_py_comes_from_target() -> None:
    """The rule ``tmux.py`` may not violate: no ``-t`` value built any other way."""
    total_dash_t = sum(
        1 for node in ast.walk(TMUX_TREE) if isinstance(node, ast.Constant) and node.value == "-t"
    )
    validated = 0
    for elements in _sequences(TMUX_TREE):
        for index, element in enumerate(elements[:-1]):
            if isinstance(element, ast.Constant) and element.value == "-t":
                following = elements[index + 1]
                assert _is_call_to(following, "target"), (
                    f"tmux.py line {element.lineno}: `-t` is followed by "
                    f"{ast.dump(following)}, not a target() call"
                )
                validated += 1

    # Guard against a vacuous pass: if the walk stops finding `-t` arguments (a refactor
    # into a helper, say), this test must fail rather than quietly assert nothing.
    assert validated == total_dash_t > 0, (
        f"found {total_dash_t} `-t` constants but validated {validated}; "
        "every one must sit in an argv sequence directly followed by target()"
    )


def test_new_session_dash_s_receives_the_bare_name_helper() -> None:
    """``-s`` must be followed by ``new_session_name()``, never ``target()``."""
    validated = 0
    for elements in _sequences(TMUX_TREE):
        if not any(isinstance(e, ast.Constant) and e.value == "new-session" for e in elements):
            continue
        for index, element in enumerate(elements[:-1]):
            if isinstance(element, ast.Constant) and element.value == "-s":
                following = elements[index + 1]
                assert _is_call_to(following, "new_session_name"), (
                    f"tmux.py line {element.lineno}: `new-session -s` is followed by "
                    f"{ast.dump(following)}, not new_session_name()"
                )
                validated += 1
    assert validated == 1, f"expected exactly one `new-session -s`, found {validated}"


def test_tmux_py_never_builds_an_anchor_itself() -> None:
    """No string in ``tmux.py`` may start with ``=`` -- anchoring belongs to target() alone.

    This is what stops the half-anchored ``=<name>`` form from being reintroduced by hand.
    """
    offenders = [
        node.value
        for node in ast.walk(TMUX_TREE)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value.startswith("=")
    ]
    assert offenders == [], f"tmux.py builds anchored strings directly: {offenders}"

    for node in ast.walk(TMUX_TREE):
        if isinstance(node, ast.JoinedStr):
            first = node.values[0] if node.values else None
            if isinstance(first, ast.Constant) and str(first.value).startswith("="):
                pytest.fail(f"tmux.py line {node.lineno} f-string builds an anchored target")
