"""T-FRAME-CODEC -- the wire form round-trips arbitrary bytes, and never text-decodes them.

The DoD word is **byte-exact**, and the failure this file exists to catch is not a dropped
frame. It is a payload that survives a round trip through ``str``: pane output is an escape
stream, a pty splits it wherever the read boundary falls, and a multi-byte UTF-8 character
split across two frames is invalid in BOTH halves. A codec that decodes payloads therefore
works on every ASCII test anyone writes by hand and corrupts the first accented character or
box-drawing glyph a real TUI emits.

So the central test here is not "bytes round-trip". It is: **encode one character as two
frames, one byte each, and reassemble it.** No text codec can pass that.

The rejection tests matter for a related reason. A truncated or over-long buffer decoded on a
best-effort basis yields a plausible frame with the wrong ``seq``, which is worse than no
frame: the subscriber then trusts an ordinal that does not describe the stream.
"""

from __future__ import annotations

import math
from dataclasses import replace

import pytest
from shellbox_transport import Frame, Stream
from shellbox_transport.codec import (
    CLOSED_DETACHED,
    CLOSED_TERMINAL_GONE,
    CONTROL_CLOSED,
    CONTROL_ERROR,
    CONTROL_HELLO,
    CONTROL_INPUT,
    CONTROL_RESUME,
    CONTROL_RESYNC,
    FIELD_ASKED_SEQ,
    FIELD_BASE_SEQ,
    FIELD_CODE,
    FIELD_MESSAGE,
    FIELD_REASON,
    FIELD_SESSION_ID,
    HEADER_SIZE,
    MAGIC,
    UNORDERED_SEQ,
    CodecError,
    ControlMessage,
    closed_message,
    control_frame,
    decode_control,
    decode_frame,
    encode_control,
    encode_frame,
    error_message,
    hello_message,
    input_message,
    resize_message,
    resume_message,
    resync_message,
)
from shellbox_transport.seq import FIRST_SEQ, Discontinuity, Epoch, SeqAllocator

# 0xc3 0xa9 is "e with an acute accent" in UTF-8. Either byte alone is undecodable, which is
# exactly the property under test.
_E_ACUTE = "é".encode()


def _frame(data: bytes, *, seq: int = 1, stream: Stream = Stream.STDOUT) -> Frame:
    return Frame(session_id="sb-1", seq=seq, t=1712345678.5, stream=stream, data=data)


def test_a_multibyte_character_split_across_two_frames_reassembles() -> None:
    """CRITICAL: The test no text-based codec can pass.

    If this fails, the codec started decoding payloads. Do not "fix" it by joining the frames
    before decoding -- the publisher cannot know where a character boundary falls, because it
    reads whatever the pty hands it.
    """
    first, second = _E_ACUTE[:1], _E_ACUTE[1:]
    assert len(first) == 1 and len(second) == 1

    got = decode_frame(encode_frame(_frame(first, seq=1)))
    rest = decode_frame(encode_frame(_frame(second, seq=2)))

    assert got.data + rest.data == _E_ACUTE
    assert isinstance(got.data, bytes)


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(b"", id="empty"),
        pytest.param(b"\x00", id="nul"),
        pytest.param(bytes(range(256)), id="every-byte-value"),
        pytest.param(b"\xed\xa0\x80", id="not-valid-utf8"),
        pytest.param(b"\x1b[2J\x1b[H", id="escape-sequence"),
        pytest.param(b"x" * 70_000, id="larger-than-a-single-read"),
    ],
)
def test_round_trip_is_byte_exact(payload: bytes) -> None:
    """A 0-byte payload is a real case, not a degenerate one: a pty read can return nothing
    interesting and the publisher still needs the ordinal to advance."""
    frame = _frame(payload)
    assert decode_frame(encode_frame(frame)) == frame


def test_every_field_survives_including_the_timestamp_and_a_large_seq() -> None:
    """``t`` is encoded as an IEEE-754 double, so it round-trips exactly rather than to the
    nearest microsecond. A lossy ``t`` would be invisible until someone diffed two logs."""
    frame = Frame(
        session_id="sb-a-rather-long-session-identifier",
        seq=2**53 + 7,
        t=1712345678.123456,
        stream=Stream.STDERR,
        data=b"payload",
    )
    got = decode_frame(encode_frame(frame))
    assert got == frame
    assert got.t == frame.t
    assert not math.isnan(got.t)


def test_a_non_ascii_session_id_round_trips() -> None:
    """The session id is the one field that IS text, so it is UTF-8 with an explicit length
    rather than a delimiter -- a delimiter would collide with the id's own bytes."""
    frame = Frame(session_id="sb-é中", seq=3, t=0.0, stream=Stream.STDOUT, data=b"d")
    assert decode_frame(encode_frame(frame)).session_id == frame.session_id


def test_a_truncated_frame_is_rejected_rather_than_short_read() -> None:
    raw = encode_frame(_frame(b"abcdef"))
    with pytest.raises(CodecError, match="declares"):
        decode_frame(raw[:-1])


def test_a_trailing_byte_is_rejected() -> None:
    """A trailing byte means the sender and this reader disagree about the format. Tolerating
    it would let a format change look like it worked."""
    raw = encode_frame(_frame(b"abcdef"))
    with pytest.raises(CodecError, match="declares"):
        decode_frame(raw + b"\x00")


def test_a_buffer_shorter_than_the_header_is_rejected() -> None:
    with pytest.raises(CodecError, match="header alone"):
        decode_frame(b"SBX1")


def test_the_wrong_magic_is_rejected() -> None:
    raw = bytearray(encode_frame(_frame(b"abc")))
    raw[0:4] = b"SBX9"
    with pytest.raises(CodecError, match="magic"):
        decode_frame(bytes(raw))


def test_a_zeroed_stream_byte_is_rejected_rather_than_read_as_stdout() -> None:
    """Why ``Stream`` starts at 1. A zeroed or misaligned header must not decode as a
    plausible ``stdout`` frame, because that is damage arriving as data."""
    raw = bytearray(encode_frame(_frame(b"abc")))
    raw[4] = 0
    with pytest.raises(CodecError, match="not a known stream"):
        decode_frame(bytes(raw))
    assert 0 not in {int(stream) for stream in Stream}


def test_the_header_size_is_the_declared_constant() -> None:
    """``HEADER_SIZE`` is public because the server half slices on it. It must not drift."""
    assert len(encode_frame(_frame(b""))) == HEADER_SIZE + len("sb-1")
    assert MAGIC == b"SBX1"


def test_a_control_payload_splits_on_the_first_newline_only() -> None:
    """A repaint is full of newlines. If the header split on the LAST newline, or on every
    one, the repaint's own content would determine where the payload started."""
    repaint = b"line one\nline two\n\x1b[Hline three"
    message = ControlMessage(kind="resync", epoch=Epoch.new().value, payload=repaint)
    got = decode_control(encode_control(message))
    assert got.payload == repaint
    assert got.kind == "resync"


def test_a_control_message_carries_its_epoch_and_cannot_be_built_without_one() -> None:
    """ADR-12 enforced structurally rather than documented: the epoch is a required field, so
    a control message that leaves a subscriber unable to detect a ``seq`` restart cannot be
    constructed."""
    with pytest.raises(TypeError):
        ControlMessage(kind="resync")  # type: ignore[call-arg]


def test_control_fields_may_not_shadow_the_kind_or_the_epoch() -> None:
    """The JSON header is flat so a captured message reads as one record. That makes the two
    structural names reserved, and a caller that supplies them is refused rather than
    silently overriding the epoch."""
    with pytest.raises(CodecError, match="reserved"):
        encode_control(ControlMessage(kind="resync", epoch="e", fields={"epoch": "other"}))


def test_a_control_header_that_is_not_a_json_object_is_rejected() -> None:
    with pytest.raises(CodecError, match="not valid JSON"):
        decode_control(b"{not json\npayload")
    with pytest.raises(CodecError, match="not an object"):
        decode_control(b'"a string"\npayload')
    with pytest.raises(CodecError, match="needs a string 'kind'"):
        decode_control(b'{"epoch":null}\npayload')
    with pytest.raises(CodecError, match="not a string or null"):
        decode_control(b'{"kind":"resync","epoch":7}\npayload')


def test_an_absent_epoch_is_malformed_but_an_explicit_null_is_not() -> None:
    """The distinction a ``pop(..., None)`` would erase, and the reason it is not written that
    way.

    A sender that OMITTED the field never decided about the epoch, which is the bug the
    required field exists to prevent. A sender that wrote ``null`` decided it holds no attach,
    which is true of every frame the App server originates. Decoding them to the same value
    would let a message with a genuinely missing epoch pass as a server-originated one.
    """
    with pytest.raises(CodecError, match="no 'epoch'"):
        decode_control(b'{"kind":"resync"}\npayload')

    got = decode_control(b'{"epoch":null,"kind":"hello"}\n')
    assert got.epoch is None


def test_a_null_epoch_survives_the_round_trip_and_matches_no_other_epoch() -> None:
    """``None`` rather than ``""`` for "this sender holds no attach", and this is the property
    that buys.

    ``tmux.py``'s ``@shellbox_incarnation`` docstring states the rule this follows: an equality
    test two empty strings can satisfy is not an identity check. Two parties that each hold no
    attach must not compare equal, because that is a reported match between two things that
    were never the same attach -- exactly the misdelivery the epoch exists to detect.
    """
    message = ControlMessage(kind=CONTROL_HELLO, epoch=None)
    got = decode_control(encode_control(message))

    assert got.epoch is None
    assert b'"epoch":null' in encode_control(message), "it must be on the wire, not omitted"
    assert got.epoch != Epoch.new().value
    assert got.epoch != "", "an empty string would compare equal to another party's empty string"


def test_the_resync_message_names_the_gap_and_carries_the_repaint() -> None:
    epoch = Epoch.new()
    discontinuity = Discontinuity(epoch=epoch, asked_seq=3, base_seq=91, reason="below_floor")
    got = decode_control(encode_control(resync_message(discontinuity, b"\x1b[2Jrepaint")))
    assert got.kind == CONTROL_RESYNC
    assert got.epoch == epoch.value
    assert got.fields[FIELD_BASE_SEQ] == 91
    assert got.payload == b"\x1b[2Jrepaint"


def test_a_resync_cannot_be_built_without_a_repaint() -> None:
    """A resync names a gap. Without the state to recover from it, it is the undeclared hole
    with a label on it."""
    discontinuity = Discontinuity(epoch=Epoch.new(), asked_seq=1, base_seq=2, reason="below_floor")
    with pytest.raises(TypeError):
        resync_message(discontinuity)  # type: ignore[call-arg]


def test_hello_carries_the_bound_session_id_and_an_optional_viewer_email() -> None:
    """The viewer email is DISPLAY only. It is here so a renderer can show who is watching,
    and nothing in shellbox may make an access decision from it."""
    epoch = Epoch.new()
    got = decode_control(encode_control(hello_message("sb-7", epoch, "someone@example.com")))
    assert got.fields["session_id"] == "sb-7"
    assert got.fields["viewer_email"] == "someone@example.com"
    bare = decode_control(encode_control(hello_message("sb-7", epoch)))
    assert "viewer_email" not in bare.fields


def test_a_hello_from_a_sender_that_holds_no_attach_carries_a_null_epoch() -> None:
    """The App server's shape. It holds no pty, mints no epoch, and allocates no ordinals.

    The default is ``None`` rather than a required argument because the server is this
    message's only sender in Phase 3, and forcing it to invent a value is how a server-side
    epoch would get minted -- which would give a subscriber two sources of truth for "did
    ``seq`` restart".
    """
    got = decode_control(encode_control(hello_message("sb-7", viewer_email="v@example.com")))

    assert got.kind == CONTROL_HELLO
    assert got.epoch is None
    assert got.fields[FIELD_SESSION_ID] == "sb-7"
    assert got.fields["viewer_email"] == "v@example.com"


def test_an_error_names_a_machine_readable_code_and_prose_for_a_human() -> None:
    """Two fields, because they have two readers. A client branches on ``code``; ``message``
    is a sentence in a log and is free to be reworded without breaking that branch."""
    got = decode_control(
        encode_control(
            error_message("publisher_conflict", "a live publisher is already bound", session_id="s")
        )
    )

    assert got.kind == CONTROL_ERROR
    assert got.fields[FIELD_CODE] == "publisher_conflict"
    assert got.fields[FIELD_MESSAGE] == "a live publisher is already bound"
    assert got.fields[FIELD_SESSION_ID] == "s"
    assert got.epoch is None, "a refusal comes from a server that holds no attach"


def test_a_resize_below_one_column_or_row_is_refused() -> None:
    epoch = Epoch.new()
    assert decode_control(encode_control(resize_message(epoch, 80, 24))).fields["cols"] == 80
    with pytest.raises(CodecError, match="at least 1"):
        resize_message(epoch, 0, 24)


def test_a_control_frame_takes_an_ordinal_from_the_same_allocator() -> None:
    """Control frames are in-band. A renderer has to know whether the stream restarted before
    or after the output it is holding, and a side channel cannot answer that."""
    epoch = Epoch.new()
    frame = control_frame("sb-1", 42, 1.0, hello_message("sb-1", epoch))
    assert frame.stream is Stream.CONTROL
    assert frame.seq == 42
    assert decode_frame(encode_frame(frame)) == frame


def test_an_oversized_session_id_is_refused_by_the_encoder() -> None:
    """The header's length field is two bytes. Refusing here beats emitting a frame whose
    declared length has silently wrapped."""
    with pytest.raises(CodecError, match="65535"):
        encode_frame(replace(_frame(b"x"), session_id="s" * 70_000))


# --------------------------------------------------------------------------------------
# The messages the data path added (W19): input, resume, and the end of the stream
# --------------------------------------------------------------------------------------


def test_input_carries_raw_bytes_and_no_ordinal_of_its_own() -> None:
    """Input is a CONTROL message rather than a fourth ``Stream`` value, and that is a decision.

    ``Stream``'s members name what the PANE EMITTED; a keystroke is not that. A ``STDIN`` member
    would also put input in the publisher's ring, which records what the publisher SENT -- so a
    resume would replay the viewer's own typing back at them.

    The payload is opaque: no encoding, no key names, no allowlist. The bytes here are a
    bracketed-paste wrapper around a bare ``;``, neither of which ``keys.py`` can express.
    """
    payload = b"\x1b[200~ls -la ; echo hi\x1b[201~\r"
    got = decode_control(encode_control(input_message(payload)))

    assert got.kind == CONTROL_INPUT
    assert got.payload == payload
    assert got.epoch is None, "a subscriber holds no attach"
    assert got.fields == {}, "input needs no fields; the payload is the whole message"


def test_input_survives_a_payload_that_looks_like_a_json_header() -> None:
    """The split is on the FIRST newline, and a viewer can type newlines all day.

    A paste is the obvious way to put a ``{`` and a newline into this payload, so the boundary
    rule has to hold against input that mimics the header's own shape.
    """
    hostile = b'{"kind":"hello","epoch":null}\nand more\nlines'
    assert decode_control(encode_control(input_message(hostile))).payload == hostile


def test_a_resume_carries_exactly_plan_resumes_two_inputs() -> None:
    """The wire form and the decision function must not drift into disagreeing.

    ``resume_message``'s two fields are ``plan_resume``'s two arguments, and this asserts they
    round-trip into the shapes that function takes -- an ``int`` and a ``str | None``.
    """
    epoch = Epoch.new()
    got = decode_control(encode_control(resume_message(41, epoch)))

    assert got.kind == CONTROL_RESUME
    assert got.fields[FIELD_ASKED_SEQ] == 41
    assert got.epoch == epoch.value


def test_a_fresh_subscriber_resumes_from_zero_with_no_epoch() -> None:
    """The shape a viewer opening a tab sends, and the reason it is safe.

    ``FIRST_SEQ`` is 1, so no ring floor can satisfy 0 and the request takes the honest branch.
    A null epoch cannot match the publisher's either, so the two agree rather than relying on
    one of them.
    """
    got = decode_control(encode_control(resume_message(0)))

    assert got.fields[FIELD_ASKED_SEQ] == 0
    assert got.epoch is None


def test_the_end_of_the_stream_names_which_of_the_two_ways_it_ended() -> None:
    """``terminal_gone`` and ``detached`` are a closed set, enforced rather than documented.

    Collapsing them is a session-destroying bug: ``terminal_gone`` tells a viewer to stop
    reconnecting, and a detach misread as one tears down a session that is still running.
    """
    epoch = Epoch.new()
    for reason in (CLOSED_TERMINAL_GONE, CLOSED_DETACHED):
        got = decode_control(encode_control(closed_message(epoch, reason)))
        assert got.kind == CONTROL_CLOSED
        assert got.fields[FIELD_REASON] == reason
        assert got.epoch == epoch.value, "the publisher holds the attach, so it has an epoch"


def test_an_unrecognised_close_reason_cannot_be_constructed() -> None:
    """A third reason would be a protocol change, so it fails at the constructor.

    Left open, a typo would reach a renderer as a reason it has no branch for -- and the safe
    default there is "keep reconnecting", which is exactly wrong for a dead pane.
    """
    with pytest.raises(CodecError, match="not one of"):
        closed_message(Epoch.new(), "probably_fine")


def test_the_unordered_seq_is_below_every_ordinal_an_allocator_issues() -> None:
    """The one property that makes 0 safe to mean "this sender holds no position".

    The App server and the subscriber both originate frames without an allocator. If 0 were a
    value ``SeqAllocator`` could hand out, a subscriber would read one of those frames as a data
    ordinal and infer a gap that does not exist.
    """
    assert UNORDERED_SEQ < FIRST_SEQ
    assert SeqAllocator(Epoch.new()).next() == FIRST_SEQ
