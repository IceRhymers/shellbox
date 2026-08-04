"""The accept path: bind one publisher and at most one subscriber to a ``session_id``.

The sandbox dials out and publishes; a viewer subscribes; this module relays between them. It
is the server end of the socket, and it is deliberately the least informed component in the
transport.

## What this server does NOT do, and why each absence is load-bearing

* **It does not parse frames on the data path.** Publisher bytes reach the subscriber opaquely.
  Only the frames this server *originates* are encoded here. Sequencing, epochs, the ring
  buffer, and the resume guarantee all live with the publisher that holds the PTY, and keeping
  the server ignorant of them is what stops a second, divergent implementation appearing here.
* **It buffers nothing.** There is no server-side ring and no frame log. Resume is
  live-stream-only, decision D7 of the epic (https://github.com/IceRhymers/shellbox/issues/9),
  and a durable frame log has already been rejected once by review -- see the note in
  `packages/shellbox-registry/src/shellbox_registry/models.py`. Frames arriving with no
  subscriber attached are dropped. That is correct rather than lossy: the subscriber's repaint
  comes from the publisher's ``capture-pane`` resync, not from anything kept here.
* **It does not dedupe publishers.** A second publisher for a live ``session_id`` is an ERROR.
  One publisher per session is arbitrated **host-side**, through a claim held in a tmux user
  option, because tmux is the declared session authority -- so the claim needs no new store and
  survives an MCP restart for free. Two live publishers on one session would produce a repaint
  loop alternating between two valid epochs, which is an undeclared discontinuity arriving twice
  a second. Refusing the second socket here is a backstop that reports the conflict; it is not
  the mechanism that prevents it.
* **It does not tell the subscriber that a publisher died.** It does not have to. A reconnecting
  or restarted publisher mints a new epoch, and an unfamiliar epoch already means "distrust
  everything, repaint" to a subscriber. Adding a second, server-originated signal for the same
  event would give a subscriber two sources of truth for one fact.
* **It serves one subscriber per session, and that is a decision rather than a stub.** The
  reason is backpressure: a slow subscriber applies it to the publisher through an ``await``
  in ``_pump``, which stalls the pane's reader. That is acceptable at one subscriber and not
  at several. Fixing it needs bounded per-subscriber queues plus a drop policy, and a drop
  policy on a terminal stream means a subscriber that falls behind must be RESYNCED -- which
  needs a resync request path from the App to the publisher that no version of this protocol
  has. Fan-out is therefore a protocol addition, not a loop change, and no phase of this
  project has scheduled it. A refusal is transient rather than terminal for the session: see
  ``WS_PING_INTERVAL_SECONDS``, whose sum with the timeout bounds how long a silently-dead
  subscriber holds the slot.
* **It holds no capability over any sandbox.** See this package's ``__init__.py``. The header
  ``X-Forwarded-Email`` is identity DISPLAY only, never authorization.

## Publisher reconnect versus a second publisher -- the distinction the accept path turns on

These arrive looking identical: a WebSocket for a ``session_id`` that is already bound. The
edge kills every open socket every 10 to 18 minutes on a wall-clock event, measured by the
Phase 1 probe and recorded in `probe/FINDINGS.md`, so reconnect is the **hot path** and must not
be reported as a conflict.

The rule is one live socket, not one connection ever:

* A live publisher socket is registered -> the new one is a **conflict**, refused.
* No live publisher socket -> the new one **rebinds** to the existing attachment, keeping the
  subscriber attached.

An attachment therefore outlives its publisher's socket, and is dropped only when neither a
publisher nor a subscriber remains. A subscriber that survived the kill keeps its socket and
sees the reconnected publisher's new epoch, which is exactly the declared discontinuity the
resume guarantee promises.

CRITICAL: **The check and the registration must not be separated by an ``await``.** Both
handlers accept the socket first, then decide, with no suspension point between reading the
registry and writing to it. asyncio interleaves only at ``await``, so that ordering is what
makes "is a live publisher registered?" a decision and not a race. Inserting an ``await``
between them would let two simultaneous publishers both pass the check.

## Why a refusal is a frame, not a rejected handshake and not a close code

A refused socket is accepted at the 101, sent a ``control`` frame naming the reason, and then
closed. Both alternatives lose information the client needs, for different reasons.

**Not a rejected handshake.** The Apps edge answers an unauthenticated upgrade with a 302,
measured by the probe. Refusing before the 101 puts a conflict in the same bucket as an
authentication failure, which the publisher treats as terminal. A conflict is not terminal: the
session becomes claimable as soon as the other holder goes away.

**Not a close code and reason alone**, which is the tempting version because it needs no
protocol vocabulary. A close after a successful accept *is* distinguishable from a 302 -- the
302 fails the handshake and never produces a close at all -- so that objection does not apply.
The one that does is reach: the edge kills every open socket **with no close frame**, measured,
so "closed carrying no reason" is already the signature of a routine edge kill. A refusal
reported that way would be read as one. Frames, by contrast, are the path the probe measured
end to end as bidirectional and in order, so a reason carried as a frame arrives by a mechanism
this repo has evidence for, and a reason carried in a close frame does not.

## Health checks report counts, never identifiers

``GET /`` returns how many sessions are bound, not which. Any workspace user the edge lets
through reaches this route, and a session name is not theirs to enumerate.

``GET /`` also touches NO database, and that is a rule rather than an accident of what it
happens to report. It is the target of the deploy's own smoke step, so a database call here
would put a Lakebase wake on the path of the one check that must answer when Lakebase is the
thing that is broken. The inventory routes are separate, and a reader adding a row count to
this payload should add a route instead.

``GET /ready`` is the peer that DOES read the database, and it is a separate route for exactly
that reason. See `packages/shellbox-app/src/shellbox_app/ready.py`, which also owns the
30-minute prober this app starts in its lifespan handler.

``GET /api/hosts`` and ``GET /api/sessions`` DO return identifiers, and that is not a
contradiction. They are the product rather than a liveness check: under decision D6 the App is
open to every workspace user, so the host list is not a per-viewer secret. See
`packages/shellbox-app/src/shellbox_app/inventory.py`, which states all five rules those two
routes obey.

## The database, and the two rules that keep it away from the relay

The registry lives on ``app.state.database``, opened once per app by
``shellbox_app.database.open_registry``. Two rules govern every route built on it, and both
exist because the event loop of this process is what relays every attached terminal:

1. **Every database-touching route is a sync ``def``, never ``async def``.** The rule covers
   every route under ``/api/`` and ``/ready``, and ``tests/unit/test_app_database.py`` asserts
   it against that list. ``PostgresRegistry`` is synchronous SQLAlchemy. FastAPI runs a sync
   ``def`` in a threadpool and an ``async def`` on the event loop, so one blocking query in a
   coroutine route stalls every attached terminal at once. The WebSocket routes below are
   correctly ``async def``, which is a different case: they suspend on socket I/O and never
   touch the database.
2. **Opening the registry is never fatal.** See the module docstring of
   `packages/shellbox-app/src/shellbox_app/database.py`. A Lakebase outage means the inventory
   goes stale. It never means a browser cannot attach, and
   `tests/unit/test_app_database.py` asserts that with a registry that raises on every call.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Annotated

from fastapi import FastAPI, Header, WebSocket, WebSocketDisconnect
from shellbox_transport.codec import (
    UNORDERED_SEQ,
    ControlMessage,
    control_frame,
    encode_frame,
    error_message,
    hello_message,
)
from starlette.websockets import WebSocketState

from shellbox_app.database import AppDatabase, open_registry
from shellbox_app.inventory import hosts_payload, sessions_payload
from shellbox_app.logs import configure_logging
from shellbox_app.ready import probe_forever, ready_payload
from shellbox_app.ui import UI_PATH, mount_ui

logger = logging.getLogger(__name__)

__all__ = [
    "CONTROL_SEQ",
    "DEFAULT_PORT",
    "REFUSED_CLOSE_CODE",
    "WS_PING_INTERVAL_SECONDS",
    "WS_PING_TIMEOUT_SECONDS",
    "Attachment",
    "Relay",
    "app",
    "build_app",
    "health_payload",
    "main",
    "resolve_port",
    "serve_publisher",
    "serve_subscriber",
]

# The port the Phase 1 probe measured the edge proxying to, used when `DATABRICKS_APP_PORT` is
# absent. A measured value rather than a conventional default -- see `__main__.py`.
DEFAULT_PORT = 8000

# `seq` on every frame this server originates. See `_control_bytes`.
#
# Aliased from the codec rather than spelled again here: the subscriber originates frames under
# the same rule, and two independent 0s would be two things to keep in step. The name is kept
# because this module's tests and docstrings refer to it.
CONTROL_SEQ = UNORDERED_SEQ

# The close code for a refusal. 1008 is "policy violation", which is what a conflict is: the
# socket was well-formed and the server declines to serve it. The reason a client branches on
# is the `control` frame sent before the close, never this number.
REFUSED_CLOSE_CODE = 1008

# The WebSocket keepalive, passed to uvicorn explicitly rather than inherited.
#
# WHAT THIS CONTROLS, and it is not the keepalive itself. This app has NO reaper of its own.
# When a subscriber's socket dies silently, `_pump`'s `await source.receive()` never returns,
# so `Relay.release` never runs and the session's one subscriber slot stays held. What frees
# the slot is uvicorn failing its own ping: it sends one every `ws_ping_interval` and closes
# the connection when no pong arrives within `ws_ping_timeout`. So the sum below is the
# worst-case time a dead subscriber holds a slot, and a browser retrying `subscriber_conflict`
# has to outlast it or it reports a conflict that was about to clear itself.
#
# MEASURED, not inferred. Read out of the installed uvicorn 0.51.0 on 2026-08-03:
# `uvicorn.config.Config.__init__` and `uvicorn.run` both declare `ws_ping_interval=20.0` and
# `ws_ping_timeout=20.0`, as floats. The values below restate those defaults, so passing them
# changes nothing about how the App runs today. That is the point: principle 5 says a value the
# design depends on is declared, and a browser retry bound derived from a library default that
# nobody wrote down is a bound that silently moves on a dependency bump.
# `tests/unit/test_app_server.py` asserts both that `main` passes them and that uvicorn's own
# defaults have not drifted away from them.
#
# WARNING: **these apply to the PUBLISHER socket too, not only the browser's.** At the values
# below that is a no-op versus today. But SHORTENING them to tighten the reaper window makes
# the App ping the sandbox more often, and
# `packages/shellbox-mcp/src/shellbox_mcp/transport.py` flags exactly that traffic as an input
# to the sandbox's autostop timer, which is Phase 5's variable. Shortening this is therefore
# not a local decision. Lengthening it is safe here and must move the browser's retry bound.
WS_PING_INTERVAL_SECONDS = 20.0
WS_PING_TIMEOUT_SECONDS = 20.0


@dataclass
class Attachment:
    """One ``session_id``'s live sockets. Outlives the publisher's socket, by design.

    ``publisher`` is ``None`` between a socket dying and the publisher re-dialling, which is a
    normal state that recurs every 10 to 18 minutes. It is not an error state and nothing here
    treats it as one.
    """

    session_id: str
    publisher: WebSocket | None = None
    subscriber: WebSocket | None = None
    # Display identity only, from `X-Forwarded-Email` on the publisher's upgrade request. Kept
    # so a subscriber's `hello` can name whose session it is reading. Never consulted for a
    # permission decision -- see this package's `__init__.py`.
    identity: str | None = None

    @property
    def deletable(self) -> bool:
        """True when neither side remains, so the entry can be dropped."""
        return self.publisher is None and self.subscriber is None


@dataclass
class Relay:
    """The bound sessions. One instance per app, never a module global.

    Per app rather than module-level so two apps in one interpreter share nothing. The
    integration lane builds a fresh app per test, and a module-level registry would let one
    test's leftover attachment decide another test's outcome -- the same reason
    `tests/conftest.py` gives a private tmux server to every test rather than to every module.
    """

    attachments: dict[str, Attachment] = field(default_factory=dict)

    def bind_publisher(
        self, session_id: str, socket: WebSocket, identity: str | None
    ) -> Attachment | None:
        """Register ``socket`` as the publisher, or return ``None`` if one is already live.

        CRITICAL: Contains no ``await``. See this module's docstring -- the absence of a
        suspension point between the check and the write is what makes this atomic.
        """
        existing = self.attachments.get(session_id)
        if existing is None:
            attachment = Attachment(session_id=session_id, publisher=socket, identity=identity)
            self.attachments[session_id] = attachment
            return attachment
        if existing.publisher is not None:
            return None
        # A rebind after the socket died. The subscriber, if any, stays attached.
        existing.publisher = socket
        existing.identity = identity
        return existing

    def bind_subscriber(self, session_id: str, socket: WebSocket) -> Attachment | None:
        """Register ``socket`` as the sole subscriber, or return ``None`` if one is already live.

        A subscriber may attach before a publisher exists. That is deliberate: a viewer opening
        a tab for a session whose publisher is mid-reconnect would otherwise be refused for a
        condition that resolves itself in under a second.

        CRITICAL: Contains no ``await``, for the reason ``bind_publisher`` gives.
        """
        existing = self.attachments.get(session_id)
        if existing is None:
            attachment = Attachment(session_id=session_id, subscriber=socket)
            self.attachments[session_id] = attachment
            return attachment
        if existing.subscriber is not None:
            return None
        existing.subscriber = socket
        return existing

    def release(self, session_id: str, socket: WebSocket) -> None:
        """Detach ``socket`` from whichever side holds it, and drop the entry when both are gone.

        Takes the socket rather than a role so a stale handler cannot detach its successor. A
        publisher whose handler is unwinding after a rebind already happened would otherwise
        clear the *new* publisher's registration.
        """
        attachment = self.attachments.get(session_id)
        if attachment is None:
            return
        if attachment.publisher is socket:
            attachment.publisher = None
        if attachment.subscriber is socket:
            attachment.subscriber = None
        if attachment.deletable:
            del self.attachments[session_id]


def resolve_port(environ: dict[str, str] | None = None) -> int:
    """``DATABRICKS_APP_PORT``, or ``DEFAULT_PORT``.

    A malformed value raises rather than falling back. An operator who set the variable believes
    the port moved, and serving on 8000 while they read 9000 is the kind of disagreement that
    presents as the edge being broken.
    """
    env = os.environ if environ is None else environ
    raw = (env.get("DATABRICKS_APP_PORT") or "").strip()
    if not raw:
        return DEFAULT_PORT
    try:
        port = int(raw)
    except ValueError as exc:
        raise ValueError(f"DATABRICKS_APP_PORT={raw!r} is not an integer") from exc
    if not 1 <= port <= 65535:
        raise ValueError(f"DATABRICKS_APP_PORT={raw!r} is not a usable port")
    return port


def health_payload(relay: Relay) -> dict[str, object]:
    """What ``GET /`` returns. Counts only, never identifiers -- see this module's docstring.

    ``ui`` is a constant, not a count, and it is here because this route is what a human reaches
    by opening the App's URL. The renderer cannot live at ``/`` -- this payload is the deploy's
    smoke target and must stay JSON -- so the alternative to naming the path is a person seeing
    this object and having nowhere to go. See `packages/shellbox-app/src/shellbox_app/ui.py`.
    """
    return {
        "service": "shellbox-app",
        "ui": f"{UI_PATH}/",
        "sessions": len(relay.attachments),
        "publishers": sum(1 for held in relay.attachments.values() if held.publisher is not None),
        "subscribers": sum(1 for held in relay.attachments.values() if held.subscriber is not None),
    }


async def serve_publisher(relay: Relay, websocket: WebSocket, session_id: str) -> None:
    """Accept a publisher, bind it, send ``hello``, then relay its frames to the subscriber."""
    await websocket.accept()
    identity = websocket.headers.get("x-forwarded-email")
    attachment = relay.bind_publisher(session_id, websocket, identity)
    if attachment is None:
        logger.warning("refused a second publisher for session %s: one is already live", session_id)
        await _refuse(
            websocket,
            session_id,
            code="publisher_conflict",
            message=(
                "a live publisher is already bound to this session; one publisher per session "
                "is arbitrated host-side through the tmux claim"
            ),
        )
        return
    logger.info("bound publisher for session %s (identity %s)", session_id, identity or "none")
    try:
        await _send_hello(websocket, attachment)
        await _pump(websocket, lambda: attachment.subscriber, "subscriber")
    finally:
        relay.release(session_id, websocket)
        logger.info("released publisher for session %s", session_id)


async def serve_subscriber(relay: Relay, websocket: WebSocket, session_id: str) -> None:
    """Accept the sole subscriber, send ``hello``, then relay its input to the publisher."""
    await websocket.accept()
    attachment = relay.bind_subscriber(session_id, websocket)
    if attachment is None:
        # CORRECTED. This line said "fan-out is Phase 4's", and Phase 4 decided the opposite:
        # fan-out is a protocol addition and is out of its scope. See `_refuse` below, and
        # `_pump`'s WARNING, which carries the reason.
        logger.warning(
            "refused a second subscriber for session %s: one subscriber per session, by design",
            session_id,
        )
        await _refuse(
            websocket,
            session_id,
            code="subscriber_conflict",
            message=(
                "a subscriber is already bound to this session; one subscriber per session "
                "is a design decision, so retry and expect the slot to clear"
            ),
        )
        return
    logger.info("bound subscriber for session %s", session_id)
    try:
        await _send_hello(websocket, attachment)
        await _pump(websocket, lambda: attachment.publisher, "publisher")
    finally:
        relay.release(session_id, websocket)
        logger.info("released subscriber for session %s", session_id)


def build_app(relay: Relay | None = None, database: AppDatabase | None = None) -> FastAPI:
    """A FastAPI app over its own ``Relay`` and its own registry.

    A factory rather than a module-level app over module-level state, mirroring
    ``shellbox_mcp.server.build_server``. Two apps in one interpreter then share nothing, so the
    integration lane can build one per test.

    The routes are thin: each delegates to the module-level ``serve_*`` function above. That
    split is what lets the unit lane drive the accept path with a fake socket instead of reaching
    into ``FastAPI``'s route table for a closure -- the behavior under test is the binding rule,
    and it should not need a running server to assert.

    ``database`` defaults to whatever the environment resolves to, which is a `NullRegistry`
    when nothing is configured. Passing one is how a test supplies a fake minter, or a registry
    that raises on every call. Building the app opens the registry and starts NO thread: the
    refresher belongs to the lifespan handler below, because this function runs at import.
    """
    sessions = Relay() if relay is None else relay
    store = open_registry() if database is None else database

    @asynccontextmanager
    async def lifespan(api: FastAPI) -> AsyncIterator[None]:
        """Start the credential refresher and the prober, serve, then stop both.

        The App is long-lived and is the case the background refresher exists for -- see the
        module docstring of `packages/shellbox-app/src/shellbox_app/database.py`. ``stop()`` is
        in the unwind path so a reload or a failed startup elsewhere still joins the thread.

        The prober is a task rather than a thread, and it is started here rather than in
        `build_app` for the reason the refresher is: this function runs at import, and a module
        a test suite imports must not start background work. See
        `packages/shellbox-app/src/shellbox_app/ready.py` for what it does and why it runs
        inside the App at all.
        """
        store.start()
        # Kept on `state` rather than in a local, so a test has a handle on it and an operator
        # reading this file can see that the App owns exactly one background task.
        api.state.prober = prober = asyncio.create_task(probe_forever(store))
        try:
            yield
        finally:
            # Cancel and await, in that order. Dropping the reference instead leaves the task
            # to be cancelled at loop teardown, which surfaces as an unretrieved exception
            # after the App has otherwise shut down cleanly.
            prober.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await prober
            store.stop()

    api = FastAPI(lifespan=lifespan)
    api.state.relay = sessions
    api.state.database = store

    @api.get("/")
    def health() -> dict[str, object]:
        """CRITICAL: zero database. See this module's docstring -- the rule, not the payload."""
        return health_payload(sessions)

    @api.get("/ready")
    def ready() -> dict[str, object]:
        """The database diagnostic. CRITICAL: a sync ``def``, and the body is generic.

        Both rules, and the reason this route is separate from ``GET /`` at all, are in
        `packages/shellbox-app/src/shellbox_app/ready.py`.
        """
        return ready_payload(store)

    @api.get("/api/hosts")
    def api_hosts(
        x_forwarded_email: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        """The host inventory. CRITICAL: a sync ``def``, and the header is DISPLAY only.

        The header binds through FastAPI rather than being read off a ``Request``, so the one
        name this route trusts is visible in its signature. Every rule it obeys is in
        `packages/shellbox-app/src/shellbox_app/inventory.py`.

        CRITICAL: the header selects NO rows. It decides only which rows carry ``mine``.
        """
        return hosts_payload(store, x_forwarded_email)

    @api.get("/api/sessions")
    def api_sessions(
        x_forwarded_email: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        """The session inventory. The same two rules as ``/api/hosts``, unchanged."""
        return sessions_payload(store, x_forwarded_email)

    @api.websocket("/publish/{session_id}")
    async def publish(websocket: WebSocket, session_id: str) -> None:
        await serve_publisher(sessions, websocket, session_id)

    @api.websocket("/subscribe/{session_id}")
    async def subscribe(websocket: WebSocket, session_id: str) -> None:
        await serve_subscriber(sessions, websocket, session_id)

    # LAST, and after every route above. A mount matches by prefix, so one registered earlier
    # would shadow anything sharing it. Nothing shares `/ui` today; the ordering is what keeps
    # that true when someone adds a route later. See
    # `packages/shellbox-app/src/shellbox_app/ui.py` for why the page is not at `/`.
    mount_ui(api)

    return api


async def _send_hello(websocket: WebSocket, attachment: Attachment) -> None:
    """Send the ``control`` frame that gates "connected" for the client.

    A 101 alone is not proof the transport is up. The Apps edge answers an unauthenticated
    upgrade with a 302 and an unauthenticated POST with an HTML login page under a 200 status,
    both measured by the Phase 1 probe and recorded in `probe/FINDINGS.md`. So the client waits
    for this frame under a deadline and treats a silent socket as not connected -- a rule that
    exists because a publisher that misreads an authentication failure as a network blip
    reconnect-loops forever behind a blank terminal.

    The frame names the ``session_id`` the server bound. The client compares it against the id
    it dialled and errors on a mismatch, so this echo is what makes that check possible.

    The same message goes to both roles, and it carries no role field. Which role a socket holds
    is decided by the route it dialled, so the client already knows -- and a field restating it
    would be a second place for the two to disagree.
    """
    await websocket.send_bytes(
        _control_bytes(
            attachment.session_id,
            # `viewer_email` is display only, and absent rather than null when the edge injected
            # no header. Loopback has no edge, so the absent case is what the integration lane
            # exercises.
            hello_message(attachment.session_id, viewer_email=attachment.identity),
        )
    )


async def _refuse(websocket: WebSocket, session_id: str, *, code: str, message: str) -> None:
    """Name the reason in a ``control`` frame, then close. See this module's docstring.

    The closed set of codes is ``publisher_conflict`` and ``subscriber_conflict``. Both are
    terminal for the socket they refuse and **neither is an authentication failure** -- a client
    that folded them into that classification would stop retrying a session it will be able to
    claim as soon as the other holder goes away.
    """
    try:
        await websocket.send_bytes(
            _control_bytes(session_id, error_message(code, message, session_id=session_id))
        )
        await websocket.close(code=REFUSED_CLOSE_CODE)
    except (WebSocketDisconnect, RuntimeError):
        # The refused client hung up first. Nothing to report: the refusal already happened and
        # the caller has logged it.
        return


def _control_bytes(session_id: str, message: ControlMessage) -> bytes:
    """One encoded ``control``-stream frame carrying ``message``.

    ``seq`` is ``CONTROL_SEQ`` on every frame this server originates, and that is a different
    rule from the one ``codec.control_frame`` documents. A publisher's control frames take a
    ``seq`` from the same allocator as its data frames, so a renderer can order "the stream
    restarted" against the bytes around it. This server allocates nothing: it does not own the
    session's sequence space and must never appear to hold a position in it, or a subscriber
    would read a server frame as a data-frame ordinal and infer a gap that does not exist.

    The frames this affects are ``hello`` and a refusal, both of which precede any data frame on
    the socket, so there is no surrounding output for them to be ordered against.
    """
    return encode_frame(
        control_frame(session_id, CONTROL_SEQ, time.time(), message),
    )


async def _pump(source: WebSocket, peer: Callable[[], WebSocket | None], peer_role: str) -> None:
    """Forward every binary message from ``source`` to whichever socket ``peer`` returns.

    ``peer`` is a callable, not a socket, and that is the point: a publisher's socket is replaced
    every 10 to 18 minutes when the edge kills it, so a subscriber holding a captured reference
    would forward input into a dead socket after the very first reconnect. The lookup happens per
    message.

    Two failures are survived rather than propagated:

    * **No peer attached.** The message is dropped. Nothing buffers here -- see the module
      docstring.
    * **The peer's socket is dead.** The send fails, this loop continues, and the peer's own
      handler unwinds and detaches it. A viewer closing a browser tab must not tear down an
      agent's publisher.

    WARNING: A slow subscriber applies backpressure to the publisher through this ``await``,
    which would stall the pane's reader. Acceptable at one subscriber and not at several, and
    it is the reason ``serve_subscriber`` refuses the second one. Whoever implements fan-out
    owns bounding it, plus the resync path this module's docstring names.
    """
    while True:
        message = await source.receive()
        if message["type"] == "websocket.disconnect":
            return
        data = message.get("bytes")
        if data is None:
            # The frame protocol is binary. A text message is a protocol violation, and it is
            # logged and dropped rather than relayed: forwarding it would make the peer's
            # decoder fail on a message this server could have named.
            logger.warning("dropped a non-binary message from a %s peer", peer_role)
            continue
        target = peer()
        if target is None:
            continue
        if target.client_state is not WebSocketState.CONNECTED:
            continue
        try:
            await target.send_bytes(data)
        except (WebSocketDisconnect, RuntimeError) as exc:
            logger.info("could not reach the %s socket, dropping a frame: %s", peer_role, exc)


# The deployed app. `app.yaml` runs `python -m shellbox_app`, which serves this object.
app = build_app()


def main() -> None:
    """Serve ``app`` on the port the Apps runtime chose. The ``app.yaml`` entrypoint."""
    import uvicorn

    # FIRST, and before anything that might log. The App configured no logging until this call
    # existed, which dropped every INFO line and left every WARNING to the standard library's
    # last-resort handler -- measured against the live `dev` deploy. The prober's only
    # notification mechanism is a WARN line, so it does not work without this. See
    # `packages/shellbox-app/src/shellbox_app/logs.py`.
    configure_logging()

    # 0.0.0.0 because the edge terminates outside the container and proxies in -- the probe
    # measured the App seeing `host: localhost:8000`, so binding the loopback alone would be
    # reachable, but the runtime does not promise that and the probe's shape is what is measured.
    #
    # The two ping settings are uvicorn's own current defaults, passed rather than inherited.
    # They are what reaps a silently-dead subscriber's slot, and the browser's retry bound is
    # derived from their sum. See the constants above, including the WARNING about the
    # publisher socket.
    uvicorn.run(
        app,
        host="0.0.0.0",  # noqa: S104 -- see above
        port=resolve_port(),
        ws_ping_interval=WS_PING_INTERVAL_SECONDS,
        ws_ping_timeout=WS_PING_TIMEOUT_SECONDS,
    )
