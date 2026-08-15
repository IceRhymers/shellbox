"""`W41`/`A46` -- the ownership filter rule (`R60`): a session shellbox cannot prove it owns
is skipped BEFORE `kill` is called, however old its timestamps -- proven with a PLANTED
registry row so the has-a-row filter rule cannot be what answers first.

`T-P5-FOREIGN-SKIPPED`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from conftest import PlantedRegistry, TmuxServer, await_output_timeout_elapsed, requires_tmux
from shellbox_mcp.reaper import Reaper

pytestmark = requires_tmux

TIMEOUT = 2
HOST_ID = "reap-ownership-host"


def _unique(label: str) -> str:
    return f"{label}-{uuid.uuid4().hex[:8]}"


def _old_row_time() -> datetime:
    return datetime.now(UTC) - timedelta(seconds=TIMEOUT * 30)


def test_an_unstamped_session_with_a_planted_row_is_skipped_before_kill(
    tmux_server: TmuxServer, tmp_path
) -> None:
    """A46/T-P5-FOREIGN-SKIPPED.

    `TmuxServer.raw` bypasses the adapter's create chain entirely, so `foreign_name` carries
    no `@shellbox_incarnation` at all -- `SessionRecord.foreign` reads `True` for it
    (`tmux.py:266-275`). A registry row is PLANTED for it anyway, so the OWNERSHIP filter
    rule -- not the has-a-row filter rule -- is what this test actually proves: with only
    the has-a-row rule, a session that carries a row would become a candidate and would
    reach the predicate.

    A stamped control, created the normal way and aged identically, IS reaped in the SAME
    sweep, so a pass cannot come from an inert reaper.
    """
    adapter = tmux_server.adapter()
    foreign_name = _unique("foreign")
    control_name = _unique("control")

    created = tmux_server.raw("new-session", "-d", "-s", foreign_name, "-x", "80", "-y", "24", "sh")
    assert created.rc == 0, created.stderr
    adapter.create(control_name, cwd=str(tmp_path), command=["sh"])

    registry = PlantedRegistry(host_id=HOST_ID)
    registry.plant(foreign_name, last_activity_at=_old_row_time())
    registry.plant(control_name, last_activity_at=_old_row_time())

    await_output_timeout_elapsed(adapter, foreign_name, TIMEOUT)
    await_output_timeout_elapsed(adapter, control_name, TIMEOUT)

    reaper = Reaper(registry, lambda: adapter, host_id=HOST_ID, timeout=TIMEOUT, interval=9999)
    reaper.sweep()

    live = tmux_server.sessions()
    assert foreign_name in live, "an unstamped (foreign) session must never be killed by the reaper"
    assert control_name not in live, (
        "the stamped, equally-aged control must be reaped in the same sweep"
    )
    assert registry.rows[foreign_name].status == "live", (
        "the foreign session's planted row must be untouched -- it was never a candidate"
    )
    assert registry.rows[control_name].status == "reaped"
