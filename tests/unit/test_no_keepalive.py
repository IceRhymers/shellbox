"""T-NO-KEEPALIVE -- probe constraint 3, and the distinction it is constantly collapsed into.

**Measured: keepalives do not extend a socket's life.** A hold pinging every 5 s died in the
same second as one pinging every 20 s (`probe/FINDINGS.md`). The kill is a wall-clock event at
the edge and nothing the client sends postpones it. So this module implements no
application-level keepalive: no background ping task, no heartbeat frame, no timer that writes
to a socket to keep it warm. Such a thing would add traffic, add a second thing that can fail,
add load to the sandbox's autostop input -- and, measurably, buy nothing.

CRITICAL: **That is a statement about SURVIVAL, not about DETECTION, and the difference is one
line of configuration.** The tempting next step from "keepalives do not help" is
``ping_interval=None``, which does not remove a useless keepalive -- it deletes the only
detector for a *silent* death, a route that disappears with no FIN and no RST. The common case
(an abrupt TCP close) surfaces immediately either way; the silent case surfaces only when the
library's own keepalive times out, and never otherwise. A publisher would then hold a dead
socket indefinitely, publishing into it, with the pane alive and the browser frozen.

So both halves are asserted here, and they pull in opposite directions on purpose:

1. shellbox writes **no** keepalive of its own -- structurally, over the AST.
2. ``websockets``' keepalive is **on** and configured explicitly, with both values reaching the
   library on every dial rather than merely appearing in the source.
"""

from __future__ import annotations

import ast
import inspect
import uuid
from pathlib import Path

import anyio
from shellbox_mcp import transport as transport_module
from shellbox_mcp.transport import WSTransport, WSTransportConfig
from shellbox_transport import Frame, Stream
from wsfakes import SESSION_ID, FakeConnection, ScriptedDial, hello_bytes

EPOCH = str(uuid.uuid4())

_SOURCE = Path(inspect.getsourcefile(transport_module) or "").read_text(encoding="utf-8")
_TREE = ast.parse(_SOURCE)


def config() -> WSTransportConfig:
    return WSTransportConfig(url="wss://app.example/publish", session_id=SESSION_ID, epoch=EPOCH)


# --------------------------------------------------------------------------------------
# 1. No keepalive of shellbox's own -- structurally
# --------------------------------------------------------------------------------------


def test_the_module_spawns_no_background_task() -> None:
    """T-NO-KEEPALIVE. An application-level keepalive needs somewhere to run.

    A ping loop is a coroutine scheduled alongside the publisher, so the absence of any task
    spawn is a much stronger claim than the absence of the word "ping": it rules out the
    mechanism rather than one spelling of it.

    It also protects a second property. This class runs on one daemon thread with its own loop
    and is explicitly not thread-safe, so a stray background task writing to the same socket
    would interleave with ``publish`` and split a frame down the middle.
    """
    spawns = {"create_task", "ensure_future", "run_coroutine_threadsafe", "start_soon"}
    found = [
        node.func.attr
        for node in ast.walk(_TREE)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in spawns
    ]

    assert found == [], f"transport.py spawns {found}; a keepalive loop would arrive this way"


def test_no_function_here_is_named_for_a_keepalive() -> None:
    """T-NO-KEEPALIVE. The readable half of the same claim.

    Weaker than the task check and kept anyway: it fails with a clear name at review time,
    where the AST check fails with a mechanism nobody was looking for.
    """
    banned = ("ping", "pong", "keepalive", "keep_alive", "heartbeat")
    named = [
        node.name
        for node in ast.walk(_TREE)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and any(word in node.name.lower() for word in banned)
    ]

    assert named == [], f"{named} looks like an application-level keepalive"


def test_the_only_socket_write_in_the_module_is_publishing_a_frame() -> None:
    """T-NO-KEEPALIVE. Whatever goes down this socket is a frame a caller asked to send.

    Counted as well as located, because a structural check that silently matches nothing is
    the failure mode for this whole family of tests.
    """
    senders = set()
    for node in ast.walk(_TREE):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for inner in ast.walk(node):
            if (
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Attribute)
                and inner.func.attr in {"send", "send_bytes", "send_text", "pong"}
            ):
                senders.add(node.name)

    assert senders == {"publish"}, f"something other than publish writes to the socket: {senders}"


# --------------------------------------------------------------------------------------
# 2. The library's keepalive IS configured, explicitly, and reaches the library
# --------------------------------------------------------------------------------------


def test_both_keepalive_values_reach_the_dial_on_every_attempt() -> None:
    """T-NO-KEEPALIVE. Passed, not defaulted -- and this is the version-safety assertion.

    MEASURED: ``websockets`` 15.0.1 defaults to ``ping_interval=20``/``ping_timeout=20``, and
    the sandbox image ships 14.2 (probe findings). The dependency range spans both, so a
    default that differs between them must not change how this transport behaves. Asserting on
    what the dial RECEIVED rather than on what the source says is what makes that true.
    """
    dial = ScriptedDial([FakeConnection([hello_bytes(SESSION_ID, EPOCH)])])
    transport = WSTransport(config(), dial=dial)

    async def scenario() -> None:
        stream = transport.connect_forever()
        try:
            await stream.__anext__()
        finally:
            await stream.aclose()

    anyio.run(scenario)

    (_url, kwargs) = dial.calls[0]
    assert kwargs["ping_interval"] == 20.0
    assert kwargs["ping_timeout"] == 20.0
    assert kwargs["open_timeout"] == 10.0


def test_the_keepalive_is_not_disabled() -> None:
    """T-NO-KEEPALIVE. The line this file exists to stop someone writing.

    ``ping_interval=None`` reads like an implementation of constraint 3 and is the opposite of
    one: it deletes the detector for a silent death while doing nothing about survival, which
    is the only thing constraint 3 measured.
    """
    defaults = config()

    assert defaults.ping_interval is not None
    assert defaults.ping_timeout is not None
    assert defaults.ping_interval > 0
    assert defaults.ping_timeout > 0


def test_the_detector_is_named_so_the_decision_is_on_the_record() -> None:
    """T-NO-KEEPALIVE. ``W17`` owes a statement of which mechanism detects an abrupt close.

    Exposed as a property rather than left in a comment so the live run can report it beside
    the reconnect counts, and so the answer is "the library's, deliberately" rather than a
    default nobody chose.
    """
    detector = WSTransport(config(), dial=ScriptedDial([])).detector

    assert "ping_timeout" in detector
    assert "TCP close" in detector


def test_a_transport_holding_a_live_socket_sends_nothing_unbidden() -> None:
    """T-NO-KEEPALIVE. The functional half: hold a connection, publish once, count the writes.

    The structural checks rule out the mechanisms a keepalive would use. This rules out the
    behavior, so a keepalive smuggled in by a means the AST checks do not name still fails.
    """
    connection = FakeConnection([hello_bytes(SESSION_ID, EPOCH)])
    dial = ScriptedDial([connection])
    transport = WSTransport(config(), dial=dial)
    frame = Frame(session_id=SESSION_ID, seq=1, t=1.0, stream=Stream.STDOUT, data=b"output")

    async def scenario() -> None:
        stream = transport.connect_forever()
        try:
            await stream.__anext__()
            assert connection.sent == [], "the handshake itself must write nothing"
            await transport.publish(frame)
            # Long enough that a 1-tick keepalive loop would have fired several times.
            await anyio.sleep(0.05)
        finally:
            await stream.aclose()

    anyio.run(scenario)

    assert len(connection.sent) == 1, f"exactly the published frame, got {connection.sent!r}"
