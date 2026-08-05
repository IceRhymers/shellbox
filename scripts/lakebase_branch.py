#!/usr/bin/env python3
"""Fork a disposable Lakebase branch, and purge it. The isolation primitive `W37b` needs.

Borrowed from `databricks-code-search`'s `scripts/ci_branch.py`, which learned every
platform fact below first. The differences are noted where they occur; the shape, the TTL
argument and the never-raising teardown are all theirs.

## Why a branch, and not a container

`make test-registry` runs the registry suite against `postgres:16-alpine` -- in CI, and against
the local docker instance in dev. That proves the registry CODE. It proves nothing
**Lakebase-specific**, and Lakebase is where the parts most likely to break live:

* OAuth tokens minted **per connect** through SQLAlchemy's ``do_connect`` hook, rather than a
  password in a DSN.
* ``pool_pre_ping`` discarding a connection that an endpoint suspended underneath the pool.
* Scale-to-zero at ``suspend_timeout_duration: 300s``, so the first connect after five idle
  minutes pays a cold start.

None of those exist in a Postgres image, so a stand-in cannot exercise them. A branch can.

## Why a branch and not a second project

A branch is a **copy-on-write fork**, so creating one is cheap and gives a private database that
a destructive suite may freely ``CREATE`` and ``DROP`` in. A second project would mean a second
set of autoscaling endpoints to pay for and reconcile, and `resources/lakebase.yml` explains at
length why this repo declares only the project.

## Platform facts, all of them non-obvious, all of them the donor's

* **A branch auto-provisions its own READ_WRITE endpoint, named ``primary``. Do NOT create one**
  -- the API rejects a second read_write endpoint on the same branch. So `up` RESOLVES the
  endpoint rather than declaring it, which is the same choice `resources/lakebase.yml` makes
  about the project's auto-provisioned objects.
* **The API REQUIRES an expiry** -- one of ``expire_time``, ``ttl`` or ``no_expiry``.
* **``ttl`` must be a protobuf ``Duration``, not a string.** ``"7200s"`` raises
  ``AttributeError`` inside the SDK.
* ``replace_existing=True`` so re-running with the same branch name replaces rather than
  collides.

## The TTL is a cost backstop, not a convenience

A branch left behind bills until someone notices. `down` is best-effort and deliberately never
raises -- a teardown error must not mask the test failure that sent you here -- so the TTL is
what actually guarantees the branch goes away. It is the only mechanism that survives the
process being killed between `up` and `down`, which is exactly how this session already leaked
a tmux server.

## What `up` prints, and the one line that is a safety mechanism

``KEY=VALUE`` lines for ``eval`` (or ``>> "$GITHUB_ENV"`` when this becomes a CI lane):

* ``SHELLBOX_PG_RESOURCE`` -- the endpoint's resource path. **Not a DSN and not a credential.**
  Everything else is derived from it: `resolve_lakebase_endpoint` reads the host from the
  workspace and the role from ``current_user.me()``, and the token is minted per connect. That
  is why this script prints no password, and it must stay that way -- `dsn.py`'s header explains
  that assembling credentials in one place is what keeps a credential-bearing URL out of the
  repo and out of a CI config.
* ``SHELLBOX_PG_DB`` -- the database a project auto-provisions.
* ``SHELLBOX_THROWAWAY_PG_HOST`` -- **the destructive fixtures' permission slip, and it is
  scoped to one host on purpose.** See `tests/registry/conftest.py`: the fixtures ``drop_all``
  on teardown, and their blanket opt-in (``SHELLBOX_ALLOW_DESTRUCTIVE_TESTS=1``) authorises
  destroying *whatever host happens to be configured*. A branch endpoint and the production
  endpoint are both ``ep-<words>-<id>.database.<region>.cloud.databricks.com``, so no human
  reviewing a shell can tell them apart. Naming the exact host means a stale variable pointing
  at production is REFUSED rather than obeyed.

Usage:

    eval "$(scripts/lakebase_branch.py up --project shellbox-pg-dev --branch w37b)"
    make test-registry
    scripts/lakebase_branch.py down --project shellbox-pg-dev --branch w37b

`make lakebase-branch-up` / `-down` wrap both with the bundle's project for a target.

WARNING: this creates a BILLABLE resource. It is not called by any lane today, and wiring it
into CI is a deliberate later step -- see `docs/deploy.md` section 9.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time

logger = logging.getLogger("lakebase_branch")

# The database a Lakebase project auto-provisions, underscored. Restated rather than imported
# from `shellbox_registry.lakebase` so this script stays runnable with nothing but the SDK on
# the path -- `tests/unit/test_lakebase_branch.py` asserts the two agree, so they cannot drift.
DEFAULT_DATABASE = "databricks_postgres"

# The branch a project auto-provisions, and the one this script forks FROM.
SOURCE_BRANCH = "production"

# Branch ids this script refuses to create or delete. `SOURCE_BRANCH` holds the registry the
# deployed App reads, and `R42` is the risk of a destructive path reaching it -- so the refusal
# is here, at the only place that can address a branch, rather than left to the caller.
#
# This guard is NOT in the donor. It does not need one: its CI project is disposable, while this
# project's production branch is the live dev registry.
PROTECTED_BRANCHES = frozenset({SOURCE_BRANCH})

# 2h, the donor's value and its reasoning: comfortably longer than any run, short enough that a
# leaked branch is self-correcting well inside a billing cycle.
DEFAULT_TTL_SECONDS = 7200

# A fresh branch can report its endpoint before that endpoint reports a host, so the host is
# polled rather than read once. `resolve_lakebase_endpoint` raises a good error on a missing
# host, but it would be raising it at the caller, several steps later, about a branch this
# script had already declared ready.
_HOST_POLL_SECONDS = 60.0
_HOST_POLL_INTERVAL = 2.0

# The clock and the sleep, as module-level seams a test replaces. NOT `time.sleep` read inside
# the loop: a test that patched the stdlib's `time.sleep` globally would make the interval free
# while `time.monotonic` stayed real, so `_await_host` would BUSY-SPIN for the whole timeout --
# measured, at 60 s of hot loop in a unit test. Patching both together is what keeps the poll a
# poll.
_now = time.monotonic
_sleep = time.sleep


def _client():  # type: ignore[no-untyped-def]
    """Lazy SDK import, and the seam the unit tests replace. The donor's pattern."""
    from databricks.sdk import WorkspaceClient

    return WorkspaceClient()


def branch_path(project: str, branch: str) -> str:
    return f"projects/{project}/branches/{branch}"


def _check_disposable(branch: str) -> None:
    """Refuse a protected branch id. Raises ``ValueError``.

    Applied to BOTH ``up`` and ``down``: forking onto ``production`` would replace it (the call
    passes ``replace_existing=True``), and purging it would delete the registry outright.
    """
    if branch in PROTECTED_BRANCHES:
        raise ValueError(
            f"refusing to operate on branch {branch!r}: it holds the registry the deployed App "
            f"reads. Pass a disposable branch id -- this script forks {SOURCE_BRANCH!r} into it."
        )


def up(project: str, branch: str, *, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> dict[str, str]:
    """Fork `SOURCE_BRANCH` into a disposable branch and return its connection environment."""
    from databricks.sdk.service import postgres as pg
    from google.protobuf.duration_pb2 import Duration

    _check_disposable(branch)
    client = _client()
    target = branch_path(project, branch)
    source = branch_path(project, SOURCE_BRANCH)

    logger.info("forking %s into %s (ttl=%ss)", source, target, ttl_seconds)
    client.postgres.create_branch(
        parent=f"projects/{project}",
        branch_id=branch,
        branch=pg.Branch(
            spec=pg.BranchSpec(
                source_branch=source,
                # Protection is what `_check_disposable` enforces by name; setting it here as
                # well would make a re-run's `replace_existing` fail on its own branch.
                is_protected=False,
                # A proto Duration. A string raises AttributeError inside the SDK.
                ttl=Duration(seconds=ttl_seconds),
            )
        ),
        replace_existing=True,
    ).wait()

    endpoint = _read_write_endpoint(client, target)
    host = _await_host(client, endpoint)
    logger.info("branch ready: endpoint=%s host=%s", endpoint, host)
    return {
        "SHELLBOX_PG_RESOURCE": endpoint,
        "SHELLBOX_PG_DB": DEFAULT_DATABASE,
        # The narrow permission slip. See this module's docstring.
        "SHELLBOX_THROWAWAY_PG_HOST": host,
    }


def _read_write_endpoint(client, branch: str) -> str:  # type: ignore[no-untyped-def]
    """The branch's auto-provisioned READ_WRITE endpoint path.

    Selected by TYPE and never by position: a branch may carry read-only endpoints too, and a
    migration or a destructive suite against one fails partway with a permission error rather
    than refusing up front.
    """
    endpoints = list(client.postgres.list_endpoints(branch))
    for candidate in endpoints:
        kind = str(getattr(getattr(candidate, "status", None), "endpoint_type", ""))
        if kind.endswith("READ_WRITE"):
            name = getattr(candidate, "name", None)
            if name:
                return str(name)
    raise RuntimeError(
        f"branch {branch} reports no READ_WRITE endpoint. A branch auto-provisions one called "
        f"'primary', so this means the fork is incomplete rather than misconfigured. Saw: "
        f"{[getattr(e, 'name', None) for e in endpoints]!r}"
    )


def _await_host(client, endpoint: str, *, timeout: float = _HOST_POLL_SECONDS) -> str:  # type: ignore[no-untyped-def]
    """The endpoint's dialable host, polled until it appears.

    Three levels of the SDK's response are Optional, so a provisioning endpoint yields ``None``
    at any of them -- the same three-hop guard `resolve_lakebase_endpoint` documents.
    """
    deadline = _now() + timeout
    while True:
        status = getattr(client.postgres.get_endpoint(endpoint), "status", None)
        hosts = getattr(status, "hosts", None) if status is not None else None
        host = getattr(hosts, "host", None) if hosts is not None else None
        if host:
            return str(host)
        if _now() >= deadline:
            raise RuntimeError(
                f"endpoint {endpoint} reported no host within {timeout:.0f}s. The branch exists, "
                f"so purge it with `scripts/lakebase_branch.py down` rather than leaving it to "
                f"the ttl: `databricks postgres get-endpoint {endpoint}` shows what it reports."
            )
        _sleep(_HOST_POLL_INTERVAL)


def down(project: str, branch: str) -> None:
    """Purge the disposable branch. NEVER raises -- teardown must not mask a real failure.

    The donor's rule, and its reasoning holds here unchanged: the TTL still reclaims the branch,
    so a warning is the right severity and an exception would replace the failure a reader came
    for. The one thing that DOES raise is a protected branch id, because that is a caller error
    rather than a teardown failure and silently doing nothing would be worse.
    """
    _check_disposable(branch)
    target = branch_path(project, branch)
    try:
        _client().postgres.delete_branch(target, purge=True).wait()
    except Exception as error:  # noqa: BLE001 - best-effort teardown, by design
        logger.warning("could not purge %s (%r); the branch ttl will reclaim it", target, error)
    else:
        logger.info("purged %s", target)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["up", "down"])
    parser.add_argument(
        "--project", required=True, help="Lakebase project id, e.g. shellbox-pg-dev"
    )
    parser.add_argument("--branch", required=True, help="disposable branch id, e.g. w37b")
    parser.add_argument("--ttl-seconds", type=int, default=DEFAULT_TTL_SECONDS)
    args = parser.parse_args()

    try:
        if args.action == "up":
            for key, value in up(args.project, args.branch, ttl_seconds=args.ttl_seconds).items():
                # Consumed by `eval`, so this goes to stdout while every log line goes to stderr.
                sys.stdout.write(f"export {key}={value}\n")
        else:
            down(args.project, args.branch)
    except ValueError as refusal:
        # A protected branch id. Not a stack trace: the message is the whole point.
        logger.error("%s", refusal)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
