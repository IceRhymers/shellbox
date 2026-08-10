"""``GET /api/hosts`` and ``GET /api/sessions``, and the rendering rules both obey.

These are the two routes that turn registry rows into something a browser draws. The relay
in `packages/shellbox-app/src/shellbox_app/server.py` is the App's data path; this is its
inventory, and the two share nothing but the process.

## The rules, and why each one is a rule

1. **Both routes are sync ``def``**, registered as such in
   `packages/shellbox-app/src/shellbox_app/server.py`. ``PostgresRegistry`` is synchronous
   SQLAlchemy, and a blocking call in a coroutine route stalls the event loop that relays
   every attached terminal. One slow inventory query would freeze every browser terminal at
   once. ``tests/unit/test_app_database.py`` asserts it for every route under ``/api/``.
2. **The viewer's identity comes from ``X-Forwarded-Email``, and NEVER from
   ``current_user.me()``.** Without an on-behalf-of token the SDK call returns the App's own
   service principal, so every row would be labelled as belonging to the App. That failure
   reads as working, which is why `packages/shellbox-app/src/shellbox_app/database.py`'s lane
   also carries a static guard against the name.
3. **CRITICAL: ``X-Forwarded-Email`` is DISPLAY, never authorization.** Decision D5 of the
   epic, https://github.com/IceRhymers/shellbox/issues/9, and this package's ``__init__.py``
   states it for the transport half. Concretely: both routes return the SAME rows whatever
   the header says. The header decides only which rows carry ``mine``. A viewer who spoofs
   the header sees the rows they could already see, relabelled.
4. **The inventory returns identifiers, and ``GET /`` does not.** That is not a
   contradiction. ``GET /`` is the deploy's smoke target and reports counts because a session
   name is not what a liveness check is for. The inventory is the product: under D6 the App
   is open to every workspace user, so the host list is not a per-viewer secret.
   `packages/shellbox-registry/src/shellbox_registry/base.py` says the same thing about
   ``list_hosts`` returning every row when no filter is passed.
5. **A registry failure degrades the inventory and never the relay.** ADR-3's contract, and
   the same promise `packages/shellbox-app/src/shellbox_app/database.py` makes about opening
   the registry. A read that raises becomes an empty list plus ``stale: true``; it is never a
   500 and never an exception out of the route.

## Why ``stale`` exists, and why an empty list alone is not enough

``NullRegistry`` returns ``[]`` and never raises. So an App that resolved no endpoint serves
an inventory that is indistinguishable from a real registry holding no rows -- the exact
failure `packages/shellbox-app/src/shellbox_app/ready.py` exists to catch, arriving on a
different route. ``stale`` is the flag that separates "there is nothing to show" from "the
App cannot see anything", and the ``reason`` codes are `ready.py`'s, deliberately: two
vocabularies for the same two facts would be two things to keep in step.

The reason codes name no host, database, schema or relation, for the reason `ready.py` gives
under its rule 3. Every workspace user the edge lets through reaches these routes too.

## The NULL ``sandbox_id`` label

`packages/shellbox-registry/src/shellbox_registry/models.py` makes ``hosts.sandbox_id``
nullable, and a host that was never bootstrapped has one by design: a sandbox cannot learn
its own id (section 1 of `docs/sandbox-environment.md`). ``host_id`` is an opaque uuid4, so
``sandbox_id`` is the only human-meaningful label a ``hosts`` row carries.

Its absence is what the primary failure mode -- a stopped sandbox a human must go start --
needs to name. So it renders `NOT_BOOTSTRAPPED_LABEL`, NEVER an empty cell and NEVER the bare
``host_id``. An empty cell tells a reader nothing; the raw uuid tells them something they
cannot act on. ``tests/unit/test_app_inventory.py`` asserts all three halves of that.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime

from shellbox_registry import HostRecord, NullRegistry, Registry, SessionRecord

from shellbox_app.database import AppDatabase
from shellbox_app.ready import REASON_NO_DATABASE, REASON_QUERY_FAILED

logger = logging.getLogger(__name__)

__all__ = [
    "NOT_BOOTSTRAPPED_LABEL",
    "host_payload",
    "hosts_payload",
    "sandbox_label",
    "session_payload",
    "sessions_payload",
    "viewer_owns",
]

# What a ``hosts`` row with no ``sandbox_id`` renders as. A Tier 1 string: short, lowercase,
# and stable, because the browser client branches on the row rather than on this text and a
# human reads it in a table cell.
#
# It is a stated label and not an empty cell. See this module's docstring -- the absence of a
# sandbox id is the single most informative thing an inventory row can report, and rendering
# it as blank space is how the primary failure mode arrives looking like a formatting bug.
NOT_BOOTSTRAPPED_LABEL = "not bootstrapped"


def sandbox_label(record: HostRecord) -> str:
    """The human-meaningful name for a host, or `NOT_BOOTSTRAPPED_LABEL`.

    NEVER returns an empty string, and NEVER falls back to ``host_id``. ``host_id`` is an
    opaque uuid4 that names nothing a person can go and start.
    """
    if record.sandbox_id is None:
        return NOT_BOOTSTRAPPED_LABEL
    return record.sandbox_id


def viewer_owns(owner_email: str, viewer_email: str | None) -> bool:
    """Whether ``owner_email`` is the viewer's, for the ``mine`` label and nothing else.

    CRITICAL: this decides a LABEL. It is never an authorization decision -- see this
    module's docstring, rule 3. A row is returned whether or not this returns ``True``.

    Compared case-insensitively after stripping. The edge injects the header and this repo
    has no measurement of how it cases an address, so an exact match would silently label a
    viewer's own rows as somebody else's.
    """
    if viewer_email is None:
        return False
    return owner_email.strip().casefold() == viewer_email.strip().casefold()


def host_payload(record: HostRecord, viewer_email: str | None) -> dict[str, object]:
    """One ``hosts`` row as the browser reads it. A display subset, not a mirror of the row.

    Three columns are deliberately absent, and the reasons differ:

    * ``tmux_socket`` is the E5 orphaning guard
      (`packages/shellbox-registry/src/shellbox_registry/models.py`) and a filesystem path
      inside somebody's sandbox. Nothing renders it.
    * ``gateway_host`` names an address this App measured itself unable to reach at all --
      see this package's ``__init__.py``. Showing it invites a reader to try.
    * ``enrolled_at`` is first-enrollment bookkeeping. ``last_seen_at`` is the timestamp a
      display sorts and reports on.

    A field is added here when something renders it, and `tests/unit/test_app_inventory.py`
    holds the assertion, because the tempting way to build this is to reflect every column.
    """
    return {
        "host_id": record.host_id,
        "sandbox_id": record.sandbox_id,
        # Both, on purpose: the client branches on the null and prints the label. Deriving
        # the label in the browser would put this rule in two places.
        "sandbox_label": sandbox_label(record),
        "kind": record.kind,
        "owner_email": record.owner_email,
        "mine": viewer_owns(record.owner_email, viewer_email),
        "status": record.status,
        "last_seen_at": _stamp(record.last_seen_at),
    }


def session_payload(record: SessionRecord, viewer_email: str | None) -> dict[str, object]:
    """One ``sessions`` row as the browser reads it.

    ``last_read_at`` is deliberately absent. Nothing renders it, and which predicates may
    read it is explicitly a Phase 5 semantic -- see section 5 of
    `docs/lakebase-handoff.md`. ``cols`` and ``rows`` are present because the renderer sizes
    a terminal from them.
    """
    return {
        "session_id": record.session_id,
        "host_id": record.host_id,
        "tmux_name": record.tmux_name,
        "owner_email": record.owner_email,
        "mine": viewer_owns(record.owner_email, viewer_email),
        "status": record.status,
        "last_activity_at": _stamp(record.last_activity_at),
        "cwd": record.cwd,
        "cols": record.cols,
        "rows": record.rows,
    }


def hosts_payload(database: AppDatabase, viewer_email: str | None) -> dict[str, object]:
    """What ``GET /api/hosts`` returns. Ordering is `list_hosts`', newest heartbeat first.

    The whole table, never a filtered one. ``viewer_email`` labels rows and does not select
    them -- see this module's docstring, rule 3, and `Registry.list_hosts` in
    `packages/shellbox-registry/src/shellbox_registry/base.py`, whose ``owner_email``
    parameter this route deliberately does NOT pass.
    """
    rows, degraded = _read(database, "hosts", lambda registry: registry.list_hosts())
    return {
        "viewer_email": viewer_email,
        "hosts": [host_payload(record, viewer_email) for record in rows],
        **degraded,
    }


def sessions_payload(database: AppDatabase, viewer_email: str | None) -> dict[str, object]:
    """What ``GET /api/sessions`` returns. Ordering is `list_sessions`', newest activity first.

    A flat list across every host, which is what an inventory renders.
    `list_sessions_for_host` stays the per-host primitive and neither replaces the other.
    """
    rows, degraded = _read(database, "sessions", lambda registry: registry.list_sessions())
    return {
        "viewer_email": viewer_email,
        "sessions": [session_payload(record, viewer_email) for record in rows],
        **degraded,
    }


def _stamp(moment: datetime) -> str:
    """One timestamp, ISO 8601. The columns are ``timestamptz``, so the offset survives."""
    return moment.isoformat()


# PEP 695's type-parameter syntax, which is 3.12 and up.
#
# WORTH KNOWING, because this line has been rewritten once already. On the pip path the Apps
# runtime was PYTHON 3.11 -- measured from a deploy log's `./.venv/lib/python3.11/site-packages`
# and its `cp311` wheels -- and this spelling was a SyntaxError at import there, so the App
# crash-looped behind a deploy that reported success. The deploy root now ships `pyproject.toml`
# and `uv.lock` and no `requirements.txt`, so Apps installs on the uv path, honors
# `requires-python` and provisions PYTHON 3.12. This syntax is correct again.
#
# What keeps it correct is not this comment. `scripts/deploy-app.sh` pins `RUNTIME_PYTHON` and
# runs its import check on that interpreter, and `tests/unit/test_runtime_python.py` parses every
# deployed module against the same pin in `make test`. Move the runtime backwards and both fail.
def _read[Row: (HostRecord, SessionRecord)](
    database: AppDatabase, relation: str, read: Callable[[Registry], list[Row]]
) -> tuple[list[Row], dict[str, object]]:
    """Run one inventory read. NEVER raises -- see this module's docstring, rule 5.

    Returns the rows and the ``stale`` half of the payload. On any failure the rows are empty
    and ``stale`` is true, so a degraded inventory is reported as degraded rather than as an
    empty one. A caller that merged only the rows would show "no hosts" for an outage.
    """
    if isinstance(database.registry, NullRegistry):
        logger.warning(
            "serving an empty %s inventory: the App resolved no Lakebase endpoint from its "
            "environment, so it has nothing to read rather than nothing to show",
            relation,
        )
        return [], {"stale": True, "reason": REASON_NO_DATABASE}

    try:
        rows = read(database.registry)
    except Exception:
        # The traceback goes to the log and a short code goes to the browser, which is
        # `ready.py`'s rule 3 applied to the routes that carry the same risk.
        logger.warning(
            "could not read the %s inventory; the App still serves terminals",
            relation,
            exc_info=True,
        )
        return [], {"stale": True, "reason": REASON_QUERY_FAILED}

    return list(rows), {"stale": False}
