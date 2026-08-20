#!/usr/bin/env python3
"""The static assertions on a ``pex3 lock`` file -- the one the release artifact is built from.

Run as steps of ``scripts/build_artifact.sh`` (issue #21), in the same shape ``make app-lock`` runs
``scripts/check_deploy_lock.py``: generate the lock through the configured index, rewrite the
mirror hosts to public ones, then prove the rewrite with these two checks before the lock is used.

WHY THE MIRROR REWRITE EXISTS, since these checks are what make it safe. This is the same argument
``scripts/check_deploy_lock.py`` makes for ``uv.lock``, transposed to a pex lock:

* This workstation resolves through an internal PyPI mirror (a user-level ``~/.pip/pip.conf`` and a
  ``uv.toml`` both name it). ``pex3 lock create`` records that host in every artifact url.
* The published artifact must resolve for everyone, so the lock is rewritten to public hosts.
* The mirror preserves PyPI's url layout 1:1 -- ``/packages/<hash-path>/<file>`` -- so a HOST
  rewrite is exact and touches nothing else. ``--assert-hosts`` proves the rewrite reached every
  url; ``--assert-hashes-unchanged`` proves it changed only hosts and no artifact.
* The lock pins a per-artifact sha256 and ``pex --lock`` verifies it at build. A wrong rewrite
  therefore fails loudly at build rather than shipping a wrong artifact -- the property that makes
  a textual host substitution acceptable to do to a lockfile.
* It is a no-op on a machine already resolving public PyPI.

CI-ONLY, in the sense ``scripts/check_lockfile.py`` documents. The build that produces the lock is
CI-only, because pex runs its vendored pip with ``--isolated`` (measured: ambient ``pip.conf`` and
``PIP_INDEX_URL`` never reach it) and because ``pypi.org`` is blackholed to ``127.0.0.1`` on the
author's box (``/etc/hosts``, measured). So a bare ``--index https://pypi.org/simple`` is
connection-refused here, and the mirror-then-rewrite path is required, not optional -- which is
also why this checker is exercised by a mutation test against SYNTHETIC locks
(``tests/unit/test_check_pex_lock.py``) rather than against a real one this machine cannot build.

RESIDUAL, stated rather than left to be discovered (issue #21, OQ-7). ``--assert-hosts`` allows
exactly one host, ``files.pythonhosted.org``, for every ``url`` it finds, the
``scripts/check_lockfile.py`` form. That is correct IF a pex lock records only artifact download
urls under a ``url`` key. It has NOT been confirmed on a real lock what other ``url``-keyed
strings a ``pex3 lock`` carries -- a VCS or local ``source`` url, or a separate index url naming
``pypi.org``, would live under a ``url`` key too and would (correctly, for a VCS pin; wrongly, for
the index) be rejected by an allowlist of one. Confirm OQ-7 against the first CI-produced lock
before relying on this: if the lock names ``pypi.org`` as an index under a ``url`` key, this needs
the two-host treatment ``scripts/check_deploy_lock.py`` gives ``uv.lock`` (artifact host vs index
host), not an allowlist of one.

CRITICAL: every mode asserts its input EXISTS, then asserts it FOUND what it is about to inspect,
and only then inspects it. That ordering is not padding. The obvious form of the host check --
``grep`` for the mirror, fail if it matched -- PASSES on a missing file and on an empty one,
because ``grep`` exits 2 on a missing file and 1 on no match, and the fail branch is never taken.
``scripts/check_lockfile.py``, ``scripts/check_deploy_lock.py`` and
``scripts/check_bundle_statics.py`` all carry this warning, and this repo has found the defect more
than once.

The host rule is an ALLOWLIST and the comparison is EXACT. A blocklist of internal names has to
name every mirror anyone might configure, and the one it does not name is the one that leaks. An
exact comparison matters because ``files.pythonhosted.org.example`` contains the allowed host and
is a different server.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

# The only host an artifact url may name after the rewrite. Compared for equality against the
# url's host.
ALLOWED_HOST = "files.pythonhosted.org"

# The non-vacuity floor. This is NOT a measured count, and the distinction is deliberate: the pex
# lock is produced only by the CI build (see the CI-ONLY note above), so its real url count cannot
# be measured on the author's machine. The floor is therefore the weakest useful guard -- it
# rejects an empty or truncated lock that holds nothing to inspect -- and no more.
#
# MEASURE AND RAISE THIS from the first CI-produced lock, the way ``scripts/check_lockfile.py`` and
# ``scripts/check_deploy_lock.py`` document their floors: record the artifact-url count, the date
# and the command (``pex3 lock create ...`` as run by ``scripts/build_artifact.sh``), and set this
# to that count minus a margin sized to exceed the largest transitive wheel subtree.
MIN_ARTIFACT_COUNT = 1

# The keys a pex lock writes a url and a hash under. The walk below is schema-independent (it finds
# these keys wherever they nest) rather than reading ``locked_resolves[].locked_requirements[]``
# ``.artifacts[].url`` by name, because a lock format that grows a third url-bearing place would
# escape a check that named the path. This mirrors the recursive collectors in
# ``scripts/check_lockfile.py`` and ``scripts/check_deploy_lock.py``.
URL_KEY = "url"
HASH_KEY = "hash"


def _fail(message: str) -> int:
    """Print the failure and return the failing exit status."""
    print(f"ERROR: {message}", file=sys.stderr)
    return 1


def _load(path: Path) -> dict[str, Any] | int:
    """The parsed lock, or a failing exit status with the reason already printed."""
    if not path.is_file():
        return _fail(
            f"{path} does not exist, so there is nothing to check. The generate step "
            "('pex3 lock create') and the host rewrite must run before this one -- "
            "scripts/build_artifact.sh runs all three in order."
        )
    raw = path.read_bytes()
    if not raw:
        return _fail(f"{path} is empty.")
    try:
        document = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        # A truncated lock usually stops being valid JSON, so this is a check and not merely input
        # validation.
        return _fail(f"{path} is not valid JSON, so it is not a pex lock: {error}")
    if not isinstance(document, dict):
        return _fail(f"{path} is valid JSON but not an object, so it is not a pex lock.")
    return document


def _collect_urls(node: Any, trail: str = "") -> list[tuple[str, str]]:
    """Every ``url`` string in the document, with the key path that held it."""
    found: list[tuple[str, str]] = []
    if isinstance(node, dict):
        for key, value in node.items():
            here = f"{trail}.{key}" if trail else key
            if key == URL_KEY and isinstance(value, str):
                found.append((here, value))
            else:
                found.extend(_collect_urls(value, here))
    elif isinstance(node, list):
        for index, item in enumerate(node):
            found.extend(_collect_urls(item, f"{trail}[{index}]"))
    return found


def _hashes(node: Any) -> Counter[str]:
    """Every ``hash`` string in the document, counted.

    A Counter and not a set, deliberately. Two artifacts legitimately share no hash, so a duplicate
    appearing or disappearing is a real change to what ships, and a set comparison would hide it.
    """
    found: Counter[str] = Counter()
    if isinstance(node, dict):
        for key, value in node.items():
            if key == HASH_KEY and isinstance(value, str):
                found[value] += 1
            else:
                found.update(_hashes(value))
    elif isinstance(node, list):
        for item in node:
            found.update(_hashes(item))
    return found


def assert_hosts(path: Path) -> int:
    """Assert the rewrite landed: every artifact host is the public one, and there are some."""
    document = _load(path)
    if isinstance(document, int):
        return document

    urls = _collect_urls(document)
    if not urls:
        return _fail(
            f"{path} holds no {URL_KEY!r} entries at all. Either the generate step produced "
            f"nothing usable, or the pex lock format moved and this check no longer knows where to "
            f"look."
        )

    offenders: list[tuple[str, str, str]] = []
    for key_path, url in urls:
        host = urlsplit(url).hostname or ""
        if host != ALLOWED_HOST:
            offenders.append((key_path, host or "(no host)", url))

    if offenders:
        hosts = sorted({host for _, host, _ in offenders})
        for key_path, host, url in offenders[:10]:
            print(f"  {key_path}: host {host!r} in {url}", file=sys.stderr)
        if len(offenders) > 10:
            print(f"  ... and {len(offenders) - 10} more", file=sys.stderr)
        return _fail(
            f"{len(offenders)} of {len(urls)} urls in {path} name a host other than "
            f"{ALLOWED_HOST}: {', '.join(hosts)}. This lock still points at the index that "
            f"resolved it. scripts/build_artifact.sh performs the mirror-to-public rewrite; a lock "
            f"generated by hand has not had it. (If a named host is a deliberate VCS/index url, "
            f"read the OQ-7 residual in this script's header before widening the allowlist.)"
        )

    if len(urls) < MIN_ARTIFACT_COUNT:
        return _fail(
            f"{path} holds {len(urls)} artifact urls, fewer than the floor of "
            f"{MIN_ARTIFACT_COUNT}. "
            f"The generate step produced a truncated or near-empty lock. If the dependency set "
            f"really is this small, re-measure and update MIN_ARTIFACT_COUNT in "
            f"{Path(__file__).name} with the new number, the date and the command."
        )

    print(
        f"OK: {len(urls)} artifact urls in {path}, all on {ALLOWED_HOST}, "
        f"at or above the floor of {MIN_ARTIFACT_COUNT}."
    )
    return 0


def assert_hashes_unchanged(path: Path, baseline: Path) -> int:
    """Assert the rewrite changed no hash, so it changed no artifact.

    ``baseline`` is the pre-rewrite file. ``scripts/build_artifact.sh`` uses the ``.bak`` copy
    ``sed -i.bak`` writes, so the baseline is the exact bytes the rewrite read and nothing
    regenerates it.
    """
    after = _load(path)
    if isinstance(after, int):
        return after
    before = _load(baseline)
    if isinstance(before, int):
        return before

    after_hashes = _hashes(after)
    before_hashes = _hashes(before)

    # The witness. Without it, two files holding no hashes at all compare equal and this reports
    # success on a rewrite of nothing.
    if not before_hashes:
        return _fail(
            f"{baseline} holds no {HASH_KEY!r} values, so there is nothing to compare. Either it "
            f"is not the pre-rewrite lock, or pex stopped pinning per-artifact hashes -- in which "
            f"case 'pex --lock' no longer verifies what the rewrite produced, and that is the "
            f"assumption this whole step rests on."
        )

    if after_hashes != before_hashes:
        gained = after_hashes - before_hashes
        lost = before_hashes - after_hashes
        for value, count in sorted(lost.items())[:5]:
            print(f"  only before the rewrite (x{count}): {value}", file=sys.stderr)
        for value, count in sorted(gained.items())[:5]:
            print(f"  only after the rewrite  (x{count}): {value}", file=sys.stderr)
        return _fail(
            f"the rewrite changed the set of pinned hashes in {path}: {len(lost)} lost, "
            f"{len(gained)} gained. A HOST rewrite cannot do that -- it edits url hosts and "
            f"nothing else -- so the rewrite matched more than it was meant to, or the lock was "
            f"regenerated between the baseline and now. Do not build from this file."
        )

    print(
        f"OK: the rewrite left all {sum(before_hashes.values())} pinned hashes "
        f"({len(before_hashes)} distinct) unchanged, so it changed no artifact."
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__ and __doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--assert-hosts",
        action="store_true",
        help="fail unless every artifact host is exactly the allowed public host, and there are "
        "some",
    )
    mode.add_argument(
        "--assert-hashes-unchanged",
        action="store_true",
        help="fail unless the multiset of pinned hashes matches --baseline exactly",
    )
    parser.add_argument("path", type=Path, help="the pex lock, normally dist/shellbox.lock")
    parser.add_argument(
        "--baseline",
        type=Path,
        help="the pre-rewrite lock; required with --assert-hashes-unchanged",
    )
    args = parser.parse_args(argv)

    if args.assert_hashes_unchanged:
        if args.baseline is None:
            parser.error("--assert-hashes-unchanged requires --baseline")
        return assert_hashes_unchanged(args.path, args.baseline)
    return assert_hosts(args.path)


if __name__ == "__main__":
    sys.exit(main())
