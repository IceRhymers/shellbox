#!/usr/bin/env python3
"""`W50`: hold a real tmux server, a real Postgres registry, and a real `Reaper` thread
together, and observe the conjunction no CI lane can hold at once.

## Why this is a sibling script, not an extension of `live_acceptance.py`

`scripts/live_acceptance.py` is `W38`'s: a real `Publisher`/`SubscriberClient` pair against a
*deployed App*, reached over a real websocket with a minted workspace OAuth token. This run
exercises a different subsystem entirely -- `Reaper`, `TmuxAdapter`, and `PostgresRegistry` --
none of which talk to the App, the edge, or any Databricks Apps deployment at all. `ADR-1` keeps
tmux host-agnostic and the reaper is a background thread inside `shellbox-mcp`, so nothing here
needs a live sandbox to be real: a real tmux server and a real Postgres are exactly as real on a
laptop as inside one. Sharing one file would mean threading an unrelated subsystem's setup
through `--url`/`--profile`/`--minutes` argument parsing that means nothing to it. `W50`'s own
row in the plan (`docs/plan-sections.md`) left this open; this is the answer.

## What no CI lane proves, and why (`R68`, `4.3`)

`make test-tmux` runs against a real tmux server with a FAKE registry (`PlantedRegistry`,
`tests/conftest.py`). `make test-registry` runs against a real Postgres with a FAKE tmux
adapter. Neither lane holds both at once, so the *conjunction* -- a real tmux session, a real
registry row, and a real reap end to end -- has no CI proof. `A51` is the criterion that names
this run as the only proof of it, and `8.2` states what "proven whole once" means: **an exit of
`0`, and nothing else.** An exit of `2` means the conjunction remains UNPROVEN and the phase is
not done; the script may be re-run.

## What runs, and why each half is the real code

* **The tmux server** is a real `tmux` binary (`TmuxAdapter` over `SubprocessRunner`), on its own
  socket under `/tmp`, holding two real sessions: one that a real `tmux attach` client
  (`shellbox_mcp.attach.AttachedPty`, the same fork-onto-a-pty mechanism the publisher uses)
  attaches to and holds silently, and one left unattended.
* **The registry** is `shellbox_registry.postgres.PostgresRegistry` against a real Postgres --
  the local dev/CI default (`localhost:55432`) unless `SHELLBOX_DATABASE_URL` /
  `SHELLBOX_PG_*` / `SHELLBOX_PG_RESOURCE` (Lakebase) says otherwise, mirroring
  `tests/registry/conftest.py`'s resolution order exactly so this script can be pointed at a real
  deployment's endpoint the same way that lane can.
* **The `Reaper`** is `shellbox_mcp.reaper.Reaper`, started for real (`Reaper.start()`), so its
  own background thread performs the sweeps -- not `sweep()` called once by hand.

`timeout` and `interval` are injected straight into `Reaper`'s constructor, never resolved from
`SHELLBOX_IDLE_TIMEOUT_SECONDS` (floor 60s, `docs/registration.md`). Section 3.7 of the plan
names this as the intended mechanism: the floor guards the environment-resolution path only, and
"a test may legally construct `Reaper(timeout=2, ...)`" -- small enough to age a session in
seconds, which no value an operator could configure could ever be. The same reasoning applies to
a script meant to finish in well under a minute, not thirty.

## What it asserts

1. **The attach veto holds against a real client.** The attached session survives the sweep and
   its row is untouched, exactly as `A25` asserts in `tests/tmux/test_reap_attached.py` -- here
   against a real registry row rather than `PlantedRegistry`'s fake one.
2. **The unattended session is reaped end to end.** Its real tmux session is gone, and its real
   Postgres row reads `status="reaped"` -- `A26`'s two halves, held at once.
3. **The reap write shape is correct against real Postgres**: `last_activity_at` is carried
   through UNCHANGED, never bumped to `now()` -- `upsert_session`'s `GREATEST` (`postgres.py`)
   would make a `now()` written here permanent, which is exactly the defect `W41`'s own
   docstring warns against.
4. **The reaper thread actually ran** (`sweeps >= 1`), so a pass cannot be explained by a reaper
   that never swept at all.

## The exit convention (unchanged from `scripts/live_acceptance.py`)

`0` -- every clause above was exercised and held. `1` -- a clause failed. `2` -- inconclusive:
the real tmux server or the real Postgres could not be reached at all, so nothing was observed.
At exit `2` the phase is NOT done and the run may be re-run once both are reachable; a dated
record is still appended either way (`docs/writing-style.md`'s "do not rewrite a findings
record" rule -- this only ever appends).

WARNING: this holds a real tmux server and forks a real `tmux attach` client on the machine it
runs on, and it writes real rows to whatever Postgres it reaches. It tears both down on the way
out, including on a signal, but a `kill -9` will leave a tmux server (and its attach client)
behind on its own throwaway socket under `/tmp`. Postgres rows are deleted by `host_id`
regardless of outcome; the schema itself is only dropped when this run is the one that created
it AND the resolved host is a recognised throwaway (`localhost` and friends) -- a real
deployment's schema is never dropped.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import shutil
import tempfile
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

import sqlalchemy
from shellbox_mcp import naming
from shellbox_mcp.attach import AttachedPty
from shellbox_mcp.reaper import TMUX_CALL_TIMEOUT_SECONDS, Reaper
from shellbox_mcp.tmux import TmuxAdapter, TmuxConfig
from shellbox_registry.base import HostRecord, SessionRecord
from shellbox_registry.dsn import dsn_from_env, normalize_postgres_dsn, redact
from shellbox_registry.models import Base
from shellbox_registry.postgres import PostgresRegistry
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

FINDINGS_FILE = Path(__file__).resolve().parent.parent / "probe" / "FINDINGS.md"

# Hosts this run may create AND drop a schema on without being asked twice. Matches
# `tests/registry/conftest.py`'s `_THROWAWAY_HOSTS` exactly -- CI's service container and the
# local dev docker instance are both `localhost`.
_THROWAWAY_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "postgres", "db"})

_SOCKET_ROOT = "/tmp"
_POLL_INTERVAL = 0.05


@dataclass
class Journal:
    """Everything observed, with timestamps. The run's actual output."""

    started: float = field(default_factory=time.monotonic)
    events: list[tuple[float, str]] = field(default_factory=list)

    def note(self, what: str) -> None:
        stamp = time.monotonic() - self.started
        self.events.append((stamp, what))
        print(f"[{stamp:7.1f}s] {what}", flush=True)


def _poll(
    predicate: Callable[[], bool], *, timeout: float, interval: float = _POLL_INTERVAL
) -> bool:
    """Poll ``predicate`` until it is true, or ``timeout`` elapses. Never sleeps for a fixed
    duration and then asserts -- the same synchronization rule `tests/conftest.py` states for
    every tmux assertion in this repo: everything here polls for a condition with a deadline.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


def _resolve_dsn() -> str:
    """The DSN to run against. Mirrors `tests/registry/conftest.py`'s `_test_dsn` exactly, so
    this script is reachable through the same environment variables that lane is: an explicit
    `SHELLBOX_DATABASE_URL` or `SHELLBOX_PG_*` set wins, and the local dev docker instance
    (`localhost:55432`, `dsn.py`'s own defaults) is the fallback with no setup required.
    """
    explicit = dsn_from_env()
    if explicit:
        return explicit
    os.environ.setdefault("SHELLBOX_PG_HOST", "localhost")
    resolved = dsn_from_env()
    assert resolved is not None  # setdefault above guarantees it
    return resolved


def _lakebase_engine_or_none() -> tuple[Engine, str] | None:
    """An engine on the Lakebase endpoint `SHELLBOX_PG_RESOURCE` names, or `None`.

    This is how the script reaches a real deployment's endpoint rather than the local docker
    default, mirroring `tests/registry/conftest.py`'s own Lakebase path so a real sandbox's
    registry is reachable the same way that lane already reaches it -- a configured endpoint
    WINS over a DSN, matching `alembic/env.py`.
    """
    resource = os.environ.get("SHELLBOX_PG_RESOURCE")
    if not resource:
        return None

    from shellbox_registry.lakebase import (
        DEFAULT_DATABASE,
        LakebaseCredentials,
        create_lakebase_engine,
        resolve_lakebase_endpoint,
        sdk_token_minter,
    )

    endpoint = resolve_lakebase_endpoint(
        resource, database=os.environ.get("SHELLBOX_PG_DB") or DEFAULT_DATABASE
    )
    credentials = LakebaseCredentials(sdk_token_minter(resource))
    return create_lakebase_engine(endpoint, credentials), endpoint.host


def _host_of(dsn: str) -> str:
    return urlparse(dsn).hostname or ""


@dataclass
class RegistrySetup:
    registry: PostgresRegistry
    engine: Engine
    host: str
    label: str
    schema_created_here: bool


def _build_registry_or_none(journal: Journal) -> RegistrySetup | None:
    """A real `PostgresRegistry` on a reachable engine, or `None` if nothing answers.

    `None` is the trigger for exit `2` -- see this module's docstring. A missing schema is
    created (`checkfirst=True`, so a real deployment's existing schema is left untouched) and
    the caller learns whether THIS run is the one that created it, which decides whether
    teardown may drop it again.
    """
    lakebase = _lakebase_engine_or_none()
    if lakebase is not None:
        engine, host = lakebase
        label = f"Lakebase endpoint {os.environ['SHELLBOX_PG_RESOURCE']}"
    else:
        dsn = normalize_postgres_dsn(_resolve_dsn())
        engine, host = create_engine(dsn), _host_of(dsn)
        label = redact(_resolve_dsn())

    try:
        with engine.connect():
            pass
    except sqlalchemy.exc.OperationalError as exc:
        journal.note(f"no live Postgres reachable at {label}: {exc}")
        engine.dispose()
        return None

    inspector = sqlalchemy.inspect(engine)
    schema_pre_existing = inspector.has_table("hosts") and inspector.has_table("sessions")
    if not schema_pre_existing:
        Base.metadata.create_all(engine, checkfirst=True)
        journal.note(f"registry schema created fresh on {label} (none was present)")
    else:
        journal.note(f"registry schema already present on {label}; left untouched")

    registry = PostgresRegistry(f"(engine supplied for {label})", engine=engine)
    return RegistrySetup(
        registry=registry,
        engine=engine,
        host=host,
        label=label,
        schema_created_here=not schema_pre_existing,
    )


def _teardown_registry(setup: RegistrySetup, host_id: str, journal: Journal) -> None:
    """Delete this run's own rows always; drop the whole schema only when this run created it
    fresh AND the resolved host is a recognised throwaway. A real deployment's schema -- one
    this run found already migrated -- is never dropped, matching
    `tests/registry/conftest.py`'s destructive-fixture guard.
    """
    with contextlib.suppress(Exception), setup.engine.begin() as conn:
        conn.execute(text("DELETE FROM sessions WHERE host_id = :h"), {"h": host_id})
        conn.execute(text("DELETE FROM hosts WHERE host_id = :h"), {"h": host_id})
    journal.note(f"deleted this run's rows for host_id={host_id!r}")

    if setup.schema_created_here and setup.host.lower() in _THROWAWAY_HOSTS:
        with contextlib.suppress(Exception):
            Base.metadata.drop_all(setup.engine)
        journal.note(f"dropped the schema this run created on throwaway host {setup.host!r}")
    setup.engine.dispose()


def _old_row_time(timeout: float) -> datetime:
    """Old enough that the registry timeout test alone cannot be why a session survives.
    Matches `tests/tmux/test_reap_attached.py`'s `_old_row_time`.
    """
    return datetime.now(UTC) - timedelta(seconds=timeout * 30)


def _await_output_timeout_elapsed(
    adapter: TmuxAdapter, name: str, seconds: float, *, timeout: float
) -> bool:
    """Poll ``name``'s real `window_activity_max` until it reads more than ``seconds`` old.
    Section 3.7's mechanism: every real tmux clock is polled until it crosses the injected
    timeout, never slept past. Matches `tests/conftest.py`'s `await_output_timeout_elapsed`.
    """
    return _poll(
        lambda: (time.time() - (adapter.window_activity_max(name) or 0)) > seconds,
        timeout=timeout,
    )


def _run(args: argparse.Namespace, journal: Journal) -> tuple[list[tuple[str, bool, str]], int]:
    """Everything that needs real components. Returns the checks and an early-exit code
    (``0`` meaning "keep going", any other value meaning "stop now, this is the final code").
    """
    tmux_bin = shutil.which("tmux") if not args.tmux_bin else args.tmux_bin
    if tmux_bin is None:
        journal.note("tmux is not on PATH; this run needs a real tmux server")
        return [], 2

    setup = _build_registry_or_none(journal)
    if setup is None:
        return [], 2

    host_id = args.host_id
    attached_name = f"w50-attached-{uuid.uuid4().hex[:8]}"
    idle_name = f"w50-idle-{uuid.uuid4().hex[:8]}"
    attached_session_id = naming.session_id(host_id, attached_name)
    idle_session_id = naming.session_id(host_id, idle_name)
    socket_path = os.path.join(_SOCKET_ROOT, f"sbxw50{uuid.uuid4().hex[:8]}")
    workspace = Path(tempfile.mkdtemp(prefix="w50-"))

    config = TmuxConfig(socket_path=socket_path, tmux_bin=tmux_bin)
    adapter = TmuxAdapter(config)
    # `ADR-37`/`R69`: the reaper's OWN factory carries a bounded `TmuxConfig.timeout`, unlike
    # every other adapter this script builds. `reaper.py`'s own docstring names this as the
    # caller's obligation.
    reaper_config = TmuxConfig(
        socket_path=socket_path, tmux_bin=tmux_bin, timeout=TMUX_CALL_TIMEOUT_SECONDS
    )
    reaper = Reaper(
        setup.registry,
        lambda: TmuxAdapter(reaper_config),
        host_id=host_id,
        timeout=args.timeout,
        interval=args.interval,
    )

    pty: AttachedPty | None = None
    try:
        adapter.create(attached_name, cwd=str(workspace), command=["/bin/sh"])
        adapter.create(idle_name, cwd=str(workspace), command=["/bin/sh"])
        journal.note(f"created real tmux sessions {attached_name!r} and {idle_name!r}")

        setup.registry.upsert_host(
            HostRecord(
                host_id=host_id,
                kind="lakebox",
                owner_email="w50-live@example.com",
                last_seen_at=datetime.now(UTC),
                status="active",
            )
        )
        old = _old_row_time(args.timeout)
        setup.registry.upsert_session(
            SessionRecord(
                session_id=attached_session_id,
                host_id=host_id,
                tmux_name=attached_name,
                owner_email="w50-live@example.com",
                last_activity_at=old,
                status="live",
            )
        )
        setup.registry.upsert_session(
            SessionRecord(
                session_id=idle_session_id,
                host_id=host_id,
                tmux_name=idle_name,
                owner_email="w50-live@example.com",
                last_activity_at=old,
                status="live",
            )
        )
        journal.note(f"planted real registry rows, aged to {old.isoformat()}")

        pty = AttachedPty.spawn(adapter.prepare_attach(attached_name), adapter.attach_env())
        attached = _poll(lambda: adapter.session_attached(attached_name) is True, timeout=10.0)
        journal.note(f"real tmux attach client reports attached={attached}")

        # Age BOTH sessions' real window clocks past `timeout` -- the attached one AFTER it is
        # already attached, so a frozen clock there is attributable to the attach veto and not
        # merely to a session that has produced no output yet.
        attached_aged = _await_output_timeout_elapsed(
            adapter, attached_name, args.timeout, timeout=args.timeout + 20.0
        )
        idle_aged = _await_output_timeout_elapsed(
            adapter, idle_name, args.timeout, timeout=args.timeout + 20.0
        )
        journal.note(
            f"real window clocks aged past timeout: attached={attached_aged} idle={idle_aged}"
        )

        reaper.start()
        journal.note(
            f"real Reaper thread started (timeout={args.timeout}s, interval={args.interval}s)"
        )

        def _idle_row_is_reaped() -> bool:
            row = setup.registry.get_session(idle_session_id)
            return row is not None and row.status == "reaped"

        reaped = _poll(_idle_row_is_reaped, timeout=args.deadline)
        journal.note(f"idle session reaped={reaped} after {reaper.sweeps} real sweep(s)")

        # Stop the reaper and observe the FINAL state here, before any cleanup kill below --
        # cleanup kills the attached session unconditionally on the way out, and reading the
        # state after that would measure this harness's own teardown, not the reaper's
        # decision. This is exactly the trap `A25`'s own fixture avoids by asserting inside
        # the `try`, before its `finally` closes the pty.
        reaper.stop()
        live_names = {record.tmux_name for record in adapter.list_sessions()}
        attached_row = setup.registry.get_session(attached_session_id)
        idle_row = setup.registry.get_session(idle_session_id)
    finally:
        reaper.stop()
        if pty is not None:
            pty.close()
        with contextlib.suppress(Exception):
            adapter.kill(attached_name)
        with contextlib.suppress(Exception):
            adapter.kill(idle_name)
        shutil.rmtree(workspace, ignore_errors=True)
        with contextlib.suppress(OSError):
            os.unlink(socket_path)

    attached_live = attached_name in live_names
    idle_live = idle_name in live_names
    attached_status = attached_row.status if attached_row else None
    idle_status = idle_row.status if idle_row else None

    checks: list[tuple[str, bool, str]] = [
        (
            "the attach veto: a real attached client survives the sweep",
            attached_live and attached_status == "live",
            f"live_tmux={attached_live} row_status={attached_status!r}",
        ),
        (
            'the unattended session is reaped: gone from tmux, row reads status="reaped"',
            not idle_live and idle_status == "reaped",
            f"live_tmux={idle_live} row_status={idle_status!r}",
        ),
        (
            "the reap write carries last_activity_at through UNCHANGED, never now()",
            idle_row is not None and idle_row.last_activity_at == old,
            f"stored={idle_row.last_activity_at if idle_row else None} planted={old}",
        ),
        (
            "the real Reaper thread actually swept at least once",
            reaper.sweeps >= 1,
            f"sweeps={reaper.sweeps}",
        ),
    ]

    _teardown_registry(setup, host_id, journal)
    return checks, 0


def _print_findings(journal: Journal, checks: list[tuple[str, bool, str]]) -> int:
    print("\n" + "=" * 78)
    print("W50 live acceptance -- findings")
    print("=" * 78)
    failed = 0
    for name, ok, detail in checks:
        label = "PASS" if ok else "FAIL"
        print(f"  {label:6s}  {name}\n                {detail}")
        if not ok:
            failed += 1
    print(f"\n  events recorded: {len(journal.events)}")
    return 1 if failed else 0


def _append_finding(exit_code: int, checks: list[tuple[str, bool, str]], journal: Journal) -> None:
    """Append a dated record. Never rewrites what is already there
    (`docs/writing-style.md`'s "do not rewrite a findings record" rule).
    """
    date = datetime.now(UTC).date().isoformat()
    lines = [
        "",
        "---",
        "",
        f"## `W50` -- reaper live acceptance run ({date})",
        "",
        "Real tmux server, real Postgres registry, real `Reaper` thread -- the conjunction "
        "`R68` names as unreachable by any CI lane, per `A51`.",
        "",
    ]
    if exit_code == 2:
        lines += [
            "**Exit 2, inconclusive.** Neither the real tmux binary nor a real Postgres was "
            "reachable in this environment, so nothing was observed. The conjunction remains "
            "unproven and the phase is not done at this exit code. Re-run once both are "
            "reachable.",
            "",
        ]
    else:
        for name, ok, detail in checks:
            lines.append(f"- **{'PASS' if ok else 'FAIL'}** -- {name} ({detail})")
        lines += [
            "",
            f"**Exit {exit_code}.** "
            + (
                "Every clause was exercised and held; this is the run `A51` names as the "
                "conjunctive proof of the definition of done's clause 2."
                if exit_code == 0
                else "At least one clause failed; see the checks above."
            ),
            "",
        ]
    with FINDINGS_FILE.open("a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--host-id", default=f"w50-live-{uuid.uuid4().hex[:8]}", help="the reaper's host_id"
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=3.0,
        metavar="SECONDS",
        help="injected straight into Reaper's constructor -- see this module's docstring",
    )
    parser.add_argument("--interval", type=float, default=1.0, metavar="SECONDS")
    parser.add_argument(
        "--deadline",
        type=float,
        default=30.0,
        metavar="SECONDS",
        help="how long to wait for the idle session to be reaped before giving up",
    )
    parser.add_argument("--tmux-bin", default=None, help="override the tmux binary on PATH")
    args = parser.parse_args()

    journal = Journal()
    try:
        checks, early = _run(args, journal)
    except KeyboardInterrupt:
        journal.note("interrupted")
        checks, early = [], 1

    if early == 2:
        print(
            "\n  INCONCLUSIVE. No real tmux and/or real Postgres was reachable, so the "
            "conjunction was never exercised."
        )
        _append_finding(2, checks, journal)
        return 2

    exit_code = _print_findings(journal, checks)
    _append_finding(exit_code, checks, journal)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
