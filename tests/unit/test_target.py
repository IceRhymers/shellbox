"""``target.py`` is the only source of ``-t`` values, asserted over every package's AST.

A behavioral test cannot prove this one: it is a statement about every ``-t`` the repo could
ever build, including the branches a given test does not reach. So it is checked structurally,
and the check counts what it validated -- a structural test that silently matches nothing is
the failure mode to guard against here.

## Why the scope is ``packages/`` and no longer ``tmux.py``

Phase 2's version read one file, via ``inspect.getsourcefile(tmux)``. That was sufficient while
one module spoke to tmux, and Phase 3's transport is exactly the change that ends it: an attach
builds ``tmux attach -t <name>``, and built anywhere other than ``tmux.py`` it would escape
R11's guard entirely.

This is not a hypothetical either. omnigent's bridge -- the code Phase 3 transcribes decisions
from -- passes an **unanchored** target at ``ws_bridge.py:492`` (``argv += ["-t",
tmux_target]``), and its ``_tmux_session_alive`` helper does it a second time. That is precisely
the form ``target.py`` exists to forbid, so transcribing without widening this check would
import the defect along with the design.

The glob is ``packages/**/*.py``, so a new workspace package inherits the rule automatically
rather than opting into it.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from shellbox_mcp import target as target_module
from shellbox_mcp.target import new_session_name, target

PACKAGES = Path(__file__).resolve().parents[2] / "packages"

# `target.py` builds the anchor; that is its whole job. Every other module is a consumer.
ANCHOR_BUILDER = "shellbox-mcp/src/shellbox_mcp/target.py"


def _package_trees() -> list[tuple[str, ast.Module]]:
    """Every shipped package source, parsed, with its repo-relative name for messages."""
    trees = [
        (str(path.relative_to(PACKAGES)), ast.parse(path.read_text()))
        for path in sorted(PACKAGES.rglob("*.py"))
    ]
    assert trees, f"no package sources found under {PACKAGES}"
    return trees


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


def test_every_dash_t_argument_in_every_package_comes_from_target() -> None:
    """The rule no shipped module may violate: no ``-t`` value built any other way.

    R11 in one assertion. The anchored ``=<name>:`` is the only form correct for all six
    targeting verbs; the bare name prefix- and fnmatch-matches, and the half-anchored
    ``=<name>`` is resolved by prefix anyway by ``resize-window``. A ``-t`` assembled by hand
    somewhere else is how a command reaches the wrong agent's session.
    """
    total_dash_t = 0
    validated = 0
    for name, tree in _package_trees():
        total_dash_t += sum(
            1 for node in ast.walk(tree) if isinstance(node, ast.Constant) and node.value == "-t"
        )
        for elements in _sequences(tree):
            for index, element in enumerate(elements[:-1]):
                if isinstance(element, ast.Constant) and element.value == "-t":
                    following = elements[index + 1]
                    assert _is_call_to(following, "target"), (
                        f"{name} line {element.lineno}: `-t` is followed by "
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
    """``-s`` must be followed by ``new_session_name()``, never ``target()``.

    The inverse of the rule above, and it needs stating separately because the intuitive fix
    for one breaks the other: ``-s '=build'`` creates a session literally named ``=build``,
    which ``target()`` can then never address.
    """
    validated = 0
    for name, tree in _package_trees():
        for elements in _sequences(tree):
            if not any(isinstance(e, ast.Constant) and e.value == "new-session" for e in elements):
                continue
            for index, element in enumerate(elements[:-1]):
                if isinstance(element, ast.Constant) and element.value == "-s":
                    following = elements[index + 1]
                    assert _is_call_to(following, "new_session_name"), (
                        f"{name} line {element.lineno}: `new-session -s` is followed by "
                        f"{ast.dump(following)}, not new_session_name()"
                    )
                    validated += 1
    assert validated == 1, f"expected exactly one `new-session -s`, found {validated}"


def test_no_package_builds_an_anchor_itself() -> None:
    """Anchoring belongs to ``target()`` alone, everywhere.

    This is what stops the half-anchored ``=<name>`` form -- rc=1, stores NOTHING -- from being
    reintroduced by hand in a module that never imports ``target``.

    Single-character ``"="`` is exempt: it is a separator in ``config.py`` and ``naming.py``,
    not an anchor, and it cannot carry a session name.
    """
    for name, tree in _package_trees():
        if name == ANCHOR_BUILDER:
            continue
        offenders = [
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value.startswith("=")
            and len(node.value) > 1
        ]
        assert offenders == [], f"{name} builds anchored strings directly: {offenders}"

        for node in ast.walk(tree):
            if isinstance(node, ast.JoinedStr):
                first = node.values[0] if node.values else None
                if isinstance(first, ast.Constant) and str(first.value).startswith("="):
                    pytest.fail(f"{name} line {node.lineno} f-string builds an anchored target")


def test_no_argv_literal_starts_with_a_bare_tmux_binary() -> None:
    """ADR-1's single-resolution-point rule, now that a second package could break it.

    ``TmuxConfig.tmux_bin`` is the one place the binary is named and ``_base_argv()`` is the one
    place it enters an argv, so every invocation also carries ``-S <socket>`` and
    ``-f /dev/null``. A hand-built ``["tmux", ...]`` gets neither: it talks to the DEFAULT tmux
    server rather than shellbox's private socket, and it inherits the user's ``~/.tmux.conf``.

    That is the specific mistake a transcription can import -- omnigent's pane-dead probe
    spawns a bare ``"tmux"`` (``ws_bridge.py:245-300``) -- and it fails in the worst way, by
    working on a developer's machine and finding a different server, or none, in the sandbox.

    The rule is head-of-argv rather than "the string appears", because ``tmux`` is legitimately
    the DEFAULT VALUE of ``tmux_bin`` (resolved through ``PATH`` on purpose) and is legitimately
    named by ``doctor`` and ``identity`` in diagnostics. Only position zero of a sequence makes
    it a binary being executed.
    """
    offenders = []
    for name, tree in _package_trees():
        for node in ast.walk(tree):
            if not isinstance(node, ast.List | ast.Tuple) or not node.elts:
                continue
            head = node.elts[0]
            if isinstance(head, ast.Constant) and head.value == "tmux":
                offenders.append(f"{name}:{head.lineno}")
    assert offenders == [], (
        f'an argv literal starts with a bare "tmux" at {offenders}. The binary, the socket '
        "and `-f /dev/null` all come from `_base_argv()`, together."
    )
