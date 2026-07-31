"""All six tools, driven end-to-end through a real MCP client (W4 criterion 1 and 2).

Nothing here imports a tool function. Every assertion goes through ``tools/list`` or
``tools/call`` against a spawned ``python -m shellbox_mcp``, because the protocol surface --
schema derivation, argument coercion, the result envelope -- is exactly what an in-process
call skips, and it is where a client-visible break would live.
"""

from __future__ import annotations

from pathlib import Path

import anyio
from conftest import TmuxServer, requires_tmux
from harness import await_content, call, make_harness, run_calls, run_script
from mcp import ClientSession

pytestmark = requires_tmux

TOOL_NAMES = {
    "shell_create",
    "shell_send",
    "shell_read",
    "shell_list",
    "shell_resize",
    "shell_kill",
}


def test_all_six_tools_are_registered_with_schemas(tmux_server: TmuxServer, tmp_path: Path) -> None:
    """``tools/list`` names all six, and each carries an input schema.

    Asserted over the wire rather than over ``mcp._tool_manager``: a tool registered but not
    advertised is invisible to every client, and that is a difference only the protocol
    shows.
    """
    harness = make_harness(tmux_server, tmp_path)

    async def script(client: ClientSession) -> dict[str, dict[str, object]]:
        listing = await client.list_tools()
        return {
            tool.name: {"input": tool.inputSchema, "output": tool.outputSchema}
            for tool in listing.tools
        }

    tools = run_script(harness, script)
    assert set(tools) == TOOL_NAMES
    for name, schemas in tools.items():
        assert schemas["input"], f"{name} advertises no input schema"
        # The output schema is derived from the tool's TypedDict return type, so its absence
        # means the payload contract silently became "whatever the function happened to
        # return" -- the drift §6 chose a typed surface to prevent.
        assert schemas["output"], f"{name} advertises no output schema"


def test_create_send_read_resize_list_kill_round_trip(
    tmux_server: TmuxServer, tmp_path: Path
) -> None:
    """One session's whole life, in one client session.

    The read is polled (§11.1): ``shell_send`` returns once tmux has accepted the paste,
    which is strictly before the shell has run anything.
    """
    harness = make_harness(tmux_server, tmp_path)
    name = harness.name("life")

    async def script(client: ClientSession) -> dict[str, object]:
        created = (await call(client, "shell_create", {"name": name, "cwd": str(tmp_path)})).data
        sent = (
            await call(
                client,
                "shell_send",
                {"session": created["session"], "text": "echo LIFE-OK\n"},
            )
        ).data
        content = await await_content(client, created["session"], "LIFE-OK")
        resized = (
            await call(client, "shell_resize", {"session": name, "cols": 100, "rows": 40})
        ).data
        listed = (await call(client, "shell_list")).data
        killed = (await call(client, "shell_kill", {"session": name})).data
        return {
            "created": created,
            "sent": sent,
            "content": content,
            "resized": resized,
            "listed": listed,
            "killed": killed,
        }

    out = run_script(harness, script)

    created = out["created"]
    assert created["created"] is True
    assert created["tmux_name"] == name
    assert created["session"] == f"itest-host:{name}"
    assert created["host_id"] == "itest-host"
    assert created["cols"] == 80 and created["rows"] == 24
    assert Path(created["cwd"]).resolve() == tmp_path.resolve()
    # 2a of T-RESTART, applied here too: an assertion about an incarnation is worthless
    # unless the incarnation exists. r2 shipped an inert `set-option` whose own tests
    # compared "" == "" and passed.
    assert created["incarnation"], "shell_create returned an empty incarnation"

    sent = out["sent"]
    assert sent["submitted_bytes"] == len("echo LIFE-OK\n")
    assert sent["keys_sent"] == []
    # Stated in the payload so no caller mistakes submission for receipt (H4).
    assert sent["delivery"] == "unverified"
    # §9.1: the incarnation the send TARGETED, which is what makes misdelivery detectable.
    assert sent["incarnation"] == created["incarnation"]

    assert "LIFE-OK" in out["content"]

    assert out["resized"] == {
        "session": f"itest-host:{name}",
        "tmux_name": name,
        "cols": 100,
        "rows": 40,
    }

    entries = {entry["tmux_name"]: entry for entry in out["listed"]["sessions"]}
    assert out["listed"]["host_id"] == "itest-host"
    entry = entries[name]
    assert entry["foreign"] is False
    assert entry["incarnation"] == created["incarnation"]
    assert entry["alive"] is True
    assert entry["cols"] == 100 and entry["rows"] == 40, "shell_list did not observe the resize"
    assert Path(str(entry["cwd"])).resolve() == tmp_path.resolve()
    assert entry["created_at"] > 0 and entry["last_activity_at"] > 0

    assert out["killed"] == {
        "session": f"itest-host:{name}",
        "tmux_name": name,
        "killed": True,
        "registry_warning": None,
    }
    assert name not in tmux_server.sessions()


def test_read_preserves_ansi(tmux_server: TmuxServer, tmp_path: Path) -> None:
    """``shell_read`` returns ESC sequences, not stripped text (``capture-pane -p -e``).

    The Phase 4 renderer is xterm.js, which needs the escapes; omnigent strips them, and the
    omission looks completely fine until a terminal has to draw the output. So the assertion
    is on a literal ESC byte, not on "the text is there".
    """
    harness = make_harness(tmux_server, tmp_path)
    name = harness.name("ansi")

    async def script(client: ClientSession) -> str:
        await call(client, "shell_create", {"name": name, "cwd": str(tmp_path)})
        await call(
            client,
            "shell_send",
            {"session": name, "text": "printf '\\033[31mRED\\033[39m\\n'\n"},
        )
        return await await_content(client, name, "RED")

    content = run_script(harness, script)
    assert "\x1b[31m" in content, f"ANSI was stripped from the capture: {content!r}"


def test_send_keys_are_delivered_after_text(tmux_server: TmuxServer, tmp_path: Path) -> None:
    """``text`` then ``keys``, in that order (M18), through the client.

    The submitted text carries no trailing newline, so nothing runs until the ``Enter`` key
    does -- which is what makes the ordering observable rather than assumed.
    """
    harness = make_harness(tmux_server, tmp_path)
    name = harness.name("keys")

    async def script(client: ClientSession) -> tuple[dict[str, object], str]:
        await call(client, "shell_create", {"name": name, "cwd": str(tmp_path)})
        sent = (
            await call(
                client,
                "shell_send",
                {"session": name, "text": "echo KEYS-OK", "keys": ["Enter"]},
            )
        ).data
        return sent, await await_content(client, name, "KEYS-OK")

    sent, content = run_script(harness, script)
    assert sent["keys_sent"] == ["Enter"]
    assert sent["submitted_bytes"] == len("echo KEYS-OK")
    assert "KEYS-OK" in content


def test_recreate_with_matching_cwd_is_idempotent(tmux_server: TmuxServer, tmp_path: Path) -> None:
    """A second ``shell_create`` succeeds with ``created: false`` and the SAME incarnation.

    This is what lets a restarted agent call ``shell_create`` unconditionally. The
    incarnation must be unchanged: a re-create that quietly replaced it would make every
    misdelivery check downstream compare against a fresh value and pass.
    """
    harness = make_harness(tmux_server, tmp_path)
    name = harness.name("idem")
    args = {"name": name, "cwd": str(tmp_path)}
    first, second = run_calls(harness, [("shell_create", args), ("shell_create", dict(args))])
    assert first.data["created"] is True
    assert second.data["created"] is False
    assert second.data["incarnation"] == first.data["incarnation"]
    assert tmux_server.sessions().count(name) == 1


def test_kill_of_an_absent_session_succeeds(tmux_server: TmuxServer, tmp_path: Path) -> None:
    """``killed: false`` + SUCCESS. Two agents racing a kill: the loser is not an error."""
    harness = make_harness(tmux_server, tmp_path)
    (outcome,) = run_calls(harness, [("shell_kill", {"session": "never-existed"})])
    assert outcome.data["killed"] is False


def test_list_is_empty_before_any_session_exists(tmux_server: TmuxServer, tmp_path: Path) -> None:
    """No tmux server on this socket yet -- the cold start -- is an empty inventory, not an error.

    Two distinct stderr signatures reach this path (``no server running`` and the
    socket-file-absent ``error connecting to ... (No such file or directory)``); this test
    exercises the second, since the fixture's server has never been started.
    """
    harness = make_harness(tmux_server, tmp_path)
    (outcome,) = run_calls(harness, [("shell_list", {})])
    assert outcome.data == {"host_id": "itest-host", "sessions": []}


def test_read_reports_a_dead_pane_and_still_returns_its_output(
    tmux_server: TmuxServer, tmp_path: Path
) -> None:
    """``alive: false`` with the output intact -- the reason ``remain-on-exit`` is on.

    Without it the pane's process exiting would destroy the session, exit the server, and
    lose exactly the output a caller wants to read: why a command failed.
    """
    harness = make_harness(tmux_server, tmp_path)
    name = harness.name("dead")

    async def script(client: ClientSession) -> dict[str, object]:
        await call(client, "shell_create", {"name": name, "cwd": str(tmp_path)})
        await call(client, "shell_send", {"session": name, "text": "echo BYE-NOW; exit\n"})
        await await_content(client, name, "BYE-NOW")
        # Poll for the pane's death rather than sleeping past it: the shell exits when it
        # gets round to it, not when tmux accepted the paste.
        last: dict[str, object] = {}
        deadline = anyio.current_time() + 10.0
        while anyio.current_time() < deadline:
            last = (await call(client, "shell_read", {"session": name})).data
            if last["alive"] is False:
                return last
            await anyio.sleep(0.05)
        raise AssertionError(f"pane never reported alive=false: {last!r}")

    read = run_script(harness, script)
    assert read["alive"] is False
    assert "BYE-NOW" in str(read["content"])


def test_read_lines_reports_scrollback_facts(tmux_server: TmuxServer, tmp_path: Path) -> None:
    """``lines`` includes scrollback, and the two raw history facts are reported.

    ``history_limit`` is asserted at 20000 because §7.3's limit only reaches a pane when it
    is set BEFORE ``new-session`` in the create chain: reading the global would report 20000
    while the pane sat at tmux's 2000 default. This is that oracle, seen from the client.
    """
    harness = make_harness(tmux_server, tmp_path)
    name = harness.name("hist")

    async def script(client: ClientSession) -> dict[str, object]:
        await call(client, "shell_create", {"name": name, "cwd": str(tmp_path)})
        await call(client, "shell_send", {"session": name, "text": "seq 1 200\n"})
        await await_content(client, name, "200")
        return (await call(client, "shell_read", {"session": name, "lines": 300})).data

    read = run_script(harness, script)
    assert read["history_limit"] == 20_000
    assert read["scrollback_lines"] > 0
    assert read["lines"] == 300
    assert "1\n" in str(read["content"]), "scrollback above the visible pane was not returned"


def test_a_session_id_from_another_host_is_rejected(
    tmux_server: TmuxServer, tmp_path: Path
) -> None:
    """``<other-host>:<name>`` is ``invalid_name``, not ``not_found``.

    The session may well exist -- elsewhere. Answering ``not_found`` would invite a caller
    to retry it against this host forever.
    """
    harness = make_harness(tmux_server, tmp_path)
    name = harness.name("host")
    outcomes = run_calls(
        harness,
        [
            ("shell_create", {"name": name, "cwd": str(tmp_path)}),
            ("shell_read", {"session": f"other-host:{name}"}),
            # The full local session id, by contrast, must resolve.
            ("shell_read", {"session": f"itest-host:{name}"}),
        ],
    )
    assert outcomes[1].code == "invalid_name"
    assert outcomes[2].data["tmux_name"] == name
