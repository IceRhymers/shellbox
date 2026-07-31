"""Zero in-process session state (W4 criterion 5; §6 rule 2).

1-32 MCP processes run against ONE tmux server, so any state a server keeps about sessions is
wrong the moment another process acts -- inside a single agent turn, not eventually. A cache
would not merely be stale: a cached "this session exists" survives another agent's kill, and
the send that follows it targets nothing.

Two levels, because they catch different mistakes:

* **Two processes** -- catches state held on a server instance, and is the property T-RESTART
  (W5) later depends on.
* **Two servers in ONE process** -- catches MODULE-level state, which two processes cannot
  see: separate interpreters have separate module globals, so a module-level cache would pass
  the process-level test and fail here.
"""

from __future__ import annotations

from pathlib import Path

import anyio
from conftest import TmuxServer, requires_tmux
from harness import Outcome, call, make_harness, run_calls
from mcp.shared.memory import create_connected_server_and_client_session
from shellbox_mcp.config import Settings
from shellbox_mcp.server import build_server

pytestmark = requires_tmux


def test_a_second_process_sees_the_first_process_session(
    tmux_server: TmuxServer, tmp_path: Path
) -> None:
    """Create in one process; observe, read and kill it from a second; then a third sees it gone.

    Every step is a separate ``python -m shellbox_mcp`` -- separate PID, no shared memory --
    and the incarnation must be IDENTICAL across them, which is what makes it session identity
    rather than a per-process token.
    """
    harness = make_harness(tmux_server, tmp_path)
    name = "crossproc"

    (created,) = run_calls(harness, [("shell_create", {"name": name, "cwd": str(tmp_path)})])
    assert created.data["incarnation"], "an incarnation assertion needs a non-empty incarnation"

    listed, read, killed = run_calls(
        harness,
        [
            ("shell_list", {}),
            ("shell_read", {"session": name}),
            ("shell_kill", {"session": name}),
        ],
    )
    entries = {entry["tmux_name"]: entry for entry in listed.data["sessions"]}
    assert name in entries, "the second process did not see the first process's session"
    assert entries[name]["incarnation"] == created.data["incarnation"]
    assert read.data["alive"] is True
    assert killed.data["killed"] is True

    # And the kill is visible to a third process -- a stale hit here is the signature of a
    # cache, which is exactly what T-CONC-2 (W5) will assert against concurrently.
    absent_read, absent_list = run_calls(
        harness, [("shell_read", {"session": name}), ("shell_list", {})]
    )
    assert absent_read.code == "not_found"
    assert absent_list.data["sessions"] == []


def test_two_servers_in_one_process_share_no_state(tmux_server: TmuxServer, tmp_path: Path) -> None:
    """Two ``build_server()`` instances, one interpreter, in-memory transport.

    This is the module-level-state test: both servers live in the same process, so a cache on
    a module global (or a lazily memoised adapter) would be shared, and the second server
    would answer from it. Driven through a real ``ClientSession`` over the SDK's in-memory
    transport, so the tools are still exercised as tools.
    """
    harness = make_harness(tmux_server, tmp_path)
    settings = Settings.from_env(harness.env)
    first = build_server(settings)
    second = build_server(settings)
    name = "inproc"

    async def main() -> tuple[Outcome, Outcome, Outcome, Outcome]:
        async with create_connected_server_and_client_session(first) as client_a:
            created = await call(client_a, "shell_create", {"name": name, "cwd": str(tmp_path)})
            async with create_connected_server_and_client_session(second) as client_b:
                listed = await call(client_b, "shell_list")
                killed = await call(client_b, "shell_kill", {"session": name})
            # Back on the FIRST server, after the second killed the session.
            stale = await call(client_a, "shell_read", {"session": name})
        return created, listed, killed, stale

    created, listed, killed, stale = anyio.run(main)

    assert created.data["created"] is True
    names = {entry["tmux_name"] for entry in listed.data["sessions"]}
    assert name in names, "the second server instance did not see the first's session"
    assert killed.data["killed"] is True
    assert stale.code == "not_found", (
        "the first server answered from state after another server killed the session"
    )


def test_nothing_the_server_holds_remembers_a_session(
    tmux_server: TmuxServer, tmp_path: Path
) -> None:
    """A structural check to go with the behavioral ones: no session NAME is retained anywhere.

    Behavioral tests catch a cache that is *consulted*. This catches one that is merely
    *populated* -- state that has not caused a bug yet -- which is why §6 states the rule as
    "no session state" rather than "no stale answers".

    It looks in the two places state could actually live in this design: the server object's
    own attributes, and the closure cells behind each registered tool (the tools are closures
    over settings and the registry, so a cache would have to appear as another cell).
    """
    harness = make_harness(tmux_server, tmp_path)
    server = build_server(Settings.from_env(harness.env))
    name = "structural"

    async def main() -> Outcome:
        async with create_connected_server_and_client_session(server) as client:
            return await call(client, "shell_create", {"name": name, "cwd": str(tmp_path)})

    assert anyio.run(main).data["created"] is True

    held: list[tuple[str, object]] = [
        (f"attribute {attribute}", value) for attribute, value in vars(server).items()
    ]
    for tool in server._tool_manager.list_tools():  # noqa: SLF001 -- structural test
        held += [(f"{tool.name} closure", cell) for cell in _closure_values(tool.fn)]
    for where, value in held:
        assert name not in repr(value), f"{where} remembers the session name: {value!r}"


def _closure_values(fn: object, depth: int = 2) -> list[object]:
    """Closure cell contents, following ``functools.wraps``' ``__wrapped__`` one level down.

    The registered callable is the error-normalizing wrapper, so the tool body's own cells --
    where a cache would sit -- are one level in.
    """
    values: list[object] = []
    current: object | None = fn
    for _ in range(depth):
        if current is None:
            break
        for cell in getattr(current, "__closure__", None) or ():
            try:
                values.append(cell.cell_contents)
            except ValueError:  # pragma: no cover - an empty cell
                continue
        current = getattr(current, "__wrapped__", None)
    return values
