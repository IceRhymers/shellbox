#!/usr/bin/env python3
"""Refuse a deploy-time database action that is configured to run as a service principal.

Two actions in this repo write to Lakebase at deploy time: ``alembic upgrade head``, and the
``GRANT`` in ``scripts/grant_app_sp.py``. Both must run as the DEPLOYING PRINCIPAL -- the human
or the CI identity doing the deploy -- and never as the App's service principal.

The reason is not tidiness. The App SP is granted ``SELECT`` on ``hosts`` and ``sessions`` and
nothing else, so a migration run with the SP's credential fails on a permission error. The
tempting fix for that error is to widen the grant until the migration works, and at that point
the serving principal holds DDL on the registry it is only supposed to read. That is how a
read-only service acquires write access nobody decided to give it, and it arrives as a fix for a
different problem.

So the rule is enforced HERE, on the credential, rather than documented and hoped for.
``make migrate`` and ``make grant`` both run this first.

**The check is on the role-name SHAPE, which is what makes it cheap and credential-free.** A
Lakebase role backed by a service principal is named for the SP client id, and that id is a
uuid. Both halves are sourced:

- ``databricks postgres create-role --help`` (CLI v1.8.0) documents the service-principal role
  spec as ``{"identity_type": "SERVICE_PRINCIPAL", "postgres_role": "<SP_CLIENT_ID>"}``, so the
  Postgres role name IS the client id.
- Measured 2026-08-03: ``databricks apps get shellbox -o json`` reports
  ``service_principal_client_id`` ``3337afac-b67b-41af-8996-828620bcc4a8``.

A workspace user's role name is their ``userName``, which is an email address. No email matches
the uuid form, so this check cannot refuse a human by accident. A bare local role such as
``shellbox`` is not a uuid either, so a deliberate migration against a local Postgres stays
possible -- that is the same line ``make lint``'s host guard draws.

**It reads the environment directly rather than importing ``dsn_from_env``**, so it runs under a
bare ``python3`` on a checkout with nothing synced. The coupling that matters -- that these are
the variable names ``dsn_from_env`` actually reads -- is pinned by
``tests/unit/test_deploy_principal.py``, which imports the real function and asserts it. The
donor project's guard named ``PGHOST``, a variable nothing in this repo consumes; a test is what
keeps this one from becoming that.

## What this guard does NOT cover, and where that half lives

``SHELLBOX_PG_RESOURCE`` names a Lakebase endpoint, and the role is then DERIVED from
``current_user.me()`` at connect time rather than exported. **So on that path this guard has
nothing to inspect and returns 0.** That is not a hole left open. Deriving the role is itself
the stronger property -- the connection authenticates as whoever ran the command -- and the
derived name is checked where it becomes known:

- ``packages/shellbox-registry/src/shellbox_registry/alembic/env.py`` refuses a derived role of
  this shape before it opens a connection.
- ``scripts/grant_app_sp.py`` refuses when the SERVER reports ``current_user`` equal to the App
  SP, which is the only authority on which identity actually connected.

The shape rule is therefore written twice, here and in ``shellbox_registry.lakebase``, because
this file must import nothing. ``tests/unit/test_deploy_principal.py`` asserts the two copies
agree on the same corpus, which is what keeps them one rule.
"""

from __future__ import annotations

import os
import sys
from urllib.parse import unquote, urlsplit

# The canonical dashed 8-4-4-4-12 form, and deliberately only that form. `uuid.UUID` also
# accepts a 32-hex-character string with no dashes, which would let this refuse a role legitimately
# named for a 32-character hex digest. Refusing a real migration is the expensive direction of a
# wrong answer here, so the pattern matches the shape the CLI actually emits.
_UUID_SEGMENTS = (8, 4, 4, 4, 12)

_GUIDANCE = (
    "  A deploy-time action must run as the DEPLOYING PRINCIPAL, not as the App's service\n"
    "  principal. The App SP holds SELECT on hosts and sessions and nothing else, so a\n"
    "  migration with its credential fails on a permission error -- and widening the grant to\n"
    "  make it pass would give the serving principal DDL on the registry it only reads.\n"
    "  Re-resolve the credential as yourself:\n"
    "    export SHELLBOX_PG_USER=$(databricks current-user me -p fevm-west -o json \\\n"
    "      | python3 -c 'import json,sys;print(json.load(sys.stdin)[\"userName\"])')\n"
    "  See docs/deploy.md section 4."
)


def is_service_principal_role(user: str) -> bool:
    """True when ``user`` has the shape of a Lakebase service-principal role name."""
    parts = user.split("-")
    if len(parts) != len(_UUID_SEGMENTS):
        return False
    return all(
        len(part) == width and all(c in "0123456789abcdefABCDEF" for c in part)
        for part, width in zip(parts, _UUID_SEGMENTS, strict=True)
    )


def configured_user(environ: dict[str, str] | None = None) -> str | None:
    """The Postgres user the registry DSN will connect as, or ``None`` if none is configured.

    The precedence matches ``dsn_from_env``: ``SHELLBOX_DATABASE_URL`` wins, and its userinfo is
    percent-decoded because that function percent-encodes what it assembles. An email address
    carries an ``@``, so a DSN built from parts spells the user ``tanner.wendland%40...``, and a
    check that skipped the decode would compare the wrong string.
    """
    env = os.environ if environ is None else environ

    url = env.get("SHELLBOX_DATABASE_URL")
    if url:
        username = urlsplit(url).username
        return unquote(username) if username else None

    return env.get("SHELLBOX_PG_USER") or None


def main(argv: list[str] | None = None) -> int:
    action = (argv or sys.argv[1:] or ["this command"])[0]

    user = configured_user()
    if user is None:
        # Two different states reach here, and neither is this guard's to report.
        #
        # A missing DSN has its own guard: `require-pg-host` in the Makefile, which says what to
        # set. Reporting it twice, in two wordings, would make one condition look like two.
        #
        # A configured SHELLBOX_PG_RESOURCE with no exported user is the NORMAL deploy path, and
        # the role is derived there rather than declared. See the module docstring for where the
        # derived name is checked.
        return 0

    if is_service_principal_role(user):
        print(
            f"ERROR: {action} is configured to connect as a service principal role.\n"
            f"  user: {user}\n{_GUIDANCE}",
            file=sys.stderr,
        )
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
