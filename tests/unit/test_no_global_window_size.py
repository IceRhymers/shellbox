"""A GLOBAL ``window-size manual`` must appear NOWHERE in the shipped packages.

Measured 15/15 in BOTH lanes (tmux 3.6b/macOS and 3.4/Ubuntu): with
``set-option -g window-size manual`` on a server, the NEXT ``new-session`` fails with
``server exited unexpectedly`` -- a SIGSEGV in ``clients_calculate_size``. The failure lands
on the *second* create, which is what makes it so bad: by then other pooled agents hold
sessions on that server, so one agent's ``shell_create`` destroys every other agent's
sessions.

Three revisions of the plan each inferred a different cause (ordering, then the option
itself) and each was wrong in the same direction. The measured answer is that the **scope**
is the variable: the per-window form ``-w -t '=<name>:'`` is safe (0/15). Phase 2 needs
neither, so the grep below is for the string at all -- the cheapest assertion that cannot
be satisfied by a subtly wrong scope.

⚠️ For whoever adds attach support in Phase 3/4: "set it lazily at first attach" is FATAL if
implemented with ``-g``. It detonates the shared server on the next agent's ``shell_create``,
i.e. this same defect with a longer fuse.
"""

from __future__ import annotations

import ast
from pathlib import Path

PACKAGES = Path(__file__).resolve().parents[2] / "packages"
SPIKE = Path(__file__).resolve().parents[2] / "spike" / "tmux_spike.py"


def _package_sources() -> list[Path]:
    sources = sorted(PACKAGES.rglob("*.py"))
    assert sources, f"no package sources found under {PACKAGES}"
    return sources


def test_no_package_source_passes_window_size_to_tmux() -> None:
    """Grep-assertable: ``grep -R '"window-size"' packages/`` must find nothing.

    Quoted, because an argv element is the only shape that reaches tmux. The prose above --
    and ``errors.py``'s CRITICAL log, which names the option so an operator can act on it --
    must not be what makes this test pass or fail.
    """
    offenders = [
        str(path.relative_to(PACKAGES))
        for path in _package_sources()
        if '"window-size"' in path.read_text() or "'window-size'" in path.read_text()
    ]
    assert offenders == [], (
        "`window-size` is passed to tmux from shipped code: at global scope it kills the "
        f"tmux server on the next new-session, taking every agent's sessions. {offenders}"
    )


def test_no_package_source_has_window_size_as_a_string_constant() -> None:
    """The same rule again, structurally, so a differently-quoted literal cannot slip past.

    A string constant equal to ``window-size`` has exactly one use: an argv element.
    """
    offenders: list[str] = []
    for path in _package_sources():
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Constant) and node.value == "window-size":
                offenders.append(f"{path.relative_to(PACKAGES)}:{node.lineno}")
    assert offenders == [], f"`window-size` used as a tmux argument: {offenders}"


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
