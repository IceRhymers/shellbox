"""T-RESTART (§11.4) -- the central architectural claim, asserted end to end.

The claim: **a shellbox session is not owned by the MCP process that created it.** A tmux
client forks a server that daemonizes and reparents away, so the tmux server is a *sibling*
of the MCP process rather than a descendant. Kill the MCP process outright and the session,
its scrollback and its **identity** all survive; a completely new process picks it up.

Three properties, and the test is only worth running because it asserts all three:

a. **The tmux server outlives the MCP process.** Proven in two halves, because either half
   alone is weak. *Not a descendant*: the server's parent is not the MCP process that
   started it (an orphan reparents on death, so a post-mortem check cannot tell a sibling
   from a reparented child). *Still alive*: the SAME server pid answers afterwards -- a
   fresh server on the same socket would look identical to a caller and prove nothing.
b. **The session is usable from a fresh process**: read, send and kill all work.
c. **``@shellbox_incarnation`` is UNCHANGED.** Without (c) this test passes just as well
   against a session that was silently destroyed and recreated under the same name, which
   is the opposite of what is being claimed. And per §9.1 an assertion *about* an
   incarnation is worthless unless the incarnation exists, so its non-emptiness is asserted
   first (§11.4 step 2a: r2 shipped an inert ``set-option``, and this test's ancestor
   compared ``"" == ""`` and reported green).

``SIGKILL``, never ``SIGTERM``: the property under test is that **no cleanup handler is
needed**, so no cleanup handler may be given the chance to run. A graceful shutdown could
tear the session down and hide exactly the failure this test exists to catch.

WARNING: **Why this module spawns the server itself instead of using ``run_script``.** The harness
drives the child through the SDK's ``stdio_client``, which owns the process and never
exposes it -- and this test's whole subject is that process's pid and its violent death.
Everything else is reused: ``make_harness`` builds the environment, ``run_script`` drives the
*successor* process through the real SDK client, and ``Outcome`` is the same payload type the
rest of the lane asserts against.
"""

from __future__ import annotations

import itertools
import json
import os
import signal
import subprocess
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from conftest import TmuxServer, await_file, requires_tmux, sentinel
from harness import Harness, Outcome, await_content, call, make_harness, run_script
from mcp import ClientSession
from mcp.types import LATEST_PROTOCOL_VERSION

pytestmark = requires_tmux

# Long enough to absorb CI scheduling noise on a cold interpreter, short enough that a hung
# handshake fails this test rather than the job.
RPC_TIMEOUT = 60.0


@dataclass
class LiveServer:
    """One ``shellbox-mcp`` process this test OWNS, so it can be ``SIGKILL``ed mid-session.

    Hand-written JSON-RPC rather than ``ClientSession`` for one reason only: the pid. The
    protocol surface itself is covered by the rest of ``tests/integration/``; here it is
    just the means of getting a session created before the process is destroyed.
    """

    process: subprocess.Popen[str]
    _ids: Iterator[int] = field(default_factory=lambda: itertools.count(1))

    @property
    def pid(self) -> int:
        return self.process.pid

    def _write(self, message: dict[str, Any]) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()

    def _request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """One request/response round-trip, with a watchdog instead of a read timeout.

        A ``readline`` on a silent child blocks forever, which would hang the suite rather
        than fail one test.
        """
        assert self.process.stdout is not None
        request_id = next(self._ids)
        self._write({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}})
        watchdog = threading.Timer(RPC_TIMEOUT, self.process.kill)
        watchdog.start()
        try:
            line = self.process.stdout.readline()
        finally:
            watchdog.cancel()
        assert line, f"the server closed stdout while answering {method!r}"
        message = dict(json.loads(line))
        assert message.get("id") == request_id, f"out-of-order response to {method!r}: {message}"
        assert "result" in message, f"{method!r} failed: {message}"
        return dict(message["result"])

    def handshake(self) -> None:
        self._request(
            "initialize",
            {
                "protocolVersion": LATEST_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "shellbox-restart-probe", "version": "0"},
            },
        )
        self._write({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def call(self, name: str, arguments: dict[str, Any]) -> Outcome:
        result = self._request("tools/call", {"name": name, "arguments": arguments})
        text = "\n".join(
            str(block.get("text", ""))
            for block in result.get("content", [])
            if block.get("type") == "text"
        )
        return Outcome(
            is_error=bool(result.get("isError")),
            text=text,
            structured=result.get("structuredContent"),
        )

    def sigkill(self) -> int:
        """``SIGKILL`` the process and reap it. Returns its exit status."""
        os.kill(self.pid, signal.SIGKILL)
        return self.process.wait(timeout=30)


@contextmanager
def live_server(harness: Harness) -> Iterator[LiveServer]:
    """Spawn ``python -m shellbox_mcp`` on stdio, handshake, and yield a handle to it.

    ``python -m`` rather than the console script, matching ``run_script``: it is the same
    entrypoint and it is guaranteed to be the interpreter running the suite, so the test
    cannot silently exercise a stale installed copy.
    """
    with harness.stderr_path.open("a", encoding="utf-8") as errlog:
        process = subprocess.Popen(  # noqa: S603 -- fixed argv, no shell
            [sys.executable, "-m", "shellbox_mcp"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=errlog,
            env=harness.env,
            text=True,
            bufsize=1,
        )
        server = LiveServer(process=process)
        try:
            server.handshake()
            yield server
        finally:
            # Only reached if the test did not kill it (or failed before doing so): a
            # leaked server would hold the socket open past the fixture's teardown.
            if process.poll() is None:  # pragma: no cover - the happy path SIGKILLs it
                process.kill()
                process.wait(timeout=30)


def tmux_server_pid(tmux: TmuxServer, name: str) -> int:
    """The tmux SERVER's pid, read from tmux itself.

    ``#{pid}`` is the server pid (``#{client_pid}`` would be the transient client). Read
    through an anchored target so a prefix match cannot resolve some other session.
    """
    result = tmux.raw("display-message", "-p", "-t", f"={name}:", "#{pid}")
    assert result.rc == 0, f"could not read the tmux server pid: {result.stderr!r}"
    value = result.stdout_raw.split("\n", 1)[0]
    assert value.isdigit(), f"#{{pid}} did not expand to a pid: {value!r}"
    return int(value)


def parent_pid(pid: int) -> int:
    """The parent pid of ``pid``, from ``/proc`` where it exists and ``ps`` otherwise.

    ``/proc`` first because the tmux-3.4 gate runs in an ``ubuntu:24.04`` container that
    does not ship ``procps``; ``ps`` is the macOS lane's path.
    """
    stat = Path(f"/proc/{pid}/stat")
    if stat.exists():
        # The command name is parenthesised and may itself contain spaces and parens, so the
        # fields are read from after the LAST ')': they are then `state ppid ...`.
        fields = stat.read_text(encoding="utf-8", errors="replace").rpartition(")")[2].split()
        return int(fields[1])
    result = subprocess.run(  # noqa: S603 -- fixed argv, no shell
        ["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True, text=True, check=True
    )
    return int(result.stdout.strip())


def is_alive(pid: int) -> bool:
    """Whether ``pid`` still names a live process. Signal 0 checks without delivering."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover - alive, owned by somebody else
        return True
    return True


def test_a_session_survives_a_sigkilled_mcp_server(tmux_server: TmuxServer, tmp_path: Path) -> None:
    """T-RESTART, all nine steps of §11.4.

    The sentinel is written to a FILE by the pane's own process, and the poll waits for that
    file: it is the only oracle that proves the shell actually *ran* the command before the
    MCP process died. ``shell_send`` returns as soon as tmux accepts the paste, which is
    strictly earlier, and a screen scrape would show the echoed command line whether or not
    it ever executed.
    """
    harness = make_harness(tmux_server, tmp_path)
    name = "build"
    marker = tmp_path / "marker"
    # Two distinct tokens: one written BEFORE the kill (so finding it afterwards proves
    # survival) and one produced AFTER it (so the successor process's send cannot be
    # satisfied by output the first process already left on the pane).
    # The pty ECHOES whatever is pasted, so a needle visible in the command line as typed
    # would be found on the pane whether or not the shell ever ran it -- steps 8 and 9 would
    # then pass against a session that is listed but dead. `conftest.sentinel` splits the
    # token so only the shell's OUTPUT holds it contiguously, and enforces that invariant in
    # its constructor rather than leaving it to a hand-written assertion here.
    before = sentinel("BEFORE")
    after = sentinel("AFTER")

    with live_server(harness) as first:
        # Steps 1-2: create, and record the incarnation.
        created = first.call("shell_create", {"name": name, "cwd": str(tmp_path)}).data
        incarnation = str(created["incarnation"])
        # Step 2a. Everything below compares against this value, and a comparison two empty
        # strings satisfy is not an identity check (§9.1).
        assert incarnation, "shell_create returned an empty incarnation; step 7 would be vacuous"
        assert created["created"] is True

        # Step 3: make the pane's process do real work, and wait for the evidence it wrote.
        # `tee`, not `>`: the token must land BOTH in the file (the oracle that the pane's
        # process really ran the command, below) and on its own output line (so step 8 can
        # find it after the restart). The ECHOED command line is no good for step 8 -- the
        # pane is 80 columns and the prompt pushes the token across a wrap, which splits it.
        sent = first.call(
            "shell_send", {"session": name, "text": f"echo {before.typed} | tee {marker}\n"}
        ).data
        assert sent["incarnation"] == incarnation
        assert before.awaited.encode() in await_file(
            str(marker),
            lambda data: before.awaited.encode() in data,
            timeout=15.0,
            what="the pre-kill sentinel",
        )

        # Property (a), first half -- asserted while the MCP process is still ALIVE, because
        # afterwards every orphan has reparented and a descendant is indistinguishable from
        # a sibling.
        tmux_pid = tmux_server_pid(tmux_server, name)
        assert tmux_pid != first.pid
        assert parent_pid(tmux_pid) != first.pid, (
            f"the tmux server (pid {tmux_pid}) is a CHILD of the MCP process (pid {first.pid}): "
            "it did not reparent away, so sessions are owned by the process that created them"
        )

        # Step 4.
        status = first.sigkill()

    assert status == -signal.SIGKILL, (
        f"the MCP process exited with {status}, not by SIGKILL; if it caught the signal or "
        "exited first, no cleanup handler was ruled out and this test proves nothing"
    )

    # Step 5 / property (a), second half. The pid check is what makes this an assertion about
    # the SAME server: a new server on the same socket would answer identically.
    assert is_alive(tmux_pid), (
        f"the tmux server (pid {tmux_pid}) died with the MCP process: shellbox sessions do "
        "not outlive their creator"
    )
    assert tmux_server.sessions() == [name]
    assert tmux_server_pid(tmux_server, name) == tmux_pid, "a DIFFERENT tmux server answered"

    # Steps 6-9: a brand new process -- new pid, no shared memory -- does all the work.
    async def successor(client: ClientSession) -> dict[str, Any]:
        listed = (await call(client, "shell_list")).data
        read = (await call(client, "shell_read", {"session": name})).data
        sent = (await call(client, "shell_send", {"session": name, "text": after.echo()})).data
        # §11.1: the send returns once tmux accepted the paste, strictly before the shell
        # has run anything, so the effect is POLLED for -- never slept on.
        content = await await_content(client, name, after.awaited, timeout=20.0, lines=200)
        killed = (await call(client, "shell_kill", {"session": name})).data
        gone = (await call(client, "shell_list")).data
        return {
            "listed": listed,
            "read": read,
            "sent": sent,
            "content": content,
            "killed": killed,
            "gone": gone,
        }

    out = run_script(harness, successor)

    # Step 7 -- property (c). `foreign: false` as well: an unstamped session would report
    # `incarnation: null`, and `None == None` is not identity either.
    entries = {entry["tmux_name"]: entry for entry in out["listed"]["sessions"]}
    assert name in entries, "the session did not survive the restart"
    entry = entries[name]
    assert entry["foreign"] is False
    assert entry["incarnation"] == incarnation, (
        "the session's incarnation CHANGED across the restart: the name survived but the "
        "session did not -- it was destroyed and recreated, which is what (c) exists to catch"
    )
    assert entry["alive"] is True

    # Step 8: the pane's scrollback survived too -- the pre-kill command line is still there.
    assert before.awaited in out["read"]["content"]

    # Step 9 -- property (b). The send reports the SAME incarnation, so the successor is
    # addressing the surviving session rather than a replacement, and `after` can only come
    # from a command the successor delivered.
    assert out["sent"]["incarnation"] == incarnation
    assert after.awaited in out["content"]
    assert out["killed"]["killed"] is True
    assert out["gone"]["sessions"] == []
    assert tmux_server.sessions() == []
