"""The subscriber's protocol state machine -- what the browser speaks, decided in Python.

This module is the **decision half** of the browser client. It holds no socket, no terminal,
no clock and no randomness it did not have injected, and it never performs I/O. Events go in
(``opened``, ``received``, ``ticked``, ``closed``, ``typed``, ``resized``) and `Action` values
come out. The wiring half -- an actual WebSocket and an actual xterm.js -- lives in
`packages/shellbox-app/src/shellbox_app/static/`, and it is a transcription of this file.

ADR-23 is why the shape is this one. The repo has zero JavaScript tooling, and adding a browser
lane buys DOM-wiring coverage for the price of a whole toolchain. The renderer's actual risk is
not DOM wiring -- it is frame decoding, sequence handling, reconnect with resume, the bounded
``subscriber_conflict`` retry, and the no-publisher state. All of that is protocol behaviour
over the same `shellbox_transport` codec both ends already share, so all of it is testable here
in milliseconds.

WARNING: **the cost of that decision is real and is not discharged by this file.** The
JavaScript in `static/protocol.js` is a second implementation of what is written here, and
nothing executes both. `tests/unit/test_client_parity.py` compares the two files' declared
constants, which catches a number moved on one side only -- it does **not** catch divergent
logic. The xterm.js layer above this is verified by the live run in `W38` and by nothing else.
That is ADR-23's stated standing cost, restated here because this is the file where someone
would otherwise assume the coverage is complete.

## The five behaviours this exists to get right

1. **A 101 is not proof of a working transport.** "Connected" is gated on a ``hello`` naming
   the bound ``session_id``, inside `HELLO_DEADLINE_SECONDS`. The Apps edge answers an
   unauthenticated upgrade with a 302 and an unauthenticated POST with an HTML login page under
   a 200 (measured, ``probe/FINDINGS.md``), so a client that treats the upgrade as success
   renders a blank terminal and reports it as working. A ``hello`` naming a DIFFERENT session is
   an ERROR and not a warning: it means two agents' streams could cross, and no retry can make
   that right.
2. **Reconnect is the steady state, not the error path.** The edge kills every open socket on a
   wall clock roughly every 10 to 18 minutes, and the kill is a SYNCHRONIZED global event -- so
   a fixed backoff keeps every client in lockstep with it. Hence full jitter, ``uniform(floor,
   cap)`` re-drawn per attempt, with a NONZERO floor because a socket can die seconds after
   opening. Same values and same reasoning as the publisher's, in
   `packages/shellbox-mcp/src/shellbox_mcp/transport.py`.
3. **``subscriber_conflict`` is retried under a bound, then surfaced.** See
   `SUBSCRIBER_CONFLICT_BOUND_SECONDS`.
4. **Resume repaints, it never gap-fills.** D7 rules out a frame log, so a ``resync`` is applied
   as a full terminal RESET followed by the repaint -- never as appended output. `Reset` and
   `Write` are separate action types for exactly this reason: a renderer cannot collapse them by
   accident, it has to delete a branch.
5. **"Attached, but no publisher" is a named state.** This is `R51`, and it is the plan's own
   primary failure mode. See below.

## `R51`: why hello-then-silence is sound evidence, and what makes it actionable

`Relay.bind_subscriber` deliberately admits a subscriber with **no publisher present**, and
``_send_hello`` then runs unconditionally. So the "connected" gate passes and nothing ever
arrives. `server.py` declines to signal a dead publisher, reasoning that a reconnecting
publisher mints a new epoch and an unfamiliar epoch already means "repaint". That reasoning is
sound for a reconnect and **false for a publisher that never returns** -- which is precisely
"a stopped sandbox a human must go start", the failure this whole phase calls its primary one.
Left unhandled it renders as a blank but apparently healthy terminal.

The naive detector is wrong, and it is worth saying why: an idle shell emits no bytes, so
"silence" alone cannot distinguish a dead publisher from a prompt nobody has typed at.

What makes silence sound here is that this client **asks a question on every hello**. It sends
``resume`` with ``from_seq=0`` and no epoch, and `shellbox_transport.seq.plan_resume` resolves
that to `Discontinuity` unconditionally -- no epoch can match, and `FIRST_SEQ` is 1 so no ring
floor can satisfy 0. A live publisher is therefore REQUIRED to answer with a ``resync`` carrying
a repaint. Silence past `NO_PUBLISHER_DEADLINE_SECONDS` is then evidence about the publisher
rather than about the pane, and pane idleness cannot confound it.

The inventory row supplies what makes the message actionable rather than what makes it correct:
`note_host` takes the row `GET /api/hosts` already returns, and the banner names the host and
its ``sandbox_label`` -- which is `NOT_BOOTSTRAPPED_LABEL` when ``sandbox_id`` is NULL, the case
`inventory.py` exists to name. Without the row the state is still reported; it just cannot tell
the reader which sandbox to go and start.

The banner is advisory and self-clearing: the first frame to arrive retracts it. So erring
slightly short on the deadline costs a banner that disappears, and erring long costs a blank
terminal for that much longer. It is set short on purpose.

## What this module deliberately does NOT do

* **It never requests a mid-stream resync.** There is no such path in this protocol -- see
  `server.py`'s docstring on fan-out. An undecodable frame is dropped and counted, and
  `decode_failures` is exposed so the live run can report whether that ever happens rather than
  leaving it silent.
* **It holds no timer.** `ticked` is called by the wiring half; every deadline is a comparison
  against the ``now`` it was handed. A state machine that read a clock could not be tested for
  a 45-second bound in milliseconds.
* **It reads ``viewer_email`` for display only, never for a permission decision.** Decision D5
  of the epic. It is whatever the edge injected as ``X-Forwarded-Email``.
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from enum import Enum

from shellbox_transport import Stream
from shellbox_transport.codec import (
    CLOSED_DETACHED,
    CLOSED_TERMINAL_GONE,
    CONTROL_CLOSED,
    CONTROL_ERROR,
    CONTROL_HELLO,
    CONTROL_RESYNC,
    FIELD_BASE_SEQ,
    FIELD_CODE,
    FIELD_MESSAGE,
    FIELD_REASON,
    FIELD_SESSION_ID,
    FIELD_VIEWER_EMAIL,
    UNORDERED_SEQ,
    CodecError,
    ControlMessage,
    control_frame,
    decode_control,
    decode_frame,
    encode_frame,
    input_message,
    resize_message,
    resume_message,
)
from shellbox_transport.seq import Epoch

logger = logging.getLogger(__name__)

__all__ = [
    "BACKOFF_CAP_SECONDS",
    "BACKOFF_FLOOR_SECONDS",
    "CODE_PUBLISHER_CONFLICT",
    "CODE_SESSION_MISMATCH",
    "CODE_SUBSCRIBER_CONFLICT",
    "CODE_TERMINAL_GONE",
    "HELLO_DEADLINE_SECONDS",
    "NOTICE_DETACHED",
    "NOTICE_NO_PUBLISHER",
    "NOTICE_STREAM_GAP",
    "NO_PUBLISHER_DEADLINE_SECONDS",
    "NO_PUBLISHER_MESSAGE",
    "SUBSCRIBER_CONFLICT_BOUND_SECONDS",
    "Action",
    "HostView",
    "Notice",
    "Phase",
    "Redial",
    "Reset",
    "Send",
    "Stop",
    "SubscriberClient",
    "Write",
]

# How long a 101 has to become a `hello`. Bounded rather than absent: a server that completes
# the upgrade and then says nothing must leave this client NOT connected. The publisher's half
# uses the same 5 s for the same reason -- see `WSTransportConfig.hello_deadline`.
HELLO_DEADLINE_SECONDS = 5.0

# How long after `hello` with NO frame at all before `R51`'s state is named.
#
# DERIVED, not chosen. The question this answers is "how long can a LIVE publisher legitimately
# leave a fresh subscriber with nothing?", and the answer is bounded by the publisher's own
# reconnect: `WSTransportConfig.backoff_cap` is 5.0 s, drawn once per attempt, plus the dial and
# the `capture-pane` that composes the repaint. 8 s leaves 3 s of headroom over that cap.
#
# It is deliberately NOT sized against `open_timeout` (10 s), which would be the conservative
# reading. The banner retracts itself on the first frame, so a false one costs a message that
# vanishes, while a deadline set past the true worst case costs a blank terminal on the failure
# this whole mechanism exists to name. The asymmetry says err short.
NO_PUBLISHER_DEADLINE_SECONDS = 8.0

# How long `subscriber_conflict` is retried before it is reported to the viewer. ADR-20.
#
# NORMATIVE: **this must exceed the App's `WS_PING_INTERVAL_SECONDS + WS_PING_TIMEOUT_SECONDS`,
# which is 40 s.** That sum is the worst-case time a SILENTLY DEAD subscriber holds the session's
# one slot: the App has no reaper of its own, so what frees the slot is uvicorn failing its own
# ping. A browser that gave up sooner would report a conflict that was about to clear itself --
# and the most likely holder of that slot is the viewer's OWN previous socket, killed by the edge
# moments earlier.
#
# The constant is restated here rather than imported, because importing
# `packages/shellbox-app/src/shellbox_app/server.py` would build a FastAPI app and open a
# registry at import time. `tests/unit/test_client_protocol.py` asserts the relationship between
# the two numbers instead, so they cannot drift apart silently.
SUBSCRIBER_CONFLICT_BOUND_SECONDS = 45.0

# Full jitter, `uniform(floor, cap)`, re-drawn per attempt and NOT widened. Identical to the
# publisher's, and for the identical reason: the edge kill is a synchronized global event, so a
# fixed delay preserves the synchronization, and there is nothing to back off from -- the App
# accepts the very next dial. A genuinely broken server is made terminal by classification, not
# by waiting longer. The floor is nonzero because a socket can die seconds after opening, so a
# zero-delay retry can hot-loop straight into an imminent kill.
BACKOFF_FLOOR_SECONDS = 0.5
BACKOFF_CAP_SECONDS = 5.0

# The stable prefix of `R51`'s message. The wiring half branches on `Notice.code`, never on this
# text, and a human reads it in a banner -- so it is free to gain detail and must not lose this
# opening. `tests/unit/test_client_protocol.py` asserts on the prefix.
NO_PUBLISHER_MESSAGE = "no publisher attached"

# `Notice.code`: advisory, the terminal stays usable and the socket stays up.
NOTICE_NO_PUBLISHER = "no_publisher"
NOTICE_DETACHED = "detached"
NOTICE_STREAM_GAP = "stream_gap"

# `Stop.code`: terminal for this viewer, and nothing is re-dialled.
#
# `CODE_SUBSCRIBER_CONFLICT` and `CODE_PUBLISHER_CONFLICT` are the App's own wire codes, spelled
# again here because a `Stop` carries whichever one ended it and the wiring half branches on the
# value. `CODE_SESSION_MISMATCH` is this client's own: the server bound a session this viewer did
# not dial.
CODE_SUBSCRIBER_CONFLICT = "subscriber_conflict"
CODE_PUBLISHER_CONFLICT = "publisher_conflict"
CODE_TERMINAL_GONE = CLOSED_TERMINAL_GONE
CODE_SESSION_MISMATCH = "session_mismatch"


class Phase(Enum):
    """Where the client is. A closed set, and every transition is in `SubscriberClient`."""

    DIALING = "dialing"
    """No socket. The wiring half is opening one, or waiting out a `Redial` delay."""

    AWAITING_HELLO = "awaiting_hello"
    """A socket is open and the 101 happened. NOT connected -- see behaviour 1."""

    LIVE = "live"
    """`hello` arrived and named the right session. Frames may now be rendered."""

    STOPPED = "stopped"
    """Terminal. Nothing is re-dialled and no further action is emitted."""


@dataclass(frozen=True, slots=True)
class Send:
    """Write these bytes on the socket. Already an encoded frame; the wiring half does not parse."""

    payload: bytes


@dataclass(frozen=True, slots=True)
class Write:
    """Append these bytes to the terminal, byte-exact. Never text -- see `Frame.data`."""

    data: bytes


@dataclass(frozen=True, slots=True)
class Reset:
    """CLEAR the terminal, then write ``repaint``. The declared discontinuity, applied.

    A separate type from `Write` so that "resume repaints, it does not gap-fill" is enforced by
    the type the renderer receives rather than by a comment it may not read. Appending a repaint
    duplicates every visible line and leaves the terminal's parser mid-escape; the failure then
    presents as "xterm.js is buggy".
    """

    repaint: bytes


@dataclass(frozen=True, slots=True)
class Notice:
    """Show ``text`` to the viewer. Advisory: the socket stays up and the terminal stays usable."""

    code: str
    text: str


@dataclass(frozen=True, slots=True)
class Redial:
    """Close whatever socket remains, wait ``delay`` seconds, then open a new one."""

    delay: float


@dataclass(frozen=True, slots=True)
class Stop:
    """Terminal. Show ``text``, do not re-dial, and expect no further actions."""

    code: str
    text: str


Action = Send | Write | Reset | Notice | Redial | Stop


@dataclass(frozen=True, slots=True)
class HostView:
    """The three fields of a `GET /api/hosts` row that `R51`'s banner reads.

    A projection rather than the whole row, so that adding a column to
    `packages/shellbox-app/src/shellbox_app/inventory.py` does not change this module. Built by
    the wiring half from the JSON envelope, whose ``stale`` flag is the caller's business: a
    stale inventory means this stays ``None`` and the banner loses its detail, not its correctness.
    """

    host_id: str
    sandbox_label: str
    """Already resolved by the server, `NOT_BOOTSTRAPPED_LABEL` when ``sandbox_id`` is NULL. The
    label is deliberately NOT derived here -- `inventory.py` owns that rule, and deriving it in a
    second place is how the two start disagreeing."""
    status: str


class SubscriberClient:
    """One viewer's protocol state, across every socket it will hold for one session.

    It OUTLIVES its sockets, which is the whole point: the epoch and the last rendered ``seq``
    are what a reconnect resumes from, so they cannot live in the connection. Construct one per
    session the viewer opens, and keep it across every `Redial`.

    Not thread-safe and does not need to be: a browser drives it from one event loop, and the
    integration lane drives it from one task.
    """

    def __init__(
        self,
        session_id: str,
        *,
        rng: random.Random | None = None,
        hello_deadline: float = HELLO_DEADLINE_SECONDS,
        no_publisher_deadline: float = NO_PUBLISHER_DEADLINE_SECONDS,
        conflict_bound: float = SUBSCRIBER_CONFLICT_BOUND_SECONDS,
        backoff_floor: float = BACKOFF_FLOOR_SECONDS,
        backoff_cap: float = BACKOFF_CAP_SECONDS,
    ) -> None:
        self.session_id = session_id
        self._hello_deadline = hello_deadline
        self._no_publisher_deadline = no_publisher_deadline
        self._conflict_bound = conflict_bound
        self._backoff_floor = backoff_floor
        self._backoff_cap = backoff_cap
        # Seeded from the OS, never from the clock. The kill is synchronized, so two clients
        # seeded from a coarse clock draw the same delay and stay in lockstep -- which is the
        # storm full jitter exists to break. The publisher's half says the same thing.
        self._rng = rng if rng is not None else random.Random()

        self.phase = Phase.DIALING
        self.epoch: str | None = None
        """The last epoch seen on a control frame. ``None`` until one arrives. A subscriber's
        only permitted use of an epoch is PESSIMISTIC -- an unfamiliar one means repaint."""
        self.last_seq = 0
        """The highest ordinal rendered in `epoch`. 0 means "I hold nothing", which
        `plan_resume` resolves to the honest branch. Reset to 0 whenever the epoch changes."""
        self.viewer_email: str | None = None
        """DISPLAY only, from ``hello``. Never an authorization input -- decision D5."""
        self.decode_failures = 0
        """Undecodable frames dropped. Exposed so the live run can report a number rather than
        leaving a silent discard silent."""

        self._host: HostView | None = None
        self._size: tuple[int, int] | None = None
        self._opened_at: float | None = None
        self._hello_at: float | None = None
        self._conflict_since: float | None = None
        self._seen_frame = False
        self._reported_no_publisher = False

    # ---------------------------------------------------------------- inputs from the wiring

    def note_host(self, host: HostView | None) -> None:
        """Supply the inventory row for this session's host, or ``None`` if it is unavailable.

        Detail for `R51`'s banner and nothing else. It never gates the detection, never selects
        rows, and is safe to call as often as the page refreshes its inventory.
        """
        self._host = host

    def opened(self, now: float) -> list[Action]:
        """The socket completed its upgrade. Arms the `hello` deadline. NOT "connected"."""
        if self.phase is Phase.STOPPED:
            return []
        self.phase = Phase.AWAITING_HELLO
        self._opened_at = now
        self._hello_at = None
        self._seen_frame = False
        self._reported_no_publisher = False
        return []

    def received(self, raw: bytes | str, now: float) -> list[Action]:
        """One inbound WebSocket message. The whole of the render decision."""
        if self.phase is Phase.STOPPED:
            return []
        if isinstance(raw, str):
            # The protocol is binary end to end. A text message means something between this
            # client and the App speaks a different protocol; it is dropped rather than rendered,
            # because rendering it would put a login page's HTML into a terminal.
            return self._dropped("a text message arrived; frames are binary")
        try:
            frame = decode_frame(raw)
        except CodecError as exc:
            return self._dropped(f"undecodable frame: {exc}")

        if frame.stream is not Stream.CONTROL:
            return self._data(frame.seq, frame.data, now)

        try:
            message = decode_control(frame.data)
        except CodecError as exc:
            return self._dropped(f"undecodable control frame: {exc}")

        if message.kind == CONTROL_ERROR:
            # CHECKED BEFORE THE PHASE, and that is the bug this ordering exists to prevent. A
            # refusal arrives INSTEAD of `hello`, not after it -- `serve_subscriber` refuses,
            # sends the reason, and closes without ever binding. Routed by phase, a refusal
            # reached the "first frame was not a hello" branch and was retried as a plain
            # transient with no bound at all, so `subscriber_conflict` never reached ADR-20's
            # 45 s window and never surfaced to the viewer. Both unit tests for the bound
            # failed on it. See `_refused`, which is correct in either phase.
            return self._refused(message, now)
        if self.phase is Phase.AWAITING_HELLO:
            return self._first(message, now)
        return self._control(message, now)

    def ticked(self, now: float) -> list[Action]:
        """Advance the deadlines. Call this on a timer; it never blocks and reads no clock."""
        if self.phase is Phase.AWAITING_HELLO:
            if self._opened_at is not None and now - self._opened_at >= self._hello_deadline:
                # A 101 that never became a `hello`. Transient, exactly as the publisher's half
                # classifies its own hello timeout: a slow or restarting App must not be terminal.
                logger.info(
                    "no hello within %.1fs for session %s", self._hello_deadline, self.session_id
                )
                return [self._redial()]
            return []

        if self.phase is not Phase.LIVE:
            return []
        if self._seen_frame or self._reported_no_publisher or self._hello_at is None:
            return []
        if now - self._hello_at < self._no_publisher_deadline:
            return []
        self._reported_no_publisher = True
        return [Notice(NOTICE_NO_PUBLISHER, self._no_publisher_text())]

    def closed(self, now: float) -> list[Action]:
        """The socket died. Routine, roughly every 10 to 18 minutes -- see behaviour 2.

        Returns nothing when a `Redial` or a `Stop` was already emitted for this socket, so a
        refusal followed by the server's close does not schedule two dials.
        """
        del now
        if self.phase in (Phase.STOPPED, Phase.DIALING):
            return []
        return [self._redial()]

    def typed(self, data: bytes, now: float) -> list[Action]:
        """A keystroke. Byte-exact to the pty, with no allowlist -- see `input_message`."""
        if self.phase is not Phase.LIVE or not data:
            return []
        return [Send(self._encode(input_message(data), now))]

    def resized(self, cols: int, rows: int, now: float) -> list[Action]:
        """The viewport changed. Remembered, and sent when there is an epoch to send it under.

        `resize_message` requires an `Epoch`, and a subscriber holds no attach of its own -- so
        the only honest value is the last epoch the publisher stated. Before one arrives the size
        is remembered and nothing is sent; it goes out on the next `hello`, and again whenever
        the epoch changes, which is what re-asserts the viewer's size across a publisher restart.
        """
        if cols < 1 or rows < 1:
            return []
        if self._size == (cols, rows):
            return []
        self._size = (cols, rows)
        return self._resize(now)

    # ---------------------------------------------------------------- internals

    def next_delay(self) -> float:
        """Full jitter: ``uniform(floor, cap)``, drawn fresh for every attempt."""
        return self._rng.uniform(self._backoff_floor, self._backoff_cap)

    def _first(self, message: ControlMessage, now: float) -> list[Action]:
        """The gate on "connected". Only a `hello` naming this session passes it."""
        if message.kind != CONTROL_HELLO:
            # A server that opens with something else is not speaking this protocol on this
            # socket. Transient rather than terminal, for the reason the deadline is.
            return [self._redial()]

        bound = message.fields.get(FIELD_SESSION_ID)
        if bound != self.session_id:
            # CRITICAL: an ERROR, never a warning. The server bound a session this viewer did
            # not dial, so rendering the stream would put another agent's terminal on this page.
            # No retry can make that right, so the loop stops instead of hiding it.
            return [
                self._stop(
                    CODE_SESSION_MISMATCH,
                    f"the server bound session {bound!r}, but this page opened "
                    f"{self.session_id!r}; refusing to render another session's terminal",
                )
            ]

        viewer = message.fields.get(FIELD_VIEWER_EMAIL)
        self.viewer_email = viewer if isinstance(viewer, str) else None
        self.phase = Phase.LIVE
        self._hello_at = now
        # A hello is proof the slot was free, so the conflict window starts fresh next time.
        self._conflict_since = None

        # The question behaviour 5 rests on. Always asked, including on a first attach, where
        # `from_seq=0` with no epoch is the honest "I hold nothing" -- see `resume_message`.
        actions: list[Action] = [
            Send(self._encode(resume_message(self.last_seq, self._parsed_epoch()), now))
        ]
        actions.extend(self._resize(now))
        return actions

    def _control(self, message: ControlMessage, now: float) -> list[Action]:
        """A control frame on a live socket."""
        if message.kind == CONTROL_RESYNC:
            return self._resync(message, now)
        if message.kind == CONTROL_CLOSED:
            return self._closed_message(message)
        if message.kind == CONTROL_HELLO:
            # A second hello on a socket already past the gate. Nothing to do: the binding did
            # not change, and re-running the gate would re-send a resume for no reason.
            return []
        # `input` and `resize` travel the other way. Anything else is a message from a release
        # this client does not know, and the safe response to both is to ignore it -- a renderer
        # that guessed would paint something the sender did not ask for.
        logger.info("ignoring an inbound %r control frame", message.kind)
        return []

    def _refused(self, message: ControlMessage, now: float) -> list[Action]:
        """A refusal, named in-band. The bounded retry lives here -- behaviour 3, ADR-20."""
        code = message.fields.get(FIELD_CODE)
        text = message.fields.get(FIELD_MESSAGE)
        reason = text if isinstance(text, str) else "the server refused this socket"

        if code != CODE_SUBSCRIBER_CONFLICT:
            # `publisher_conflict` cannot reach a subscriber's route, and an unknown code is not
            # something to retry blindly. Both stop and surface what the server said.
            return [self._stop(code if isinstance(code, str) else "refused", reason)]

        if self._conflict_since is None:
            self._conflict_since = now
        waited = now - self._conflict_since
        if waited >= self._conflict_bound:
            return [
                self._stop(
                    CODE_SUBSCRIBER_CONFLICT,
                    f"{reason} (still held after {waited:.0f}s). Close the other tab or "
                    f"window viewing this session, then reload.",
                )
            ]
        return [self._redial()]

    def _resync(self, message: ControlMessage, now: float) -> list[Action]:
        """The declared discontinuity. A full RESET, never appended -- behaviour 4.

        The publisher sends this FIRST when it cannot serve a resume byte-exactly, and its
        payload is a ``capture-pane`` snapshot of the VISIBLE pane. ``base_seq`` is the last
        ordinal that snapshot accounts for, so live frames resume at ``base_seq + 1`` and the
        next data frame must not be treated as a gap.
        """
        self._note_epoch(message.epoch)
        base = message.fields.get(FIELD_BASE_SEQ)
        if isinstance(base, int) and not isinstance(base, bool):
            self.last_seq = base
        self._seen_frame = True
        actions: list[Action] = []
        if self._reported_no_publisher:
            # The banner retracts itself. A publisher answered, so the state it named is over.
            self._reported_no_publisher = False
        actions.append(Reset(message.payload))
        # The epoch just changed under us, so the viewer's size has to be re-asserted against it.
        actions.extend(self._resize(now))
        return actions

    def _closed_message(self, message: ControlMessage) -> list[Action]:
        """The pane's stream ended, and which of the two ways -- see `closed_message`.

        CRITICAL: the two reasons must not be collapsed, and the cost is asymmetric.
        ``terminal_gone`` means the pane's process exited and there is nothing left to watch, so
        reconnecting is pointless. ``detached`` means only that attach client went away, and a
        viewer that read it as ``terminal_gone`` would tear down a session that is still running.
        """
        reason = message.fields.get(FIELD_REASON)
        if reason == CLOSED_TERMINAL_GONE:
            return [
                self._stop(
                    CODE_TERMINAL_GONE,
                    "the terminal's process exited. Its final output is above; there is "
                    "nothing further to stream.",
                )
            ]
        if reason == CLOSED_DETACHED:
            return [
                Notice(
                    NOTICE_DETACHED,
                    "the publisher detached from this session. The session is still running, "
                    "and output resumes when it re-attaches.",
                )
            ]
        logger.info("ignoring a 'closed' frame with reason %r", reason)
        return []

    def _data(self, seq: int, data: bytes, now: float) -> list[Action]:
        """A ``stdout`` or ``stderr`` frame. Rendered verbatim."""
        if self.phase is not Phase.LIVE:
            # Data ahead of `hello` is a server that skipped the handshake. Dropped rather than
            # rendered: the gate exists precisely so that bytes from an unverified peer do not
            # reach the terminal.
            return self._dropped("a data frame arrived before hello")

        actions: list[Action] = []
        if self._reported_no_publisher:
            self._reported_no_publisher = False
        if seq > self.last_seq + 1 and self.last_seq > 0:
            # Gap-free is a construction property of `SeqAllocator`, so a jump inside one epoch
            # is a publisher bug rather than a lossy network. It is surfaced and the bytes are
            # still rendered -- there is no resync-request path to take instead, and dropping
            # them would turn a reportable defect into a silent one.
            actions.append(
                Notice(
                    NOTICE_STREAM_GAP,
                    f"the stream skipped from {self.last_seq} to {seq}; output may be missing.",
                )
            )
        self._seen_frame = True
        self.last_seq = max(self.last_seq, seq)
        actions.append(Write(data))
        del now
        return actions

    def _note_epoch(self, epoch: str | None) -> None:
        """Adopt an epoch seen on a control frame, resetting the ordinal space if it changed.

        CRITICAL: ``seq`` restarts in every epoch, so carrying `last_seq` across a change would
        make the next resume name a position in a stream that no longer exists -- and
        `plan_resume` checks the epoch before the floor for exactly that reason.
        """
        if epoch is None or epoch == self.epoch:
            return
        self.epoch = epoch
        self.last_seq = 0

    def _parsed_epoch(self) -> Epoch | None:
        """`self.epoch` as an `Epoch`, or ``None`` if it is absent or not shaped like one."""
        if self.epoch is None:
            return None
        try:
            return Epoch.parse(self.epoch)
        except ValueError:
            # A malformed epoch is a protocol violation, and the pessimistic reading is the safe
            # one: present none, which resolves the resume to the honest branch and repaints.
            logger.warning("discarding a malformed epoch %r", self.epoch)
            return None

    def _resize(self, now: float) -> list[Action]:
        """The pending size as a frame, or nothing when there is no size or no epoch yet."""
        epoch = self._parsed_epoch()
        if self._size is None or epoch is None or self.phase is not Phase.LIVE:
            return []
        cols, rows = self._size
        return [Send(self._encode(resize_message(epoch, cols, rows), now))]

    def _encode(self, message: ControlMessage, now: float) -> bytes:
        """One frame this client originates.

        ``seq`` is `UNORDERED_SEQ`. A subscriber allocates no ordinals and must not appear to
        hold a position in the publisher's sequence space -- see the constant's own comment.
        """
        return encode_frame(control_frame(self.session_id, UNORDERED_SEQ, now, message))

    def _no_publisher_text(self) -> str:
        """`R51`'s message. Names the host and its sandbox when the inventory supplied one."""
        if self._host is None:
            return (
                f"{NO_PUBLISHER_MESSAGE}: this session is bound, but nothing is streaming to "
                f"it. The sandbox that owns it is probably stopped. Reload the host list to "
                f"see which one."
            )
        return (
            f"{NO_PUBLISHER_MESSAGE}: host {self._host.host_id} ({self._host.sandbox_label}) "
            f"is {self._host.status}, and nothing is streaming to this session. Start that "
            f"sandbox, then reload."
        )

    def _dropped(self, why: str) -> list[Action]:
        self.decode_failures += 1
        logger.warning("dropped an inbound message for session %s: %s", self.session_id, why)
        return []

    def _redial(self) -> Redial:
        self.phase = Phase.DIALING
        self._opened_at = None
        self._hello_at = None
        return Redial(self.next_delay())

    def _stop(self, code: str, text: str) -> Stop:
        self.phase = Phase.STOPPED
        return Stop(code, text)
