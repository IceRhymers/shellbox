"""`W41` -- `A35`, `A42`, `A43`, `A48`: the predicate's output-timeout clause and the two
states that most need it exercised correctly, against real tmux.

`T-P5-OUTPUT-NOT-REAPED`, `T-P5-ADOPT-NOT-REAPED`, `T-P5-DEAD-PANE-REAPED`,
`T-P5-FRESH-SESSION`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import anyio
from conftest import (
    PlantedRegistry,
    TmuxServer,
    await_condition,
    await_file,
    await_output_timeout_elapsed,
    requires_tmux,
)
from mcp import ClientSession
from mcp.shared.memory import create_connected_server_and_client_session
from shellbox_mcp.config import Settings
from shellbox_mcp.reaper import Reaper
from shellbox_mcp.server import HostContext, build_server

pytestmark = requires_tmux

TIMEOUT = 2
HOST_ID = "reap-activity-host"


def _unique(label: str) -> str:
    return f"{label}-{uuid.uuid4().hex[:8]}"


def _old_row_time() -> datetime:
    return datetime.now(UTC) - timedelta(seconds=TIMEOUT * 30)


def _output_producer(marker: str, ticks: int = 30) -> list[str]:
    """A pane command emitting one line per second, mirrored to ``marker``.

    Bounded (not an infinite loop), matching `tests/tmux/test_window_activity.py`'s
    `_pane_output_producer`, so a hung poll still leaves the process exiting on its own.
    """
    return [
        "sh",
        "-c",
        f"i=0; while [ $i -lt {ticks} ]; do i=$((i+1)); echo tick-$i | tee -a {marker}; "
        "sleep 1; done",
    ]


def _settings(tmux_server: TmuxServer, tmp_path: Path) -> Settings:
    """`Settings` pointed at the real per-test tmux server, per section 3.3's fixture fact 1.

    An explicit mapping, never the real environment, so this test cannot pick up a
    developer's own `SHELLBOX_DATABASE_URL` -- the registry is injected directly below and
    `_open_registry` is never called (`build_server`'s `registry is None` branch is skipped).
    """
    return Settings.from_env(
        {
            "SHELLBOX_TMUX_BIN": tmux_server.tmux_bin,
            "SHELLBOX_TMUX_SOCKET": tmux_server.socket_path,
            "SHELLBOX_STATE_DIR": str(tmp_path / "state"),
        }
    )


async def _shell_create(client: ClientSession, name: str, cwd: str) -> None:
    """One `shell_create` call through the REAL tool, over an in-memory MCP session.

    Fixture fact 2 (`server.py:375`): passing `host=` to `build_server` skips identity
    resolution entirely, so this never touches the identity cache. Fixture fact 4's trap:
    the injected `HostContext` MUST carry a non-`None` `owner_email`, or `project` silently
    skips the write (`server.py:465`) and both `A42` and `A48` would pass vacuously against
    an empty registry.
    """
    result = await client.call_tool("shell_create", {"name": name, "cwd": cwd})
    assert not result.isError, f"shell_create({name!r}) failed: {result}"


# --------------------------------------------------------------------------------------
# A35 / T-P5-OUTPUT-NOT-REAPED
# --------------------------------------------------------------------------------------


def test_a_session_producing_output_survives_while_a_silent_control_is_reaped(
    tmux_server: TmuxServer, tmp_path: Path
) -> None:
    """A35/T-P5-OUTPUT-NOT-REAPED -- the central defect this phase exists to fix.

    `Reaper(timeout=2)`. The control is aged past `TIMEOUT`; the producing session emits
    output through the whole sweep, so its real `window_activity` stays inside the window.
    """
    adapter = tmux_server.adapter()
    producing = _unique("producing")
    control = _unique("control")
    marker = tmp_path / "producing.log"

    adapter.create(producing, cwd=str(tmp_path), command=_output_producer(str(marker)))
    adapter.create(control, cwd=str(tmp_path), command=["sh"])

    registry = PlantedRegistry(host_id=HOST_ID)
    registry.plant(producing, last_activity_at=_old_row_time())
    registry.plant(control, last_activity_at=_old_row_time())

    await_file(str(marker), lambda data: len(data) > 0, what="the producing pane's first tick")
    await_output_timeout_elapsed(adapter, control, TIMEOUT)

    reaper = Reaper(registry, lambda: adapter, host_id=HOST_ID, timeout=TIMEOUT, interval=9999)
    reaper.sweep()

    live = tmux_server.sessions()
    assert producing in live, "a session producing real output must not be reaped"
    assert control not in live, "the silent, unattended control must be reaped in the same sweep"
    assert registry.rows[control].status == "reaped"
    assert registry.rows[producing].status == "live"


# --------------------------------------------------------------------------------------
# A43 / T-P5-DEAD-PANE-REAPED
# --------------------------------------------------------------------------------------


def test_a_dead_pane_session_is_reaped_after_the_timeout(
    tmux_server: TmuxServer, tmp_path: Path
) -> None:
    """A43/T-P5-DEAD-PANE-REAPED -- the canonical garbage session revision 2 spared forever.

    Real tmux with `remain-on-exit on` (set globally by the first `create()` call), so the
    pane is genuinely dead and the session genuinely persists. Polling `window_activity_max`
    past `TIMEOUT` seconds old also asserts the `ASSUMED` claim that a dead pane's window
    clock freezes (`W51` arm 6): if it did not, this poll would time out and the test would
    fail by name rather than silently reporting a reap the predicate cannot actually perform.
    """
    adapter = tmux_server.adapter()
    name = _unique("dying")
    adapter.create(name, cwd=str(tmp_path), command=["sh", "-c", "printf 'LASTLINE\\n'"])

    await_condition(lambda: adapter.pane_dead(name) is True, what="the pane to die")

    registry = PlantedRegistry(host_id=HOST_ID)
    registry.plant(name, last_activity_at=_old_row_time())

    await_output_timeout_elapsed(adapter, name, TIMEOUT)

    reaper = Reaper(registry, lambda: adapter, host_id=HOST_ID, timeout=TIMEOUT, interval=9999)
    reaper.sweep()

    assert name not in tmux_server.sessions(), (
        "a dead-pane, unattended, aged session must be reaped"
    )
    assert registry.rows[name].status == "reaped"


# --------------------------------------------------------------------------------------
# A42 / T-P5-ADOPT-NOT-REAPED
# --------------------------------------------------------------------------------------


def test_an_adopted_session_survives_while_an_identically_aged_control_is_reaped(
    tmux_server: TmuxServer, tmp_path: Path
) -> None:
    """A42/T-P5-ADOPT-NOT-REAPED.

    Driven through the REAL `shell_create` tool (section 3.3's fixture facts), not
    `TmuxAdapter.create` and not a planted timestamp. The adopt refreshes the registry's send
    column (`last_activity_at`) while the tmux-side window clock stays frozen -- the state
    `ADR-28`'s row 5 says genuinely needs `last_activity_at` in clause 1 at all.
    """
    settings = _settings(tmux_server, tmp_path)
    registry = PlantedRegistry(host_id=HOST_ID)
    host = HostContext(host_id=HOST_ID, kind="test", owner_email="a42@example.com")
    server = build_server(settings, registry=registry, host=host)
    raw_adapter = tmux_server.adapter()

    adopted = _unique("adopted")
    control = _unique("control")

    async def main() -> None:
        async with create_connected_server_and_client_session(server) as client:
            await _shell_create(client, adopted, str(tmp_path))
            await _shell_create(client, control, str(tmp_path))

            await_output_timeout_elapsed(raw_adapter, adopted, TIMEOUT)
            await_output_timeout_elapsed(raw_adapter, control, TIMEOUT)

            # The real adopt: same name, same cwd -> `created=False`
            # (`tmux.py:624-632`/`server.py:557-561`'s unconditional projection).
            await _shell_create(client, adopted, str(tmp_path))

    anyio.run(main)

    reaper = Reaper(
        registry, lambda: raw_adapter, host_id=HOST_ID, timeout=TIMEOUT, interval=9999
    )
    reaper.sweep()

    live = tmux_server.sessions()
    assert adopted in live, "the adopted session must NOT be reaped"
    assert control not in live, "the identically-aged, non-adopted control must BE reaped"


# --------------------------------------------------------------------------------------
# A48 / T-P5-FRESH-SESSION
# --------------------------------------------------------------------------------------


def test_a_fresh_never_used_session_is_not_reaped_by_the_immediately_following_sweep(
    tmux_server: TmuxServer, tmp_path: Path
) -> None:
    """A48/T-P5-FRESH-SESSION.

    The fresh session is created by the real `shell_create` tool, so the row under test is
    the one `server.py:523` writes. The sparing term is `last_activity_at`, a Python
    datetime written at creation -- NOT `window_activity_max` -- which is what makes
    `timeout=2` safe here: this criterion does not rest on `W51` arm 4's tmux-side measurement.
    """
    settings = _settings(tmux_server, tmp_path)
    registry = PlantedRegistry(host_id=HOST_ID)
    host = HostContext(host_id=HOST_ID, kind="test", owner_email="a48@example.com")
    server = build_server(settings, registry=registry, host=host)
    raw_adapter = tmux_server.adapter()

    control = _unique("control")
    fresh = _unique("fresh")

    async def main() -> None:
        async with create_connected_server_and_client_session(server) as client:
            await _shell_create(client, control, str(tmp_path))
            await_output_timeout_elapsed(raw_adapter, control, TIMEOUT)
            # Created AFTER the control has already aged, so its own registry row is written
            # near `now` -- the sparing term this criterion rests on.
            await _shell_create(client, fresh, str(tmp_path))

    anyio.run(main)

    reaper = Reaper(
        registry, lambda: raw_adapter, host_id=HOST_ID, timeout=TIMEOUT, interval=9999
    )
    reaper.sweep()

    live = tmux_server.sessions()
    assert fresh in live, "a fresh, never-used session must not be reaped one sweep after creation"
    assert control not in live, "the aged control must BE reaped in the same sweep"
