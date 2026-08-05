#!/usr/bin/env python3
"""The three static assertions on the bundle files. Run as a step of ``make lint``.

These are file assertions, not tests of running code, and they are the only mitigation for two
failures that are certainties rather than possibilities:

1. Someone adds ``lifecycle: started: true`` to the App resource -- the natural thing to add
   when an app will not start -- and every ``bundle deploy`` from then on silently reverts the
   code deployment ``scripts/deploy-app.sh`` made.
2. The ``dev`` and ``prod`` targets share an app name, so ``make deploy TARGET=dev`` reconciles
   the production App. Nothing errors, because ``mode: development`` does not prefix app names.

They live in ``make lint`` rather than in ``tests/`` for two reasons. ``make lint`` is a lane a
reader already reads as "static checks", and it runs on a checkout with no Databricks
credential, which is a hard requirement here: these must not authenticate. A separate ``make``
target was the alternative and is not one, because CI invokes a fixed list of targets and a new
one would be in none of them.

CRITICAL: every check begins by asserting the files EXIST.

That is not defensive padding. Before this bundle was written there was no ``databricks.yml``
and no ``resources/``, and the obvious form of each of these checks -- ``grep``, then fail if it
matched -- PASSES on that tree. ``grep`` exits 2 on a missing file, the fail branch is not
taken, and the check reports success against an artifact that does not exist. The same defect
was found four separate times while this work was planned, in four checks written by people who
had just read about it. So each check here asserts its input exists, then asserts it found the
thing it is about to inspect, and only then inspects it. A check that cannot fail is worse than
no check, because it is also a claim.

Deliberately NOT in scope: ``docs/``, ``tests/``, and comments anywhere.

A doc recording a manual verification recipe, a test fixture, and a comment naming the
hierarchical form are all describing a resource path rather than building one, and all three
legitimately spell it out. Every file this checks is ``#``-commented -- Makefile, shell, Python
and YAML -- so one rule covers them: check three reads each line up to its first ``#``.

The residual, stated rather than left to be discovered: a hardcoded resource path hidden inside
a comment passes. It is also inert, because a comment does not build a DSN. The simplification
is that a ``#`` inside a quoted string ends the scan for that line early, which can only make
this check look at less, never at the wrong thing.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# NOTE: `yaml` is imported LAZILY, inside the two functions that parse a bundle file. It is not a
# project dependency -- `make lint` supplies it with `uv run --no-project --with pyyaml`, so that a
# YAML parser nothing else in the repo needs stays out of pyproject.toml.
#
# The lazy import is what lets `tests/unit/test_check_bundle_statics.py` import this module and
# assert on `_EXPANSION`'s vocabulary without a parser present. Those regexes ARE the rule, so
# widening one must be a test failure rather than a review catch -- and a top-level import made
# them unreachable from `make test`.

REPO = Path(__file__).resolve().parent.parent
BUNDLE_FILE = REPO / "databricks.yml"
RESOURCES_DIR = REPO / "resources"
APP_KEY = "shellbox_app"

# The forbidden and required forms of a Lakebase resource path, built rather than written.
#
# This file is itself inside the scope check three scans, so a literal "projects/" here would be
# a finding against this script. Composing the token keeps the scan honest with no exclusion
# list -- and an exclusion list is exactly how a scope check stops covering the thing that
# matters.
_SEGMENT = "projects"
# What counts as "this segment is interpolated, not written down". Two shell forms and one
# Python form.
#
# The Python form -- a bare `{`, as in an f-string's `f"projects/{project}"` -- was added when
# `scripts/lakebase_branch.py` arrived and this check reported it twice. That was a FALSE
# POSITIVE: the value comes from a `--project` argument, which `make lakebase-branch-up` cuts out
# of `SHELLBOX_PG_RESOURCE`, so no project name is written anywhere. The rule was right and its
# vocabulary was shell-only.
#
# It does NOT loosen the rule. A literal `projects/shellbox-pg-dev` still fails, because `s` is
# none of `{`, `$(` or `${` -- asserted by `test_a_literal_project_name_is_still_caught` in
# `tests/unit/test_check_bundle_statics.py`.
_EXPANSION = r"(?:\$\(|\$\{|\{)"
ANY_PROJECT_PATH = re.compile(rf"{_SEGMENT}/")
CONSTRUCTED_PATH = re.compile(
    rf"{_SEGMENT}/{_EXPANSION}[^/]+/branches/{_EXPANSION}[^/]+/endpoints/{_EXPANSION}[^/]+"
)
EXPANDED_SEGMENT = re.compile(rf"{_SEGMENT}/{_EXPANSION}")


def code_lines(text: str) -> list[tuple[int, str]]:
    """The numbered lines of ``text`` with comments removed, and blank results dropped."""
    lines = []
    for number, line in enumerate(text.splitlines(), 1):
        code = line.split("#", 1)[0]
        if code.strip():
            lines.append((number, code))
    return lines


class Failures(list):
    """Collected failures, so one run reports every problem instead of only the first."""

    def check(self, ok: bool, message: str) -> bool:
        if not ok:
            self.append(message)
        return ok


def require_bundle_files(failures: Failures) -> bool:
    """Step (a) of all three checks: the artifact under inspection exists."""
    ok = failures.check(
        BUNDLE_FILE.is_file(), f"{BUNDLE_FILE.relative_to(REPO)} does not exist"
    )
    ok &= failures.check(
        RESOURCES_DIR.is_dir(), f"{RESOURCES_DIR.relative_to(REPO)}/ does not exist"
    )
    return ok


def load_resource_files() -> dict[Path, dict]:
    import yaml

    return {path: yaml.safe_load(path.read_text()) or {} for path in resource_files()}


def resource_files() -> list[Path]:
    return sorted(RESOURCES_DIR.glob("*.yml"))


def build_machinery() -> list[Path]:
    """The files check three scopes: the Makefile, and everything under ``scripts/``.

    The scope is exactly this, and the reason is that the property being asserted is about how
    a deploy BUILDS a resource path. A docstring or a test fixture that spells out the same form
    is describing it, not building it.
    """
    paths = [REPO / "Makefile"]
    paths += sorted(p for p in (REPO / "scripts").rglob("*") if p.is_file())
    return [p for p in paths if p.is_file()]


def find_app_resources(loaded: dict[Path, dict]) -> dict[str, tuple[Path, dict]]:
    found: dict[str, tuple[Path, dict]] = {}
    for path, doc in loaded.items():
        apps = (doc.get("resources") or {}).get("apps") or {}
        for key, body in apps.items():
            found[key] = (path, body or {})
    return found


def check_no_lifecycle(failures: Failures) -> None:
    """The App resource declares neither ``lifecycle`` nor ``started``.

    This absence is the single line separating "two mechanisms coexist" from "the bundle
    silently reverts every script deploy", so it is asserted rather than only commented.
    """
    apps = find_app_resources(load_resource_files())

    # The positive witness. Without it this check also passes on a bundle declaring no app at
    # all -- which cannot revert a deploy only because it cannot deploy anything.
    if not failures.check(
        bool(apps), "no app resource found in resources/*.yml; there is nothing to assert on"
    ):
        return
    failures.check(
        APP_KEY in apps,
        f"no app resource keyed {APP_KEY!r}; found {sorted(apps)}. That key is part of the "
        f"documented one-time bind command in docs/deploy.md, so renaming it breaks the "
        f"adoption step",
    )

    for key, (path, body) in sorted(apps.items()):
        where = f"{path.relative_to(REPO)} resources.apps.{key}"
        for forbidden in ("lifecycle", "started"):
            failures.check(
                forbidden not in body,
                f"{where} declares {forbidden!r}. Remove it. With `started: true` every "
                f"`bundle deploy` re-deploys the app from the bundle's own source_code_path "
                f"and clobbers whatever scripts/deploy-app.sh deployed. With it omitted the "
                f"bundle creates no app deployment and cannot revert the script",
            )
        # `started` is nested under `lifecycle`, so the flat check above would miss it if a
        # future schema moved it. Scan the whole subtree rather than trusting today's shape.
        failures.check(
            "started" not in _flatten_keys(body),
            f"{where} carries a nested {'started'!r} key somewhere in its subtree",
        )


def _flatten_keys(node: object) -> set[str]:
    keys: set[str] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            keys.add(str(key))
            keys |= _flatten_keys(value)
    elif isinstance(node, list):
        for item in node:
            keys |= _flatten_keys(item)
    return keys


def check_per_target_app_name(failures: Failures) -> None:
    """The ``dev`` and ``prod`` targets declare DIFFERENT app names.

    ``mode: development`` applies its ``name_prefix`` preset to jobs and pipelines, not to apps,
    so a dev-target validate keeps the app name verbatim. Two targets sharing a name therefore
    means a dev deploy reconciles the production App and reports success.
    """
    import yaml

    bundle = yaml.safe_load(BUNDLE_FILE.read_text()) or {}
    targets = bundle.get("targets") or {}

    if not failures.check(
        set(targets) >= {"dev", "prod"},
        f"databricks.yml must declare both a dev and a prod target; found {sorted(targets)}",
    ):
        return

    # The App resource must take its name FROM a variable. If it hardcodes one, the per-target
    # values below are decorative and this check would compare two numbers nothing reads.
    apps = find_app_resources(load_resource_files())
    for key, (path, body) in sorted(apps.items()):
        failures.check(
            "${var." in str(body.get("name", "")),
            f"{path.relative_to(REPO)} resources.apps.{key}.name is not a variable reference, "
            f"so the per-target names in databricks.yml do not reach it",
        )

    names = {}
    for target in ("dev", "prod"):
        value = ((targets[target] or {}).get("variables") or {}).get("app_name")
        if failures.check(
            bool(value), f"target {target!r} declares no app_name variable value"
        ):
            names[target] = value

    if len(names) == 2:
        failures.check(
            names["dev"] != names["prod"],
            f"the dev and prod targets both name the app {names['dev']!r}. A dev deploy would "
            f"reconcile the production App, and nothing would error",
        )


def check_no_literal_project(failures: Failures) -> None:
    """A Lakebase resource path is CONSTRUCTED from declared ids, never written out.

    Three parts. The bundle files contain no such path at all, because the endpoint id does not
    resolve there and a path written into YAML could only be a hardcoded one. Every occurrence
    in the build machinery is followed by an expansion. And the full constructed form occurs at
    least once -- the positive witness, without which the second part is a rule about a string
    that nothing produces.
    """
    for path in [BUNDLE_FILE, *resource_files()]:
        hits = [
            f"{path.relative_to(REPO)}:{n}"
            for n, line in code_lines(path.read_text())
            if ANY_PROJECT_PATH.search(line)
        ]
        failures.check(
            not hits,
            f"a Lakebase resource path appears in a bundle file at {', '.join(hits)}. "
            f"The endpoint id never resolves pre-deploy, so a path there can only be hardcoded",
        )

    witnessed = False
    for path in build_machinery():
        try:
            text = path.read_text()
        except UnicodeDecodeError:
            continue
        for number, line in code_lines(text):
            for match in ANY_PROJECT_PATH.finditer(line):
                failures.check(
                    bool(EXPANDED_SEGMENT.match(line[match.start() :])),
                    f"{path.relative_to(REPO)}:{number} writes a bare name after the project "
                    f"segment. Every segment must come from a bundle variable",
                )
            witnessed |= bool(CONSTRUCTED_PATH.search(line))

    failures.check(
        witnessed,
        "no file in the Makefile or scripts/ constructs a full "
        "project/branch/endpoint path from expansions. Nothing derives the endpoint the "
        "migration and the credential mint address, so the rule above governs nothing",
    )


def main() -> int:
    failures = Failures()
    if not require_bundle_files(failures):
        # Nothing below can run, and reporting "OK" here is the exact defect this guards.
        print("FAIL bundle statics: the bundle does not exist")
        for message in failures:
            print(f"  - {message}")
        return 1

    checks = (
        ("no-lifecycle", check_no_lifecycle),
        ("per-target-app-name", check_per_target_app_name),
        ("no-literal-project", check_no_literal_project),
    )
    for name, check in checks:
        before = len(failures)
        check(failures)
        status = "ok" if len(failures) == before else "FAIL"
        print(f"    {status:4}  {name}")

    if failures:
        print("\nbundle statics failed:")
        for message in failures:
            print(f"  - {message}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
