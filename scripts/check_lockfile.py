#!/usr/bin/env python3
"""The static assertions on an EMITTED ``uv.lock``. Run as a step of the CI lockfile job.

CRITICAL: this is a CI-only check. Do NOT add it to ``make lint``.

``uv.lock`` is not committed. The comment above ``uv.lock`` in ``.gitignore`` says why, and it
is still true: ``uv lock`` bakes the artifact URLs of whatever package index the developer has
configured into the file, so a lockfile committed from a machine pointed at an internal mirror
publishes that hostname. Commit ``57ad374`` did exactly that and ``58b83a8`` reverted it.

The reverse is also measured, and it is why the file stays ignored rather than being committed
with public URLs. Measured 2026-08-03 on ``uv 0.10.9``: on a machine whose uv configuration
names an internal mirror, ``uv sync --locked`` against a lockfile whose URL hosts are
``files.pythonhosted.org`` FAILS with ``The lockfile at 'uv.lock' needs to be updated, but
'--locked' was provided``. It fails at the staleness check, before any download, because the
recorded hosts do not match the configured index. A negative control -- the same command against
the mirror-named lockfile -- succeeds. Egress is not the issue: ``files.pythonhosted.org``
answered HTTP 200 in 0.31 s from that same machine. So a committed public-URL lockfile would
break every mirror-configured contributor, and a committed mirror-URL lockfile would break
everyone outside that network. Neither direction is available until there is one index that
every contributor and CI resolve through.

What is available is CI. CI configures no mirror, so a lockfile emitted there resolves against
the public index by construction. The CI job emits one with ``uv lock`` and runs this script
against the artifact it just emitted -- the file exists in the CI workspace whether or not it is
committed.

CRITICAL, and this is the reason for the ``make lint`` prohibition above: on a
mirror-configured developer machine the local ``uv.lock`` legitimately names the mirror, so
``--assert-hosts`` fails there BY DESIGN. Running this in ``make lint`` would fail ``make lint``
on the repository owner's own laptop, and the fix a reader would reach for is deleting the
check.

Two modes, matching the two things the CI job has to prove:

* ``--assert-absent`` -- run on a fresh checkout, BEFORE the emit. It fails if the path exists.
  Without it, someone who commits ``uv.lock`` gets a green lockfile job that asserts on a
  committed artifact instead of an emitted one, and the whole argument above is undone with
  nothing turning red.
* ``--assert-hosts`` -- run AFTER the emit, on the emitted file. Every URL host must be exactly
  ``files.pythonhosted.org``, and there must be at least ``MIN_URL_COUNT`` of them.

CRITICAL: this asserts its input EXISTS, then asserts it FOUND URLs, and only then inspects
them. That ordering is not padding. The obvious form of this check -- ``grep`` for a forbidden
host, fail if it matched -- PASSES on a missing file and on an empty one, because ``grep`` exits
2 on a missing file and 1 on no match, and the fail branch is never taken. It reports success
against an artifact that does not exist. ``scripts/check_bundle_statics.py`` carries the same
warning for the same reason.

The host rule is an ALLOWLIST, not a blocklist of internal names. A blocklist has to name every
mirror anyone might ever configure, and the one it does not name is the one that leaks. It is
also an EXACT host comparison, not a suffix or substring test: ``files.pythonhosted.org.example``
contains the allowed host and is a different server.
"""

from __future__ import annotations

import argparse
import sys
import tomllib
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

# The only host an artifact URL may name. Compared for equality against the URL's host.
ALLOWED_HOST = "files.pythonhosted.org"

# The non-vacuity floor: at least this many artifact URLs, so a broken emit step cannot pass by
# producing an empty or truncated lockfile.
#
# MEASURED, not guessed. On 2026-08-03, ``uv lock`` on ``uv 0.10.9`` against this workspace
# emitted a lockfile holding **727** artifact URLs across 64 packages -- 58 ``sdist.url`` and 669
# ``wheels[].url``. Three facts make that count the right basis for a floor CI can rely on:
#
# 1. It does not depend on which host served the artifacts. The lockfile measured was emitted on
#    a mirror-configured machine. All 727 URLs, with the host rewritten to
#    ``files.pythonhosted.org`` and nothing else changed, answered HTTP 200 -- so the mirror is a
#    path-preserving proxy of the same upstream artifacts, and CI resolving through the public
#    index records the same artifact set.
# 2. It does not depend on the platform that emitted it. ``uv.lock`` is a universal lockfile:
#    that same file holds 242 manylinux, 137 musllinux, 92 macosx and 106 Windows wheels. A
#    count measured on macOS is the count CI sees on Linux.
# 3. Only removing dependencies can drive it down. Adding one raises it.
#
# The floor is **450**, which is 727 minus a margin of 277. The margin is sized to exceed the
# three largest transitive subtrees combined -- ``rpds-py`` 89, ``cffi`` 75 and ``pydantic-core``
# 65, so 229 -- because dropping a dependency must not redden an untouched pull request. It is
# still far above what any broken emit produces, which is zero.
#
# Re-measure and raise this when the dependency set changes materially. Record the new count, the
# date and the command, as above.
MIN_URL_COUNT = 450


def _collect_urls(node: Any, trail: str = "") -> list[tuple[str, str]]:
    """Every ``url`` string anywhere in the parsed document, with the key path that held it.

    The walk is recursive and schema-independent rather than reading ``package[].sdist.url`` and
    ``package[].wheels[].url`` by name. Those are the two places uv writes today, and a check
    that names them stops covering a lockfile format that grows a third.
    """
    found: list[tuple[str, str]] = []
    if isinstance(node, dict):
        for key, value in node.items():
            here = f"{trail}.{key}" if trail else key
            if key == "url" and isinstance(value, str):
                found.append((here, value))
            else:
                found.extend(_collect_urls(value, here))
    elif isinstance(node, list):
        for index, item in enumerate(node):
            found.extend(_collect_urls(item, f"{trail}[{index}]"))
    return found


def _fail(message: str) -> int:
    """Print a GitHub-annotated error and return the failing exit status."""
    print(f"::error::{message}", file=sys.stderr)
    return 1


def assert_absent(path: Path) -> int:
    """Fail if ``path`` exists. Run on a fresh checkout, before the emit step."""
    if path.exists():
        return _fail(
            f"{path} exists in a fresh checkout, so it is committed. It must stay ignored: "
            "a committed lockfile names one package index, and that breaks every contributor "
            "who resolves through the other one. Read the comment above 'uv.lock' in "
            ".gitignore and the header of scripts/check_lockfile.py before changing this."
        )
    print(f"OK: {path} is absent in a fresh checkout, so the emit step below produces it.")
    return 0


def assert_hosts(path: Path) -> int:
    """Assert the URL hosts and the URL count. Run on the file the emit step produced."""
    if not path.is_file():
        return _fail(
            f"{path} does not exist, so there is nothing to check. The emit step "
            "('uv lock') must run before this one."
        )

    raw = path.read_bytes()
    if not raw:
        return _fail(f"{path} is empty.")

    try:
        document = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        # A truncated lockfile usually stops being valid TOML, so this is a check and not
        # merely input validation.
        return _fail(f"{path} is not valid TOML, so it is not a lockfile: {error}")

    urls = _collect_urls(document)
    if not urls:
        return _fail(
            f"{path} holds no artifact URLs at all. Either the emit step produced nothing "
            "usable, or the lockfile format moved and this check no longer knows where to look."
        )

    # Hosts first: a lockfile with the wrong hosts is the failure this job exists for, and
    # naming it is more useful than reporting a count that also happens to be short.
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
            f"{len(offenders)} of {len(urls)} artifact URLs in {path} name a host other than "
            f"{ALLOWED_HOST}: {', '.join(hosts)}. This lockfile was resolved through another "
            "package index, so it is not usable by anyone who resolves through the public one. "
            "Emit it on a machine with no index configured."
        )

    if len(urls) < MIN_URL_COUNT:
        return _fail(
            f"{path} holds {len(urls)} artifact URLs, fewer than the floor of {MIN_URL_COUNT}. "
            "The emit step produced a truncated or near-empty lockfile. If the dependency set "
            "really did shrink this far, re-measure the count and update MIN_URL_COUNT in "
            "scripts/check_lockfile.py with the new number, the date and the command."
        )

    print(
        f"OK: {len(urls)} artifact URLs in {path}, all on {ALLOWED_HOST}, "
        f"at or above the floor of {MIN_URL_COUNT}."
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__ and __doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--assert-absent",
        action="store_true",
        help="fail if the path exists; run on a fresh checkout, before the emit step",
    )
    mode.add_argument(
        "--assert-hosts",
        action="store_true",
        help="fail unless every URL host is exactly the allowed host and there are enough URLs",
    )
    parser.add_argument("path", type=Path, help="the lockfile path, normally uv.lock")
    args = parser.parse_args(argv)

    if args.assert_absent:
        return assert_absent(args.path)
    return assert_hosts(args.path)


if __name__ == "__main__":
    sys.exit(main())
