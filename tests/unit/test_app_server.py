"""T-APP-* -- the App server's accept path, driven with a fake socket.

The unit lane asserts the **binding rule**: who gets bound, who gets refused, what the refusal
says, and what survives a publisher's socket dying. None of that needs a running server, and a
fake socket makes two things assertable that a real one hides -- the exact bytes sent before a
close, and the registry's state *while* a handler is still suspended mid-connection.

The loopback lane covers what this cannot: real frames over a real socket against a real tmux
session. Both are needed, and neither is the DoD proof.

WARNING: **The distinction these tests exist to protect is reconnect versus conflict.** A
publisher socket for an already-bound ``session_id`` is a CONFLICT when a live publisher holds
it and a REBIND when one does not. The Databricks Apps edge kills every open socket every 10 to
18 minutes on a wall-clock event -- measured by the Phase 1 probe, recorded in
`probe/FINDINGS.md` -- so a rebind is the hot path, and collapsing the two cases into "already
bound, refuse" would break the transport roughly every quarter hour while every happy-path test
stayed green.
"""

from __future__ import annotations

import ast
import inspect
import json
import logging
from collections.abc import Callable
from dataclasses import fields
from pathlib import Path

import anyio
import pytest
from appfakes import POLL_INTERVAL, FakeSocket, controls, hello_of
from shellbox_app.server import (
    CONTROL_SEQ,
    DEFAULT_PORT,
    REFUSED_CLOSE_CODE,
    WS_PING_INTERVAL_SECONDS,
    WS_PING_TIMEOUT_SECONDS,
    Attachment,
    Relay,
    build_app,
    health_payload,
    main,
    resolve_port,
    serve_publisher,
    serve_subscriber,
)
from shellbox_transport import Stream
from shellbox_transport.codec import (
    CONTROL_ERROR,
    CONTROL_HELLO,
    FIELD_CODE,
    FIELD_SESSION_ID,
    FIELD_VIEWER_EMAIL,
    decode_frame,
)

# `FakeSocket`, `controls` and `hello_of` moved to `tests/unit/appfakes.py` when
# `tests/unit/test_app_database.py` needed the same socket double. One definition, so the two
# files cannot disagree about what a dead socket does.

# Long enough to absorb scheduling noise, short enough that a handler which never binds fails
# one test instead of hanging the suite.
_BIND_TIMEOUT = 5.0


async def until(predicate: Callable[[], bool], what: str) -> None:
    """Poll until ``predicate`` holds, or fail naming ``what``.

    §11.1's rule, in the async lane: poll for a condition with a deadline, never sleep for a
    duration and then assert. ``what`` is required rather than optional because a bare timeout
    here reports only that something did not happen.
    """
    try:
        with anyio.fail_after(_BIND_TIMEOUT):
            while not predicate():
                await anyio.sleep(POLL_INTERVAL)
    except TimeoutError as exc:
        raise AssertionError(f"timed out after {_BIND_TIMEOUT}s waiting for {what}") from exc


def bound(relay: Relay, session_id: str) -> Attachment:
    attachment = relay.attachments.get(session_id)
    assert attachment is not None, f"{session_id} is not bound: {relay.attachments!r}"
    return attachment


# --------------------------------------------------------------------------------------
# T-APP-HELLO -- the frame that gates "connected"
# --------------------------------------------------------------------------------------


def test_a_publisher_gets_a_control_hello_naming_the_session_it_bound() -> None:
    """T-APP-HELLO. A 101 is not proof the transport is up; this frame is.

    The edge answers an unauthenticated upgrade with a 302 and an unauthenticated POST with an
    HTML login page under a 200 status, both measured by the probe. So the client waits for this
    frame under a deadline, and the ``session_id`` echoed here is what lets it detect that it
    reached a server which bound something other than what it dialled.
    """
    relay = Relay()
    socket = FakeSocket(headers={"x-forwarded-email": "agent@example.com"})

    anyio.run(serve_publisher, relay, socket, "sess-hello")

    assert socket.accepted, "the server must accept before it decides anything"
    frame = decode_frame(socket.sent[0])
    assert frame.stream is Stream.CONTROL, "hello is a control frame, never a data frame"
    assert frame.session_id == "sess-hello"
    assert frame.seq == CONTROL_SEQ, (
        "a server-originated frame must not occupy a position in the publisher's sequence space, "
        "or a subscriber reads it as a gap"
    )
    hello = hello_of(socket)
    assert hello.fields[FIELD_SESSION_ID] == "sess-hello"
    assert hello.fields[FIELD_VIEWER_EMAIL] == "agent@example.com"


def test_both_roles_get_the_same_hello_shape() -> None:
    """T-APP-HELLO. One message for both roles, carrying no role field.

    Which role a socket holds is decided by the route it dialled, so the client already knows. A
    field restating it would be a second place for the two to disagree.
    """
    publisher = FakeSocket()
    subscriber = FakeSocket()

    anyio.run(serve_publisher, Relay(), publisher, "sess-shape")
    anyio.run(serve_subscriber, Relay(), subscriber, "sess-shape")

    assert hello_of(publisher).kind == hello_of(subscriber).kind == CONTROL_HELLO
    assert hello_of(publisher).fields == hello_of(subscriber).fields


# --------------------------------------------------------------------------------------
# T-APP-IDENTITY-DISPLAY-ONLY -- D5
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({}, None),
        ({"x-forwarded-email": "someone@example.com"}, "someone@example.com"),
        ({"x-forwarded-email": "a-stranger@elsewhere.invalid"}, "a-stranger@elsewhere.invalid"),
    ],
)
def test_the_forwarded_email_is_reported_and_never_gates_the_bind(
    headers: dict[str, str], expected: str | None
) -> None:
    """T-APP-IDENTITY-DISPLAY-ONLY. ``X-Forwarded-Email`` is display, not authorization (D5).

    Every case binds. A missing header binds with the field absent, and an unfamiliar address
    binds exactly like a familiar one -- there is no allowlist here and there must not be one.
    The App holds no capability over any sandbox with any identity the Apps runtime gives it, so
    an authorization rule written against this header would be enforcement theatre over a value
    the App cannot act on anyway. See the package docstring for the three measured facts.

    The absent case is not a corner: the loopback lane has no edge to inject the header, so it is
    the case the integration suite runs.
    """
    relay = Relay()
    socket = FakeSocket(headers=headers)

    anyio.run(serve_publisher, relay, socket, "sess-identity")

    assert hello_of(socket).fields.get(FIELD_VIEWER_EMAIL) == expected


# --------------------------------------------------------------------------------------
# T-APP-PUBLISHER-CONFLICT and T-APP-PUBLISHER-REBIND -- the distinction the phase turns on
# --------------------------------------------------------------------------------------


def test_a_second_live_publisher_is_refused_and_the_first_keeps_the_session() -> None:
    """T-APP-PUBLISHER-CONFLICT. A second publisher for a live session is an ERROR.

    Two live publishers on one session would each mint an epoch, and a subscriber obeying the
    "unfamiliar epoch means repaint" rule would repaint continuously between two valid epochs --
    an undeclared discontinuity arriving twice a second, which the resume guarantee does not
    cover.

    The refusal is a frame and then a close, NOT a rejected handshake. The edge 302s an
    unauthenticated upgrade, so a handshake-level rejection is indistinguishable from an
    authentication failure -- which the publisher treats as terminal and stops retrying. A
    conflict must not be reported in a way that looks terminal.
    """
    relay = Relay()
    first = FakeSocket(hold=True)
    second = FakeSocket()

    async def scenario() -> None:
        async with anyio.create_task_group() as group:
            group.start_soon(serve_publisher, relay, first, "sess-conflict")
            await until(
                lambda: (
                    relay.attachments.get("sess-conflict") is not None
                    and bound(relay, "sess-conflict").publisher is first
                ),
                "the first publisher to bind",
            )
            await serve_publisher(relay, second, "sess-conflict")
            assert bound(relay, "sess-conflict").publisher is first, (
                "a refused publisher must not displace the live one"
            )
            first.hang_up()

    anyio.run(scenario)

    assert second.accepted, "the refusal happens after the 101, deliberately"
    assert [message.kind for message in controls(second)] == [CONTROL_ERROR], (
        "a refused publisher must get no hello -- a hello means connected"
    )
    assert controls(second)[0].fields[FIELD_CODE] == "publisher_conflict"
    assert second.closed == REFUSED_CLOSE_CODE


def test_a_reconnecting_publisher_rebinds_and_the_subscriber_survives() -> None:
    """T-APP-PUBLISHER-REBIND. The hot path: the edge killed the socket, so re-dial and continue.

    One live socket is the rule, not one connection ever. The subscriber must keep its socket
    across the publisher's death, or every edge kill would also drop the viewer -- and the
    viewer's repaint would then have to come from somewhere this server refuses to keep.
    """
    relay = Relay()
    subscriber = FakeSocket(hold=True)
    first = FakeSocket(hold=True)
    second = FakeSocket(hold=True)

    async def scenario() -> None:
        async with anyio.create_task_group() as group:
            group.start_soon(serve_subscriber, relay, subscriber, "sess-rebind")
            await until(
                lambda: (
                    relay.attachments.get("sess-rebind") is not None
                    and bound(relay, "sess-rebind").subscriber is subscriber
                ),
                "the subscriber to bind",
            )
            group.start_soon(serve_publisher, relay, first, "sess-rebind")
            await until(
                lambda: bound(relay, "sess-rebind").publisher is first, "the first publisher"
            )

            first.hang_up()
            await until(
                lambda: bound(relay, "sess-rebind").publisher is None, "the publisher to detach"
            )
            assert bound(relay, "sess-rebind").subscriber is subscriber, (
                "the subscriber must survive its publisher's socket dying"
            )

            group.start_soon(serve_publisher, relay, second, "sess-rebind")
            await until(
                lambda: bound(relay, "sess-rebind").publisher is second,
                "the reconnecting publisher to rebind",
            )
            assert bound(relay, "sess-rebind").subscriber is subscriber

            second.hang_up()
            subscriber.hang_up()

    anyio.run(scenario)

    assert [message.kind for message in controls(second)] == [CONTROL_HELLO], (
        "a reconnecting publisher is bound, not refused"
    )


def test_an_attachment_is_dropped_only_when_both_sides_are_gone() -> None:
    """T-APP-PUBLISHER-REBIND, the other half: the entry outlives one side but not both."""
    relay = Relay()
    publisher = FakeSocket(hold=True)
    subscriber = FakeSocket(hold=True)

    async def scenario() -> None:
        async with anyio.create_task_group() as group:
            group.start_soon(serve_publisher, relay, publisher, "sess-lifetime")
            group.start_soon(serve_subscriber, relay, subscriber, "sess-lifetime")
            await until(
                lambda: (
                    relay.attachments.get("sess-lifetime") is not None
                    and bound(relay, "sess-lifetime").publisher is publisher
                    and bound(relay, "sess-lifetime").subscriber is subscriber
                ),
                "both sides to bind",
            )
            publisher.hang_up()
            await until(
                lambda: bound(relay, "sess-lifetime").publisher is None, "the publisher to detach"
            )
            assert "sess-lifetime" in relay.attachments, "one side left, so the entry stays"
            subscriber.hang_up()

    anyio.run(scenario)

    assert relay.attachments == {}, "both sides gone, so the entry must not leak"


def test_a_stale_handler_cannot_detach_its_successor() -> None:
    """T-APP-PUBLISHER-REBIND. ``release`` takes the socket, not the role, and this is why.

    A publisher handler unwinds *after* a replacement has already bound. If the release cleared
    whichever socket held the publisher slot, the unwinding handler would detach the live
    successor -- and the next reconnect would then be refused as a conflict against a
    registration nobody was serving.
    """
    relay = Relay()
    dead = FakeSocket()
    live = FakeSocket()
    attachment = relay.bind_publisher("sess-stale", dead, None)
    assert attachment is not None
    attachment.publisher = live

    relay.release("sess-stale", dead)

    assert bound(relay, "sess-stale").publisher is live


# --------------------------------------------------------------------------------------
# T-APP-SUBSCRIBER-CONFLICT -- fan-out is Phase 4's
# --------------------------------------------------------------------------------------


def test_a_second_subscriber_is_refused() -> None:
    """T-APP-SUBSCRIBER-CONFLICT. One subscriber, by scope.

    Multi-subscriber fan-out is a Phase 4 product behavior (D6), and refusing the second socket
    is how that boundary is visible at runtime rather than only in a docstring.
    """
    relay = Relay()
    first = FakeSocket(hold=True)
    second = FakeSocket()

    async def scenario() -> None:
        async with anyio.create_task_group() as group:
            group.start_soon(serve_subscriber, relay, first, "sess-two-subs")
            await until(
                lambda: (
                    relay.attachments.get("sess-two-subs") is not None
                    and bound(relay, "sess-two-subs").subscriber is first
                ),
                "the first subscriber",
            )
            await serve_subscriber(relay, second, "sess-two-subs")
            assert bound(relay, "sess-two-subs").subscriber is first
            first.hang_up()

    anyio.run(scenario)

    assert controls(second)[0].fields[FIELD_CODE] == "subscriber_conflict"
    assert second.closed == REFUSED_CLOSE_CODE


def test_a_subscriber_may_attach_before_any_publisher_exists() -> None:
    """T-APP-SUBSCRIBER-CONFLICT, inverted. A viewer arriving mid-reconnect is not an error.

    The publisher's socket is absent for a moment every 10 to 18 minutes. Refusing a subscriber
    for that would refuse it for a condition that resolves itself in under a second.
    """
    relay = Relay()
    socket = FakeSocket()

    anyio.run(serve_subscriber, relay, socket, "sess-early")

    assert hello_of(socket).fields[FIELD_SESSION_ID] == "sess-early"


def test_a_refused_subscriber_leaves_no_empty_attachment_behind() -> None:
    """T-APP-SUBSCRIBER-CONFLICT. A refusal must not create the session it refused to join."""
    relay = Relay()
    first = FakeSocket(hold=True)

    async def scenario() -> None:
        async with anyio.create_task_group() as group:
            group.start_soon(serve_subscriber, relay, first, "sess-no-litter")
            await until(
                lambda: relay.attachments.get("sess-no-litter") is not None,
                "the first subscriber",
            )
            await serve_subscriber(relay, FakeSocket(), "sess-no-litter")
            first.hang_up()

    anyio.run(scenario)

    assert relay.attachments == {}


# --------------------------------------------------------------------------------------
# T-APP-RELAY -- the data path, both directions
# --------------------------------------------------------------------------------------


def test_publisher_frames_reach_the_subscriber_and_input_reaches_the_publisher() -> None:
    """T-APP-RELAY. Opaque bytes, both ways, unmodified.

    The payloads here are deliberately not valid frames: the server must not parse the data
    path. If a change here starts decoding relayed bytes, this test fails, which is the point.
    """
    relay = Relay()
    publisher = FakeSocket(hold=True)
    subscriber = FakeSocket(hold=True)
    outbound = b"\x00\xff not a frame, and it does not have to be"
    inbound = b"\x01\xfe neither is this"

    async def scenario() -> None:
        async with anyio.create_task_group() as group:
            group.start_soon(serve_subscriber, relay, subscriber, "sess-relay")
            await until(
                lambda: (
                    relay.attachments.get("sess-relay") is not None
                    and bound(relay, "sess-relay").subscriber is subscriber
                ),
                "the subscriber",
            )
            publisher.queue_bytes(outbound)
            group.start_soon(serve_publisher, relay, publisher, "sess-relay")
            await until(lambda: outbound in subscriber.sent, "the relayed publisher frame")

            subscriber.queue_bytes(inbound)
            await until(lambda: inbound in publisher.sent, "the relayed subscriber input")

            publisher.hang_up()
            subscriber.hang_up()

    anyio.run(scenario)


def test_frames_arriving_with_no_subscriber_are_dropped() -> None:
    """T-APP-RELAY. Nothing buffers here. Resume is live-stream-only (D7).

    A dropped frame is not a lost repaint: the subscriber's repaint comes from the publisher's
    ``capture-pane`` resync, which is why this server can afford to keep nothing.
    """
    relay = Relay()
    publisher = FakeSocket()
    publisher.queue_bytes(b"nobody is listening")

    anyio.run(serve_publisher, relay, publisher, "sess-void")

    assert [message.kind for message in controls(publisher)] == [CONTROL_HELLO], (
        "the publisher is served normally; only the frame is dropped"
    )
    assert relay.attachments == {}


def test_a_dead_subscriber_does_not_take_the_publisher_down() -> None:
    """T-APP-RELAY. A viewer closing a browser tab must not tear down an agent's publisher."""
    relay = Relay()
    publisher = FakeSocket(hold=True)
    subscriber = FakeSocket(hold=True)

    async def scenario() -> None:
        async with anyio.create_task_group() as group:
            group.start_soon(serve_subscriber, relay, subscriber, "sess-dead-peer")
            await until(
                lambda: (
                    relay.attachments.get("sess-dead-peer") is not None
                    and bound(relay, "sess-dead-peer").subscriber is subscriber
                ),
                "the subscriber",
            )
            subscriber.die()
            publisher.queue_bytes(b"into a dead socket", b"and again")
            group.start_soon(serve_publisher, relay, publisher, "sess-dead-peer")
            await until(
                lambda: bound(relay, "sess-dead-peer").publisher is publisher, "the publisher"
            )
            # Still bound and still being served after two sends into a dead peer.
            assert bound(relay, "sess-dead-peer").publisher is publisher
            publisher.hang_up()
            subscriber.hang_up()

    anyio.run(scenario)


def test_a_non_binary_message_is_dropped_rather_than_relayed() -> None:
    """T-APP-RELAY. The frame protocol is binary, and a text message is named here, not forwarded.

    Relaying it would make the peer's decoder fail on a message this server could have reported.
    """
    relay = Relay()
    publisher = FakeSocket(hold=True)
    subscriber = FakeSocket(hold=True)

    async def scenario() -> None:
        async with anyio.create_task_group() as group:
            group.start_soon(serve_subscriber, relay, subscriber, "sess-text")
            await until(
                lambda: (
                    relay.attachments.get("sess-text") is not None
                    and bound(relay, "sess-text").subscriber is subscriber
                ),
                "the subscriber",
            )
            publisher.queue_text("a text frame is a protocol violation")
            publisher.queue_bytes(b"and this one is not")
            group.start_soon(serve_publisher, relay, publisher, "sess-text")
            await until(lambda: b"and this one is not" in subscriber.sent, "the binary frame")
            publisher.hang_up()
            subscriber.hang_up()

    anyio.run(scenario)

    # `sent[0]` is the subscriber's own hello; everything after it was relayed.
    assert subscriber.sent[1:] == [b"and this one is not"], (
        "the text message must be dropped, and the binary one after it must still arrive"
    )


# --------------------------------------------------------------------------------------
# T-APP-NO-BUFFER -- the structural half of D7
# --------------------------------------------------------------------------------------


def test_an_attachment_holds_two_sockets_and_a_display_name_and_nothing_else() -> None:
    """T-APP-NO-BUFFER. A new field here is how a server-side frame log would arrive.

    Resume is live-stream-only (D7), a ``session_frames`` table was already rejected once by
    review, and a ring buffer on this object would be that rejection re-litigated in a place
    nobody is watching. The ring that does exist belongs to the publisher and dies with it.
    """
    assert {field_.name for field_ in fields(Attachment)} == {
        "session_id",
        "publisher",
        "subscriber",
        "identity",
    }


def test_the_binding_methods_suspend_nowhere() -> None:
    """T-APP-NO-BUFFER's sibling: the race guard, structurally.

    asyncio interleaves only at an ``await``, so "is a live publisher registered?" is a decision
    rather than a race precisely because no suspension point separates the check from the write.
    An ``await`` introduced into either method would let two simultaneous publishers both pass
    the check, and no functional test in this suite would reliably catch it.
    """
    source = ast.parse(Path(inspect.getfile(Relay)).read_text(encoding="utf-8"))
    checked = 0
    for node in ast.walk(source):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if node.name not in {"bind_publisher", "bind_subscriber"}:
            continue
        checked += 1
        assert not isinstance(node, ast.AsyncFunctionDef), f"{node.name} must not be a coroutine"
        awaits = [inner for inner in ast.walk(node) if isinstance(inner, ast.Await)]
        assert not awaits, f"{node.name} suspends, so its check and its write can interleave"
    assert checked == 2, f"expected to inspect two binding methods, inspected {checked}"


# --------------------------------------------------------------------------------------
# T-APP-HEALTH-NO-NAMES and T-APP-PORT
# --------------------------------------------------------------------------------------


def test_the_health_payload_counts_sessions_and_never_names_one() -> None:
    """T-APP-HEALTH-NO-NAMES. Any workspace user the edge admits reaches ``GET /``.

    A session name is not theirs to enumerate, so the route reports cardinality only.
    """
    relay = Relay()
    relay.attachments["a-private-session"] = Attachment(
        session_id="a-private-session", publisher=FakeSocket()
    )
    relay.attachments["another-one"] = Attachment(session_id="another-one")

    payload = health_payload(relay)

    assert payload["sessions"] == 2
    assert payload["publishers"] == 1
    assert payload["subscribers"] == 0
    rendered = json.dumps(payload)
    assert "a-private-session" not in rendered
    assert "another-one" not in rendered


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, DEFAULT_PORT), ("", DEFAULT_PORT), ("  ", DEFAULT_PORT), ("9001", 9001), ("80", 80)],
)
def test_resolve_port_reads_the_runtime_variable(raw: str | None, expected: int) -> None:
    """T-APP-PORT. ``DATABRICKS_APP_PORT``, resolved in Python rather than by shell expansion.

    Whether the Apps runtime expands ``${VAR}`` inside ``app.yaml``'s ``command`` is not
    verified by this repo, so the resolution happens where the semantics are testable.
    """
    environ = {} if raw is None else {"DATABRICKS_APP_PORT": raw}
    assert resolve_port(environ) == expected


@pytest.mark.parametrize("raw", ["eight thousand", "8000.5", "0", "-1", "65536", "99999"])
def test_resolve_port_refuses_a_value_it_cannot_serve(raw: str) -> None:
    """T-APP-PORT. A malformed port raises instead of falling back.

    An operator who set the variable believes the port moved. Serving on 8000 while they read
    9000 presents as the edge being broken, which is the most expensive way to learn about a
    typo.
    """
    with pytest.raises(ValueError, match="DATABRICKS_APP_PORT"):
        resolve_port({"DATABRICKS_APP_PORT": raw})


# --------------------------------------------------------------------------------------
# The app factory
# --------------------------------------------------------------------------------------


def test_build_app_exposes_the_health_route_and_both_socket_routes() -> None:
    paths = {getattr(route, "path", None) for route in build_app().routes}
    assert {"/", "/ready", "/publish/{session_id}", "/subscribe/{session_id}"} <= paths


def test_two_apps_in_one_interpreter_share_no_relay() -> None:
    """The reason ``build_app`` is a factory: one test's attachment must not reach another's.

    ``shellbox_mcp.server.build_server`` is the precedent, and
    `tests/integration/test_no_session_state.py` is the test that exists because module-level
    state passes a process-level check and fails this one.
    """
    assert build_app().state.relay is not build_app().state.relay


# --------------------------------------------------------------------------------------
# The entrypoint: logging, and the WebSocket keepalive
# --------------------------------------------------------------------------------------


def _run_main(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """Run ``main`` with ``uvicorn.run`` replaced, and return the keyword arguments it got.

    ``main`` imports uvicorn inside the function and calls the module attribute, so patching
    the attribute is enough and no server is started.
    """
    import uvicorn

    seen: dict[str, object] = {}

    def fake_run(application: object, **kwargs: object) -> None:
        seen.update(kwargs)
        seen["app"] = application

    monkeypatch.setattr(uvicorn, "run", fake_run)
    main()
    return seen


def test_main_passes_both_websocket_ping_settings_explicitly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Principle 5, applied to the two values the browser's retry bound is derived from.

    This app has no reaper of its own: uvicorn's failed ping is what releases a silently-dead
    subscriber's slot. A bound derived from a library default nobody wrote down moves on a
    dependency bump, and the browser then reports a conflict that was about to clear.
    """
    root = logging.getLogger()
    before = list(root.handlers)
    try:
        seen = _run_main(monkeypatch)
    finally:
        for handler in list(root.handlers):
            if handler not in before:
                root.removeHandler(handler)

    assert seen["ws_ping_interval"] == WS_PING_INTERVAL_SECONDS
    assert seen["ws_ping_timeout"] == WS_PING_TIMEOUT_SECONDS


def test_main_configures_logging_before_it_serves(monkeypatch: pytest.MonkeyPatch) -> None:
    """The prober's only notification is a WARN line, and this is what gives it a destination.

    Measured against the live `dev` deploy on 2026-08-03: with no handler on the root logger,
    ``databricks apps logs`` carried neither the App's INFO lines nor its WARNINGs. See
    `packages/shellbox-app/src/shellbox_app/logs.py`.
    """
    root = logging.getLogger()
    before = list(root.handlers)
    try:
        _run_main(monkeypatch)
        added = [handler for handler in root.handlers if handler not in before]
        assert added, "main served without configuring logging"
        assert root.level <= logging.INFO, f"root logger is at {root.level}, which drops INFO"
    finally:
        for handler in list(root.handlers):
            if handler not in before:
                root.removeHandler(handler)


def test_uvicorns_own_ping_defaults_have_not_drifted_from_the_recorded_values() -> None:
    """A drift detector, and the reason the constants above can be called MEASURED.

    Read from the installed uvicorn rather than hardcoded a second time, the same habit
    `tests/unit/test_lakebase.py` uses for SQLAlchemy's ``pool_timeout`` default. The App
    passes these values explicitly, so a drift changes nothing about how it runs -- it changes
    whether the comment claiming "these are uvicorn's defaults" is still true.

    A deliberate change to the App's own values makes this fail, which is correct: read the
    WARNING on the constants first. Shortening them reaches into the sandbox's autostop timer.
    """
    import uvicorn

    signature = inspect.signature(uvicorn.run)
    assert signature.parameters["ws_ping_interval"].default == WS_PING_INTERVAL_SECONDS
    assert signature.parameters["ws_ping_timeout"].default == WS_PING_TIMEOUT_SECONDS
