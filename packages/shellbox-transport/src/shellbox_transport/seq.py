"""Ordinals, epochs, the publisher's ring, and the resume decision.

This module answers one question: **when a subscriber asks to resume at ``from_seq``, can
the publisher serve that byte-exactly, and if not, what must it say instead?**
``plan_resume`` is that answer, and it has exactly two outcomes -- ``Continuity`` or
``Discontinuity``. There is no third.

Why ``seq`` is scoped to an epoch, and why it is deliberately not durable. A durable ``seq``
needs a frame log, which is the ``session_frames`` table that D7 rules out and that
``packages/shellbox-registry/src/shellbox_registry/models.py`` records as already rejected
once by review. So an attach mints a uuid4 **epoch**, ``seq`` is monotonic within it, and
``seq`` restarts in the next one. That mirrors ``@shellbox_incarnation`` in
``packages/shellbox-mcp/src/shellbox_mcp/tmux.py`` exactly, including what it buys: post-hoc
misdelivery DETECTION, never prevention.

**The epoch is published, and that is safe for a specific, checkable reason.** It travels on
``control``-stream frames, so a subscriber does read it. This does not reintroduce the hazard
that invariant 7 ("zero in-process session state") exists to prevent, because a subscriber's
only permitted use of an epoch is **pessimistic**: an unfamiliar epoch means "distrust
everything, repaint". Misreading one can cost an unnecessary repaint. It can never yield a
stale answer, which is the failure invariant 7 guards -- one process answering from state
another process already invalidated. The dependency is fail-safe and one-directional.

CRITICAL: ``tests/integration/test_no_session_state.py``'s transport check therefore asserts
that the **ring** is unreachable from the server's attributes and from every tool closure. It
must NOT be extended to the epoch. Asserting the epoch is unreachable would assert the
opposite of the design.

The ring itself is not session state either, and the distinction is load-bearing rather than
semantic: the ring answers "what did I send down this socket", never "does this session
exist" or "what are its dimensions". It is read by no other process, and it dies with the
process that holds it.

WARNING: **The continuity branch may be deleted.** ``docs/architecture.md`` commits only to
"repaints from ``capture-pane``; it cannot gap-fill", and this module is more generous than
that. If the live run in a real sandbox shows the byte-exact branch taken in fewer than 20%
of observed reconnects, the ring and the floor check go and ``subscribe`` always resyncs --
which lands exactly on the committed position. Keep the two branches separable so that
removal stays a deletion rather than a rewrite.
"""

from __future__ import annotations

import re
import uuid
from collections import deque
from dataclasses import dataclass

from shellbox_transport import Frame

__all__ = [
    "DEFAULT_RING_BYTES",
    "DEFAULT_RING_FRAMES",
    "FIRST_SEQ",
    "REASON_AHEAD_OF_PUBLISHER",
    "REASON_BELOW_FLOOR",
    "REASON_EPOCH_CHANGED",
    "REASON_NO_EPOCH",
    "Continuity",
    "Discontinuity",
    "Epoch",
    "RingBuffer",
    "Resume",
    "SeqAllocator",
    "plan_resume",
]

# The first ordinal in every epoch. 1, not 0, so that 0 is free to mean "I hold nothing,
# send me everything" -- a claim no ring floor can satisfy, so it resolves to the honest
# branch, which is the correct answer for a fresh subscriber.
FIRST_SEQ = 1

# Ring defaults: 1 MiB or 512 frames, whichever binds first. Both are configurable, and
# `packages/shellbox-mcp/src/shellbox_mcp/config.py` is the one place the environment is read.
#
# Sized against the reconnect GAP, not against replay. The edge kill is instantaneous and a
# re-dial is sub-second, so the ring must cover the bytes a pane emits while the publisher
# notices the death and reconnects -- seconds, not the 10-18 minute kill cadence. Sizing it
# for replay is the frame log by another name.
DEFAULT_RING_BYTES = 1 << 20
DEFAULT_RING_FRAMES = 512

# Why a discontinuity happened, as a closed set. A subscriber does not branch on these -- its
# response to all four is the same repaint -- but an operator reading a log does, because
# `epoch_changed` is a publisher restart and `below_floor` is a slow reconnect. Those have
# different fixes, and the live run has to report which one it saw to decide whether the
# continuity branch survives at all.
REASON_NO_EPOCH = "no_epoch"
REASON_EPOCH_CHANGED = "epoch_changed"
REASON_BELOW_FLOOR = "below_floor"
REASON_AHEAD_OF_PUBLISHER = "ahead_of_publisher"

# shellbox only ever mints a uuid4 here, so a value read back off the wire is shape-checked
# rather than merely tested for emptiness -- the same discipline `_INCARNATION_RE` applies in
# `tmux.py`, and for the same reason. A value that can be truncated or padded into looking
# equal to another defeats the detection this mechanism exists for.
_EPOCH_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")


@dataclass(frozen=True, slots=True)
class Epoch:
    """One attach's identity. A uuid4, minted when the attach starts.

    Construct through ``new()`` or ``parse()``. The constructor does not validate, because
    ``parse`` is where a value crossing a trust boundary belongs and a second check inside
    ``__post_init__`` would make the two disagree about what "valid" means.
    """

    value: str

    @classmethod
    def new(cls) -> Epoch:
        """Mint a fresh epoch. One per attach, never reused."""
        return cls(str(uuid.uuid4()))

    @classmethod
    def parse(cls, raw: str) -> Epoch:
        """Shape-check a value read off the wire. Raises ``ValueError`` if it is not a uuid4.

        Raising rather than returning ``None`` is deliberate here, and it differs from
        ``_own_incarnation`` in ``tmux.py`` on purpose. That function has a legitimate
        "absent" case -- an unstamped session -- and reports it. A frame that reached this
        point already claims to carry an epoch, so a malformed one is a protocol violation
        and the caller must not proceed as though it read a value.

        The caller's outer handler still treats a raised ``ValueError`` the same way it
        treats an unfamiliar epoch: distrust everything, repaint. That is the pessimistic
        rule, and it applies to "I could not read it" as much as to "I do not know it".
        """
        if not _EPOCH_RE.match(raw):
            raise ValueError(f"epoch {raw!r} is not a uuid4")
        return cls(raw)

    def __str__(self) -> str:
        return self.value


class SeqAllocator:
    """Monotonic, gap-free ordinals within one epoch.

    Gap-free **by construction**: there is one allocator per epoch and it is the only source
    of ordinals, so a gap cannot be produced and therefore does not need to be detected. A
    subscriber that sees the same epoch with a ``seq`` jump has found a bug in the publisher,
    which is a usable invariant rather than a tolerated one.

    Not thread-safe, and it does not need to be. One publisher owns one session (a second
    publisher for a live session is an error, not a fan-in case), and it allocates from its
    own thread.
    """

    __slots__ = ("_epoch", "_next")

    def __init__(self, epoch: Epoch) -> None:
        self._epoch = epoch
        self._next = FIRST_SEQ

    @property
    def epoch(self) -> Epoch:
        return self._epoch

    @property
    def issued(self) -> int:
        """The last ordinal handed out, or ``FIRST_SEQ - 1`` before the first call."""
        return self._next - 1

    def next(self) -> int:
        """Return the next ordinal and advance. The only way to obtain a ``seq``."""
        seq = self._next
        self._next += 1
        return seq


class RingBuffer:
    """The frames one attach has published recently, oldest evicted first.

    Per attach, therefore per epoch, therefore per session -- one publisher owns one session
    by construction. It holds ``control`` frames as well as data frames, because continuity
    means continuity of the *stream*, and "the stream restarted here" is part of that stream.

    Not thread-safe, for the same reason ``SeqAllocator`` is not.
    """

    __slots__ = ("_bytes", "_epoch", "_frames", "_max_bytes", "_max_frames")

    def __init__(
        self,
        epoch: Epoch,
        *,
        max_bytes: int = DEFAULT_RING_BYTES,
        max_frames: int = DEFAULT_RING_FRAMES,
    ) -> None:
        if max_bytes < 1 or max_frames < 1:
            raise ValueError("max_bytes and max_frames must each be at least 1")
        self._epoch = epoch
        self._frames: deque[Frame] = deque()
        self._bytes = 0
        self._max_bytes = max_bytes
        self._max_frames = max_frames

    @property
    def epoch(self) -> Epoch:
        return self._epoch

    @property
    def total_bytes(self) -> int:
        """Payload bytes held. Counts ``Frame.data`` only.

        Header overhead is excluded on purpose: the configured size means "a megabyte of
        pane output", which is the number an operator can reason about against a reconnect
        gap. Counting the encoded size would make the ring's depth depend on how long the
        session ids are.
        """
        return self._bytes

    @property
    def floor(self) -> int | None:
        """The oldest ``seq`` still held, or ``None`` while the ring is empty.

        This is the value the continuity guarantee is stated against: at or above the floor
        is byte-exact, below it is a declared discontinuity.
        """
        return self._frames[0].seq if self._frames else None

    @property
    def newest(self) -> int:
        """The newest ``seq`` held, or ``FIRST_SEQ - 1`` while the ring is empty."""
        return self._frames[-1].seq if self._frames else FIRST_SEQ - 1

    def __len__(self) -> int:
        return len(self._frames)

    def append(self, frame: Frame) -> None:
        """Hold ``frame``, evicting oldest until both limits are satisfied.

        Raises ``ValueError`` if ``frame.seq`` does not advance. That is a publisher bug, not
        subscriber input, so it fails loudly here rather than becoming a hole a subscriber
        discovers later.

        CRITICAL: Eviction never empties the ring. A frame larger than ``max_bytes`` is
        retained alone rather than dropped, because an empty ring makes every reconnect take
        the resync branch -- and that turns the branch this ring exists to serve into dead
        code silently, which is the one outcome that must be a measurement rather than an
        accident.
        """
        if self._frames and frame.seq <= self._frames[-1].seq:
            raise ValueError(
                f"seq {frame.seq} does not advance past {self._frames[-1].seq}; "
                "one SeqAllocator per epoch is the only source of ordinals"
            )
        self._frames.append(frame)
        self._bytes += len(frame.data)
        while len(self._frames) > 1 and (
            len(self._frames) > self._max_frames or self._bytes > self._max_bytes
        ):
            self._bytes -= len(self._frames.popleft().data)

    def frames_from(self, from_seq: int) -> tuple[Frame, ...]:
        """Every held frame at or after ``from_seq``, in order.

        Raises ``ValueError`` below the floor. Returning a short list instead would be the
        undeclared hole this package exists to prevent, so the check is here and not only in
        ``plan_resume``. Call ``plan_resume`` rather than this method: it decides which branch
        a request gets, and only the continuity branch may read the ring directly.
        """
        floor = self.floor
        if floor is None:
            if from_seq < FIRST_SEQ:
                raise ValueError(f"from_seq {from_seq} precedes FIRST_SEQ on an empty ring")
            return ()
        if from_seq < floor:
            raise ValueError(f"from_seq {from_seq} is below the ring floor {floor}")
        return tuple(frame for frame in self._frames if frame.seq >= from_seq)


@dataclass(frozen=True, slots=True)
class Continuity:
    """The byte-exact branch: these frames, then live frames, with nothing missing.

    ``frames`` is empty when the subscriber was already current. That is a normal outcome
    and the common one after a fast reconnect, not a degenerate case.
    """

    frames: tuple[Frame, ...]


@dataclass(frozen=True, slots=True)
class Discontinuity:
    """The honest branch: the publisher cannot serve this request byte-exactly, and says so.

    The caller MUST send a ``control`` frame built from this -- ``codec.resync_message``
    takes a repaint argument, so a resync cannot be constructed without one -- and it must be
    the FIRST thing the subscriber receives. Data frames before it are exactly the undeclared
    hole the guarantee forbids.

    WARNING: Do not publish anything between planning a discontinuity and sending its resync.
    ``base_seq`` is the newest ordinal the ring held at planning time, so the repaint's base
    is wrong if the stream advanced in between, and the subscriber then double-paints the
    frames the snapshot already contains.
    """

    epoch: Epoch
    asked_seq: int
    """What the subscriber asked for, echoed back so a log line can name the gap."""
    base_seq: int
    """The last ordinal the repaint accounts for. Live frames resume at ``base_seq + 1``."""
    reason: str
    """One of the four ``REASON_*`` constants."""


Resume = Continuity | Discontinuity


def plan_resume(ring: RingBuffer, *, from_seq: int, epoch: str | None = None) -> Resume:
    """Decide which resume branch a subscriber's request gets. The whole of ADR-11, as code.

    ``epoch`` is the raw wire string, or ``None`` when the subscriber presented none.

    CRITICAL: **The epoch is checked before the floor, and that order is the point.** ``seq``
    restarts in every epoch, so a ``seq`` from a previous epoch can sit comfortably above the
    current ring's floor while naming a completely different position in the stream. Checking
    the floor first reports continuity and delivers a hole -- the failure that gets
    misdiagnosed as a renderer bug.

    This function never raises on subscriber input. A malformed epoch, a negative
    ``from_seq``, or an ordinal the publisher has not issued yet all resolve to
    ``Discontinuity``, because the honest branch is always available and a subscriber must
    not be able to make a publisher throw.
    """
    if epoch is None:
        return _declared(ring, from_seq, REASON_NO_EPOCH)
    if epoch != ring.epoch.value:
        return _declared(ring, from_seq, REASON_EPOCH_CHANGED)
    if from_seq > ring.newest + 1:
        # The subscriber claims an ordinal this publisher never issued, in this publisher's
        # own epoch. Something is wrong upstream, but a repaint is still a correct answer, so
        # it gets one rather than an exception.
        return _declared(ring, from_seq, REASON_AHEAD_OF_PUBLISHER)
    floor = ring.floor
    if from_seq < (FIRST_SEQ if floor is None else floor):
        return _declared(ring, from_seq, REASON_BELOW_FLOOR)
    return Continuity(frames=ring.frames_from(from_seq))


def _declared(ring: RingBuffer, from_seq: int, reason: str) -> Discontinuity:
    return Discontinuity(epoch=ring.epoch, asked_seq=from_seq, base_seq=ring.newest, reason=reason)
