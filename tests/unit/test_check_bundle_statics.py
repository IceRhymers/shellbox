"""`scripts/check_bundle_statics.py`'s vocabulary -- what counts as an interpolated path segment.

`A2` says a Lakebase resource path is CONSTRUCTED from declared ids and never written out, and
`check_no_literal_project` enforces it by requiring an interpolation immediately after
``projects/``. That makes `_EXPANSION` the whole rule: whatever it accepts is what the check
permits, so widening it is how the rule quietly stops meaning anything.

It WAS widened, on 2026-08-05, to accept Python's f-string ``{``. `scripts/lakebase_branch.py`
builds its paths in Python (``f"projects/{project}"``, from a ``--project`` argument), and the
check -- whose vocabulary was shell-only -- reported it twice as a bare name. A false positive,
but the fix touches the rule itself, so this file exists to hold the line the fix must not cross.

The one assertion that matters is `test_a_literal_project_name_is_still_caught`. Everything else
here is scaffolding for it.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_bundle_statics.py"

_spec = importlib.util.spec_from_file_location("check_bundle_statics", _SCRIPT)
assert _spec is not None and _spec.loader is not None, f"cannot load {_SCRIPT}"
statics = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(statics)

# Composed, never written, for the reason the script composes its own: a literal here would be a
# finding against this file, which the check also scans.
_P = "projects" + "/"


@pytest.mark.parametrize(
    "form",
    [
        f'"{_P}$(PG_PROJECT_ID)/branches/x"',  # shell, command substitution
        f'"{_P}${{PG_PROJECT_ID}}/branches/x"',  # shell, brace expansion
        f'f"{_P}{{project}}/branches/{{branch}}"',  # Python f-string -- added 2026-08-05
    ],
)
def test_an_interpolated_segment_is_accepted(form: str) -> None:
    """The three ways this repo derives a project id. All three are values, not names."""
    match = statics.ANY_PROJECT_PATH.search(form)
    assert match is not None, "the scan should see a project path here at all"
    assert statics.EXPANDED_SEGMENT.match(form[match.start() :]), form


@pytest.mark.parametrize(
    "form",
    [
        f'"{_P}shellbox-pg-dev/branches/production"',  # the real dev project, hardcoded
        f'"{_P}shellbox-pg/branches/production/endpoints/primary"',  # the real prod project
        f"'{_P}some-other-project'",
        f'f"{_P}shellbox-pg-{{target}}"',  # a PARTIAL literal: the prefix is still written down
    ],
)
def test_a_literal_project_name_is_still_caught(form: str) -> None:
    """The line the 2026-08-05 widening must not cross.

    The last case is the subtle one and the reason this is parametrised rather than a single
    assertion: ``f"projects/shellbox-pg-{target}"`` interpolates *something*, so a check that
    merely looked for a ``{`` anywhere on the line would pass it. The project name is still half
    hardcoded, and `EXPANSION` is anchored immediately after the separator precisely so that
    shape fails.
    """
    match = statics.ANY_PROJECT_PATH.search(form)
    assert match is not None, "the scan should see a project path here at all"
    assert not statics.EXPANDED_SEGMENT.match(form[match.start() :]), (
        f"{form} names a project literally and the check accepted it. _EXPANSION has been "
        f"widened past its purpose -- see this module's docstring."
    )


def test_the_constructed_witness_requires_all_three_segments() -> None:
    """The positive witness, which stops the rule governing a string nothing produces.

    A path that interpolates the project but hardcodes the branch is NOT a witness: it is the
    half-derived form the rule exists to prevent.
    """
    full = f'"{_P}$(A)/branches/$(B)/endpoints/$(C)"'
    assert statics.CONSTRUCTED_PATH.search(full)

    partial = f'"{_P}$(A)/branches/production/endpoints/$(C)"'
    assert not statics.CONSTRUCTED_PATH.search(partial)


def test_the_repo_passes_its_own_check() -> None:
    """The check, run for real. It is a `make lint` step, so a failure here is a lint failure --
    this test is what makes that reachable from `make test` too."""
    failures: list[str] = statics.Failures()
    statics.check_no_literal_project(failures)
    assert list(failures) == [], failures


def test_comments_are_stripped_before_scanning() -> None:
    """Prose may cite a path; code may not. Both halves matter -- a scanner that read comments
    would fire on every docstring explaining the rule, and the fix would be an exclusion list."""
    assert statics.code_lines(f"# see {_P}shellbox-pg-dev/branches/production\n") == []
    assert statics.code_lines(f'x = "{_P}$(A)"  # trailing prose\n') != []
