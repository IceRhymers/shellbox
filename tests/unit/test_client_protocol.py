"""`SubscriberClient` -- the browser's protocol behaviour, driven with no socket and no clock.

This is ADR-23's lane. The renderer's risk is frame decoding, sequence handling, reconnect with
resume, the bounded ``subscriber_conflict`` retry and the no-publisher state, and every one of
those is asserted here in milliseconds against the same `shellbox_transport` codec the App
serves. `T-P4-NO-PUBLISHER`, `T-P4-SUBSCRIBER-RETRY` and `T-P4-RESUME-REPAINT` all live in this
file, named in the tests below.

WARNING: this file tests the PYTHON half. `packages/shellbox-app/src/shellbox_app/static/`
holds a second implementation in JavaScript, and `tests/unit/test_client_parity.py` compares
only the two files' declared constants. Nothing here executes the JavaScript, and ADR-23 says
so on the record rather than leaving it to be discovered.
"""

from __future__ import annotations

import random

import pytest
from shellbox_app.client import (
    BACKOFF_CAP_SECONDS,
    BACKOFF_FLOOR_SECONDS,
    CODE_SESSION_MISMATCH,
    CODE_SUBSCRIBER_CONFLICT,
    CODE_TERMINAL_GONE,
    HELLO_DEADLINE_SECONDS,
    NO_PUBLISHER_DEADLINE_SECONDS,
    NO_PUBLISHER_MESSAGE,
    NOTICE_DETACHED,
    NOTICE_NO_PUBLISHER,
    NOTICE_STREAM_GAP,
    SUBSCRIBER_CONFLICT_BOUND_SECONDS,
    HostView,
    Notice,
    Phase,
    Redial,
    Reset,
    Send,
    Stop,
    SubscriberClient,
    Write,
)
from shellbox_app.server import WS_PING_INTERVAL_SECONDS, WS_PING_TIMEOUT_SECONDS
from shellbox_transport import Frame, Stream
from shellbox_transport.codec import (
    CLOSED_DETACHED,
    CLOSED_TERMINAL_GONE,
    CONTROL_INPUT,
    CONTROL_RESIZE,
    CONTROL_RESUME,
    CONTROL_RESYNC,
    FIELD_ASKED_SEQ,
    FIELD_COLS,
    FIELD_ROWS,
    ControlMessage,
    closed_message,
    control_frame,
    decode_control,
    decode_frame,
    encode_frame,
    error_message,
    hello_message,
    resize_message,
)
from shellbox_transport.seq import Discontinuity, Epoch

SESSION = "sb-browser-1"
EPOCH = "8b1f0c2a-4d5e-4f60-9a71-2c3d4e5f6a7b"
OTHER_EPOCH = "1a2b3c4d-5e6f-4071-8293-a4b5c6d7e8f9"

# Sizes the resize assertions use. Distinct from any default so a passing assertion cannot be
# satisfied by a value the client made up.
COLS, ROWS = 132, 41


def client(**kwargs: object) -> SubscriberClient:
    """A client with a SEEDED rng, so a jitter draw is reproducible in every test but one."""
    kwargs.setdefault("rng", random.Random(20260804))
    return SubscriberClient(SESSION, **kwargs)  # type: ignore[arg-type]


def hello(session_id: str = SESSION, *, viewer: str | None = None) -> bytes:
    return _wrap(session_id, hello_message(session_id, None, viewer))


def resync(
    repaint: bytes, *, epoch: str = EPOCH, base_seq: int = 0, frame_seq: int | None = None
) -> bytes:
    """A resync as the publisher actually sends it.

    ``frame_seq`` defaults to ``base_seq + 1`` because that is what the wire carries: the ring's
    newest at planning time is ``base_seq``, and `PtyBridge` then allocates the resync's own
    ordinal from the same `SeqAllocator`. Defaulting it to `base_seq` instead would let a test
    pass against a shape no publisher produces -- which is how the live run found a gap that
    every unit test had agreed was fine.
    """
    from shellbox_transport.codec import resync_message

    gap = Discontinuity(epoch=Epoch(epoch), asked_seq=0, base_seq=base_seq, reason="epoch_changed")
    return _wrap(
        SESSION, resync_message(gap, repaint), seq=base_seq + 1 if frame_seq is None else frame_seq
    )


def refusal(code: str, message: str = "refused") -> bytes:
    return _wrap(SESSION, error_message(code, message, session_id=SESSION))


def data(seq: int, payload: bytes) -> bytes:
    return encode_frame(
        Frame(session_id=SESSION, seq=seq, t=1.0, stream=Stream.STDOUT, data=payload)
    )


def _wrap(session_id: str, message: object, *, seq: int = 0) -> bytes:
    """Wrap a control message. ``seq=0`` is `UNORDERED_SEQ` -- what the APP originates."""
    return encode_frame(control_frame(session_id, seq, 1.0, message))  # type: ignore[arg-type]


def sent(actions: list[object]) -> list[object]:
    """Every `Send` in ``actions``, decoded back to its control message."""
    return [
        decode_control(decode_frame(action.payload).data)
        for action in actions
        if isinstance(action, Send)
    ]


def live(now: float = 0.0, **kwargs: object) -> SubscriberClient:
    """A client past the `hello` gate, which is where most of these tests start."""
    subscriber = client(**kwargs)
    subscriber.opened(now)
    subscriber.received(hello(), now)
    return subscriber


# --------------------------------------------------------------- the hello gate (behaviour 1)


def test_a_socket_that_opens_is_not_yet_connected() -> None:
    subscriber = client()
    assert subscriber.opened(0.0) == []
    assert subscriber.phase is Phase.AWAITING_HELLO


def test_hello_naming_this_session_connects_and_asks_to_resume() -> None:
    subscriber = client()
    subscriber.opened(0.0)
    actions = subscriber.received(hello(viewer="tanner@example.com"), 0.1)

    assert subscriber.phase is Phase.LIVE
    assert subscriber.viewer_email == "tanner@example.com"
    messages = sent(actions)
    assert [message.kind for message in messages] == [CONTROL_RESUME]
    # `from_seq=0` with no epoch is the honest "I hold nothing", and it is what makes silence
    # afterwards evidence about the publisher rather than about an idle pane.
    assert messages[0].fields[FIELD_ASKED_SEQ] == 0
    assert messages[0].epoch is None


def test_hello_naming_another_session_is_terminal_and_never_retried() -> None:
    """Two agents' streams could cross. No retry can make that right."""
    subscriber = client()
    subscriber.opened(0.0)
    actions = subscriber.received(hello("sb-somebody-else"), 0.1)

    assert [type(action) for action in actions] == [Stop]
    assert isinstance(actions[0], Stop)
    assert actions[0].code == CODE_SESSION_MISMATCH
    assert "sb-somebody-else" in actions[0].text
    assert subscriber.phase is Phase.STOPPED
    # STOPPED is absorbing: a later close must not schedule a dial.
    assert subscriber.closed(1.0) == []


def test_a_101_that_never_becomes_a_hello_redials() -> None:
    subscriber = client()
    subscriber.opened(0.0)

    assert subscriber.ticked(HELLO_DEADLINE_SECONDS - 0.01) == []
    actions = subscriber.ticked(HELLO_DEADLINE_SECONDS)
    assert [type(action) for action in actions] == [Redial]
    assert subscriber.phase is Phase.DIALING


def test_data_before_hello_is_dropped_rather_than_rendered() -> None:
    subscriber = client()
    subscriber.opened(0.0)
    assert subscriber.received(data(1, b"secrets"), 0.1) == []
    assert subscriber.decode_failures == 1


def test_a_text_message_is_dropped_because_frames_are_binary() -> None:
    subscriber = live()
    assert subscriber.received("<!doctype html>", 1.0) == []
    assert subscriber.decode_failures == 1


def test_an_undecodable_frame_is_dropped_and_counted() -> None:
    subscriber = live()
    assert subscriber.received(b"not a frame", 1.0) == []
    assert subscriber.decode_failures == 1


# ------------------------------------------------------- T-P4-NO-PUBLISHER (`R51`, behaviour 5)


def test_no_publisher_hello_then_silence_names_the_state() -> None:
    """`T-P4-NO-PUBLISHER`. The plan's own primary failure mode, and it must not be blank."""
    subscriber = live()
    subscriber.note_host(HostView("host-abc", "sb-sandbox-7", "stopped"))

    assert subscriber.ticked(NO_PUBLISHER_DEADLINE_SECONDS - 0.01) == []
    actions = subscriber.ticked(NO_PUBLISHER_DEADLINE_SECONDS)

    assert [type(action) for action in actions] == [Notice]
    assert isinstance(actions[0], Notice)
    assert actions[0].code == NOTICE_NO_PUBLISHER
    assert actions[0].text.startswith(NO_PUBLISHER_MESSAGE)
    # Actionable: which host, which sandbox to go and start, and what state it is in.
    assert "host-abc" in actions[0].text
    assert "sb-sandbox-7" in actions[0].text
    assert "stopped" in actions[0].text


def test_no_publisher_carries_the_not_bootstrapped_label_verbatim() -> None:
    """The NULL ``sandbox_id`` case, which is exactly what this state usually means."""
    from shellbox_app.inventory import NOT_BOOTSTRAPPED_LABEL

    subscriber = live()
    subscriber.note_host(HostView("host-abc", NOT_BOOTSTRAPPED_LABEL, "stopped"))
    actions = subscriber.ticked(NO_PUBLISHER_DEADLINE_SECONDS)

    assert isinstance(actions[0], Notice)
    assert NOT_BOOTSTRAPPED_LABEL in actions[0].text


def test_no_publisher_is_reported_without_an_inventory_row() -> None:
    """A stale inventory costs the banner its detail, never its correctness."""
    subscriber = live()
    actions = subscriber.ticked(NO_PUBLISHER_DEADLINE_SECONDS)

    assert isinstance(actions[0], Notice)
    assert actions[0].text.startswith(NO_PUBLISHER_MESSAGE)


def test_no_publisher_is_reported_once_not_on_every_tick() -> None:
    subscriber = live()
    assert len(subscriber.ticked(NO_PUBLISHER_DEADLINE_SECONDS)) == 1
    assert subscriber.ticked(NO_PUBLISHER_DEADLINE_SECONDS + 60) == []


def test_a_frame_arriving_retracts_the_no_publisher_state_for_good_on_that_socket() -> None:
    """The banner is advisory and self-clearing, which is why the deadline errs short.

    CRITICAL: it does NOT re-arm once output has been seen, and that is the whole soundness
    argument rather than a shortcut. Silence is evidence about the publisher only because a
    fresh `hello` is followed by a `resume` that a live publisher MUST answer with a `resync`.
    Nothing obliges a publisher to say anything later, so mid-session silence is exactly what an
    idle shell looks like -- and re-arming would put "no publisher attached" under every prompt
    nobody has typed at for eight seconds.
    """
    subscriber = live()
    subscriber.ticked(NO_PUBLISHER_DEADLINE_SECONDS)

    subscriber.received(data(1, b"$ "), NO_PUBLISHER_DEADLINE_SECONDS + 1)
    assert subscriber.ticked(NO_PUBLISHER_DEADLINE_SECONDS * 100) == []


def test_a_reconnect_re_arms_the_no_publisher_detector() -> None:
    """A new socket asks the question again, so the evidence is fresh and the state is rechecked."""
    subscriber = live()
    subscriber.received(data(1, b"$ "), 1.0)
    subscriber.closed(2.0)

    subscriber.opened(3.0)
    subscriber.received(hello(), 3.0)
    actions = subscriber.ticked(3.0 + NO_PUBLISHER_DEADLINE_SECONDS)
    assert [type(action) for action in actions] == [Notice]


def test_output_before_the_deadline_means_no_banner_at_all() -> None:
    subscriber = live()
    subscriber.received(data(1, b"$ "), 0.5)
    assert subscriber.ticked(NO_PUBLISHER_DEADLINE_SECONDS * 10) == []


# --------------------------------------------------- T-P4-SUBSCRIBER-RETRY (behaviour 3, ADR-20)


def test_subscriber_conflict_retries_then_gives_up_and_says_why() -> None:
    """`T-P4-SUBSCRIBER-RETRY`. Bounded: it retries, and it STOPS, surfacing the reason."""
    subscriber = client()
    subscriber.opened(0.0)
    first = subscriber.received(refusal(CODE_SUBSCRIBER_CONFLICT, "already bound"), 0.0)
    assert [type(action) for action in first] == [Redial]

    # Still inside the bound: still retrying, however many times the slot stays held.
    for moment in (5.0, 20.0, SUBSCRIBER_CONFLICT_BOUND_SECONDS - 0.01):
        subscriber.opened(moment)
        actions = subscriber.received(refusal(CODE_SUBSCRIBER_CONFLICT, "already bound"), moment)
        assert [type(action) for action in actions] == [Redial], moment

    subscriber.opened(SUBSCRIBER_CONFLICT_BOUND_SECONDS)
    actions = subscriber.received(
        refusal(CODE_SUBSCRIBER_CONFLICT, "already bound"), SUBSCRIBER_CONFLICT_BOUND_SECONDS
    )
    assert [type(action) for action in actions] == [Stop]
    assert isinstance(actions[0], Stop)
    assert actions[0].code == CODE_SUBSCRIBER_CONFLICT
    assert "already bound" in actions[0].text
    assert subscriber.phase is Phase.STOPPED


def test_the_conflict_bound_outlasts_the_apps_own_reaper() -> None:
    """The relationship that makes the bound correct, asserted so the two cannot drift.

    The App has no reaper of its own: what frees a silently-dead subscriber's slot is uvicorn
    failing its own ping, after `ws_ping_interval + ws_ping_timeout`. A browser that gave up
    sooner would report a conflict that was about to clear itself.
    """
    assert SUBSCRIBER_CONFLICT_BOUND_SECONDS > WS_PING_INTERVAL_SECONDS + WS_PING_TIMEOUT_SECONDS


def test_reaching_hello_clears_the_conflict_window() -> None:
    """A hello proves the slot was free, so the next conflict gets a fresh 45 s of its own."""
    subscriber = client()
    subscriber.opened(0.0)
    subscriber.received(refusal(CODE_SUBSCRIBER_CONFLICT), 0.0)
    subscriber.opened(1.0)
    subscriber.received(hello(), 1.0)

    late = SUBSCRIBER_CONFLICT_BOUND_SECONDS * 4
    subscriber.opened(late)
    actions = subscriber.received(refusal(CODE_SUBSCRIBER_CONFLICT), late)
    assert [type(action) for action in actions] == [Redial]


def test_an_unknown_refusal_code_stops_rather_than_retrying_blindly() -> None:
    subscriber = client()
    subscriber.opened(0.0)
    actions = subscriber.received(refusal("publisher_conflict", "one publisher per session"), 0.0)

    assert [type(action) for action in actions] == [Stop]
    assert isinstance(actions[0], Stop)
    assert actions[0].code == "publisher_conflict"


def test_a_refusal_then_the_servers_close_schedules_only_one_dial() -> None:
    """The server sends the reason and then closes. Two `Redial`s would double the rate."""
    subscriber = client()
    subscriber.opened(0.0)
    assert [
        type(action) for action in subscriber.received(refusal(CODE_SUBSCRIBER_CONFLICT), 0.0)
    ] == [Redial]
    assert subscriber.closed(0.0) == []


# --------------------------------------------------------- T-P4-RESUME-REPAINT (behaviour 4, D7)


def test_a_resync_resets_the_terminal_and_is_never_appended() -> None:
    """`T-P4-RESUME-REPAINT`. Resume repaints; it cannot gap-fill, because D7 rules out a log."""
    subscriber = live()
    subscriber.received(data(1, b"stale output"), 1.0)

    actions = subscriber.received(resync(b"\x1b[2Jrepainted", base_seq=9), 2.0)

    resets = [action for action in actions if isinstance(action, Reset)]
    assert len(resets) == 1
    assert resets[0].repaint == b"\x1b[2Jrepainted"
    # The one assertion this test exists for: a repaint must NEVER arrive as appended output.
    assert not any(isinstance(action, Write) for action in actions)


def test_a_resync_rebases_the_stream_so_the_next_frame_is_not_a_gap() -> None:
    subscriber = live()
    subscriber.received(resync(b"repaint", base_seq=9, frame_seq=10), 1.0)
    assert subscriber.last_seq == 10

    actions = subscriber.received(data(11, b"live"), 2.0)
    assert [type(action) for action in actions] == [Write]


def test_the_resync_frames_own_ordinal_advances_the_position() -> None:
    """The live run's finding: `base_seq` is not where the subscriber actually is.

    MEASURED 2026-08-04 against the deployed `dev` App -- a fresh attach reported
    `stream skipped from 2 to 4` on its very first resume. `plan_resume` sets `base_seq` to the
    ring's newest at PLANNING time and `PtyBridge` then sends the resync through
    `control_frame(..., self._seq.next(), ...)`, so the resync itself consumes `base_seq + 1`
    and live output resumes at `base_seq + 2`. A client that trusted `Discontinuity.base_seq`'s
    "live frames resume at base_seq + 1" reported a phantom gap on EVERY reconnect -- roughly
    four times an hour per viewer, on the hot path, telling the reader output was missing when
    none was.
    """
    subscriber = live()
    # Exactly the live shape: base_seq=2, the resync frame itself carrying seq=3.
    subscriber.received(resync(b"repaint", base_seq=2, frame_seq=3), 1.0)
    assert subscriber.last_seq == 3

    actions = subscriber.received(data(4, b"live output"), 2.0)
    assert [type(action) for action in actions] == [Write], (
        "the resync frame's own ordinal was not counted, so the next live frame looks like a gap"
    )


def test_the_apps_own_control_frames_never_move_the_position() -> None:
    """`hello` and refusals carry `UNORDERED_SEQ`; the App holds no allocator.

    Adopting its 0 would drag the position BACKWARDS, and the next resume would then ask for a
    part of the stream the subscriber had already rendered.
    """
    subscriber = live()
    subscriber.received(resync(b"repaint", base_seq=40, frame_seq=41), 1.0)
    subscriber.received(hello(), 2.0)
    assert subscriber.last_seq == 41


def test_a_resync_adopts_its_epoch_so_the_next_resume_names_it() -> None:
    subscriber = live()
    subscriber.received(resync(b"repaint", epoch=EPOCH, base_seq=4), 1.0)
    assert subscriber.epoch == EPOCH

    subscriber.closed(2.0)
    subscriber.opened(3.0)
    messages = sent(subscriber.received(hello(), 3.0))
    resume = next(message for message in messages if message.kind == CONTROL_RESUME)
    assert resume.epoch == EPOCH
    # 5, not the resync's `base_seq` of 4: the resync frame itself carried ordinal 5, and that
    # is the last thing this subscriber actually received.
    assert resume.fields[FIELD_ASKED_SEQ] == 5


def test_a_new_epoch_resets_the_ordinal_space() -> None:
    """``seq`` restarts per epoch, so carrying it across would name a position that is gone."""
    subscriber = live()
    subscriber.received(resync(b"first", epoch=EPOCH, base_seq=500), 1.0)
    subscriber.received(resync(b"second", epoch=OTHER_EPOCH, base_seq=0), 2.0)

    assert subscriber.epoch == OTHER_EPOCH
    # 1, not 500: the old epoch's ordinal is gone, and the new epoch's resync is its own
    # `FIRST_SEQ`. Carrying 500 across would make the next resume name a position in a stream
    # that no longer exists.
    assert subscriber.last_seq == 1
    assert [type(action) for action in subscriber.received(data(2, b"live"), 3.0)] == [Write]


def test_an_epoch_change_clears_the_ordinal_even_when_no_base_seq_arrives() -> None:
    """The case that makes `_note_epoch`'s reset load-bearing rather than decorative.

    A well-formed ``resync`` overwrites `last_seq` with its ``base_seq`` immediately, so the
    reset inside `_note_epoch` is invisible there -- MEASURED: deleting that line passed the
    whole of this file until this test existed. What it actually guards is a resync whose
    ``base_seq`` is absent or malformed. Without the reset the client would keep the PREVIOUS
    epoch's ordinal and then send it on the next resume, and `plan_resume` checks the epoch
    before the floor precisely because such a pairing reports continuity and delivers a hole.
    """
    subscriber = live()
    subscriber.received(resync(b"first", epoch=EPOCH, base_seq=500), 1.0)
    assert subscriber.last_seq == 501

    malformed = _wrap(
        SESSION,
        ControlMessage(kind=CONTROL_RESYNC, epoch=OTHER_EPOCH, fields={}, payload=b"repaint"),
    )
    subscriber.received(malformed, 2.0)

    assert subscriber.epoch == OTHER_EPOCH
    assert subscriber.last_seq == 0, (
        "the client carried an ordinal from the previous epoch. The next resume would name a "
        "position in a stream that no longer exists."
    )

    subscriber.closed(3.0)
    subscriber.opened(4.0)
    resume = next(
        message
        for message in sent(subscriber.received(hello(), 4.0))
        if message.kind == CONTROL_RESUME
    )
    assert resume.fields[FIELD_ASKED_SEQ] == 0


def test_a_resync_retracts_a_standing_no_publisher_banner() -> None:
    subscriber = live()
    subscriber.ticked(NO_PUBLISHER_DEADLINE_SECONDS)
    actions = subscriber.received(resync(b"repaint"), NO_PUBLISHER_DEADLINE_SECONDS + 1)
    assert any(isinstance(action, Reset) for action in actions)
    assert subscriber.ticked(NO_PUBLISHER_DEADLINE_SECONDS + 2) == []


# ---------------------------------------------------------------------------- the data path


def test_output_is_written_byte_exact() -> None:
    subscriber = live()
    payload = b"\xe2\x94\x80\x1b[31m not text \x00\xff"
    actions = subscriber.received(data(1, payload), 1.0)

    assert [type(action) for action in actions] == [Write]
    assert isinstance(actions[0], Write)
    assert actions[0].data == payload


def test_a_seq_jump_inside_one_epoch_is_surfaced_and_the_bytes_still_render() -> None:
    """Gap-free is a construction property, so a jump is a publisher bug worth naming."""
    subscriber = live()
    subscriber.received(resync(b"repaint", base_seq=1), 1.0)
    actions = subscriber.received(data(9, b"output"), 2.0)

    kinds = [type(action) for action in actions]
    assert kinds == [Notice, Write]
    assert isinstance(actions[0], Notice)
    assert actions[0].code == NOTICE_STREAM_GAP


def test_a_first_frame_at_any_ordinal_is_not_a_gap() -> None:
    """A fresh subscriber holds no base, so it has nothing to measure a jump against."""
    subscriber = live()
    actions = subscriber.received(data(4096, b"output"), 1.0)
    assert [type(action) for action in actions] == [Write]


# ---------------------------------------------------------------- closed: the two reasons


def test_terminal_gone_stops_because_there_is_nothing_left_to_watch() -> None:
    subscriber = live()
    actions = subscriber.received(
        _wrap(SESSION, closed_message(Epoch(EPOCH), CLOSED_TERMINAL_GONE)), 1.0
    )
    assert [type(action) for action in actions] == [Stop]
    assert isinstance(actions[0], Stop)
    assert actions[0].code == CODE_TERMINAL_GONE


def test_detached_is_advisory_because_the_session_is_still_running() -> None:
    """Collapsing this into ``terminal_gone`` would tear down a live session."""
    subscriber = live()
    actions = subscriber.received(
        _wrap(SESSION, closed_message(Epoch(EPOCH), CLOSED_DETACHED)), 1.0
    )
    assert [type(action) for action in actions] == [Notice]
    assert isinstance(actions[0], Notice)
    assert actions[0].code == NOTICE_DETACHED
    assert subscriber.phase is Phase.LIVE
    # Still reconnects, because the pane is still there to reattach to.
    assert [type(action) for action in subscriber.closed(2.0)] == [Redial]


# ------------------------------------------------------------------------- input and resize


def test_a_keystroke_travels_byte_exact_with_no_ordinal() -> None:
    subscriber = live()
    actions = subscriber.typed(b"\x1b[A\r", 1.0)

    messages = sent(actions)
    assert [message.kind for message in messages] == [CONTROL_INPUT]
    assert messages[0].payload == b"\x1b[A\r"
    # A subscriber allocates no ordinals and must not appear to hold a position in the stream.
    assert decode_frame(actions[0].payload).seq == 0  # type: ignore[union-attr]
    assert messages[0].epoch is None


def test_typing_before_hello_sends_nothing() -> None:
    subscriber = client()
    subscriber.opened(0.0)
    assert subscriber.typed(b"rm -rf /", 0.1) == []


def test_a_resize_waits_for_an_epoch_and_then_goes_out() -> None:
    """A subscriber holds no attach, so the only honest epoch is the publisher's last stated one."""
    subscriber = live()
    assert subscriber.resized(COLS, ROWS, 1.0) == []

    actions = subscriber.received(resync(b"repaint", epoch=EPOCH), 2.0)
    messages = [message for message in sent(actions) if message.kind == CONTROL_RESIZE]
    assert len(messages) == 1
    assert messages[0].fields[FIELD_COLS] == COLS
    assert messages[0].fields[FIELD_ROWS] == ROWS
    assert messages[0].epoch == EPOCH


def test_a_publisher_restart_re_asserts_the_viewers_size() -> None:
    subscriber = live()
    subscriber.resized(COLS, ROWS, 1.0)
    subscriber.received(resync(b"repaint", epoch=EPOCH), 2.0)

    actions = subscriber.received(resync(b"repaint", epoch=OTHER_EPOCH), 3.0)
    messages = [message for message in sent(actions) if message.kind == CONTROL_RESIZE]
    assert len(messages) == 1
    assert messages[0].epoch == OTHER_EPOCH


def test_an_unchanged_size_sends_nothing() -> None:
    subscriber = live()
    subscriber.received(resync(b"repaint", epoch=EPOCH), 1.0)
    assert len(subscriber.resized(COLS, ROWS, 2.0)) == 1
    assert subscriber.resized(COLS, ROWS, 3.0) == []


@pytest.mark.parametrize(("cols", "rows"), [(0, 24), (80, 0), (-1, -1)])
def test_a_degenerate_size_is_ignored_rather_than_encoded(cols: int, rows: int) -> None:
    """`resize_message` raises below 1x1, and a viewport can legitimately report 0 mid-layout."""
    subscriber = live()
    subscriber.received(resync(b"repaint", epoch=EPOCH), 1.0)
    assert subscriber.resized(cols, rows, 2.0) == []


# ------------------------------------------------------------------ reconnect (behaviour 2)


def test_every_delay_is_inside_the_jittered_window_with_a_nonzero_floor() -> None:
    subscriber = live()
    delays = [subscriber.next_delay() for _ in range(200)]

    assert all(BACKOFF_FLOOR_SECONDS <= delay <= BACKOFF_CAP_SECONDS for delay in delays)
    assert min(delays) > 0.0
    # Full jitter, not a constant. A fixed delay would keep every client synchronized with the
    # edge kill, which is itself a synchronized global event.
    assert len(set(delays)) > 1


def test_two_clients_constructed_together_do_not_draw_the_same_delay() -> None:
    """Unseeded, which is the deployed case: the rng must not be derived from the clock."""
    one = SubscriberClient(SESSION)
    two = SubscriberClient(SESSION)
    assert [one.next_delay() for _ in range(20)] != [two.next_delay() for _ in range(20)]


def test_a_socket_death_schedules_exactly_one_dial() -> None:
    subscriber = live()
    actions = subscriber.closed(1.0)
    assert [type(action) for action in actions] == [Redial]
    assert isinstance(actions[0], Redial)
    assert BACKOFF_FLOOR_SECONDS <= actions[0].delay <= BACKOFF_CAP_SECONDS
    assert subscriber.phase is Phase.DIALING
    # Already dialing: a second notification must not double the rate.
    assert subscriber.closed(2.0) == []


def test_the_epoch_and_the_ordinal_outlive_the_socket() -> None:
    """The whole reason this state is not held in the connection."""
    subscriber = live()
    subscriber.received(resync(b"repaint", epoch=EPOCH, base_seq=3), 1.0)
    subscriber.received(data(4, b"more"), 2.0)
    subscriber.closed(3.0)

    assert subscriber.epoch == EPOCH
    assert subscriber.last_seq == 4


def test_a_resize_survives_a_reconnect_and_is_re_sent_on_hello() -> None:
    subscriber = live()
    subscriber.received(resync(b"repaint", epoch=EPOCH), 1.0)
    subscriber.resized(COLS, ROWS, 2.0)
    subscriber.closed(3.0)

    subscriber.opened(4.0)
    messages = sent(subscriber.received(hello(), 4.0))
    kinds = [message.kind for message in messages]
    assert kinds == [CONTROL_RESUME, CONTROL_RESIZE]


def test_a_malformed_epoch_falls_back_to_the_honest_branch() -> None:
    """Pessimistic by rule: what cannot be read is treated as not held, so the resume repaints."""
    subscriber = live()
    subscriber.epoch = "not-a-uuid4"
    subscriber.last_seq = 12
    subscriber.closed(1.0)
    subscriber.opened(2.0)

    messages = sent(subscriber.received(hello(), 2.0))
    assert messages[0].epoch is None
    assert not any(message.kind == CONTROL_RESIZE for message in messages)


def test_an_inbound_resize_is_ignored_because_it_travels_the_other_way() -> None:
    subscriber = live()
    inbound = _wrap(SESSION, resize_message(Epoch(EPOCH), 80, 24))
    assert subscriber.received(inbound, 1.0) == []
