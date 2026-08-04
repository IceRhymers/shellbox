"""Every module the App DEPLOYS must parse on the Apps runtime's Python, not on this one.

The runtime's Python is **3.12**, and it is 3.12 BY DECLARATION rather than by measurement.
The deploy root ships ``pyproject.toml`` + ``uv.lock`` and no ``requirements.txt``, so
Databricks Apps installs on the uv path -- which honors ``requires-python`` and provisions the
interpreter it names. `packages/shellbox-app/src/pyproject.toml` declares
``>=3.12,<3.13`` and `.python-version` beside it pins 3.12.

WHAT THIS TEST WAS WRITTEN FOR, because that history is why it still exists. On the pip path
the runtime was **Python 3.11** -- measured 2026-08-04 from a deploy log's
``./.venv/lib/python3.11/site-packages`` and its ``cp311`` wheels -- while every
``pyproject.toml`` in this repo declared ``requires-python = ">=3.12"`` and local development
and CI both ran 3.12. That declaration was true of the workspace and **false of the deployed
artifact**, so `make lint` and `make test` could both pass on syntax the App could not import.
It happened: `shellbox_app/inventory.py` shipped a PEP 695 type-parameter list
(``def f[T](...)``). `scripts/deploy-app.sh`'s import check caught it, but only because uv
happened to resolve 3.11 for its ephemeral environment -- luck rather than design.

The two numbers agree now, so this file is a GUARD on the agreement rather than a live bug
report. It fails the moment `scripts/deploy-app.sh`'s ``RUNTIME_PYTHON`` names something older
than what a deployed module uses -- which is what a platform change, or a widened
``requires-python``, would look like.

WHY ``ast.parse`` AND NOT A SECOND INTERPRETER. ``feature_version`` makes CPython's own parser
reject syntax newer than the named version, so this needs no second install, no network and no
subprocess. It catches SYNTAX only -- a 3.13 standard-library call would still pass here and
fail on the runtime. `scripts/deploy-app.sh` catches that class, because it imports the modules
for real.

CRITICAL, ON THE STRENGTH OF ``feature_version`` WHEN THIS HOST AND THE RUNTIME MATCH.
MEASURED 2026-08-04 on CPython 3.12.8, which is what CI pins: every construct newer than 3.12
raises ``SyntaxError`` from this host's parser with ``feature_version=None`` as well as with
``feature_version=(3, 12)`` -- PEP 696 defaults (``def f[T = int](...)``), PEP 758 unparenthesized
``except`` tuples, and PEP 750 t-strings all do. The sweep below is therefore still SOUND, because
a rejection is a rejection whichever half produced it. But it means a non-vacuity test cannot be
written with a post-3.12 construct: such a test passes with ``feature_version`` removed
altogether, so it proves nothing about the mechanism it is there to prove. The non-vacuity test at
the bottom of this file uses the version pair that CAN be told apart on this host, and
`test_the_hosts_python_is_not_older_than_the_runtimes` guards the one relationship that would
turn the sweep into a check of the wrong interpreter.

NOTE: `shellbox-mcp` is deliberately NOT checked. It runs in a Lakebox sandbox on Ubuntu 24.04,
not on the Apps runtime, and it is free to use syntax the App cannot. The packages checked here
are exactly the ones `scripts/deploy-app.sh` copies into the deploy root, read from that script
so the two cannot drift.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
DEPLOY_SCRIPT = REPO / "scripts" / "deploy-app.sh"


def _runtime_feature_version() -> tuple[int, int]:
    """The runtime's Python, read from `scripts/deploy-app.sh`'s own pin.

    One declaration. A restated literal here would let the two drift, and the drift would be
    invisible: this test would keep passing against a version nothing deploys on.
    """
    match = re.search(r"^RUNTIME_PYTHON=(\d+)\.(\d+)", DEPLOY_SCRIPT.read_text(), re.MULTILINE)
    assert match is not None, (
        f"{DEPLOY_SCRIPT.relative_to(REPO)} no longer declares RUNTIME_PYTHON=<major>.<minor>. "
        "That pin is this test's only source for which interpreter the App runs on."
    )
    return int(match.group(1)), int(match.group(2))


def _deployed_packages() -> list[Path]:
    """The package directories `scripts/deploy-app.sh` copies into the deploy root.

    Derived from the script's `cp -R` lines rather than listed here, so adding a fourth package
    to the deploy root extends this test automatically instead of silently leaving it behind.
    """
    found = re.findall(
        r'^cp -R "\$REPO/(packages/[^"]+/src/([a-z_]+))" "\$STAGE/"',
        DEPLOY_SCRIPT.read_text(),
        re.MULTILINE,
    )
    assert found, (
        f"no `cp -R .../src/<package>` lines found in {DEPLOY_SCRIPT.relative_to(REPO)}. "
        "This test derives what is deployed from that script; if the staging step was "
        "rewritten, teach this helper the new shape rather than hardcoding a list."
    )
    return [REPO / relative for relative, _ in found]


def _deployed_modules() -> list[Path]:
    return sorted(
        path
        for package in _deployed_packages()
        for path in package.rglob("*.py")
        if "__pycache__" not in path.parts
    )


def test_the_staged_packages_are_the_three_that_ship() -> None:
    """A witness, so the sweep below cannot pass by finding nothing.

    Without this, a change that broke `_deployed_packages`'s regex into matching zero
    directories would make every assertion here vacuously true.
    """
    names = {path.name for path in _deployed_packages()}
    assert names == {"shellbox_app", "shellbox_transport", "shellbox_registry"}, names
    assert "shellbox_mcp" not in names, (
        "shellbox-mcp is not deployed to the Apps runtime and is not bound by its Python; "
        "it runs in a sandbox on Ubuntu 24.04"
    )


def test_there_are_modules_to_check() -> None:
    """The other half of the witness: the packages exist and hold Python."""
    modules = _deployed_modules()
    assert len(modules) >= 15, f"only {len(modules)} deployed modules found"


@pytest.mark.parametrize("module", _deployed_modules(), ids=lambda p: p.name)
def test_every_deployed_module_parses_on_the_runtimes_python(module: Path) -> None:
    """The assertion. Fails on the file and line, naming the runtime it would break on."""
    version = _runtime_feature_version()
    try:
        ast.parse(module.read_text(), filename=str(module), feature_version=version)
    except SyntaxError as error:  # pragma: no cover - the failure path is the point
        pytest.fail(
            f"{module.relative_to(REPO)}:{error.lineno} does not parse on Python "
            f"{version[0]}.{version[1]}, which is what the Databricks Apps runtime uses: "
            f"{error.msg}. The App crash-loops on a SyntaxError at import, behind a deploy that "
            f"reported success. Either use syntax the runtime has, or move the runtime forward -- "
            f"which means requires-python and .python-version in packages/shellbox-app/src/ and "
            f"RUNTIME_PYTHON in scripts/deploy-app.sh, together. See this file's docstring."
        )


# PEP 695's type-parameter list, which is 3.12 syntax and is the exact construct that shipped
# once. It is used below as the SOURCE for both halves of the non-vacuity pair, because 3.12 is
# where this host's parser and `feature_version` can still be told apart -- see the CRITICAL note
# in this module's docstring for why nothing newer can be.
PEP_695_SOURCE = "def f[T](x: T) -> T: return x"

# The last version that REFUSES the source above. Written as a literal and not derived from the
# runtime pin, deliberately: the pair below is a test of the PARSER, and it must keep proving that
# whatever `scripts/deploy-app.sh` pins.
BEFORE_PEP_695 = (3, 11)


def test_feature_version_refuses_syntax_newer_than_the_version_it_names() -> None:
    """Non-vacuity. The sweep above is only worth having if ``feature_version`` really refuses.

    WHY THIS USES ``BEFORE_PEP_695`` AND NOT THE RUNTIME PIN. The runtime is 3.12 and this test
    host is 3.12, so at the runtime's own version there is nothing left for ``feature_version`` to
    refuse that the host's parser does not refuse anyway. Asserting on a post-3.12 construct
    instead would pass with ``feature_version`` deleted from the call, which is a test that cannot
    fail for the reason it claims. This pair can: the same source parses one minor later.
    """
    with pytest.raises(SyntaxError, match="[Tt]ype parameter"):
        ast.parse(PEP_695_SOURCE, feature_version=BEFORE_PEP_695)


def test_feature_version_accepts_the_same_source_one_minor_later() -> None:
    """The other half of the pair, and the negative control.

    Without it the test above passes on a parser that refuses everything, and the refusal above
    would prove nothing about the version being the reason.
    """
    later = (BEFORE_PEP_695[0], BEFORE_PEP_695[1] + 1)
    ast.parse(PEP_695_SOURCE, feature_version=later)


def test_the_check_accepts_syntax_the_runtime_supports() -> None:
    """The sweep's own version accepts what the deployed modules actually use.

    ``TypeVar`` is what `shellbox_app/inventory.py` is spelled with. PEP 695 is checked too, and
    it is now the more informative of the two: it was a SyntaxError on the 3.11 runtime, and it is
    accepted here, which is the whole of what this change bought.
    """
    version = _runtime_feature_version()
    ast.parse(
        "from typing import TypeVar\nT = TypeVar('T')\ndef f(x: T) -> T: return x",
        feature_version=version,
    )
    ast.parse(PEP_695_SOURCE, feature_version=version)


def test_the_hosts_python_is_not_older_than_the_runtimes() -> None:
    """The one relationship that would make the sweep check the WRONG interpreter.

    ``ast.parse`` cannot enforce a ``feature_version`` newer than the interpreter running it --
    there is no parser for syntax CPython does not yet know. So a ``RUNTIME_PYTHON`` ahead of this
    host means the sweep checks the HOST's syntax while its failure message names the runtime's,
    and it would then reject syntax the App accepts.

    CI pins 3.12 in every job of `.github/workflows/ci.yml`. If `scripts/deploy-app.sh`'s pin
    moves forward, that pin has to move with it, and this is where that is said.
    """
    runtime = _runtime_feature_version()
    host = sys.version_info[:2]
    assert host >= runtime, (
        f"this test runs on Python {host[0]}.{host[1]} and scripts/deploy-app.sh pins "
        f"RUNTIME_PYTHON={runtime[0]}.{runtime[1]}. ast.parse cannot check syntax newer than the "
        "interpreter parsing it, so the sweep in this file would silently check "
        f"{host[0]}.{host[1]} while reporting {runtime[0]}.{runtime[1]}. Raise the Python in "
        ".github/workflows/ci.yml and in the workspace pyproject.toml files together with the pin."
    )
