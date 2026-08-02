"""T-FRAME-SEQ and T-EPOCH-NEW -- ordinals inside an epoch, and what a new epoch forces.

``seq`` is monotonic and gap-free within one attach epoch, and it restarts in the next one.
Both halves matter, and the second is the one that bites: **a ``seq`` is meaningless across
epochs.** A subscriber holding ``seq`` 57 from a dead publisher can present it to a fresh
publisher whose ring happens to hold 40 through 80. A floor check alone says "57 is above 40,
serve it byte-exact" and hands over frames from a completely different position in the stream.
That is the undeclared hole the resume guarantee forbids, reached by obeying the guarantee's
own floor rule -- so ``plan_resume`` checks the epoch FIRST, and
``test_a_seq_from_a_previous_epoch_never_satisfies_the_floor`` is the test that pins it.

The epoch exists because ``seq`` cannot be durable. Durability needs a frame log, which is the
``session_frames`` table D7 rules out (``tests/unit/test_no_frames_table.py``). So a uuid4 per
attach makes the restart VISIBLE instead of silent, exactly as ``@shellbox_incarnation`` makes
misdelivery detectable rather than preventable.

NOTE: There is deliberately no test that the epoch is unreachable from a subscriber. The epoch
is published on purpose -- a subscriber's only permitted use of it is pessimistic, "distrust
everything and repaint" -- and asserting otherwise would assert the opposite of the design.
What must stay unreachable is the ring, which the integration lane checks.
"""

from __future__ import annotations

import pytest
from shellbox_transport import Frame, Stream
from shellbox_transport.seq import (
    FIRST_SEQ,
    REASON_AHEAD_OF_PUBLISHER,
    REASON_BELOW_FLOOR,
    REASON_EPOCH_CHANGED,
    REASON_NO_EPOCH,
    Continuity,
    Discontinuity,
    Epoch,
    RingBuffer,
    SeqAllocator,
    plan_resume,
)


def _frame(seq: int, data: bytes = b"x") -> Frame:
    return Frame(session_id="sb-1", seq=seq, t=1.0, stream=Stream.STDOUT, data=data)


def test_seq_is_monotonic_and_gap_free_within_an_epoch() -> None:
    """T-FRAME-SEQ. Gap-free by construction: one allocator per epoch is the only source, so
    a gap cannot be produced and therefore never has to be detected."""
    allocator = SeqAllocator(Epoch.new())
    issued = [allocator.next() for _ in range(500)]

    assert issued[0] == FIRST_SEQ
    assert issued == list(range(FIRST_SEQ, FIRST_SEQ + 500))
    assert allocator.issued == issued[-1]


def test_the_first_ordinal_is_one_so_zero_can_mean_i_hold_nothing() -> None:
    """A fresh subscriber has no ``seq``. It sends 0, which no floor can satisfy, so it gets
    the honest branch -- the correct answer, and one that needs no special case."""
    assert FIRST_SEQ == 1
    epoch = Epoch.new()
    ring = RingBuffer(epoch)
    ring.append(_frame(1))

    resumed = plan_resume(ring, from_seq=0, epoch=epoch.value)

    assert isinstance(resumed, Discontinuity)
    assert resumed.reason == REASON_BELOW_FLOOR


def test_a_new_epoch_restarts_seq_at_one() -> None:
    """T-FRAME-SEQ, second half. Restarting is the honest answer to "how does ``seq`` survive
    an MCP restart": it does not, and the epoch says so."""
    first, second = SeqAllocator(Epoch.new()), SeqAllocator(Epoch.new())
    first.next()
    first.next()

    assert second.next() == FIRST_SEQ
    assert first.epoch != second.epoch


def test_each_epoch_is_a_distinct_uuid4() -> None:
    epochs = {Epoch.new().value for _ in range(1000)}
    assert len(epochs) == 1000


def test_an_epoch_read_off_the_wire_is_shape_checked() -> None:
    """Shape-checked rather than merely tested for emptiness, which is the discipline
    ``_own_incarnation`` in ``tmux.py`` applies to ``@shellbox_incarnation``. A value that can
    be truncated or padded into looking equal to another defeats the detection the mechanism
    exists for."""
    minted = Epoch.new()
    assert Epoch.parse(minted.value) == minted

    for bad in ["", "not-a-uuid", minted.value[:-1], minted.value + "0", f"{minted}\nx"]:
        with pytest.raises(ValueError, match="not a uuid4"):
            Epoch.parse(bad)


def test_a_seq_from_a_previous_epoch_never_satisfies_the_floor() -> None:
    """T-EPOCH-NEW, and the defect this module is arranged around.

    The old ``seq`` is deliberately chosen to sit comfortably ABOVE the new ring's floor. A
    floor-first implementation returns ``Continuity`` here and delivers frames from an
    unrelated position in the stream. If this test ever fails, the epoch check moved after the
    floor check, and the symptom in production is a renderer painting plausible garbage.
    """
    dead, live = Epoch.new(), Epoch.new()
    ring = RingBuffer(live)
    for seq in range(40, 81):
        ring.append(_frame(seq))
    assert ring.floor == 40

    resumed = plan_resume(ring, from_seq=57, epoch=dead.value)

    assert isinstance(resumed, Discontinuity)
    assert resumed.reason == REASON_EPOCH_CHANGED
    assert resumed.epoch == live
    assert resumed.asked_seq == 57


def test_a_new_epoch_yields_a_declared_discontinuity_before_any_data_frame() -> None:
    """T-EPOCH-NEW. The structural half: ``Discontinuity`` has no ``frames`` attribute, so
    there are no data frames for a caller to send ahead of the resync. The ordering guarantee
    is not a convention a publisher has to remember."""
    ring = RingBuffer(Epoch.new())
    ring.append(_frame(1))

    resumed = plan_resume(ring, from_seq=1, epoch=Epoch.new().value)

    assert isinstance(resumed, Discontinuity)
    assert not hasattr(resumed, "frames")


def test_a_subscriber_presenting_no_epoch_gets_the_honest_branch() -> None:
    """The epic's two-argument ``subscribe(session_id, from_seq)`` call still works, and this
    is why it is safe: an absent epoch cannot match, so it degrades to a repaint."""
    epoch = Epoch.new()
    ring = RingBuffer(epoch)
    ring.append(_frame(1))

    resumed = plan_resume(ring, from_seq=1, epoch=None)

    assert isinstance(resumed, Discontinuity)
    assert resumed.reason == REASON_NO_EPOCH


def test_a_matching_epoch_at_or_above_the_floor_is_byte_exact() -> None:
    """The continuity half of the guarantee, and the branch the live run measures. If fewer
    than 20% of real reconnects reach this branch, it is deleted and ``subscribe`` always
    resyncs."""
    epoch = Epoch.new()
    ring = RingBuffer(epoch)
    payloads = [f"line-{n}".encode() for n in range(1, 11)]
    for seq, payload in enumerate(payloads, start=FIRST_SEQ):
        ring.append(_frame(seq, payload))

    resumed = plan_resume(ring, from_seq=4, epoch=epoch.value)

    assert isinstance(resumed, Continuity)
    assert [frame.seq for frame in resumed.frames] == list(range(4, 11))
    assert b"".join(frame.data for frame in resumed.frames) == b"".join(payloads[3:])


def test_a_caught_up_subscriber_gets_continuity_with_no_frames() -> None:
    """The common case after a fast reconnect, and a normal outcome rather than a degenerate
    one. ``from_seq`` is one past the newest ordinal: nothing is missing."""
    epoch = Epoch.new()
    ring = RingBuffer(epoch)
    ring.append(_frame(1))
    ring.append(_frame(2))

    resumed = plan_resume(ring, from_seq=3, epoch=epoch.value)

    assert isinstance(resumed, Continuity)
    assert resumed.frames == ()


def test_an_ordinal_the_publisher_never_issued_is_declared_not_raised() -> None:
    """A subscriber must not be able to make a publisher throw. The honest branch is always
    available, so malformed input gets a repaint and a named reason instead of an exception."""
    epoch = Epoch.new()
    ring = RingBuffer(epoch)
    ring.append(_frame(1))

    resumed = plan_resume(ring, from_seq=9999, epoch=epoch.value)

    assert isinstance(resumed, Discontinuity)
    assert resumed.reason == REASON_AHEAD_OF_PUBLISHER


@pytest.mark.parametrize("from_seq", [-1, -(2**40)])
def test_a_negative_from_seq_is_declared_not_raised(from_seq: int) -> None:
    epoch = Epoch.new()
    ring = RingBuffer(epoch)
    ring.append(_frame(1))

    resumed = plan_resume(ring, from_seq=from_seq, epoch=epoch.value)

    assert isinstance(resumed, Discontinuity)
    assert resumed.reason == REASON_BELOW_FLOOR


def test_a_malformed_epoch_string_is_declared_not_raised() -> None:
    """``plan_resume`` never parses the epoch, it compares it. A value that ``Epoch.parse``
    would reject simply fails to match, which is the same pessimistic outcome."""
    ring = RingBuffer(Epoch.new())
    ring.append(_frame(1))

    resumed = plan_resume(ring, from_seq=1, epoch="not-a-uuid")

    assert isinstance(resumed, Discontinuity)
    assert resumed.reason == REASON_EPOCH_CHANGED


def test_the_base_seq_a_discontinuity_reports_is_where_live_frames_resume() -> None:
    """The subscriber expects the next data frame at ``base_seq + 1``. The publisher must not
    publish between planning this and sending the resync, or the repaint's base is stale and
    the subscriber double-paints what the snapshot already contains."""
    epoch = Epoch.new()
    ring = RingBuffer(epoch, max_frames=4)
    for seq in range(1, 21):
        ring.append(_frame(seq))

    resumed = plan_resume(ring, from_seq=2, epoch=epoch.value)

    assert isinstance(resumed, Discontinuity)
    assert resumed.base_seq == 20
    assert ring.newest == 20


def test_an_empty_ring_serves_the_first_ordinal_rather_than_special_casing_it() -> None:
    epoch = Epoch.new()
    ring = RingBuffer(epoch)

    assert ring.floor is None
    assert ring.newest == FIRST_SEQ - 1
    resumed = plan_resume(ring, from_seq=FIRST_SEQ, epoch=epoch.value)
    assert isinstance(resumed, Continuity)
    assert resumed.frames == ()
