"""``GET /ready``, and the in-App prober that calls the same code path on a timer.

``GET /`` reports that the process is up. It touches no database, deliberately -- see the
module docstring of `packages/shellbox-app/src/shellbox_app/server.py`. That rule leaves one
failure invisible, and this module is the answer to it.

## The failure this exists to catch

A Postgres role with ``CAN_CONNECT`` and no reader grant ships **green**. The App starts, binds
its port, serves every terminal, and answers ``GET /`` exactly as a healthy App does. Only an
inventory call fails, and only when a human happens to open the page.

`scripts/grant_app_sp.py` reads its own grant back with ``has_table_privilege``, which proves
the catalog agrees. It does **not** prove the App can read the tables **as itself**: that is
the App's own credential path, and only the App can exercise it. So the check has to run here.

`docs/deploy.md` section 4 states the same split under "Verifying the grant".

## The rules this route obeys, and why each one is a rule

1. **It reads a real table.** A connection test alone would pass for the exact role this route
   exists to catch. `check_registry` below runs two primary-key lookups, one on each relation
   `scripts/grant_app_sp.py` grants.
2. **It uses the SHARED engine.** A connection opened outside the pool would lose
   ``pool_pre_ping`` and ``pool_recycle=1800``, and both are load-bearing for a diagnostic
   specifically: a Lakebase endpoint suspends, and a stale pooled connection is what
   ``pool_pre_ping`` discards before the caller sees it. A bespoke connection is how this route
   would start reporting false negatives after a suspend. What bounds the wait is
   ``pool_timeout``, in `packages/shellbox-app/src/shellbox_app/config.py`.
3. **The body is generic.** ``{"ready": true}``, or ``{"ready": false, "reason": "<code>"}``
   from the closed set below. It never carries the Lakebase host, the database, the schema or a
   relation name. Every workspace user the edge lets through reaches this route, which is the
   same reason ``GET /`` reports counts and never a session name. The detail goes to the log,
   where an operator with `databricks apps logs` reads it and a browser does not.
4. **It is a sync ``def``**, registered as one in
   `packages/shellbox-app/src/shellbox_app/server.py`. The registry is synchronous SQLAlchemy
   and a blocking call in a coroutine route stalls the event loop that relays every attached
   terminal.

## The prober, and the one detail that makes it correct

Nothing on the platform calls this route on a schedule. There is no configurable health-probe
path: ``app.yaml`` supports ``command`` and ``env`` and nothing else, so the platform's
readiness signal is the process binding its port. A route nothing calls observes nothing, so
the App calls it itself.

The prober is an asyncio task on a 30-minute timer, started by the lifespan handler in
`packages/shellbox-app/src/shellbox_app/server.py`. It shares `check_registry` with the route
and does **not** go through the route, so it never queues behind an inventory storm. Its
failure path is a WARN line, which lands in ``databricks apps logs``.

CRITICAL: **the probe must not run the database call on the event loop.** It is an ``async``
task and the registry is synchronous, so `probe_once` hands the call to a thread with
``anyio.to_thread.run_sync``. A prober that blocked the loop to check the database would stall
every attached terminal every 30 minutes -- the mitigation would *become* the failure it was
added to avoid. This is the detail that makes an in-App prober correct rather than merely cheap.

``anyio`` is not a new dependency. It arrives transitively with fastapi, which
`packages/shellbox-app/src/requirements.txt` already pins.
``starlette.concurrency.run_in_threadpool`` is the same mechanism through one import instead of
two.

NORMATIVE: **the prober is two functions, not one.** `probe_once` is one cycle with no loop and
no sleep in it. `probe_forever` is the loop, and it takes the interval as an argument.
Written inline as ``while True: await asyncio.sleep(1800)`` there would be nothing for a test to
call, and "the probe does not block the event loop" could only be asserted by monkeypatching
``asyncio.sleep`` or by waiting half an hour. `tests/unit/test_app_ready.py` calls `probe_once`
directly, and that costs milliseconds.

WARNING: **a WARN line is not a page.** This bounds the time until a failure is *recorded*, not
the time until anyone is told. What it converts is "a week of silence, then a confusing
investigation" into "a WARN line at the top of the log the moment anyone looks". No alerting
path is built.
"""

from __future__ import annotations

import asyncio
import logging

import anyio.to_thread
from shellbox_registry import NullRegistry

from shellbox_app.database import AppDatabase

logger = logging.getLogger(__name__)

__all__ = [
    "PROBE_HOST_ID",
    "PROBE_INTERVAL_SECONDS",
    "PROBE_SESSION_ID",
    "REASON_NO_DATABASE",
    "REASON_QUERY_FAILED",
    "check_registry",
    "probe_forever",
    "probe_once",
    "ready_payload",
]

# The closed set of `reason` codes. A caller branches on these, so they are Tier 1 strings:
# short, lowercase, and stable. Neither one names a host, a database, a schema or a relation.
REASON_NO_DATABASE = "no_database"
"""No registry is configured, so there is nothing to read. This is a real deploy failure and
NOT a healthy state: `packages/shellbox-app/src/shellbox_app/config.py` records that an App
given no endpoint answers ``GET /`` exactly like a healthy one and serves an empty inventory.
Reporting ``ready`` here would hide the deploy this route exists to fail."""

REASON_QUERY_FAILED = "query_failed"
"""The read raised. The exception, with its traceback, is a WARN line in the App's log; it is
deliberately not in the response body."""

# The sentinel keys the probe looks up. Both are primary-key lookups that match nothing, so the
# read is bounded to zero rows however large the registry grows. `list_hosts` would be a full
# scan of a table this route has no interest in the contents of.
#
# The ids are plain strings in `packages/shellbox-registry/src/shellbox_registry/models.py`
# rather than a Postgres uuid type, so a non-uuid sentinel needs no cast and cannot fail on its
# shape. These are spelled to be recognisable in a query log.
PROBE_HOST_ID = "shellbox-ready-probe"
PROBE_SESSION_ID = "shellbox-ready-probe:none"

# 30 minutes. The interval is a cost decision, not a latency one: each probe wakes the Lakebase
# endpoint for one `suspend_timeout_duration`, so the duty cycle is that timeout divided by this
# interval. At the 300 s suspend the bundle declares, 1800 s is a 16.7 percent duty cycle.
# Halving this interval doubles the Lakebase bill and buys 15 minutes on a signal nobody is
# paged by. See this module's docstring.
PROBE_INTERVAL_SECONDS = 1800.0


def check_registry(database: AppDatabase) -> str | None:
    """Read both granted relations. ``None`` when the App can read them, else a reason code.

    This is the whole diagnostic, and it is the code path BOTH `/ready` and the prober use.
    Synchronous, because `PostgresRegistry` is synchronous SQLAlchemy -- the route is a sync
    ``def`` and the prober hands this to a thread.

    Both relations are read because `scripts/grant_app_sp.py` grants ``SELECT`` on both, and a
    grant that landed on one and not the other is a state this check should not report as ready.

    NEVER raises. A diagnostic that fails by raising is a 500, and a 500 through the Apps edge
    is indistinguishable at the caller from the edge itself refusing the request.
    """
    if isinstance(database.registry, NullRegistry):
        logger.warning(
            "the readiness check has no registry to read: the App resolved no Lakebase "
            "endpoint from its environment, so its inventory is empty rather than stale"
        )
        return REASON_NO_DATABASE

    try:
        database.registry.get_host(PROBE_HOST_ID)
        database.registry.get_session(PROBE_SESSION_ID)
    except Exception:
        # `exc_info` here and nothing in the response body. This is the whole of rule 3: the
        # operator reading `databricks apps logs` gets the host, the driver error and the
        # traceback; the browser gets a short code.
        logger.warning(
            "the readiness check could not read the registry; the App can serve terminals "
            "but its inventory is unavailable",
            exc_info=True,
        )
        return REASON_QUERY_FAILED

    return None


def ready_payload(database: AppDatabase) -> dict[str, object]:
    """What ``GET /ready`` returns. Generic by rule -- see this module's docstring.

    The status code stays 200 on a failure, and that is deliberate rather than an oversight.
    The Phase 1 probe measured this edge answering an unauthenticated request with **HTTP 200
    carrying an HTML login body** (`probe/FINDINGS.md`), so a status code from this route is
    already not a signal a caller can trust. Adding a second signal that can disagree with the
    body would give a reader two answers to one question. Every caller in this repo parses the
    body: `scripts/check_ready.py`, `scripts/deploy.sh`, and the curl in `docs/deploy.md`
    section 4. Nothing on the platform reads this route at all -- there is no configurable
    health-probe path -- so there is no consumer that wants a 503.
    """
    reason = check_registry(database)
    if reason is None:
        return {"ready": True}
    return {"ready": False, "reason": reason}


async def probe_once(database: AppDatabase) -> bool:
    """One probe cycle: read, log on failure, return. No loop and no sleep -- NORMATIVE.

    CRITICAL: `check_registry` runs in a worker thread. It is synchronous SQLAlchemy, and this
    coroutine runs on the event loop that relays every attached terminal. See this module's
    docstring for why a prober that skipped this line would introduce the failure it was added
    to avoid.
    """
    reason = await anyio.to_thread.run_sync(check_registry, database)
    if reason is None:
        logger.info("readiness probe: the App can read the registry as itself")
        return True
    # The notification mechanism, in full. `packages/shellbox-app/src/shellbox_app/logs.py`
    # is what makes sure this line has somewhere to go.
    logger.warning(
        "readiness probe FAILED (%s): the App cannot read its registry. Terminals still "
        "work; the inventory is unavailable. Check the service principal's SELECT grant "
        "with `make grant`, and see docs/deploy.md section 4",
        reason,
    )
    return False


async def probe_forever(
    database: AppDatabase, interval: float = PROBE_INTERVAL_SECONDS
) -> None:
    """Sleep, probe, repeat, until cancelled. The lifespan handler owns this task.

    CRITICAL: **it sleeps FIRST.** Probing on startup is the tempting order and it is the wrong
    one, for two reasons. Starting the App would then do database I/O, and keeping database I/O
    off the startup path is the same rule that keeps it out of ``GET /``. And the fresh-deploy
    case is already covered by something stronger: `scripts/deploy.sh` calls ``/ready`` as its
    last step and fails the deploy on a false answer, so a broken grant is caught before the
    prober's first interval elapses. The prober covers what happens AFTER a green deploy.

    NOTE: a test that runs the lifespan handler therefore performs no read at all, which is why
    the App lane does not open a socket to whatever host its fixtures name.

    ``interval`` is a parameter and not the constant read inline, so a test drives several
    cycles in milliseconds. That split is the reason `probe_once` exists separately -- see this
    module's docstring.
    """
    while True:
        await asyncio.sleep(interval)
        await probe_once(database)
