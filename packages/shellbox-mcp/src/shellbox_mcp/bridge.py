"""The data path: pane bytes out, keystrokes and resizes in, and ADR-11's resume answered.

This is where the four pieces built separately meet. ``attach.py`` holds the pty, ``tmux.py``
builds the argv and reads the pane, ``transport.py`` owns the socket, and ``shellbox_transport``
owns the ordinals, the ring, and the resume decision. Nothing here re-implements any of them;
what it adds is the composition and the ordering rules that only exist once they are combined.

## The three rules that only exist at this layer

**1. The ring is appended to whether or not a socket is live.** The edge kills every open
socket in the same second, roughly every 10-18 minutes, and the pane keeps writing during the
sub-second reconnect. A publisher that only recorded what it managed to send would have nothing
to serve the resume that follows -- which is precisely the window ADR-11's byte-exact branch
exists for, so the ring would be permanently empty at the one moment it is asked.

**2. Output and resume are serialized against each other.** ``seq.Discontinuity`` carries the
warning: ``base_seq`` is the ring's newest ordinal at planning time, so anything published
between planning a discontinuity and sending its resync makes the repaint's base wrong and the
subscriber double-paints. The pty pump and the inbound handler are separate tasks, so that
window is real here in a way it is not in ``seq.py``'s single-threaded tests. ``_lock`` closes
it, and that is its only job.

**3. Nothing a subscriber sends may kill the publisher.** A malformed control frame, an
oversized line, an unknown message kind -- each is logged and dropped. The agent's session is
the asset; a viewer is a guest, and a guest must not be able to end an agent's stream. This is
the same rule ``server.py`` follows for tool input, one layer out.

## What this module deliberately does not do

It does not decide **whether** it may attach. One publisher per session is arbitrated host-side
through a tmux claim keyed on the publisher thread's kernel identity, and that -- with the
daemon thread that hosts this loop and the main-thread shutdown path that stops it -- is
``W19b``. This module is written to be driven by that: ``run`` is a coroutine that returns when
the pane's stream ends, and ``close`` is idempotent.

WARNING: **The attach child outlives this coroutine if ``run`` is not awaited to completion.**
An orphaned ``tmux attach`` stays a live tmux client on the session indefinitely, holding the
window at the last viewer's size -- PM3's reflow made permanent. ``run``'s ``finally`` closes
the pty, but a daemon thread killed at interpreter shutdown never reaches a ``finally``. That
is why ``W19b``'s shutdown path is not optional tidying.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable

from shellbox_transport import Frame, Stream
from shellbox_transport.codec import (
    CLOSED_DETACHED,
    CLOSED_TERMINAL_GONE,
    CONTROL_INPUT,
    CONTROL_RESIZE,
    CONTROL_RESUME,
    FIELD_ASKED_SEQ,
    FIELD_COLS,
    FIELD_ROWS,
    CodecError,
    ControlMessage,
    closed_message,
    control_frame,
    decode_control,
    resync_message,
)
from shellbox_transport.seq import Continuity, Epoch, RingBuffer, SeqAllocator, plan_resume

from shellbox_mcp.attach import AttachedPty, PtySource, read_limit
from shellbox_mcp.errors import LineTooLong, ShellboxError
from shellbox_mcp.tmux import TmuxAdapter
from shellbox_mcp.transport import Connected, WSTransport

logger = logging.getLogger(__name__)

__all__ = ["PtyBridge"]


class PtyBridge:
    """One session's publisher: an attached pty, a socket, an epoch, and a ring.

    Not thread-safe. One publisher owns one session -- a second publisher for a live session is
    an error, not a fan-in case -- and it runs on one thread with one loop.
    """

    def __init__(
        self,
        adapter: TmuxAdapter,
        transport: WSTransport,
        session_id: str,
        *,
        tmux_name: str,
        epoch: Epoch | None = None,
        attach: Callable[[], PtySource] | None = None,
        ring: RingBuffer | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Bind a wire identity to a local tmux session. **The two are not the same string.**

        CRITICAL: ``session_id`` is the GLOBAL ``<host_id>:<tmux_name>`` from
        ``naming.session_id`` -- what goes on every ``Frame``, what the App binds an attachment
        to, and what ``hello`` echoes back for the client to compare. ``tmux_name`` is the LOCAL
        session name this host's tmux server knows, and it is the only one an adapter call may
        receive.

        They were one parameter until `W23` wired a bridge to a real App and found they cannot
        be: ``naming.validate_session_name`` rejects ``:``, so a global id passed to
        ``prepare_attach`` raises ``InvalidName`` -- which meant the wire could only ever carry
        a bare tmux name. Two sandboxes each holding a session called ``build`` would then dial
        the same ``/publish/build``, and the second would either be refused as a conflict or
        REBIND to the first's attachment and serve a viewer somebody else's terminal. The
        ``hello`` check cannot catch that, because both sides agree on the string.

        ``tmux_name`` is keyword-only and required. Two adjacent string parameters are a swap
        waiting to happen, and a swap here is silent: both are plausible names, and the failure
        surfaces as a session that cannot be found on a host that does have it.
        """
        self._adapter = adapter
        self._transport = transport
        self._session_id = session_id
        self._tmux_name = tmux_name
        # One epoch per attach, minted here because an attach is what an epoch identifies. A
        # subscriber that sees an unfamiliar one knows `seq` restarted, which is the whole of
        # what ADR-12 buys -- post-hoc misdelivery DETECTION, exactly as `@shellbox_incarnation`
        # buys it one layer down.
        self._epoch = epoch if epoch is not None else Epoch.new()
        self._seq = SeqAllocator(self._epoch)
        self._ring = ring if ring is not None else RingBuffer(self._epoch)
        self._attach = attach if attach is not None else self._attach_real
        self._clock = clock

        self._pty: PtySource | None = None
        self._live = False
        self._last_input_at: float | None = None
        # See rule 2 in the module docstring. This lock exists for `Discontinuity`'s warning
        # and for nothing else.
        self._lock = asyncio.Lock()

    @property
    def epoch(self) -> Epoch:
        return self._epoch

    @property
    def ring(self) -> RingBuffer:
        """This attach's ring. Exposed for the live run's reporting, never for another process.

        `tests/integration/test_no_session_state.py` asserts it is unreachable from the server's
        attributes and from every tool closure, which is what keeps invariant 7 true: this
        answers "what did I send down this socket", never "does this session exist".
        """
        return self._ring

    # -- lifecycle -----------------------------------------------------------------------

    async def run(self) -> None:
        """Attach, then pump until the pane's stream ends. Returns; does not loop forever.

        Reconnect happens INSIDE this call, in ``connect_forever``, and the pty survives it --
        which is the point. A socket dying is not the attach dying, and rebuilding the attach on
        every edge kill would reflow the agent's window every ten minutes and restart ``seq``
        four times an hour for no reason.

        Raises ``TransportTerminal`` when the transport gives up: an auth failure that survived
        a fresh token, a ``hello`` naming another session, or a malformed URL. The pty is closed
        on the way out either way.

        CRITICAL: **The two pumps are RACED, not awaited in sequence.** Each ends for its own
        reason and either one ending ends the publisher: the pty pump returns when the pane's
        stream does, and the socket pump returns only on a terminal transport failure. Awaiting
        the socket pump alone -- the obvious shape -- hangs forever in the case that matters
        most: the App is unreachable, so the dial loop is suspended mid-backoff, and the pane
        exits. Nothing would then be listening for the pane's death, ``run`` would never return,
        and the attach child would stay a live tmux client holding the agent's window at the
        last viewer's size.
        """
        self.attach()
        pty_pump = asyncio.create_task(self._pump_pty(), name=f"shellbox-pty-{self._session_id}")
        sockets = asyncio.create_task(
            self._serve_sockets(), name=f"shellbox-socket-{self._session_id}"
        )
        try:
            done, _ = await asyncio.wait({pty_pump, sockets}, return_when=asyncio.FIRST_COMPLETED)
            # Re-raise whatever ended it. Without this a terminal transport failure would be
            # swallowed into a clean return, and the caller would read "the pane exited".
            for task in done:
                task.result()
        finally:
            for task in (pty_pump, sockets):
                task.cancel()
            # `gather(return_exceptions=True)`, and NOT `await task` in a loop. Whichever task
            # ended `run` is already finished carrying its exception, so re-awaiting it here
            # would raise that exception a second time -- out of the `finally`, before
            # `self.close()`, replacing the real error with a leaked attach child. Collecting
            # the results instead makes the cleanup unconditional.
            await asyncio.gather(pty_pump, sockets, return_exceptions=True)
            self.close()

    def attach(self) -> None:
        """Fork the attach client, if one is not already running.

        Separate from ``run`` because ``W19b`` claims the session before attaching and must be
        able to order the two, and because it lets the input path be exercised without a socket.
        """
        if self._pty is None:
            self._pty = self._attach()

    async def aclose(self) -> None:
        """Close the SOCKET and then the pty. The shutdown path's deterministic teardown.

        ``close()`` below reaps the attach child and is synchronous, which is what makes it
        callable from the main thread after a loop has stopped. It cannot close the socket,
        because closing a WebSocket is an ``await``. So the two are separate, and a shutdown
        that still has a live loop calls this one -- which does both, in the order that matters.

        Socket first: the App refuses a second publisher while a live one is registered, so
        until this returns the session is unpublishable by anyone else. The pty can wait a
        few milliseconds; the binding cannot.

        See ``WSTransport.aclose`` for the measurement that made this necessary -- the App held
        a stale publisher binding for 30 seconds after a stop, because the generator cleanup it
        used to rely on was reached through a cancellation.
        """
        try:
            if self._transport is not None:
                await self._transport.aclose()
        finally:
            self.close()

    def close(self) -> None:
        """Close the pty and reap the attach child. Idempotent, and safe to call from anywhere.

        Synchronous on purpose. It is the last thing that must happen and the one thing that
        must not depend on a loop still running -- ``W19b``'s shutdown path calls it from the
        main thread after the publisher's loop has already stopped.
        """
        pty, self._pty = self._pty, None
        if pty is not None:
            pty.close()

    async def _serve_sockets(self) -> None:
        """Hold one socket at a time, re-dialling forever. Returns only on a terminal failure.

        ``aclose`` on the way out is not tidying. This generator holds a live socket at its
        suspension point, so abandoning it without closing leaves that socket open until a
        finalizer happens to run -- and the App would go on counting a publisher that left,
        which is the state that makes the next dial look like a second publisher.
        """
        stream = self._transport.connect_forever()
        try:
            async for connected in stream:
                await self._serve_socket(connected)
        finally:
            await stream.aclose()

    async def _serve_socket(self, connected: Connected) -> None:
        """Serve one live socket. Returns when it closes, which the edge causes on a wall clock.

        ``_live`` is what makes ``_publish`` a no-op rather than an error between sockets. It is
        cleared in a ``finally`` because the interesting exit from here is a cancellation --
        ``run`` cancels this whole task when the pane's stream ends.
        """
        self._live = True
        logger.info(
            "bridge serving session %s on attempt %d, epoch %s",
            self._session_id,
            connected.attempt,
            self._epoch,
        )
        try:
            await self._pump_socket(connected)
        finally:
            self._live = False

    # -- pane bytes out ------------------------------------------------------------------

    async def _pump_pty(self) -> None:
        """Read the pty forever, framing each read. The output half of the data path.

        The initial repaint needs no ``capture-pane``: ``tmux attach`` redraws the whole screen
        to the new client in this same byte stream, ordered by tmux itself. Measured rather
        than inferred (spike F17) -- a sentinel printed into the pane before any client existed
        arrived in the first ~730 bytes of this fd with no capture issued. That ordering is
        ADR-9's strongest argument over the ``pipe-pane`` alternative, whose out-of-band capture
        cannot be gated against the pipe and so either loses or duplicates bytes.
        """
        pty = self._pty
        assert pty is not None, "run() sets the pty before starting this task"
        while True:
            limit = read_limit(now=self._clock(), last_input_at=self._last_input_at)
            data = await pty.read(limit)
            if not data:
                break
            async with self._lock:
                await self._emit(Stream.STDOUT, data)
        await self._announce_closed()

    async def _emit(self, stream: Stream, data: bytes) -> None:
        """Frame ``data``, record it in the ring, then try to send it. In that order.

        The ring is appended to first and unconditionally -- rule 1 in the module docstring. A
        frame the socket could not carry is exactly the frame a resume will ask for.

        Caller must hold ``_lock``.
        """
        frame = Frame(
            session_id=self._session_id,
            seq=self._seq.next(),
            t=self._clock(),
            stream=stream,
            data=data,
        )
        self._ring.append(frame)
        await self._publish(frame)

    async def _publish(self, frame: Frame) -> None:
        """Send one frame if a socket is live, and never raise because one is not.

        A dead socket is the steady state between an edge kill and the re-dial that follows it,
        so it cannot be an error path. What was not sent is in the ring.
        """
        if not self._live:
            return
        try:
            await self._transport.publish(frame)
        except Exception as exc:  # noqa: BLE001 - the socket died mid-send, which is routine
            logger.debug("frame %d not sent for session %s: %s", frame.seq, self._session_id, exc)

    async def _announce_closed(self) -> None:
        """Say WHICH way the stream ended, then stop. The detach-versus-dead split.

        CRITICAL: These must never collapse into one another. ``terminal_gone`` tells a viewer
        to stop reconnecting; ``detached`` must not, because a detach misread as terminal-gone
        tears down a session that is still running.

        ``has-session`` cannot answer it -- shellbox sets ``remain-on-exit on`` globally, so a
        session deliberately outlives its pane's process. ``#{pane_dead}`` can, measured in both
        directions with a live client attached (spike F19), and the same finding is what makes
        this reportable at all: the attach client OUTLIVES the pane's process, so there is still
        a socket to say it on.

        A session that does not resolve reads as ``terminal_gone``. That is the conservative
        direction of the two: a session that is gone certainly has nothing left to watch, while
        reporting it as a detach would leave a viewer reconnecting to nothing forever.
        """
        dead = await asyncio.to_thread(self._adapter.pane_dead, self._tmux_name)
        reason = CLOSED_DETACHED if dead is False else CLOSED_TERMINAL_GONE
        logger.info("session %s stream ended: %s", self._session_id, reason)
        async with self._lock:
            frame = control_frame(
                self._session_id,
                self._seq.next(),
                self._clock(),
                closed_message(self._epoch, reason),
            )
            self._ring.append(frame)
            await self._publish(frame)

    # -- keystrokes, resizes and resume in ------------------------------------------------

    async def _pump_socket(self, connected: Connected) -> None:
        """Read the socket until it closes, handling each control frame. The inbound half."""
        async for frame in self._transport.receive(connected.connection):
            if frame.stream is not Stream.CONTROL:
                # Only the publisher originates data frames, and it does not send them to
                # itself. Anything else on this direction is a peer speaking a protocol this
                # one does not.
                logger.warning(
                    "dropped an inbound %s frame; only control arrives here", frame.stream
                )
                continue
            try:
                message = decode_control(frame.data)
            except CodecError as exc:
                logger.warning("dropped an undecodable control frame: %s", exc)
                continue
            await self._handle(message)

    async def _handle(self, message: ControlMessage) -> None:
        """Dispatch one inbound control message. Never raises -- rule 3 in the docstring."""
        try:
            if message.kind == CONTROL_INPUT:
                self.send_input(message.payload)
            elif message.kind == CONTROL_RESIZE:
                self._resize(message)
            elif message.kind == CONTROL_RESUME:
                await self._resume(message)
            else:
                logger.warning("ignoring an inbound control message of kind %r", message.kind)
        except ShellboxError as exc:
            # A rejected line is the designed outcome, not a bug -- see `send_input`.
            logger.warning(
                "refused inbound %s for session %s: %s", message.kind, self._session_id, exc
            )
        except Exception as exc:  # noqa: BLE001 - a guest must not be able to end the stream
            logger.exception(
                "inbound %s failed for session %s: %s", message.kind, self._session_id, exc
            )

    def send_input(self, data: bytes) -> None:
        """Write keystrokes to the pty, byte-exact, behind the shipped per-line ceiling.

        CRITICAL: **The ceiling is not optional and it is not new.** H4 is the receiving pane's
        tty in canonical mode, and tmux forwards an attach client's input to that same tty -- so
        the pty path does not escape it. Measured on this exact path (spike F18): 8192 bytes
        plus a newline delivered **4096 bytes on Linux, silently truncated**, and 0 on macOS.
        A truncated command is a different, still-executable command, which is the worse of the
        two failures and the sandbox's behaviour.

        REJECT is the only implementable branch. Chunking does not help -- the truncation is the
        kernel's ``MAX_CANON`` line buffer, not a write-size limit -- and no tmux format exposes
        the pane pty's termios, so this process cannot know whether the pane is in canonical
        mode at all. So it refuses the whole write rather than delivering a prefix.

        The check is ``TmuxAdapter.check_send_limits``, the same call ``shell_send`` makes, so
        the two paths cannot drift into different numbers or different errors. Raises
        ``LineTooLong``, which ``errors.py`` calls "the real boundary".

        WARNING: The refusal reaches the operator's log and NOT the viewer's screen. Naming it
        in-band needs a control code outside the closed set ``shellbox_app.server`` documents,
        and the renderer that would display it is Phase 4's. Until then a viewer whose paste is
        refused sees nothing happen, which is worse than a message and much better than a
        truncated command running.
        """
        pty = self._pty
        if pty is None:
            raise LineTooLong("no pty is attached", session=self._session_id)
        if not data:
            return
        self._adapter.check_send_limits(data, self._session_id)
        self._last_input_at = self._clock()
        pty.write(data)

    def _resize(self, message: ControlMessage) -> None:
        """Apply a viewer resize with ``TIOCSWINSZ``. No tmux round trip.

        The agent's window does not follow, and that is `W15`'s ``freeze_window_size`` doing its
        job: per-window ``window-size manual`` is set before the client is spawned, measured to
        hold an 80x24 window through a 120x40 client over 1714 samples (spike F16). What this
        changes is the CLIENT's viewport -- what tmux renders down this pty -- which is what the
        viewer's browser is showing.

        A resize naming a stale epoch is applied rather than refused. A viewer's window is that
        size whichever attach it learned the epoch from, and refusing would leave the browser
        rendering at the wrong size until it reconnected again.
        """
        pty = self._pty
        cols = message.fields.get(FIELD_COLS)
        rows = message.fields.get(FIELD_ROWS)
        if pty is None or not isinstance(cols, int) or not isinstance(rows, int):
            logger.warning("ignoring a malformed resize: cols=%r rows=%r", cols, rows)
            return
        pty.set_window_size(cols, rows)
        logger.info("resized session %s viewport to %dx%d", self._session_id, cols, rows)

    async def _resume(self, message: ControlMessage) -> None:
        """Answer a subscriber's resume request. ADR-11's two branches, and no third.

        ``plan_resume`` makes the decision and this method only carries it out, which is the
        split that keeps the decision testable without a socket. It checks the epoch BEFORE the
        ring floor, because ``seq`` restarts in each epoch and a stale ``seq`` can sit
        comfortably above the current floor while naming a different position in the stream.

        Holds ``_lock`` across plan-and-send. ``Discontinuity.base_seq`` is the ring's newest
        ordinal at planning time, so a frame emitted in between would make the repaint's base
        wrong and the subscriber would double-paint the output the snapshot already contains.

        The continuity branch republishes the held frames with their ORIGINAL ordinals and does
        not re-append them: they are already in the ring, and re-appending would raise on a
        ``seq`` that does not advance -- correctly, since that would be the publisher inventing
        a second position for one frame.
        """
        asked = message.fields.get(FIELD_ASKED_SEQ)
        if not isinstance(asked, int):
            logger.warning("ignoring a resume with a non-integer %s: %r", FIELD_ASKED_SEQ, asked)
            return
        async with self._lock:
            plan = plan_resume(self._ring, from_seq=asked, epoch=message.epoch)
            if isinstance(plan, Continuity):
                logger.info(
                    "resuming session %s byte-exactly from %d (%d frames)",
                    self._session_id,
                    asked,
                    len(plan.frames),
                )
                for frame in plan.frames:
                    await self._publish(frame)
                return
            logger.info(
                "resyncing session %s: asked %d, base %d, %s",
                self._session_id,
                plan.asked_seq,
                plan.base_seq,
                plan.reason,
            )
            repaint = await self._repaint()
            frame = control_frame(
                self._session_id,
                self._seq.next(),
                self._clock(),
                resync_message(plan, repaint),
            )
            self._ring.append(frame)
            await self._publish(frame)

    async def _repaint(self) -> bytes:
        """The VISIBLE pane, with ANSI preserved. ``capture-pane -p -e`` at ``lines=0``.

        Never scrollback. ``history_limit`` is 20000 lines of ANSI, and every publisher in a
        sandbox resyncs in the same second after an edge kill -- so a scrollback repaint turns
        one reconnect storm into an outage.

        In a thread because ``capture-pane`` is a subprocess round trip, and blocking this
        publisher's loop would stall the pty pump behind it. The bytes are what tmux wrote,
        re-encoded: ``CommandResult`` decodes stdout as UTF-8 with replacement, so a pane
        holding invalid UTF-8 repaints with U+FFFD where those bytes were. Acceptable for a
        repaint, which is a picture of a rendered screen -- and specifically NOT acceptable on
        the live path, which is why that one never goes near ``str``.
        """
        read = await asyncio.to_thread(self._adapter.read, self._tmux_name, lines=0)
        return read.content.encode("utf-8")

    # -- the real attach -----------------------------------------------------------------

    def _attach_real(self) -> PtySource:
        """Resolve ownership, freeze the window, fork the client, size its viewport.

        The order is `W15`'s and it matters: freezing after the client is live costs one real
        reflow of the agent's pane. Measured self-healing (spike F16 consequence 3), but visible,
        and avoidable by doing it first -- which ``prepare_attach`` does.

        The viewport is then set to the session's CURRENT size rather than left at the pty's
        default. Without this the client renders an 80x24 viewport onto a 120x40 window and the
        subscriber sees a clipped screen -- a bug that looks like the renderer's, not the
        publisher's. It costs one ``capture-pane`` this method does not otherwise need; the
        alternative was a new tmux read path, and Principle 5 puts a new form in the spike
        first.
        """
        argv = self._adapter.prepare_attach(self._tmux_name)
        pty = AttachedPty.spawn(argv, self._adapter.attach_env())
        size = self._adapter.read(self._tmux_name, lines=0)
        if size.cols > 0 and size.rows > 0:
            pty.set_window_size(size.cols, size.rows)
        return pty
