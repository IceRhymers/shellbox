"""T-HELLO-DEADLINE -- a 101 is not proof of a working transport, and silence must not hang.

Two measured facts force everything here. The Apps edge answers an unauthenticated upgrade
with a **302**, and an unauthenticated POST with an **HTTP 200 carrying an HTML login body**
(`probe/FINDINGS.md`). So the edge can complete a handshake to something that is not the App,
and a client that treats the upgrade as success streams a pty into a void while reporting that
it is connected.

The gate is therefore a ``hello`` control frame naming the bound ``session_id``, under a
deadline. The deadline is the half that is easy to leave out, because a plain ``recv()`` looks
correct and passes every test where the server does answer -- it only fails against a server
that completes the upgrade and then says nothing, and it fails by hanging forever with the
publisher believing it is up. ``FakeConnection(hang=True)`` is exactly that server.

WARNING: A ``hello`` naming a DIFFERENT ``session_id`` is terminal, not transient. It means the
server bound a session this publisher does not own, so streaming into it would cross two
agents' terminals. Retrying cannot make that right, and these tests pin it as the one
handshake failure that stops the loop.
"""

from __future__ import annotations

import logging
import uuid

import anyio
import pytest
from shellbox_mcp.transport import (
    Connected,
    Failure,
    TransportTerminal,
    WSTransport,
    WSTransportConfig,
)
from wsfakes import (
    SESSION_ID,
    FakeConnection,
    RecordingSleep,
    ScriptedDial,
    data_frame_bytes,
    hello_bytes,
    other_kind_bytes,
)

EPOCH = str(uuid.uuid4())

# Short enough that the hang case costs milliseconds, long enough that a loaded CI box does not
# time out a handshake that a real fake actually answered.
_DEADLINE = 0.05


def config(**overrides: object) -> WSTransportConfig:
    base: dict[str, object] = {
        "url": "wss://app.example/publish",
        "session_id": SESSION_ID,
        "epoch": EPOCH,
        "hello_deadline": _DEADLINE,
        "backoff_floor": 0.0,
        "backoff_cap": 0.0,
    }
    base.update(overrides)
    return WSTransportConfig(**base)  # type: ignore[arg-type]


async def first_connection(transport: WSTransport) -> Connected:
    """Drive ``connect_forever`` until it yields once, then shut the generator down.

    ``aclose`` matters: the loop is an async generator holding a live connection, and leaving
    it suspended leaks the socket into the next test.
    """
    stream = transport.connect_forever()
    try:
        return await stream.__anext__()
    finally:
        await stream.aclose()


# --------------------------------------------------------------------------------------
# The happy path
# --------------------------------------------------------------------------------------


def test_a_hello_naming_the_dialed_session_is_what_connects() -> None:
    """T-HELLO-DEADLINE. "Connected" is this frame, not the 101 that preceded it."""
    dial = ScriptedDial([FakeConnection([hello_bytes(SESSION_ID, EPOCH)])])
    transport = WSTransport(config(), dial=dial)

    connected = anyio.run(first_connection, transport)

    assert connected.hello.session_id == SESSION_ID
    assert connected.attempt == 1, "a first-try success is attempt 1"
    assert len(dial.calls) == 1


def test_the_dial_url_carries_the_session_and_the_epoch() -> None:
    """T-HELLO-DEADLINE. Both sides' logs must be able to name the same attach.

    The epoch is not a credential and not a secret. A subscriber's only permitted use of one is
    pessimistic -- "distrust everything and repaint" -- so putting it in a query string costs
    nothing and buys a correlatable log line.
    """
    transport = WSTransport(config(url="wss://app.example/publish"), dial=ScriptedDial([]))

    assert f"session_id={SESSION_ID}" in transport.dial_url()
    assert f"epoch={EPOCH}" in transport.dial_url()
    assert transport.dial_url().startswith("wss://app.example/publish?")

    with_query = WSTransport(config(url="wss://app.example/publish?x=1"), dial=ScriptedDial([]))
    assert "?x=1&" in with_query.dial_url(), "an existing query string is appended to, not broken"


def test_the_viewer_email_is_carried_through_for_display() -> None:
    """T-HELLO-DEADLINE. D5: display only, and never an input to any decision here."""
    dial = ScriptedDial(
        [FakeConnection([hello_bytes(SESSION_ID, EPOCH, viewer_email="someone@example.com")])]
    )

    connected = anyio.run(first_connection, WSTransport(config(), dial=dial))

    assert connected.hello.viewer_email == "someone@example.com"


def test_a_hello_with_no_viewer_email_reports_none_rather_than_failing() -> None:
    """T-HELLO-DEADLINE. The loopback lane's case: no edge, so no header to inject."""
    dial = ScriptedDial([FakeConnection([hello_bytes(SESSION_ID, EPOCH)])])

    connected = anyio.run(first_connection, WSTransport(config(), dial=dial))

    assert connected.hello.viewer_email is None


# --------------------------------------------------------------------------------------
# The deadline itself
# --------------------------------------------------------------------------------------


def test_a_server_that_upgrades_and_then_says_nothing_does_not_connect() -> None:
    """T-HELLO-DEADLINE. The test the deadline exists for.

    Without ``asyncio.wait_for`` this hangs forever, and it hangs in the worst possible state:
    the publisher believes it is connected, so nothing retries and nothing reports a failure.
    A hang is also the one failure a green suite cannot show you, which is why the fake makes
    it reachable in milliseconds.
    """
    sleep = RecordingSleep()
    dial = ScriptedDial(
        [FakeConnection(hang=True), FakeConnection([hello_bytes(SESSION_ID, EPOCH)])]
    )
    transport = WSTransport(config(), dial=dial, sleep=sleep)

    connected = anyio.run(first_connection, transport)

    assert connected.attempt == 2, "the silent server must be abandoned and re-dialed"
    assert len(sleep.delays) == 1, "and the retry must be delayed, not immediate"


def test_a_connection_whose_hello_never_arrives_is_closed_rather_than_leaked() -> None:
    """T-HELLO-DEADLINE. A timed-out handshake still holds a socket.

    Leaving it open leaks a file descriptor per attempt, and this loop retries forever by
    design -- so a leak here is unbounded rather than merely untidy.
    """
    silent = FakeConnection(hang=True)
    dial = ScriptedDial([silent, FakeConnection([hello_bytes(SESSION_ID, EPOCH)])])

    anyio.run(first_connection, WSTransport(config(), dial=dial, sleep=RecordingSleep()))

    assert silent.closed, "the abandoned socket must be closed on the way out"


# --------------------------------------------------------------------------------------
# What the first frame is allowed to be
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "opening",
    [
        data_frame_bytes(SESSION_ID),
        other_kind_bytes(SESSION_ID, EPOCH),
        "a text message where bytes were required",
    ],
    ids=["data-frame-first", "wrong-control-kind", "text-message"],
)
def test_a_first_frame_that_is_not_a_hello_is_transient_and_retried(opening: bytes | str) -> None:
    """T-HELLO-DEADLINE. Not connected, but not terminal either.

    A slow or restarting App must not kill a publisher. Each of these says "whatever answered
    is not speaking this protocol yet", which the next dial may well resolve -- and the
    jittered floor bounds how fast the retry comes back.
    """
    dial = ScriptedDial(
        [FakeConnection([opening]), FakeConnection([hello_bytes(SESSION_ID, EPOCH)])]
    )

    connected = anyio.run(
        first_connection, WSTransport(config(), dial=dial, sleep=RecordingSleep())
    )

    assert connected.attempt == 2


def test_a_hello_naming_another_session_is_terminal() -> None:
    """T-HELLO-DEADLINE. The one handshake failure that stops the loop, and why.

    The server bound a session this publisher does not own. Streaming a pty into it would put
    one agent's terminal on another agent's screen, and would do it silently. No number of
    retries makes that correct, so the loop raises instead of hiding it behind a reconnect.
    """
    dial = ScriptedDial([FakeConnection([hello_bytes("somebody-elses-session", EPOCH)])])
    transport = WSTransport(config(), dial=dial, sleep=RecordingSleep())

    with pytest.raises(TransportTerminal) as caught:
        anyio.run(first_connection, transport)

    assert caught.value.failure is Failure.PROTOCOL
    assert "somebody-elses-session" in str(caught.value)
    assert len(dial.calls) == 1, "a terminal failure must not be retried"


# --------------------------------------------------------------------------------------
# The epoch the server echoes -- or does not
# --------------------------------------------------------------------------------------


def test_a_null_epoch_from_the_app_server_is_the_expected_shape_and_logs_nothing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """T-HELLO-DEADLINE. The App server holds no attach and echoes no epoch.

    That is the Phase 3 shape on **every** dial, and the edge forces a dial roughly every 10 to
    18 minutes per publisher. A warning here would therefore be a log line four times an hour
    per session for the expected case, which is how a real warning becomes invisible.
    """
    hello = hello_bytes(SESSION_ID, epoch=None)
    dial = ScriptedDial([FakeConnection([hello])])

    with caplog.at_level(logging.WARNING, logger="shellbox_mcp.transport"):
        connected = anyio.run(first_connection, WSTransport(config(), dial=dial))

    assert connected.hello.epoch is None
    assert caplog.records == [], f"the expected shape must be silent, got {caplog.text!r}"


def test_a_server_echoing_the_wrong_epoch_warns_but_still_connects(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """T-HELLO-DEADLINE. Diagnostic, not fatal.

    This publisher's own epoch governs its frames either way, so a mismatched echo costs a
    confusing log line and nothing else. It is worth the line because it means the two sides
    disagree about which attach they are discussing.
    """
    dial = ScriptedDial([FakeConnection([hello_bytes(SESSION_ID, str(uuid.uuid4()))])])

    with caplog.at_level(logging.WARNING, logger="shellbox_mcp.transport"):
        connected = anyio.run(first_connection, WSTransport(config(), dial=dial))

    assert connected.hello.session_id == SESSION_ID, "it connects: only the epoch disagreed"
    assert "echoed epoch" in caplog.text
