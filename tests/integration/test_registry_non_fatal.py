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

from pathlib import Path

from conftest import TmuxServer, requires_tmux
from harness import make_harness, run_calls, unreachable_dsn

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
    """🔴 With no resolvable `owner_email`, the session row is SKIPPED, not written with a
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
