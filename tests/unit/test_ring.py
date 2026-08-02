"""T-RING-EVICT -- a full ring drops the oldest, and what a subscriber below the floor gets.

The happy-path test never reaches this file's subject. A ring that has not filled serves every
resume byte-exactly, so the eviction branch only appears once a real session has emitted a
megabyte -- which is minutes of a build log, and never a unit test that does not force it. So
the floor case is forced here, explicitly.

Two properties, and the second is the one that protects a renderer:

1. **Eviction drops the oldest frames and nothing else**, and it never empties the ring. A
   frame larger than ``max_bytes`` is retained alone. An eviction that could empty the ring
   would make every reconnect take the resync branch, which would retire the continuity branch
   by accident rather than by the measurement that is supposed to decide it.
2. **A subscriber below the floor receives a ``control`` resync FIRST.** Never a data frame
   mid-stream. ``plan_resume`` returns a ``Discontinuity`` that carries no frames at all, so
   there is nothing for a publisher to send ahead of the resync even by mistake.

The ring is sized against the reconnect GAP, not against replay: the edge kill is instantaneous
and a re-dial is sub-second, so what must fit is the output of those seconds. Sizing it for
replay would be the forbidden frame log with a memory budget.
"""

from __future__ import annotations

import pytest
from shellbox_transport import Frame, Stream
from shellbox_transport.codec import control_frame, decode_control, resync_message
from shellbox_transport.seq import (
    DEFAULT_RING_BYTES,
    DEFAULT_RING_FRAMES,
    REASON_BELOW_FLOOR,
    Continuity,
    Discontinuity,
    Epoch,
    RingBuffer,
    SeqAllocator,
    plan_resume,
)

_SESSION = "sb-1"


def _frame(seq: int, data: bytes = b"x") -> Frame:
    return Frame(session_id=_SESSION, seq=seq, t=1.0, stream=Stream.STDOUT, data=data)


def _resume_stream(
    ring: RingBuffer, *, from_seq: int, epoch: str | None, repaint: bytes, allocator: SeqAllocator
) -> list[Frame]:
    """What a publisher sends for one resume request, assembled the way ``W19`` must.

    It lives in the test rather than in the package because building the repaint needs
    ``tmux capture-pane``, and the transport package holds no tmux knowledge. What the package
    guarantees is that this function has only two branches to write.
    """
    resumed = plan_resume(ring, from_seq=from_seq, epoch=epoch)
    if isinstance(resumed, Continuity):
        return list(resumed.frames)
    message = resync_message(resumed, repaint)
    return [control_frame(_SESSION, allocator.next(), 2.0, message)]


def test_the_defaults_are_one_mebibyte_or_five_hundred_twelve_frames() -> None:
    """Whichever binds first. Both are configurable through ``shellbox-mcp``'s ``config.py``,
    which is the one place the environment is read."""
    assert DEFAULT_RING_BYTES == 1 << 20
    assert DEFAULT_RING_FRAMES == 512


def test_a_full_ring_evicts_the_oldest_frame_first() -> None:
    epoch = Epoch.new()
    ring = RingBuffer(epoch, max_frames=3)
    for seq in range(1, 6):
        ring.append(_frame(seq))

    assert len(ring) == 3
    assert ring.floor == 3
    assert ring.newest == 5


def test_the_byte_ceiling_binds_before_the_frame_ceiling_when_frames_are_large() -> None:
    """ "Whichever binds first" is one rule, not two, and this is the half a frame-count test
    misses: 10 frames of 100 bytes overflow a 256-byte ring long before the count limit."""
    epoch = Epoch.new()
    ring = RingBuffer(epoch, max_bytes=256, max_frames=1000)
    for seq in range(1, 11):
        ring.append(_frame(seq, b"y" * 100))

    assert len(ring) == 2
    assert ring.total_bytes == 200
    assert ring.floor == 9


def test_eviction_never_empties_the_ring() -> None:
    """CRITICAL: A frame larger than the whole ring is retained alone rather than dropped.

    An empty ring answers every resume with a resync, which would silently retire the
    continuity branch. Whether that branch survives has to be a decision from the live run's
    measured ratio, not a side effect of a payload larger than someone's default.
    """
    epoch = Epoch.new()
    ring = RingBuffer(epoch, max_bytes=16)

    ring.append(_frame(1, b"z" * 4096))

    assert len(ring) == 1
    assert ring.floor == 1
    assert plan_resume(ring, from_seq=1, epoch=epoch.value) == Continuity((ring.frames_from(1)[0],))


def test_a_ring_smaller_than_one_frame_is_refused_at_construction() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        RingBuffer(Epoch.new(), max_frames=0)
    with pytest.raises(ValueError, match="at least 1"):
        RingBuffer(Epoch.new(), max_bytes=0)


def test_a_seq_that_does_not_advance_is_a_publisher_bug_and_raises() -> None:
    """Subscriber input never raises; publisher input does. A non-advancing ``seq`` means two
    allocators are feeding one ring, and the hole that produces is invisible later."""
    ring = RingBuffer(Epoch.new())
    ring.append(_frame(5))

    with pytest.raises(ValueError, match="does not advance"):
        ring.append(_frame(5))
    with pytest.raises(ValueError, match="does not advance"):
        ring.append(_frame(4))


def test_reading_the_ring_below_its_floor_raises_rather_than_returning_a_short_list() -> None:
    """The check is in ``frames_from`` as well as in ``plan_resume``, so a caller that reaches
    past the decision cannot get a truncated stream that looks complete."""
    ring = RingBuffer(Epoch.new(), max_frames=2)
    for seq in range(1, 6):
        ring.append(_frame(seq))

    with pytest.raises(ValueError, match="below the ring floor"):
        ring.frames_from(1)


def test_a_subscriber_below_the_floor_receives_the_resync_first() -> None:
    """T-RING-EVICT's second half. The FIRST frame is ``control``, and it carries the repaint.

    A data frame in this position is the undeclared hole: the renderer would apply bytes from
    the middle of an escape stream, desynchronize its parser, and paint plausible garbage.
    """
    epoch = Epoch.new()
    allocator = SeqAllocator(epoch)
    ring = RingBuffer(epoch, max_frames=3)
    for seq in range(1, 21):
        ring.append(_frame(seq))
    assert ring.floor == 18

    sent = _resume_stream(
        ring,
        from_seq=2,
        epoch=epoch.value,
        repaint=b"\x1b[2J\x1b[Hvisible pane",
        allocator=allocator,
    )

    assert [frame.stream for frame in sent] == [Stream.CONTROL]
    message = decode_control(sent[0].data)
    assert message.epoch == epoch.value
    assert message.fields["reason"] == REASON_BELOW_FLOOR
    assert message.fields["asked_seq"] == 2
    assert message.fields["base_seq"] == 20
    assert message.payload == b"\x1b[2J\x1b[Hvisible pane"


def test_a_subscriber_at_the_floor_receives_data_with_no_resync() -> None:
    """The boundary is inclusive: at the floor is byte-exact. Off by one here means either a
    needless repaint on every reconnect, or a hole."""
    epoch = Epoch.new()
    allocator = SeqAllocator(epoch)
    ring = RingBuffer(epoch, max_frames=3)
    for seq in range(1, 21):
        ring.append(_frame(seq, f"line-{seq}".encode()))

    sent = _resume_stream(
        ring, from_seq=18, epoch=epoch.value, repaint=b"unused", allocator=allocator
    )

    assert [frame.seq for frame in sent] == [18, 19, 20]
    assert all(frame.stream is Stream.STDOUT for frame in sent)


def test_the_ring_holds_control_frames_too() -> None:
    """Continuity means continuity of the STREAM. "The stream restarted here" is part of that
    stream, so replaying a resumable window must not drop it."""
    epoch = Epoch.new()
    allocator = SeqAllocator(epoch)
    ring = RingBuffer(epoch)
    hello = control_frame(
        _SESSION,
        allocator.next(),
        1.0,
        resync_message(
            Discontinuity(epoch=epoch, asked_seq=0, base_seq=0, reason=REASON_BELOW_FLOOR),
            b"repaint",
        ),
    )
    ring.append(hello)
    ring.append(_frame(allocator.next(), b"after"))

    resumed = plan_resume(ring, from_seq=1, epoch=epoch.value)

    assert isinstance(resumed, Continuity)
    assert [frame.stream for frame in resumed.frames] == [Stream.CONTROL, Stream.STDOUT]


def test_total_bytes_counts_payloads_and_not_headers() -> None:
    """The configured size means "a megabyte of pane output", which is the number an operator
    can weigh against a reconnect gap. Counting encoded size would make the ring's depth depend
    on how long the session ids are."""
    ring = RingBuffer(Epoch.new())
    ring.append(_frame(1, b"1234"))
    ring.append(_frame(2, b""))

    assert ring.total_bytes == 4
    assert len(ring) == 2
