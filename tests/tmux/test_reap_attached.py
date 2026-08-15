"""`W41`/`A25` -- the attach veto (`ADR-28`'s clause 2): a real attached client is never
reaped, however old, while an unattended control in the SAME sweep is.

`T-P5-ATTACHED-VETO`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from conftest import (
    PlantedRegistry,
    TmuxServer,
    await_condition,
    await_output_timeout_elapsed,
    requires_tmux,
)
from shellbox_mcp.attach import AttachedPty
from shellbox_mcp.reaper import Reaper

pytestmark = requires_tmux

# Section 3.7's mechanism: 2 seconds, not 0 (a race against `now`) and not 60 (no test may
# cost a minute). Injected straight into `Reaper`'s constructor -- never a `Settings` read.
TIMEOUT = 2
HOST_ID = "reap-attached-host"


def _unique(label: str) -> str:
    return f"{label}-{uuid.uuid4().hex[:8]}"


def _old_row_time() -> datetime:
    """Old enough that the registry timeout test alone cannot be why either session survives."""
    return datetime.now(UTC) - timedelta(seconds=TIMEOUT * 30)


def test_an_attached_session_is_never_reaped_while_an_unattended_control_is(
    tmux_server: TmuxServer, tmp_path
) -> None:
    """A25/T-P5-ATTACHED-VETO.

    Both sessions carry registry rows aged well past `TIMEOUT`, so the registry timeout test
    alone cannot spare the attached one. The subject's OWN `window_activity_max` is polled
    past `TIMEOUT` seconds old AFTER the client attaches (`W51` arm 7: an attached, silent
    client's window clock does not advance), so the output timeout cannot be what spares it
    either -- only the attach veto can. The SAME sweep reaps an unattended control, so a pass
    cannot come from an inert reaper.
    """
    adapter = tmux_server.adapter()
    attached_name = _unique("attached")
    control_name = _unique("control")

    adapter.create(attached_name, cwd=str(tmp_path), command=["sh"])
    adapter.create(control_name, cwd=str(tmp_path), command=["sh"])

    registry = PlantedRegistry(host_id=HOST_ID)
    registry.plant(attached_name, last_activity_at=_old_row_time())
    registry.plant(control_name, last_activity_at=_old_row_time())

    pty = AttachedPty.spawn(adapter.prepare_attach(attached_name), adapter.attach_env())
    try:
        await_condition(
            lambda: adapter.session_attached(attached_name) is True,
            what="the client to actually attach",
        )

        # Age BOTH sessions' real window clocks past TIMEOUT -- the attached one AFTER it is
        # already attached, so a frozen clock there is attributable to the attach veto and not
        # merely to a session that has not produced output yet.
        await_output_timeout_elapsed(adapter, attached_name, TIMEOUT)
        await_output_timeout_elapsed(adapter, control_name, TIMEOUT)

        reaper = Reaper(
            registry, lambda: adapter, host_id=HOST_ID, timeout=TIMEOUT, interval=9999
        )
        reaper.sweep()
    finally:
        pty.close()

    live_sessions = tmux_server.sessions()
    assert attached_name in live_sessions, "the attached session must NOT be reaped"
    assert control_name not in live_sessions, "the unattended control must BE reaped"
    assert registry.rows[attached_name].status == "live", (
        "the attached session's row must be untouched"
    )
    assert registry.rows[control_name].status == "reaped"
