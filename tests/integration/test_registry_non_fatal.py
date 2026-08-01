"""The registry is a projection, never a dependency (W4 criterion 7; §5, §9).

Two configurations, one property. With nothing configured the registry layer is
``NullRegistry`` and every tool works -- §5's deliberate design choice, so the tool surface is
usable on a laptop. With a DSN that cannot be reached, every tool STILL works and the call
reports a ``registry_warning``: tmux is the authority, and a Lakebase outage must degrade
shellbox to "shells work, the inventory is stale", never to "shells are down".

The unreachable DSN is a real one pointed at a closed port, not a monkeypatch: the failure
under test is a connection failure inside a separate process, which is what an outage looks
like and which no in-process patch reproduces.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from conftest import TmuxServer, requires_tmux
from harness import blackholed_dsn, call, make_harness, run_calls, run_script, unreachable_dsn
from mcp import ClientSession

pytestmark = requires_tmux

# Port 1 is reserved and never listening, so this fails fast and deterministically rather than
# waiting on a connect timeout. The password is distinctive so the test can assert it never
# appears in a tool payload. Composed by the harness rather than written out here -- see
# unreachable_dsn for why.
UNREACHABLE_DSN = unreachable_dsn()


def test_every_tool_works_with_no_registry_configured(
    tmux_server: TmuxServer, tmp_path: Path
) -> None:
    """``SHELLBOX_DATABASE_URL`` unset: ``NullRegistry``, no warnings, all six tools fine."""
    harness = make_harness(tmux_server, tmp_path)
    name = "noregistry"
    created, sent, read, listed, resized, killed = run_calls(
        harness,
        [
            ("shell_create", {"name": name, "cwd": str(tmp_path)}),
            ("shell_send", {"session": name, "keys": ["Enter"]}),
            ("shell_read", {"session": name}),
            ("shell_list", {}),
            ("shell_resize", {"session": name, "cols": 100, "rows": 30}),
            ("shell_kill", {"session": name}),
        ],
        env=harness.env_with(SHELLBOX_DATABASE_URL=None),
    )
    assert created.data["registry_warning"] is None
    assert killed.data["registry_warning"] is None
    for outcome in (sent, read, listed, resized):
        assert not outcome.is_error, outcome.text
    assert "registry projection failed" not in harness.stderr()


def test_an_unreachable_registry_warns_and_the_call_still_succeeds(
    tmux_server: TmuxServer, tmp_path: Path
) -> None:
    """The tool SUCCEEDS, carries a warning, and the warning leaks no credential.

    The credential check is not incidental: the obvious way to build this warning is to
    interpolate the driver's exception, and a SQLAlchemy connection error can carry the URL,
    the failing statement and its bound parameters. A payload is the one place that must not
    happen -- an agent may echo it anywhere. The full exception goes to stderr instead.
    """
    harness = make_harness(tmux_server, tmp_path)
    name = "warnonly"
    env = harness.env_with(SHELLBOX_DATABASE_URL=UNREACHABLE_DSN)
    created, sent, read, killed = run_calls(
        harness,
        [
            ("shell_create", {"name": name, "cwd": str(tmp_path)}),
            ("shell_send", {"session": name, "keys": ["Enter"]}),
            ("shell_read", {"session": name}),
            ("shell_kill", {"session": name}),
        ],
        env=env,
    )

    warning = created.data["registry_warning"]
    assert warning, "an unreachable registry produced no warning at all"
    assert "registry unavailable" in warning
    assert "pw-must-not-leak" not in warning
    # The tools that do not project inventory are unaffected either way.
    assert not sent.is_error and not read.is_error
    assert killed.data["killed"] is True
    assert killed.data["registry_warning"], "the kill projection failed silently"

    stderr = harness.stderr()
    assert "registry projection failed" in stderr, "the failure was not logged as a diagnostic"
    assert f"itest-host:{name}" in stderr, "the log does not say which session was affected"


def test_a_malformed_dsn_does_not_stop_the_server_from_starting(
    tmux_server: TmuxServer, tmp_path: Path
) -> None:
    """A DSN that cannot even build an engine degrades to no inventory, not to no shells.

    ``create_registry`` fails at construction for a bad scheme or a missing driver -- before
    any request. Letting that propagate would take the handshake down, i.e. turn an inventory
    misconfiguration into "no agent on this host can get a shell".
    """
    harness = make_harness(tmux_server, tmp_path)
    (created,) = run_calls(
        harness,
        [("shell_create", {"name": "baddsn", "cwd": str(tmp_path)})],
        env=harness.env_with(SHELLBOX_DATABASE_URL="not-a-dsn-at-all"),
    )
    assert created.data["created"] is True
    assert "could not open the registry" in harness.stderr()


def test_an_unresolved_owner_defers_the_inventory_instead_of_inventing_a_principal(
    tmux_server: TmuxServer, tmp_path: Path
) -> None:
    """CRITICAL: With no resolvable `owner_email`, the session row is SKIPPED, not written with a
    placeholder.

    `sessions.owner_email` is NOT NULL and is the column #7's ACL will filter on, so an earlier
    version writing the literal ``"unknown"`` would have accumulated real sessions under a fake
    principal — rows a later ``WHERE owner_email = ...`` either grants to nobody or, worse,
    matches for whoever ends up owning that string. E2d says defer, so it defers.

    The DSN is deliberately the unreachable one: it makes the assertion prove *ordering*. A
    "deferred" warning rather than "registry unavailable" can only mean the skip happened before
    any connection was attempted, which is what makes this a policy and not an accident of a
    database being down.
    """
    harness = make_harness(tmux_server, tmp_path)
    name = "noowner"
    env = harness.env_with(
        SHELLBOX_OWNER_EMAIL=None,
        SHELLBOX_DATABASE_URL=UNREACHABLE_DSN,
    )
    created, killed = run_calls(
        harness,
        [
            ("shell_create", {"name": name, "cwd": str(tmp_path)}),
            ("shell_kill", {"session": name}),
        ],
        env=env,
    )

    # The shell itself is entirely unaffected — that is the whole point of E2d.
    assert not created.is_error, created.text
    assert created.data["created"] is True

    warning = created.data["registry_warning"]
    assert warning and "inventory deferred" in warning, (
        f"expected a deferred-inventory warning, got {warning!r}"
    )
    assert "registry unavailable" not in warning, (
        "the projection attempted a connection before checking the owner, so this test is not "
        "proving that the skip is a policy"
    )
    assert killed.data["killed"] is True

    stderr = harness.stderr()
    assert "owner_email is unresolved" in stderr, "the skip was not logged as a diagnostic"
    assert f"itest-host:{name}" in stderr, "the log does not say which session was affected"


def test_enrollment_cannot_delay_the_handshake_even_against_a_hanging_registry(
    tmux_server: TmuxServer, tmp_path: Path
) -> None:
    """CRITICAL: The W7 promise, asserted with a clock: enrollment never blocks the handshake.

    This is the one criterion that needs the *wiring* to be real rather than the module to be
    correct. `enroll` resolves the sandbox creator (a network call) and writes a `hosts` row, both
    at startup. Doing either before `FastMCP.run()` would sit between the client and its
    `initialize`/`tools/list` — which a harness reports as a failed handshake, with nothing to
    suggest an inventory write was the cause. So enrollment runs on a daemon thread.

    WARNING: The DSN **hangs**; it is deliberately not `UNREACHABLE_DSN`. Port 1 is closed, so a
    connect there RSTs instantly and this assertion would pass whether or not enrollment blocked —
    it would measure nothing. 198.51.100.1 is unrouted TEST-NET-2, so a connect stalls, and the only
    way to answer inside the bound is to not be waiting on it.

    `shell_list` specifically, because it is the one tool that does **not** project to the
    registry. That isolates the question to startup: any delay here is enrollment's, not a
    projection's. The projecting tools are the next test's subject.
    """
    harness = make_harness(tmux_server, tmp_path)
    env = harness.env_with(SHELLBOX_DATABASE_URL=blackholed_dsn())

    started = time.monotonic()
    (listed,) = run_calls(harness, [("shell_list", {})], env=env)
    elapsed = time.monotonic() - started

    assert not listed.is_error, listed.text
    assert elapsed < 15.0, (
        f"handshake plus a non-projecting tool call took {elapsed:.1f}s against a registry that "
        "never answers; enrollment is on the startup path instead of a background thread"
    )


def test_a_hanging_registry_delays_a_projecting_call_by_a_BOUNDED_amount(
    tmux_server: TmuxServer, tmp_path: Path
) -> None:
    """CRITICAL: "Non-fatal" has to mean bounded, not merely eventually-successful.

    Registry writes are non-fatal by construction — a failure becomes a `registry_warning` on an
    otherwise successful call — but that covers *errors*, not *latency*. `shell_create` projects
    synchronously, so with no connect timeout a DSN pointing at an unrouted address made it wait
    out the OS default: **63 seconds, measured**, for a shell the agent had already got. An agent
    cannot tell that from a hung server.

    Every pre-existing non-fatal test points at port 1, which RSTs immediately — so none of them
    could have caught this, and the fix (`connect_timeout` on the engine) is asserted here
    against an address that actually stalls.
    """
    harness = make_harness(tmux_server, tmp_path)
    env = harness.env_with(SHELLBOX_DATABASE_URL=blackholed_dsn())

    started = time.monotonic()
    (created,) = run_calls(
        harness, [("shell_create", {"name": "bounded", "cwd": str(tmp_path)})], env=env
    )
    elapsed = time.monotonic() - started

    # The shell still works. That part was never in doubt and is the whole point of §9.
    assert not created.is_error, created.text
    assert created.data["created"] is True
    assert created.data["registry_warning"], "a hanging registry produced no warning"
    assert elapsed < 30.0, (
        f"a projecting tool call took {elapsed:.1f}s against an unrouted address; the engine's "
        "connect_timeout is not being applied, so a registry that never answers reads to an "
        "agent as a hung server"
    )


def test_an_owner_resolved_after_startup_starts_being_recorded(
    tmux_server: TmuxServer, tmp_path: Path
) -> None:
    """CRITICAL: The bug a real sandbox run found, and nothing local could have.

    `resolve_host_context` runs before `FastMCP.run()` and deliberately does NOT make the
    network call that resolves the sandbox's creator — `enroll.py` does that on a background
    thread, measured at ~1.4s against a live workspace, and caches the result.

    Capturing the startup value meant a process that began inside that window skipped every
    projection for its **whole life**, even after enrollment succeeded. On a fresh sandbox
    with no cached owner that is the first `serve`, so its sessions were never recorded at
    all. The integration harness sets `SHELLBOX_OWNER_EMAIL`, so it never enters the window —
    which is exactly why this went unseen until it was run for real.

    Asserted by the warning CHANGING: "inventory deferred" means the write was skipped before
    any connection; "registry unavailable" means it was attempted and the (deliberately
    unreachable) database refused. The transition is the proof that the owner was picked up.
    """
    harness = make_harness(tmux_server, tmp_path)
    state_dir = Path(harness.env["SHELLBOX_STATE_DIR"])
    # SHELLBOX_HOST_ID is unset too: it is an OVERRIDE and deliberately not persisted, so
    # with it set there is no identity cache for enrollment to write an owner into. Unsetting
    # it makes the host self-assign and cache, which is the real sandbox path.
    env = harness.env_with(
        SHELLBOX_OWNER_EMAIL=None, SHELLBOX_HOST_ID=None, SHELLBOX_DATABASE_URL=UNREACHABLE_DSN
    )

    async def script(client: ClientSession) -> dict[str, object]:
        first = (await call(client, "shell_create", {"name": "late-a", "cwd": str(tmp_path)})).data

        # Enrollment completes on its background thread and caches the owner. Simulated by
        # writing exactly what `identity` writes, because the point under test is whether the
        # server NOTICES, not how the value got there.
        cache = state_dir / "host.json"
        payload = json.loads(cache.read_text())
        payload["owner_email"] = "resolved-later@example.com"
        cache.write_text(json.dumps(payload))

        second = (await call(client, "shell_create", {"name": "late-b", "cwd": str(tmp_path)})).data
        return {"first": first, "second": second}

    out = run_script(harness, script, env=env)

    assert "inventory deferred" in out["first"]["registry_warning"], (
        "the first create should have skipped the write before connecting"
    )
    assert "registry unavailable" in out["second"]["registry_warning"], (
        "the second create still reported 'deferred', so the owner resolved by enrollment was "
        "never picked up and this process would ignore the inventory for its whole life"
    )
