"""T-BRIDGE -- the data path's composition, with a fake pty and a fake socket.

``attach.py`` and ``tmux.py`` are exercised for real elsewhere. What this file asserts is the
part that exists only once they are combined, and that a real pty makes *harder* to test rather
than easier: the ordering rules.

Three of them, all from ``bridge.py``'s module docstring, and each has a test here because each
fails silently:

1. **The ring is appended to whether or not a socket is live.** A publisher that only recorded
   what it managed to send has an empty ring at exactly the moment a resume asks -- which is
   after an edge kill, which is the only moment it is ever asked.
2. **Output and resume are serialized.** ``seq.Discontinuity`` warns that ``base_seq`` is stale
   the instant anything else publishes, and the pty pump and the inbound handler are separate
   tasks, so the window is real here in a way it is not in ``seq.py``'s own tests.
3. **Nothing a subscriber sends may kill the publisher.** The agent's session is the asset; a
   viewer is a guest.

The adapter is a REAL ``TmuxAdapter`` over ``conftest.RecordingRunner``, not a hand-written
double. The ceiling logic, the ``#{pane_dead}`` parsing and the ``capture-pane`` path are all
things this bridge depends on the exact behavior of, and a fake adapter would be free to drift
from them in precisely the direction that makes these tests pass.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
from collections import deque
from collections.abc import AsyncIterator
from pathlib import Path

import anyio
import pytest
from conftest import RecordingRunner, result
from shellbox_mcp import bridge as bridge_module
from shellbox_mcp.attach import COALESCE_INTERACTIVE_MAX, COALESCE_MAX
from shellbox_mcp.bridge import PtyBridge
from shellbox_mcp.errors import LineTooLong
from shellbox_mcp.tmux import TmuxAdapter, TmuxConfig
from shellbox_mcp.transport import Connected, Hello
from shellbox_transport import Frame, Stream
from shellbox_transport.codec import (
    CLOSED_DETACHED,
    CLOSED_TERMINAL_GONE,
    CONTROL_CLOSED,
    CONTROL_RESYNC,
    FIELD_BASE_SEQ,
    FIELD_REASON,
    UNORDERED_SEQ,
    ControlMessage,
    control_frame,
    decode_control,
    input_message,
    resize_message,
    resume_message,
)
from shellbox_transport.seq import REASON_BELOW_FLOOR, REASON_EPOCH_CHANGED, Epoch, RingBuffer

TMUX_NAME = "build"
# The GLOBAL wire id, deliberately different from the tmux name and deliberately
# containing the `:` that `naming.validate_session_name` rejects. Every frame assertion
# below therefore also proves the local name never reaches the wire, and every adapter
# response proves the wire id never reaches tmux.
SESSION = "itest-host:build"
INCARNATION = "11111111-2222-4333-8444-555555555555"
REPAINT = "\x1b[2J\x1b[31mred\x1b[39m pane"

# The exact format `_display_numeric` builds for a one-field read. Matched rather than guessed
# so that a change to the read path fails here instead of silently taking the `read` branch.
_PANE_DEAD_FORMAT = "#{session_name}\t#{pane_dead}"


def tmux(*, pane_dead: str = "0") -> TmuxAdapter:
    """A real adapter whose runner answers the two reads the bridge performs."""

    def respond(argv: tuple[str, ...]) -> object:
        if "capture-pane" in argv:
            return result(stdout=REPAINT)
        if "display-message" in argv and argv[-1] == _PANE_DEAD_FORMAT:
            return result(stdout=f"{TMUX_NAME}\t{pane_dead}\n")
        if "display-message" in argv:
            # `_READ_FIELDS`: width, height, pane_dead, history_size, history_limit, incarnation.
            return result(stdout=f"{TMUX_NAME}\t120\t40\t{pane_dead}\t10\t20000\t{INCARNATION}\n")
        return result(stdout=f"{TMUX_NAME}\t{INCARNATION}\n")

    return TmuxAdapter(
        TmuxConfig(socket_path="/tmp/sbx-bridge.sock"), runner=RecordingRunner(respond=respond)
    )


class FakePty:
    """A queue standing in for the pty master. ``b""`` on the queue is EOF.

    Records the read LIMITS as well as the reads, because the coalescing decision is a limit and
    nothing else -- a single ``os.read`` returns what the kernel has, so the limit is the whole
    of the policy and the only thing there is to assert.
    """

    def __init__(self, chunks: tuple[bytes, ...] = ()) -> None:
        self.queue: asyncio.Queue[bytes] = asyncio.Queue()
        for chunk in chunks:
            self.queue.put_nowait(chunk)
        self.limits: list[int] = []
        self.written: list[bytes] = []
        self.sizes: list[tuple[int, int]] = []
        self.closes = 0

    async def read(self, limit: int) -> bytes:
        self.limits.append(limit)
        return await self.queue.get()

    def write(self, data: bytes) -> None:
        self.written.append(data)

    def set_window_size(self, cols: int, rows: int) -> None:
        self.sizes.append((cols, rows))

    def close(self) -> None:
        self.closes += 1

    def feed(self, *chunks: bytes) -> None:
        for chunk in chunks:
            self.queue.put_nowait(chunk)

    def eof(self) -> None:
        self.queue.put_nowait(b"")


class FakeTransport:
    """``WSTransport``'s surface, as the bridge uses it: dial, receive, publish.

    ``sockets`` is how many times ``connect_forever`` yields before it stops yielding and simply
    waits -- which is what a real one does between an edge kill and the App coming back. A
    ``None`` pushed onto an inbox closes that socket, which is the abrupt close the edge
    performs with no close frame.
    """

    def __init__(self, sockets: int = 1) -> None:
        self.published: list[Frame] = []
        self.inboxes: list[asyncio.Queue[Frame | None]] = []
        self.dials = 0
        self.generator_closed = False
        self._sockets = sockets

    async def connect_forever(self) -> AsyncIterator[Connected]:
        try:
            while self.dials < self._sockets:
                self.dials += 1
                inbox: asyncio.Queue[Frame | None] = asyncio.Queue()
                self.inboxes.append(inbox)
                yield Connected(
                    connection=inbox,  # type: ignore[arg-type]
                    hello=Hello(session_id=SESSION, epoch=None, viewer_email=None),
                    attempt=self.dials,
                )
            await asyncio.Event().wait()
        finally:
            self.generator_closed = True

    async def receive(self, connection: object) -> AsyncIterator[Frame]:
        inbox: asyncio.Queue[Frame | None] = connection  # type: ignore[assignment]
        while True:
            item = await inbox.get()
            if item is None:
                return
            yield item

    async def publish(self, frame: Frame) -> None:
        self.published.append(frame)

    # -- test-side helpers ---------------------------------------------------------------

    def send(self, message: object, *, socket: int = 0) -> None:
        """Put one control frame on a socket's inbox, as a subscriber would."""
        self.inboxes[socket].put_nowait(
            control_frame(SESSION, UNORDERED_SEQ, 1.0, message)  # type: ignore[arg-type]
        )

    def kill(self, socket: int = 0) -> None:
        self.inboxes[socket].put_nowait(None)

    def data(self) -> list[Frame]:
        return [f for f in self.published if f.stream is Stream.STDOUT]

    def controls(self) -> list[ControlMessage]:
        return [decode_control(f.data) for f in self.published if f.stream is Stream.CONTROL]

    def resyncs(self) -> list[ControlMessage]:
        """Only the resyncs.

        Named rather than indexing with ``[-1]``, because the LAST control frame on any complete
        run is the ``closed`` one -- the stream always ends by saying how it ended. A test that
        reached for the last message would assert against that instead, and pass or fail for a
        reason unrelated to what it names.
        """
        return [m for m in self.controls() if m.kind == CONTROL_RESYNC]


def build(
    pty: FakePty,
    transport: FakeTransport,
    *,
    pane_dead: str = "0",
    ring: RingBuffer | None = None,
    epoch: Epoch | None = None,
    clock: object | None = None,
) -> PtyBridge:
    return PtyBridge(
        tmux(pane_dead=pane_dead),
        transport,  # type: ignore[arg-type]
        SESSION,
        tmux_name=TMUX_NAME,
        epoch=epoch,
        attach=lambda: pty,
        ring=ring,
        clock=clock or _ticking(),  # type: ignore[arg-type]
    )


def _ticking() -> object:
    """A clock that advances a second per call, so `t` is deterministic and ordered."""
    counter = deque([float(n) for n in range(1, 10_000)])
    return counter.popleft


async def run_until_done(bridge: PtyBridge, *, timeout: float = 5.0) -> None:
    """Drive ``run`` to completion, failing loudly rather than hanging the suite."""
    with anyio.fail_after(timeout):
        await bridge.run()


# --------------------------------------------------------------------------------------
# Output: pane bytes become frames, and the ring records them either way
# --------------------------------------------------------------------------------------


def test_the_wire_id_never_reaches_tmux_and_the_tmux_name_never_reaches_the_wire() -> None:
    """The `W23` correction, pinned. **These are two different strings and must stay so.**

    They were one parameter until a bridge was wired to a real App. ``session_id`` is the
    global ``<host_id>:<tmux_name>``, which contains a ``:`` that
    ``naming.validate_session_name`` rejects -- so passing it to an adapter raises
    ``InvalidName``, and the only way the old signature could work was to put a bare tmux name
    on the wire. Two sandboxes each holding a session called ``build`` would then collide on
    one App: the second either refused as a conflict, or REBOUND to the first's attachment and
    served a viewer somebody else's terminal. ``hello`` cannot catch it, because both sides
    agree on the string.

    Asserted in both directions because each is a separate mistake with a separate symptom: a
    wire id reaching tmux fails loudly at the adapter, while a tmux name reaching the wire
    fails silently and crosses two agents' terminals.
    """
    pty = FakePty()
    transport = FakeTransport()
    adapter = tmux()
    bridge = PtyBridge(
        adapter,
        transport,  # type: ignore[arg-type]
        SESSION,
        tmux_name=TMUX_NAME,
        attach=lambda: pty,
        clock=_ticking(),  # type: ignore[arg-type]
    )

    anyio.run(_drive, bridge, transport, pty, [b"pane output"])

    assert transport.published, "nothing was published, so neither direction is proven"
    for frame in transport.published:
        assert frame.session_id == SESSION, "a frame carried something other than the wire id"

    runner = adapter._run_command  # noqa: SLF001 -- structural assertion over the argv built
    assert isinstance(runner, RecordingRunner)
    argv_seen = [argv for argv, _ in runner.calls]
    assert argv_seen, "no tmux command ran, so the tmux direction is unproven"
    for argv in argv_seen:
        joined = " ".join(argv)
        assert SESSION not in joined, f"the WIRE id reached tmux: {joined!r}"
        assert TMUX_NAME in joined, f"a tmux command named neither: {joined!r}"


def test_each_pty_read_becomes_one_ordered_stdout_frame() -> None:
    """T-BRIDGE. The output half, at its simplest.

    Ordinals are gap-free by construction -- one ``SeqAllocator`` per epoch is the only source
    -- so this asserts they are also CONSECUTIVE from ``FIRST_SEQ``, which is the property a
    subscriber's floor comparison depends on.
    """
    pty = FakePty()
    transport = FakeTransport()
    bridge = build(pty, transport)

    anyio.run(_drive, bridge, transport, pty, [b"first", b"second", b"third"])

    assert [f.data for f in transport.data()] == [b"first", b"second", b"third"]
    assert [f.seq for f in transport.data()] == [1, 2, 3]
    assert all(f.session_id == SESSION for f in transport.data())
    assert all(f.stream is Stream.STDOUT for f in transport.data())


def test_frames_are_ringed_even_while_no_socket_is_live() -> None:
    """T-BRIDGE. Rule 1, and the reason the byte-exact branch is reachable at all.

    The edge kills every open socket in the same second and the pane keeps writing during the
    sub-second re-dial. A publisher that recorded only what it managed to SEND would hold
    nothing at the one moment a resume asks -- so ADR-11's continuity branch would be dead code,
    and it would be dead silently.
    """
    pty = FakePty((b"into the void", b"and more", b""))
    transport = FakeTransport(sockets=0)
    bridge = build(pty, transport)

    anyio.run(run_until_done, bridge)

    assert transport.published == [], "there was never a socket to send on"
    assert [f.data for f in bridge.ring.frames_from(1)][:2] == [b"into the void", b"and more"]


def test_the_read_limit_tightens_after_input_and_relaxes_again() -> None:
    """T-BRIDGE. The coalescing decision, observed at the pty rather than in isolation.

    ``read_limit`` is unit-tested in ``test_attached_pty.py``. What this adds is that the bridge
    actually feeds it the moment of the last keystroke -- the wiring, which is the part that
    silently does nothing if ``_last_input_at`` is never set.
    """
    pty = FakePty()
    transport = FakeTransport()
    # A FROZEN clock, not the file's ticking one. That advances a second per call, which puts
    # every read outside a 0.75 s window by construction -- so this test would assert the
    # tightening never happens and pass for the wrong reason.
    bridge = build(pty, transport, clock=lambda: 1000.0)

    async def scenario() -> None:
        task = asyncio.create_task(bridge.run())
        await _settle()
        pty.feed(b"before")
        await _settle()
        bridge.send_input(b"x")
        pty.feed(b"after")
        await _settle()
        pty.eof()
        with anyio.fail_after(5.0):
            await task

    anyio.run(scenario)

    assert pty.limits[0] == COALESCE_MAX, "nobody had typed yet"
    assert COALESCE_INTERACTIVE_MAX in pty.limits, "the read after a keystroke must tighten"


# --------------------------------------------------------------------------------------
# Input: byte-exact, behind the shipped ceiling
# --------------------------------------------------------------------------------------


def test_input_reaches_the_pty_byte_exactly() -> None:
    """T-BRIDGE. No allowlist, no re-encoding -- the bridge writes exactly what it was given.

    ``keys.py``'s allowlist is closed by construction, so an application-mode keystroke it does
    not name is unreachable through the tool surface. This path names nothing and passes bytes
    through untouched.

    Byte-exact TO THE PTY, which is the only thing this layer controls. tmux parses its client's
    input into keys before forwarding, and measured against 3.6b it consumes a bracketed-paste
    wrapper while passing the text inside it --
    ``tests/tmux/test_attach_pty.py::test_a_bracketed_paste_wrapper_is_consumed_by_the_tmux_client``
    pins that. The payload here carries the wrapper deliberately, so the two tests together say
    where the transformation happens: not here.
    """
    pty = FakePty()
    bridge = build(pty, FakeTransport())
    bridge.attach()
    payload = b"\x1b[200~ls -la ; echo done\x1b[201~\r"

    bridge.send_input(payload)

    assert pty.written == [payload]


def test_an_over_ceiling_line_is_rejected_and_nothing_is_written() -> None:
    """T-INPUT-LINE-CEILING (unit half). REJECT, and reject the WHOLE write.

    MEASURED on this exact path (spike F18): 8192 bytes plus a newline through a live attach
    client delivered **4096 bytes on Linux, silently truncated**, and 0 on macOS. A truncated
    command is a different, still-executable command.

    Rejection is the only implementable branch. Chunking cannot help -- the truncation is the
    kernel's ``MAX_CANON`` line buffer, not a write-size limit -- and no tmux format exposes the
    pane pty's termios, so this process cannot know whether the pane is in canonical mode.

    ``pty.written == []`` is the load-bearing half. Writing a prefix and then raising would
    deliver exactly the truncated command the ceiling exists to prevent.
    """
    pty = FakePty()
    bridge = build(pty, FakeTransport())
    bridge.attach()

    with pytest.raises(LineTooLong):
        bridge.send_input(b"x" * 1000 + b"\n")

    assert pty.written == []


def test_the_ceiling_counts_bytes_since_the_last_newline_not_the_total() -> None:
    """T-INPUT-LINE-CEILING. H4 is a per-LINE hazard, so a long multi-line paste is fine.

    This matters for the real case: a browser paste of a shell snippet is many short lines, and
    a total-bytes ceiling would refuse it for no reason.
    """
    pty = FakePty()
    bridge = build(pty, FakeTransport())
    bridge.attach()
    payload = (b"a" * 100 + b"\n") * 50

    bridge.send_input(payload)

    assert pty.written == [payload]
    assert len(payload) > 1000, "the total must exceed the per-line ceiling for this to mean it"


def test_the_ceiling_is_the_one_the_tool_path_uses() -> None:
    """T-INPUT-LINE-CEILING. One ceiling, one error class, one implementation.

    F18's action was explicit that the pty path reuses ``max_send_line_bytes`` rather than
    introducing a third number at 4096. Asserted by driving both paths through the same adapter
    and requiring them to agree at the boundary, so a change to either is a change to both.
    """
    pty = FakePty()
    adapter = tmux()
    bridge = PtyBridge(adapter, FakeTransport(), SESSION, tmux_name=TMUX_NAME, attach=lambda: pty)  # type: ignore[arg-type]
    bridge.attach()
    limit = adapter.config.max_send_line_bytes

    bridge.send_input(b"y" * (limit - 1))
    with pytest.raises(LineTooLong):
        bridge.send_input(b"y" * limit)

    assert pty.written == [b"y" * (limit - 1)], "the limit is the first REJECTED length"


def test_an_empty_input_is_a_no_op_rather_than_an_error() -> None:
    pty = FakePty()
    bridge = build(pty, FakeTransport())
    bridge.attach()
    bridge.send_input(b"")

    assert pty.written == []


# --------------------------------------------------------------------------------------
# Resize
# --------------------------------------------------------------------------------------


def test_a_resize_control_frame_reaches_the_pty() -> None:
    """T-BRIDGE. ``TIOCSWINSZ`` on the master, with no tmux round trip -- ADR-9's driver 4.

    What moves is the CLIENT's viewport. The agent's window does not follow, because
    ``prepare_attach`` froze it first -- measured to hold 80x24 through a 120x40 client over
    1714 samples (spike F16).
    """
    pty = FakePty()
    transport = FakeTransport()
    bridge = build(pty, transport)

    anyio.run(_drive, bridge, transport, pty, [resize_message(Epoch.new(), 132, 43)])

    assert pty.sizes == [(132, 43)]


def test_a_resize_naming_a_stale_epoch_is_still_applied() -> None:
    """T-BRIDGE. A viewer's window is that size whichever attach it learned the epoch from.

    Refusing would leave the browser rendering at the wrong size until it happened to reconnect
    again, which is a worse outcome than applying a resize that is one epoch late.
    """
    pty = FakePty()
    transport = FakeTransport()
    bridge = build(pty, transport, epoch=Epoch.new())

    anyio.run(_drive, bridge, transport, pty, [resize_message(Epoch.new(), 100, 30)])

    assert pty.sizes == [(100, 30)]


# --------------------------------------------------------------------------------------
# Resume -- ADR-11's two branches, and no third
# --------------------------------------------------------------------------------------


def test_a_resume_inside_the_ring_replays_the_original_frames_byte_exactly() -> None:
    """T-BRIDGE. The continuity branch: same bytes, same ordinals, no repaint.

    The ordinals must be the ORIGINALS. Re-allocating would present one frame at two positions,
    and a subscriber that had already rendered the first would render it again at the second.
    """
    epoch = Epoch.new()
    pty = FakePty()
    transport = FakeTransport()
    bridge = build(pty, transport, epoch=epoch)

    anyio.run(
        _drive,
        bridge,
        transport,
        pty,
        [b"one", b"two", b"three", resume_message(2, epoch)],
    )

    # Everything after the three originals, minus the `closed` frame every complete run ends on.
    replayed = [f for f in transport.published[3:] if f.stream is Stream.STDOUT]
    assert [f.seq for f in replayed] == [2, 3]
    assert [f.data for f in replayed] == [b"two", b"three"]
    assert all(f.stream is Stream.STDOUT for f in replayed), "continuity sends no resync"


def test_a_resume_below_the_floor_gets_a_resync_carrying_the_visible_pane() -> None:
    """T-BRIDGE. The honest branch. A declared gap plus the state to recover from it.

    The repaint is the VISIBLE pane and never scrollback: ``history_limit`` is 20000 lines of
    ANSI, and every publisher in a sandbox resyncs in the same second after an edge kill, so a
    scrollback repaint turns one reconnect storm into an outage.
    """
    epoch = Epoch.new()
    pty = FakePty()
    transport = FakeTransport()
    # A ring that holds one frame, so the second output evicts the first.
    bridge = build(pty, transport, epoch=epoch, ring=RingBuffer(epoch, max_frames=1))

    anyio.run(_drive, bridge, transport, pty, [b"evicted", b"kept", resume_message(1, epoch)])

    (resync,) = transport.resyncs()
    assert resync.fields[FIELD_REASON] == REASON_BELOW_FLOOR
    assert resync.fields[FIELD_BASE_SEQ] == 2
    assert resync.payload == REPAINT.encode()


def test_a_resume_from_a_previous_epoch_resyncs_even_though_its_seq_is_in_range() -> None:
    """T-BRIDGE. The ordering property, which is the one that silently delivers a hole.

    ``seq`` restarts in every epoch, so ``seq=1`` from a previous attach sits comfortably above
    this ring's floor while naming a completely different position in the stream. A publisher
    that checked the floor first would report continuity and hand over the wrong bytes -- and
    that failure gets diagnosed as a renderer bug.
    """
    pty = FakePty()
    transport = FakeTransport()
    bridge = build(pty, transport, epoch=Epoch.new())

    anyio.run(_drive, bridge, transport, pty, [b"a", b"b", resume_message(1, Epoch.new())])

    (resync,) = transport.resyncs()
    assert resync.fields[FIELD_REASON] == REASON_EPOCH_CHANGED


def test_a_fresh_subscriber_holding_nothing_gets_a_repaint() -> None:
    """T-BRIDGE. ``from_seq=0`` with no epoch. ``FIRST_SEQ`` is 1, so no floor can satisfy it.

    That resolution is deliberate rather than incidental: a viewer opening a tab wants the
    screen as it is, not the stream from whenever the publisher happened to start.
    """
    pty = FakePty()
    transport = FakeTransport()
    bridge = build(pty, transport)

    anyio.run(_drive, bridge, transport, pty, [b"output", resume_message(0)])

    assert len(transport.resyncs()) == 1


def test_a_resync_takes_an_ordinal_and_enters_the_ring() -> None:
    """T-BRIDGE. "The stream restarted here" is part of the stream.

    A control frame carries a ``seq`` from the same allocator as the data around it, so a
    renderer can order the restart against the output it is holding -- which a side channel
    could not answer. And it is ringed, so a second resume after it stays continuous.
    """
    epoch = Epoch.new()
    pty = FakePty()
    transport = FakeTransport()
    bridge = build(pty, transport, epoch=epoch)

    anyio.run(_drive, bridge, transport, pty, [b"a", resume_message(0)])

    resync_frame = [f for f in transport.published if f.stream is Stream.CONTROL][0]
    assert resync_frame.seq == 2, "the data frame took 1; the resync takes the next"
    assert [f.seq for f in bridge.ring.frames_from(2)][0] == 2, "and it is held for a later resume"


# --------------------------------------------------------------------------------------
# The end of the stream: detach versus a dead pane
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("pane_dead", "expected"),
    [("0", CLOSED_DETACHED), ("1", CLOSED_TERMINAL_GONE)],
    ids=["detach", "process-exited"],
)
def test_the_end_of_the_stream_names_which_way_it_ended(pane_dead: str, expected: str) -> None:
    """T-ATTACH-DETACH (unit half). The two must never collapse.

    ``terminal_gone`` tells a viewer to stop reconnecting. ``detached`` must not, because a
    detach misread as terminal-gone tears down a session that is still running.

    ``has-session`` cannot tell them apart -- shellbox sets ``remain-on-exit on`` globally, so a
    session deliberately outlives its pane's process. ``#{pane_dead}`` can, measured in both
    directions with a live client attached (spike F19).
    """
    pty = FakePty()
    transport = FakeTransport()

    anyio.run(_drive, build(pty, transport, pane_dead=pane_dead), transport, pty, [b"last output"])

    closed = transport.controls()[-1]
    assert closed.kind == CONTROL_CLOSED
    assert closed.fields[FIELD_REASON] == expected


def test_a_session_that_no_longer_resolves_reads_as_terminal_gone() -> None:
    """T-ATTACH-DETACH. The conservative direction of the two.

    A session that is gone certainly has nothing left to watch. Reporting it as a detach would
    leave a viewer reconnecting to a session that will never come back.
    """
    pty = FakePty()
    transport = FakeTransport()
    adapter = TmuxAdapter(
        TmuxConfig(socket_path="/tmp/sbx-bridge.sock"),
        runner=RecordingRunner(default=result(stdout="")),
    )
    bridge = PtyBridge(adapter, transport, SESSION, tmux_name=TMUX_NAME, attach=lambda: pty)  # type: ignore[arg-type]

    anyio.run(_drive, bridge, transport, pty, [])

    assert transport.controls()[-1].fields[FIELD_REASON] == CLOSED_TERMINAL_GONE


def test_the_pty_is_closed_and_the_dial_loop_shut_down_when_run_returns() -> None:
    """T-BRIDGE. An orphaned attach child is PM3's reflow made permanent.

    Both halves matter. The pty close reaps the child; the generator close releases the socket
    the dial loop was suspended on, which the App would otherwise go on counting as a live
    publisher.
    """
    pty = FakePty()
    transport = FakeTransport()

    anyio.run(_drive, build(pty, transport), transport, pty, [])

    assert pty.closes == 1
    assert transport.generator_closed


def test_the_pty_is_closed_even_when_the_transport_gives_up() -> None:
    """T-BRIDGE. ``run``'s ``finally``, on the path nobody writes a test for.

    A terminal transport failure is the case where the publisher is going away for good, so it
    is exactly the case where a leaked tmux client would never be cleaned up by anything.
    """
    pty = FakePty()

    class Failing(FakeTransport):
        async def connect_forever(self) -> AsyncIterator[Connected]:
            raise RuntimeError("authentication failed twice")
            yield  # pragma: no cover - makes this a generator

    bridge = build(pty, Failing())

    with pytest.raises(RuntimeError, match="authentication failed"):
        anyio.run(run_until_done, bridge)

    assert pty.closes == 1


def test_close_is_idempotent_and_safe_after_run_returned() -> None:
    """``W19b``'s shutdown path calls this from the main thread, possibly after ``run`` ended."""
    pty = FakePty()
    transport = FakeTransport()
    bridge = build(pty, transport)

    anyio.run(_drive, bridge, transport, pty, [])
    bridge.close()

    assert pty.closes == 1


# --------------------------------------------------------------------------------------
# Rule 3: a guest must not be able to end an agent's stream
# --------------------------------------------------------------------------------------


def test_an_over_ceiling_paste_is_refused_without_killing_the_publisher() -> None:
    """T-BRIDGE. Rule 3, on the failure a viewer can actually trigger by pasting.

    The refusal reaches the operator's log and not the viewer's screen -- naming it in-band
    needs a control code outside the closed set ``shellbox_app.server`` documents, and the
    renderer that would display it is Phase 4's. What must not happen is the stream ending.
    """
    pty = FakePty()
    transport = FakeTransport()
    bridge = build(pty, transport)

    anyio.run(
        _drive,
        bridge,
        transport,
        pty,
        [input_message(b"z" * 5000 + b"\n"), b"still alive"],
    )

    assert pty.written == [], "the oversized line must not reach the pty"
    assert [f.data for f in transport.data()] == [b"still alive"]


@pytest.mark.parametrize(
    "hostile",
    [
        Frame(session_id=SESSION, seq=0, t=1.0, stream=Stream.CONTROL, data=b"not json\npayload"),
        Frame(session_id=SESSION, seq=0, t=1.0, stream=Stream.CONTROL, data=b""),
        Frame(session_id=SESSION, seq=0, t=1.0, stream=Stream.STDOUT, data=b"a data frame"),
    ],
    ids=["undecodable-control", "empty-control", "data-frame-inbound"],
)
def test_a_hostile_inbound_frame_is_dropped_and_the_stream_continues(hostile: Frame) -> None:
    """T-BRIDGE. Rule 3. The agent's session is the asset; a viewer is a guest."""
    pty = FakePty()
    transport = FakeTransport()
    bridge = build(pty, transport)

    anyio.run(_drive, bridge, transport, pty, [hostile, b"survived"])

    assert [f.data for f in transport.data()] == [b"survived"]


def test_an_unknown_control_kind_is_ignored_rather_than_fatal() -> None:
    """T-BRIDGE. Forward compatibility: a Phase 4 renderer may speak more than this publisher."""
    pty = FakePty()
    transport = FakeTransport()
    bridge = build(pty, transport)
    unknown = control_frame(
        SESSION, UNORDERED_SEQ, 1.0, decode_control(b'{"epoch":null,"kind":"from-the-future"}\n')
    )

    anyio.run(_drive, bridge, transport, pty, [unknown, b"survived"])

    assert [f.data for f in transport.data()] == [b"survived"]


def test_a_resume_with_a_non_integer_seq_is_ignored() -> None:
    """T-BRIDGE. ``plan_resume`` never raises on subscriber input; neither may the wire layer."""
    pty = FakePty()
    transport = FakeTransport()
    bridge = build(pty, transport)
    malformed = decode_control(b'{"asked_seq":"lots","epoch":null,"kind":"resume"}\n')

    anyio.run(_drive, bridge, transport, pty, [malformed, b"survived"])

    assert transport.resyncs() == [], "a malformed resume must not produce an answer"
    assert [f.data for f in transport.data()] == [b"survived"]


# --------------------------------------------------------------------------------------
# Rule 2: the ordering the lock exists for
# --------------------------------------------------------------------------------------


def test_output_and_resume_are_serialized_by_the_same_lock() -> None:
    """T-BRIDGE. Rule 2, asserted structurally, because the race it prevents is not schedulable.

    ``seq.Discontinuity`` carries the warning: ``base_seq`` is the ring's newest ordinal at
    planning time, so a frame published between planning a discontinuity and sending its resync
    makes the repaint's base wrong and the subscriber double-paints whatever the snapshot
    already contained.

    A behavioral test cannot prove the absence of an interleaving -- it can only fail to find
    one on the schedules it happens to hit, which is the failure mode that makes concurrency
    bugs ship. So this asserts the mechanism: both the emit path and the resume path take
    ``_lock``, and it counts what it validated so a refactor that renames the lock fails loudly
    rather than passing vacuously.
    """
    source = ast.parse(Path(inspect.getsourcefile(bridge_module) or "").read_text())
    guarded = set()
    for node in ast.walk(source):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        for inner in ast.walk(node):
            if isinstance(inner, ast.AsyncWith) and "self._lock" in ast.unparse(
                inner.items[0].context_expr
            ):
                guarded.add(node.name)

    assert guarded == {"_pump_pty", "_announce_closed", "_resume"}, (
        f"the emit and resume paths must both hold _lock; found {guarded}"
    )


def test_no_frame_is_published_between_planning_a_resync_and_sending_it() -> None:
    """T-BRIDGE. Rule 2's consequence, asserted on the wire.

    The resync's ``base_seq`` must name the frame immediately before it, with nothing in
    between. Output is streamed continuously here while the resume arrives, so a missing lock
    shows up as a ``base_seq`` that is already behind by the time the resync lands.
    """
    epoch = Epoch.new()
    pty = FakePty()
    transport = FakeTransport()
    bridge = build(pty, transport, epoch=epoch, ring=RingBuffer(epoch, max_frames=2))

    async def scenario() -> None:
        task = asyncio.create_task(bridge.run())
        await _settle()
        for index in range(20):
            pty.feed(f"chunk-{index}".encode())
        transport.send(resume_message(1, epoch))
        await _settle()
        pty.eof()
        with anyio.fail_after(5.0):
            await task

    anyio.run(scenario)

    published = transport.published
    resync_index = next(i for i, f in enumerate(published) if f.stream is Stream.CONTROL)
    resync = decode_control(published[resync_index].data)
    assert resync.kind == CONTROL_RESYNC
    assert resync.fields[FIELD_BASE_SEQ] == published[resync_index].seq - 1, (
        "a frame slipped between planning the discontinuity and sending it, so the repaint's "
        "base is stale and the subscriber will double-paint"
    )


# --------------------------------------------------------------------------------------
# Reconnect
# --------------------------------------------------------------------------------------


def test_the_pty_survives_a_socket_dying_and_the_epoch_does_not_change() -> None:
    """T-BRIDGE. The edge kill is not the attach dying, and the difference is the whole design.

    Rebuilding the attach on every kill would reflow the agent's window every ten minutes and
    restart ``seq`` four times an hour -- turning a reconnect the subscriber could resume
    through into a guaranteed repaint. So ``connect_forever`` re-dials INSIDE ``run``, the pty
    is untouched, and the ordinals keep counting.
    """
    epoch = Epoch.new()
    pty = FakePty()
    transport = FakeTransport(sockets=2)
    bridge = build(pty, transport, epoch=epoch)

    async def scenario() -> None:
        task = asyncio.create_task(bridge.run())
        await _settle()
        pty.feed(b"before")
        await _settle()
        transport.kill(socket=0)
        await _settle()
        pty.feed(b"after")
        await _settle()
        pty.eof()
        with anyio.fail_after(5.0):
            await task

    anyio.run(scenario)

    assert transport.dials == 2, "the socket died, so the publisher re-dialled"
    assert pty.closes == 1, "the attach was NOT rebuilt; it was closed once, at the end"
    assert bridge.epoch == epoch, "one epoch per attach, not per socket"
    assert [f.seq for f in transport.data()] == [1, 2], "the ordinals kept counting"


# --------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------


async def _settle() -> None:
    """Yield until the bridge's tasks have quiesced.

    Several passes rather than one: a chunk crosses the pty queue, the pump frames it, the
    publish awaits, and the inbound handler may then run -- each is its own suspension point.
    """
    for _ in range(20):
        await asyncio.sleep(0)


async def _drive(
    bridge: PtyBridge,
    transport: FakeTransport,
    pty: FakePty,
    script: list[object],
) -> None:
    """Run the bridge while feeding a script of pane output (``bytes``) and inbound messages.

    One helper for the whole file so that every test drives the bridge the same way, and so the
    interleaving is explicit: each item is delivered and allowed to settle before the next.
    """
    task = asyncio.create_task(bridge.run())
    await _settle()
    for item in script:
        if isinstance(item, bytes):
            pty.feed(item)
        elif isinstance(item, Frame):
            transport.inboxes[0].put_nowait(item)
        else:
            transport.send(item)
        await _settle()
    pty.eof()
    with anyio.fail_after(5.0):
        await task
