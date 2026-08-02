"""A GLOBAL ``window-size manual`` must appear NOWHERE in the shipped packages.

Measured 15/15 in BOTH lanes (tmux 3.6b/macOS and 3.4/Ubuntu): with
``set-option -g window-size manual`` on a server, the NEXT ``new-session`` fails with
``server exited unexpectedly`` -- a SIGSEGV in ``clients_calculate_size``. The failure lands
on the *second* create, which is what makes it so bad: by then other pooled agents hold
sessions on that server, so one agent's ``shell_create`` destroys every other agent's
sessions.

Three revisions of the plan each inferred a different cause (ordering, then the option
itself) and each was wrong in the same direction. The measured answer is that the **scope**
is the variable: the per-window form ``-w -t '=<name>:'`` is safe (0/15).

## What changed in Phase 3, and why the check is no longer a grep

Phase 2 needed neither scope, so this file banned the string outright -- the cheapest
assertion that cannot be satisfied by a subtly wrong scope. Phase 3's attach path needs the
per-window form: an attach is a tmux *client*, a client's size drives its window's size, and
without the option a 120x40 viewer reflows an 80x24 agent's pane. That is not hypothetical
either -- spike F16's control row measured the reflow on 3.4.

So the ban becomes a **structural allowlist**: one exact argv shape is permitted and every
other use of the string is still an error. The previous docstring's warning is now the thing
being enforced rather than advice to a future reader:

WARNING: "Set it lazily at first attach" is FATAL if implemented with ``-g``. It detonates the
shared server on the next agent's ``shell_create`` -- this same defect with a longer fuse.

The permitted shape, transcribed from the spike, is::

    "set-option", "-w", "-t", target(name), "window-size", "manual"

Four requirements, each of which has failed somewhere before: ``-w`` present, ``-g`` absent,
the ``-t`` value from ``target()`` (R11 -- the half-anchored ``=<name>`` form returns rc=1 and
stores nothing), and the value ``manual``.
"""

from __future__ import annotations

import ast
from pathlib import Path

PACKAGES = Path(__file__).resolve().parents[2] / "packages"
SPIKE = Path(__file__).resolve().parents[2] / "spike" / "tmux_spike.py"

OPTION = "window-size"

# The one module allowed to name the option at all. Widening this is the change that needs a
# measurement behind it, not a review comment: every tmux form lives in `tmux.py` (decision
# B3) because that is where `target()` and `Settings.tmux_bin` are in scope and where both
# shipped AST guards already reach.
ALLOWED_IN = "shellbox-mcp/src/shellbox_mcp/tmux.py"


def _package_sources() -> list[Path]:
    sources = sorted(PACKAGES.rglob("*.py"))
    assert sources, f"no package sources found under {PACKAGES}"
    return sources


def _sequences(tree: ast.AST) -> list[list[ast.expr]]:
    """Every argument list and literal sequence in the module, as ordered element lists.

    The same helper ``tests/unit/test_target.py`` uses, and deliberately so: an argv is built
    as one or the other, and a rule that inspected only calls would miss a list literal.
    """
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


def _constants(elements: list[ast.expr]) -> list[object]:
    return [e.value for e in elements if isinstance(e, ast.Constant)]


def test_only_tmux_py_may_name_the_option_at_all() -> None:
    """The blast radius, kept to one file.

    ``_package_sources`` globs ``packages/**/*.py``, so a third or fourth workspace package
    inherits this automatically -- which is the property that made splitting the transport
    into its own package safe.
    """
    offenders = [
        str(path.relative_to(PACKAGES))
        for path in _package_sources()
        if f'"{OPTION}"' in path.read_text() or f"'{OPTION}'" in path.read_text()
    ]
    assert offenders == [ALLOWED_IN], (
        f"`{OPTION}` may be named only in {ALLOWED_IN}, and it is named in {offenders}. "
        "At global scope it kills the tmux server on the next new-session, taking every "
        "pooled agent's sessions with it."
    )


def test_every_use_of_the_option_is_the_measured_per_window_form() -> None:
    """The structural allowlist. This is the test that replaced the outright ban.

    Checked over the AST rather than by grep so that a differently-quoted literal, a
    reordered argv, or a value other than ``manual`` cannot slip past -- and counted, because
    a structural check that silently matches nothing is the failure mode for this whole family
    of tests. The count is the reason a refactor that hides the argv behind a helper fails
    here loudly instead of passing vacuously.
    """
    validated = 0
    total = 0
    for path in _package_sources():
        tree = ast.parse(path.read_text())
        total += sum(
            1 for node in ast.walk(tree) if isinstance(node, ast.Constant) and node.value == OPTION
        )
        for elements in _sequences(tree):
            for index, element in enumerate(elements):
                if not (isinstance(element, ast.Constant) and element.value == OPTION):
                    continue
                where = f"{path.relative_to(PACKAGES)}:{element.lineno}"
                constants = _constants(elements)

                assert "-g" not in constants, (
                    f"{where}: `{OPTION}` in an argv that also carries `-g`. The GLOBAL form "
                    "SIGSEGVs the server on the next new-session -- 15/15, both lanes."
                )
                assert "-w" in constants, (
                    f"{where}: `{OPTION}` with no `-w`. The scope is the variable, and an "
                    "unscoped set-option defaults to the session, not the window."
                )
                assert index >= 2, f"{where}: `{OPTION}` has no room for a `-t` before it"
                assert (
                    isinstance(elements[index - 2], ast.Constant)
                    and elements[index - 2].value == "-t"  # type: ignore[attr-defined]
                ), f"{where}: `{OPTION}` is not immediately preceded by a `-t` value"
                assert _is_call_to(elements[index - 1], "target"), (
                    f"{where}: the `-t` before `{OPTION}` is not a target() call. The "
                    "half-anchored `=<name>` form returns rc=1 and stores NOTHING (R11)."
                )
                assert index + 1 < len(elements), f"{where}: `{OPTION}` has no value"
                following = elements[index + 1]
                assert isinstance(following, ast.Constant) and following.value == "manual", (
                    f"{where}: `{OPTION}` is set to something other than `manual`, which is "
                    "the only value this repo has measured"
                )
                validated += 1

    assert validated == total == 1, (
        f"found {total} `{OPTION}` constants and validated {validated}; expected exactly one, "
        "in `TmuxAdapter.freeze_window_size`"
    )


def test_the_option_is_never_set_in_the_create_chain() -> None:
    """Placement, which spike F16 decided and which is invisible to the shape check above.

    Both placements are measured safe -- the create-chain form is 0/15 too -- so this is not a
    safety rule. It is a cost rule, and it protects the 1 to 32 agents who never open a
    browser: they must not pay for the transport, and ``shell_create`` is the chain four review
    rounds were spent getting right.

    F16 also removed the argument for the other placement. The attach-time exposure window
    measured EMPTY over 1714 samples, and one call protects every later viewer, including a
    second client attaching at a different size.
    """
    source = (PACKAGES / ALLOWED_IN).read_text()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if node.name == "freeze_window_size":
            continue
        offenders = [
            inner.lineno
            for inner in ast.walk(node)
            if isinstance(inner, ast.Constant) and inner.value == OPTION
        ]
        assert offenders == [], (
            f"`{OPTION}` appears in `{node.name}` at line(s) {offenders}. It belongs in "
            "`freeze_window_size` alone, which the attach path calls and `create` does not."
        )


def test_the_spike_still_measures_both_scopes() -> None:
    """The evidence for the rule above must stay executable, or the rule becomes folklore.

    This guards against the plausible-looking cleanup where the crashing variant is deleted
    from the spike because "we don't use that option anyway" -- after which nothing in the
    repo demonstrates why, and a future attach path reintroduces it.
    """
    source = SPIKE.read_text()
    assert '"set-option", "-g", "window-size", "manual"' in source, (
        "the spike must keep measuring the GLOBAL form that crashes the server"
    )
    assert '"set-option", "-w", "-t", T("a"), "window-size", "manual"' in source, (
        "the spike must keep measuring the per-window form that is safe"
    )
