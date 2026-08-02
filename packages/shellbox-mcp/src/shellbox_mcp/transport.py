"""``WSTransport`` -- the sandbox-side WebSocket client: dial, liveness, reconnect, auth.

The direction is forced, not chosen: a Databricks App cannot open a TCP connection into a
sandbox, so the sandbox dials out and the App accepts. This module is that dial, and it lives
in ``shellbox-mcp`` rather than in ``shellbox-transport`` because it performs I/O -- the pure
protocol package holds ``Frame``, the codec, and the ordinals, and nothing that opens a socket.

**Reconnect is not the error path here. It is the steady state.** Six constraints below are
MEASURED against a real deployed App from inside a real sandbox (``probe/FINDINGS.md``), and
each one rules out an implementation that would otherwise look correct:

1. **The kill is a global wall-clock edge event, roughly every 10-18 minutes, with no fixed
   period.** Three holds opened 3 minutes apart died in the same second. The event is at the
   edge, so it does not respect sandbox boundaries: every publisher talking to one App loses
   its socket simultaneously. A fixed backoff therefore keeps them synchronized, because the
   input event was itself synchronized. Hence full jitter -- ``uniform(floor, cap)``, re-drawn
   per attempt.
2. **Never assume a minimum lifetime.** A socket can die seconds after opening, so a
   zero-delay retry can hot-loop straight into an imminent kill. Hence a NONZERO floor.
3. **Keepalives do not extend a socket's life.** A 5 s ping interval died in the same second
   as a 20 s one. So this module sends no application-level keepalive, and
   ``tests/unit/test_no_keepalive.py`` asserts that structurally. Read that as a statement
   about *survival*, not about detection -- see ``_DETECTOR_NOTE`` below, which is the
   distinction someone will otherwise collapse into ``ping_interval=None``.
4. **Teardown sends no close frame.** The measured signature is
   ``ConnectionClosedError: no close frame received or sent``. There is nothing to await, so
   detection is entirely client-side.
5. **Session state does not live in the socket**, because all sockets die together. This class
   holds no frames and no session state beyond its own configuration. The publisher's ring
   lives with the publisher.
6. **Validate response CONTENT, not status.** An unauthenticated POST returned HTTP 200 with
   an HTML login body. A status-only check reads an auth failure as success, and the publisher
   then streams into a void and reports success. ``classify_failure`` is where this is
   enforced, and ``tests/unit/test_auth_content.py`` is the assertion.

WARNING: **A 101 is not proof of a working transport.** The probe measured a 302 on an
unauthenticated upgrade, and the edge can complete an upgrade to something that is not the
App. So "connected" is gated on a ``hello`` control frame naming the bound ``session_id``,
within a bounded deadline. A ``hello`` naming a DIFFERENT ``session_id`` is an error and not a
warning: it means two agents' streams could cross.

This module never fails a tool call. It raises nothing onto the tool path; a transport that
cannot connect is a publisher that does not publish, and ``shell_create`` still succeeds
(ADR-3). Credential minting is INJECTED as a callable, so nothing here imports the Databricks
SDK and the credential chain stays in one place.
"""

from __future__ import annotations

import asyncio
import logging
import random
import urllib.parse
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from enum import Enum

import websockets
from shellbox_transport import Frame
from shellbox_transport.codec import (
    CONTROL_HELLO,
    FIELD_SESSION_ID,
    FIELD_VIEWER_EMAIL,
    CodecError,
    decode_control,
    decode_frame,
    encode_frame,
)
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidMessage, InvalidStatus, InvalidURI

logger = logging.getLogger(__name__)

__all__ = [
    "Connected",
    "Failure",
    "Hello",
    "TransportTerminal",
    "WSTransport",
    "WSTransportConfig",
    "classify_failure",
]

# MEASURED in this repo's environment, not read from the documentation:
# `websockets` 15.0.1 defaults are `ping_interval=20`, `ping_timeout=20`, `open_timeout=10`,
# `close_timeout=10`, `max_size=1048576`. The sandbox image ships 14.2 (probe findings), so the
# dependency range spans both and every value this module depends on is passed EXPLICITLY. A
# default that changes between those two versions must not change this transport's behavior.
_DEFAULT_PING_INTERVAL = 20.0
_DEFAULT_PING_TIMEOUT = 20.0

# The detector decision, which the plan left to this work item to make and to state.
#
# **The detector is `websockets`' own keepalive timeout, not a bespoke timer.** Two reasons,
# and the second is measured here rather than assumed:
#
#   * A dropped TCP connection surfaces immediately as `ConnectionClosedError` with the exact
#     "no close frame received or sent" text the probe recorded. That is the common case and it
#     is sub-second, which is what the publisher's ring is sized to cover.
#   * `ping_interval`/`ping_timeout` are the backstop for a SILENT death -- a route that
#     disappears with no FIN -- and they raise the same `ConnectionClosedError`. One `except`
#     branch therefore covers both, and a bespoke timer would be a second implementation of a
#     detector the library already has.
#
# Constraint 3 says keepalives do not help. That is a statement about SURVIVAL: pings do not
# stop the edge from killing the socket. It is not an argument for `ping_interval=None`, which
# would delete the only detector for a silent death. The two values are passed explicitly so
# this is a decision on the record rather than a library default nobody chose.
#
# The cost, stated because the live run has to weigh it: a silent death is detected after
# `ping_interval + ping_timeout` at worst, which is far longer than a 1 MiB ring covers, so
# that case always resyncs. Shortening both would narrow the gap and add traffic that reads as
# activity to the sandbox's autostop input, which is Phase 5's variable, not this module's.
_DETECTOR_NOTE = "websockets ping_timeout, with the TCP close as the primary signal"


class Failure(Enum):
    """How a dial or a live socket failed. A closed set, because the loop branches on it.

    ``AUTH_FAILED`` is not simply "the server said no". It is the classification the probe's
    constraint 6 forces: a 200 with an HTML body, and a redirect to a login page, are auth
    failures wearing a success or a routing status. Treating either as transient is `R26` --
    an endless reconnect loop that logs like network trouble while the browser shows a blank
    terminal.
    """

    TRANSIENT = "transient"
    AUTH_FAILED = "auth_failed"
    PROTOCOL = "protocol"
    CONFIG = "config"


class TransportTerminal(Exception):
    """The reconnect loop gave up, and retrying cannot fix it.

    Carries the ``Failure`` that ended it so a caller can log a remedy. It is NOT a
    ``ShellboxError``: a transport failure must never become a tool payload, because a
    publisher that cannot connect does not make ``shell_create`` fail (ADR-3, R7).
    """

    def __init__(self, failure: Failure, message: str) -> None:
        super().__init__(message)
        self.failure = failure


@dataclass(frozen=True, slots=True)
class Hello:
    """The server's handshake, decoded. What gates "connected"."""

    session_id: str
    epoch: str | None
    """The epoch the server echoed, or ``None`` when it echoed none.

    ``None`` is the normal Phase 3 value, not a degraded one: the App server holds no attach and
    says so. This publisher's own ``config.epoch`` governs its frames either way, so nothing
    here reads this field to make a decision -- it exists so a log line on both sides can name
    the same attach when the server is one that does echo."""
    viewer_email: str | None


@dataclass(frozen=True, slots=True)
class Connected:
    """One live socket, after ``hello``. Valid until it closes; never stored across a dial."""

    connection: ClientConnection
    hello: Hello
    attempt: int
    """How many dials it took to get here. 1 on a first-try success. Logged so the live run can
    report the reconnect count without instrumenting the loop again."""


@dataclass(frozen=True)
class WSTransportConfig:
    """One publisher's transport configuration. Immutable, and never re-read per dial.

    ``url`` comes from ``$SHELLBOX_APP_URL`` through ``config.py``, which is the one place the
    environment is read. Everything here is a constructor argument so tests drive the loop
    without waiting on real clocks.
    """

    url: str
    session_id: str
    epoch: str
    """This publisher's attach epoch. Sent on the dial so the server can echo it, and so a log
    line on either side names the same attach."""
    ping_interval: float = _DEFAULT_PING_INTERVAL
    ping_timeout: float = _DEFAULT_PING_TIMEOUT
    open_timeout: float = 10.0
    hello_deadline: float = 5.0
    """How long a 101 has to become a ``hello``. Bounded rather than absent: a server that
    completes the upgrade and then sends nothing must leave the client NOT connected, and a
    plain ``recv()`` would wait forever with the publisher believing it was up."""
    backoff_floor: float = 0.5
    backoff_cap: float = 5.0
    """Full jitter, drawn from ``uniform(floor, cap)`` per attempt, and NOT widened per attempt.

    The cap does not grow because there is nothing to back off from: the edge kill is not a
    failing server, and the App will accept the very next dial. A widening cap would only
    lengthen the blank-terminal window. A genuinely broken server is handled by
    ``classify_failure`` making it terminal, not by waiting longer.

    The floor is nonzero for constraint 2 alone -- a socket can die seconds after opening, so a
    zero-delay retry can hot-loop into an imminent kill. The cap stays small because the delay
    IS the gap the publisher's ring has to cover."""
    max_auth_remints: int = 1
    """One re-mint per ``auth_failed``. A SECOND consecutive ``auth_failed`` after a fresh mint
    is terminal, because the token was not the problem. Apps OAuth expires in about an hour, so
    the first case is routine and must not be fatal."""


def classify_failure(exc: BaseException) -> Failure:
    """Classify a dial or socket failure. Constraint 6 lives here.

    CRITICAL: **Status alone never decides.** The measured shapes are a 302 on an
    unauthenticated upgrade and a 200 carrying an HTML login page, so an HTML body is an auth
    failure at ANY status, and a 200 on a WebSocket upgrade is never success.

    Only the four gateway statuses ``websockets`` itself considers retryable are transient
    here: 500, 502, 503, 504. Everything else that is not obviously a network error is treated
    as an auth or protocol failure, because the expensive mistake is the other direction -- a
    retryable classification on an auth failure is an endless loop with a blank terminal.
    """
    if isinstance(exc, InvalidStatus):
        response = exc.response
        status = response.status_code
        if status in (500, 502, 503, 504):
            return Failure.TRANSIENT
        if _looks_like_html(response.headers.get("Content-Type"), bytes(response.body or b"")):
            # The measured PM2 shape: an HTML login page. It arrives as a 200 in the probe's
            # POST measurement, which is exactly why the body is inspected before the status.
            return Failure.AUTH_FAILED
        if status in (300, 301, 302, 303, 307, 308) or status in (401, 403, 200):
            return Failure.AUTH_FAILED
        # Any other status: the server answered, and it was not one of the four gateway codes
        # that mean "try again". `PROTOCOL` rather than `TRANSIENT` so the log line names what
        # a 404 on `$SHELLBOX_APP_URL` actually is -- a wrong path, not network trouble.
        #
        # It is still RETRIED, and that is deliberate rather than an oversight: `connect_forever`
        # only stops on `CONFIG` and on a second `AUTH_FAILED`. An Apps redeploy can answer 404
        # for a few seconds at the edge, and giving up there would kill every publisher on every
        # deploy. The classification buys the diagnosis; it does not change the loop.
        return Failure.PROTOCOL
    if isinstance(exc, InvalidURI):
        # Not a redirect chase: `_NoRedirect` below refuses to follow a `Location`, so the only
        # way here is a malformed `$SHELLBOX_APP_URL`, which retrying cannot fix.
        return Failure.CONFIG
    if isinstance(exc, ConnectionClosed | InvalidMessage | OSError | TimeoutError | EOFError):
        return Failure.TRANSIENT
    if isinstance(exc, CodecError):
        return Failure.TRANSIENT
    return Failure.TRANSIENT


def _looks_like_html(content_type: str | None, body: bytes) -> bool:
    """Whether a response body is a web page rather than this protocol.

    Two signals, and no third: the declared content type, and the two markers that start an
    HTML document. Deliberately NOT a search for words like "sign in" -- that is a guess about
    one login page's copy, and it would break silently when the wording changed.
    """
    if content_type is not None and "text/html" in content_type.lower():
        return True
    head = body[:512].lstrip().lower()
    return head.startswith(b"<!doctype html") or head.startswith(b"<html")


class _NoRedirect(connect):
    """``connect``, with redirect following disabled.

    MEASURED against ``websockets`` 15.0.1: the default ``connect`` FOLLOWS a 302 that carries
    a ``Location``, and a Databricks login redirect points at an ``https://`` URL, so the
    follow fails with ``InvalidURI: scheme isn't ws or wss``. That is the auth failure the
    probe measured, arriving under a name that says nothing about auth -- and a classifier
    reading it would have to guess.

    Refusing the follow makes the 302 surface as ``InvalidStatus`` with the status intact, so
    ``classify_failure`` sees the shape the probe recorded. It also means this client never
    sends its bearer token to whatever host a ``Location`` header names.
    """

    def process_redirect(self, exc: Exception) -> Exception | str:
        return exc


class WSTransport:
    """One publisher's socket to the App: dial, hold, detect death, re-dial.

    Socket cardinality is **one socket per publisher, and at most one publisher per session**,
    so a sandbox with N attached sessions opens N sockets rather than 32xN. Arbitration of "who
    publishes this session" happens in tmux before anything here is constructed.

    This class does not implement ``FrameTransport``. It is the publisher half of the client
    side: it sends frames and receives the server's control frames. The pty bridge, the ring,
    and the resync composition belong to the publisher work item, which builds on this.

    Holds no frames and no session state beyond ``config`` (constraint 5). Not thread-safe, and
    it does not need to be: it runs on one daemon thread with its own event loop.
    """

    def __init__(
        self,
        config: WSTransportConfig,
        *,
        mint_token: Callable[[], str] | None = None,
        dial: Callable[..., Awaitable[ClientConnection]] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self._config = config
        self._mint_token = mint_token
        self._dial = dial if dial is not None else _NoRedirect
        self._sleep = sleep if sleep is not None else asyncio.sleep
        # A per-instance generator seeded from the OS, NOT from the clock. Constraint 1 is a
        # SYNCHRONIZED input: every publisher is killed in the same second, so two publishers
        # seeded from a coarse clock draw the identical delay and stay in lockstep -- which is
        # the storm full jitter exists to break. `tests/unit/test_backoff_jitter.py` asserts it
        # for two instances constructed a millisecond apart.
        self._rng = rng if rng is not None else random.Random()
        self._connection: ClientConnection | None = None

    @property
    def detector(self) -> str:
        """Which mechanism detects an abrupt close. Stated, per this work item's obligation."""
        return _DETECTOR_NOTE

    def next_delay(self) -> float:
        """Full jitter: ``uniform(floor, cap)``, drawn fresh for every attempt."""
        return self._rng.uniform(self._config.backoff_floor, self._config.backoff_cap)

    def dial_url(self) -> str:
        """The dial URL, carrying the session and this attach's epoch as query parameters.

        The epoch is on the dial so the server can echo it in ``hello`` and so both sides' logs
        name the same attach. It is not a secret and not a credential: a subscriber's only
        permitted use of an epoch is pessimistic, "distrust everything and repaint".
        """
        query = urllib.parse.urlencode(
            {"session_id": self._config.session_id, "epoch": self._config.epoch}
        )
        separator = "&" if urllib.parse.urlparse(self._config.url).query else "?"
        return f"{self._config.url}{separator}{query}"

    async def connect_forever(self) -> AsyncIterator[Connected]:
        """Yield each live, ``hello``-confirmed connection until a terminal failure.

        The caller streams on the yielded connection and returns to this loop when it closes,
        which is the normal course of events roughly every 10-18 minutes rather than an error.

        Raises ``TransportTerminal`` when retrying cannot help: an auth failure that survived a
        fresh token, a ``hello`` naming another session, or a malformed URL. Everything else is
        retried after a jittered delay, forever, because the App recycling a healthy socket is
        not a condition to give up on.
        """
        attempt = 0
        auth_remints = 0
        while True:
            attempt += 1
            try:
                connected = await self._dial_once(attempt)
            except TransportTerminal:
                raise
            # `Exception`, deliberately NOT `BaseException`. `asyncio.CancelledError` is a
            # `BaseException`, so a broader clause would classify a cancellation as transient
            # and retry forever -- a publisher thread asked to stop would keep dialing instead.
            except Exception as exc:  # noqa: BLE001 - classified, then re-raised or retried
                failure = classify_failure(exc)
                if failure is Failure.CONFIG:
                    raise TransportTerminal(
                        failure, f"cannot dial {self._config.url}: {exc}"
                    ) from exc
                if failure is Failure.AUTH_FAILED:
                    auth_remints += 1
                    if auth_remints > self._config.max_auth_remints:
                        # The second consecutive auth failure AFTER a fresh mint. The token was
                        # not the problem, so the remedy is a person, not another dial.
                        raise TransportTerminal(
                            failure,
                            "authentication failed twice with a freshly minted token. "
                            "Run `shellbox-mcp doctor`, then `databricks auth login`.",
                        ) from exc
                    logger.warning(
                        "transport auth_failed for session %s; re-minting the token and "
                        "retrying once (%s)",
                        self._config.session_id,
                        exc,
                    )
                else:
                    logger.info(
                        "transport dial %d failed for session %s (%s): %s",
                        attempt,
                        self._config.session_id,
                        failure.value,
                        exc,
                    )
                await self._sleep(self.next_delay())
                continue

            # A dial that reached `hello` clears the auth budget: the token that just worked is
            # proof the credential chain is intact, so an auth failure an hour from now is a
            # fresh expiry and gets its own re-mint rather than inheriting this one's count.
            auth_remints = 0
            attempt = 0
            self._connection = connected.connection
            try:
                yield connected
            finally:
                self._connection = None

    async def _dial_once(self, attempt: int) -> Connected:
        """One dial: mint a token, upgrade, then wait for ``hello`` within the deadline."""
        headers: dict[str, str] = {}
        if self._mint_token is not None:
            # Re-minted on EVERY dial, not cached: Apps OAuth expires in about an hour, and a
            # publisher outlives that. Minting is the injected callable's problem, which is how
            # the credential chain stays in one module.
            headers["Authorization"] = f"Bearer {self._mint_token()}"
        connection = await self._dial(
            self.dial_url(),
            additional_headers=headers or None,
            open_timeout=self._config.open_timeout,
            # Explicit, both of them, so the detector is a decision and not a default. See
            # `_DETECTOR_NOTE`. This is the ONLY keepalive in this module: there is no
            # application-level ping, and `tests/unit/test_no_keepalive.py` asserts that.
            ping_interval=self._config.ping_interval,
            ping_timeout=self._config.ping_timeout,
        )
        try:
            hello = await self._await_hello(connection)
        except BaseException:
            await connection.close()
            raise
        logger.info(
            "transport connected for session %s, epoch %s, attempt %d, detector %s",
            hello.session_id,
            hello.epoch,
            attempt,
            _DETECTOR_NOTE,
        )
        return Connected(connection=connection, hello=hello, attempt=attempt)

    async def _await_hello(self, connection: ClientConnection) -> Hello:
        """Wait for the ``hello`` control frame. The gate on "connected".

        Raises ``TransportTerminal`` when the server names a different ``session_id``. That is
        an ERROR and not a warning: it means the server bound a session this publisher does not
        own, and streaming a pty into it would cross two agents' terminals. Retrying cannot
        make it right, so the loop stops instead of hiding it.

        A timeout, a text message where bytes were required, or an undecodable frame is
        transient. A slow or restarting App must not kill the publisher, and the jittered floor
        bounds the retry.
        """
        raw = await asyncio.wait_for(connection.recv(), timeout=self._config.hello_deadline)
        if isinstance(raw, str):
            # The protocol is binary end to end. A text message here means something between
            # this client and the App is speaking a different protocol.
            raise CodecError("hello arrived as a text message; frames are binary")
        frame = decode_frame(raw)
        message = decode_control(frame.data)
        if message.kind != CONTROL_HELLO:
            raise CodecError(f"first frame is {message.kind!r}, not {CONTROL_HELLO!r}")
        bound = message.fields.get(FIELD_SESSION_ID)
        if bound != self._config.session_id:
            raise TransportTerminal(
                Failure.PROTOCOL,
                f"server bound session {bound!r} but this publisher dialed "
                f"{self._config.session_id!r}; refusing to stream a pty into another session",
            )
        if message.epoch is not None and message.epoch != self._config.epoch:
            # Diagnostic, not fatal. This publisher's own epoch governs its frames, so a
            # server that echoes the wrong one costs a confusing log line and nothing else.
            #
            # A NULL epoch is not a mismatch and must not reach here. The App server holds no
            # attach and echoes none, which is the Phase 3 shape on every dial -- so warning on
            # it would log a line per reconnect, roughly four times an hour per publisher, for
            # the expected case. `tests/unit/test_hello_deadline.py` asserts the silence.
            logger.warning(
                "hello echoed epoch %s but this publisher dialed %s",
                message.epoch,
                self._config.epoch,
            )
        viewer = message.fields.get(FIELD_VIEWER_EMAIL)
        return Hello(
            session_id=self._config.session_id,
            epoch=message.epoch,
            # DISPLAY only, never authorization. It is whatever the edge injected as
            # `X-Forwarded-Email`, and no access decision in shellbox may read it.
            viewer_email=viewer if isinstance(viewer, str) else None,
        )

    async def publish(self, frame: Frame) -> None:
        """Encode and send one frame on the current connection.

        Raises ``ConnectionClosed`` when the socket died, which is the caller's signal to
        return to ``connect_forever``. It does NOT buffer: what a dead socket did not carry is
        the publisher's ring's problem, and duplicating that here would put the same frames in
        two places with two floors.
        """
        connection = self._connection
        if connection is None:
            raise TransportTerminal(Failure.PROTOCOL, "publish called with no live connection")
        await connection.send(encode_frame(frame))

    async def receive(self, connection: ClientConnection) -> AsyncIterator[Frame]:
        """Yield inbound frames -- the server's input and resize control frames.

        Stops when the socket closes. It does not re-dial: ``connect_forever`` owns that, so
        there is exactly one place a reconnect can happen.
        """
        try:
            async for raw in connection:
                if isinstance(raw, str):
                    logger.warning("discarding a text message; frames are binary")
                    continue
                try:
                    yield decode_frame(raw)
                except CodecError as exc:
                    logger.warning("discarding an undecodable frame: %s", exc)
        except ConnectionClosed as exc:
            # The measured teardown signature. Expected, roughly every 10-18 minutes.
            logger.info("socket closed for session %s: %s", self._config.session_id, exc)


def websockets_version() -> str:
    """The installed ``websockets`` version.

    Exposed because two of this module's decisions are version-dependent -- the redirect
    behavior ``_NoRedirect`` overrides, and the keepalive defaults it overrides explicitly --
    and the sandbox image's 14.2 is not the 15.0.1 those were measured against. A findings
    record that names the version is the difference between a reproducible result and a story.
    """
    return websockets.__version__
