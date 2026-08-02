"""The transport protocol: ``Frame``, ``Stream``, and ``FrameTransport``.

This package is **pure**. No I/O, no tmux, no web framework, and no runtime dependencies.
Both halves of the transport import it, so anything added here is installed in both: the
sandbox half in ``packages/shellbox-mcp``, and the App half that serves the browser. The
socket, the pty, and every ``tmux`` argv live with their consumer, never here.

That split is mechanical, not aesthetic. An earlier revision put the client and the server
I/O halves in this package too. It does not work: the detach-versus-dead probe needs a
``#{pane_dead}`` read, every ``-t`` in this repo must come from ``target()`` in
``packages/shellbox-mcp/src/shellbox_mcp/target.py``, and satisfying that from here means
importing ``shellbox_mcp`` -- which is the import cycle the separate package existed to
prevent. Splitting on purity deletes the cycle instead of routing around it.

WARNING: **Nothing here may read a clock, a socket, or the environment.** Timestamps and
ordinals are supplied by the caller. The two structural guards this repo already ships --
``tests/unit/test_target.py`` and ``tests/unit/test_no_global_window_size.py`` -- glob
``packages/**``, so this package inherits both. That is correct. Do not exempt it.

Three modules, and the layering between them is one-directional:

* This module defines ``Frame``, ``Stream``, and the ``FrameTransport`` protocol.
* ``seq.py`` decides: ``Epoch``, ``SeqAllocator``, ``RingBuffer``, and ``plan_resume`` --
  which of the two resume branches a request gets, and why.
* ``codec.py`` encodes: the wire form of a frame, and the ``control``-stream messages both
  halves must agree on.

``codec.py`` imports ``seq.py``; ``seq.py`` imports this module; nothing imports backwards.
The reason to keep it that way: a resume *decision* must be testable without constructing a
wire frame, because the decision is the part that can silently hand a subscriber a hole.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from enum import IntEnum
from typing import Protocol

__all__ = ["Frame", "FrameTransport", "Stream"]


class Stream(IntEnum):
    """Which stream a frame belongs to.

    NORMATIVE: the wire values are fixed. A subscriber written against one release must
    read frames from another.

    ``CONTROL`` is not a side channel. It is in-band and carries a ``seq`` like any other
    frame, because the ordering between "the pane wrote these bytes" and "the stream
    restarted here" is exactly what a renderer needs and exactly what a side channel loses.

    The numbering starts at 1 on purpose. A zeroed or truncated header decodes its stream
    byte as 0, so 0 must not name a real stream -- otherwise damaged input arrives as a
    plausible ``stdout`` frame instead of a decode error.
    """

    STDOUT = 1
    STDERR = 2
    CONTROL = 3


@dataclass(frozen=True, slots=True)
class Frame:
    """One frame. Immutable, and the unit of everything else in this package.

    NORMATIVE: five fields, in this order. The epic fixed them, and the count is a
    boundary rather than a default. In particular the attach **epoch** is NOT a sixth
    field: it travels in the payload of ``control``-stream frames, using the ``stream``
    field that already exists. ``codec.ControlMessage`` takes it as a required field, which
    is why that is enough.

    The one deviation from the epic's spelling: the payload field is ``data``, not
    ``bytes``. A dataclass field named ``bytes`` and annotated ``bytes`` shadows the
    builtin inside the class body, which mypy rejects outright. The field order and count
    are unchanged.
    """

    session_id: str
    seq: int
    """The frame's ordinal within one attach epoch. Monotonic and gap-free, allocated by
    ``SeqAllocator`` in ``seq.py``.

    WARNING: ``seq`` is meaningless across epochs -- it restarts at ``seq.FIRST_SEQ`` in
    each one. Comparing a ``seq`` from one epoch against a ring floor from another reports
    success and then delivers a hole. ``seq.plan_resume`` checks the epoch before it checks
    the floor for exactly this reason."""
    t: float
    """Wall-clock Unix seconds, from the publisher, for display and diagnostics only.

    Never for ordering. ``seq`` orders the stream; a wall clock can step backwards, and an
    NTP correction mid-attach would then reorder a terminal's escape stream."""
    stream: Stream
    data: bytes
    """The payload, byte-exact. Never text.

    A decode-and-re-encode round trip through ``str`` is not lossless in the way it looks:
    a multi-byte UTF-8 character split across two frames has no valid decoding in either
    half. The codec therefore treats this field as opaque bytes end to end, and
    ``tests/unit/test_frame_codec.py`` asserts the split case directly."""


class FrameTransport(Protocol):
    """The interface both halves implement: publish frames, subscribe to them, send input.

    Lifecycle is deliberately absent. There is no ``connect``, ``reconnect``, or ``close``
    here, because reconnect is this transport's steady state -- the Databricks Apps edge
    kills every open socket on a wall clock roughly every 10-18 minutes, measured by
    ``probe/FINDINGS.md``. A caller that could observe or trigger a re-dial would start
    branching on it. Recovery belongs to the implementation, and a subscriber learns about
    it the one way that is safe: from a ``control`` frame.

    Resize is likewise not a method. A viewer resize arrives as a ``control`` frame
    (``codec.resize_message``), so it stays ordered against the byte stream it reflows.
    """

    async def publish(self, frame: Frame) -> None:
        """Send one frame to every current subscriber.

        The publisher owns the ``seq``: it allocates from a single ``SeqAllocator`` per
        epoch, so frames are gap-free by construction rather than by check.
        """
        ...

    def subscribe(
        self, session_id: str, from_seq: int, *, epoch: str | None = None
    ) -> AsyncIterator[Frame]:
        """Stream frames for ``session_id``, resuming at ``from_seq``. Two guarantees.

        They are stated separately so that neither can be weakened without deleting a
        sentence, and so that no reader infers byte-exact replay from this signature.

        1. **Continuity.** If ``epoch`` names the publisher's current epoch AND
           ``from_seq`` is at or above the publisher's ring floor, every frame from
           ``from_seq`` onward arrives, in order, byte-exact.
        2. **Honesty.** Otherwise the FIRST thing this iterator yields is a ``control``
           frame that names the gap and carries a repaint, then live frames from a new
           base. **A subscriber is never handed a stream with an undeclared hole.**

        There is no third branch, and specifically no gap-fill. Filling a gap needs a
        durable frame log, which is the ``session_frames`` table that D7 rules out and that
        ``packages/shellbox-registry/src/shellbox_registry/models.py`` records as already
        rejected once by review. What survives instead is a repaint from ``tmux capture-pane
        -p -e``, which is what ``docs/architecture.md`` commits to: subscribe "repaints from
        ``capture-pane``; it cannot gap-fill".

        Why guarantee 2 is worth this much prose: a terminal is a state machine over an
        escape stream. A dropped run of bytes does not lose a line, it desynchronizes the
        parser, and the renderer then paints plausible garbage with no indication that
        anything was lost. That failure gets diagnosed as "xterm.js is buggy" and costs
        days. A declared discontinuity costs one repaint.

        ``epoch`` is the string that travelled on the wire, shape-checked by
        ``seq.Epoch.parse``. It is keyword-only and optional, and omitting it is safe: an
        absent epoch cannot match the publisher's, so the request takes the honest branch.
        It exists because a floor comparison without it is unsound -- see the warning on
        ``Frame.seq``. Pass ``0`` as ``from_seq`` to mean "I hold nothing", which also
        resolves to the honest branch. That is the correct answer for a fresh subscriber.
        """
        ...

    async def send_input(self, session_id: str, data: bytes) -> None:
        """Deliver ``data`` to the session's pty, byte-exact.

        Not a frame: input carries no ``seq``, because the publisher's ordinals describe
        what the pane emitted and a keystroke is not that. What the pane echoes in response
        arrives as an ordinary ``stdout`` frame.
        """
        ...
