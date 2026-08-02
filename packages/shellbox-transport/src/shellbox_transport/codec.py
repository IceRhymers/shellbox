"""The wire form: one frame in, one frame out, byte-exact -- plus the ``control`` messages.

Two encodings live here, and they are deliberately different shapes.

**A frame** is a fixed binary header followed by its payload, untouched. Not JSON: a JSON
frame must either base64 its payload, which costs a third of the bandwidth on the hot path,
or text-decode it, which cannot work. A multi-byte UTF-8 character split across two frames is
invalid in both halves, and a pty splits wherever the read boundary falls. So the payload is
never decoded, and ``decode_frame`` reaches it by slicing rather than by parsing.

**A control message** is a single-line JSON header, a newline, then optional raw bytes. The
JSON carries the fields both halves must agree on; the bytes after the newline carry a
repaint, which is pane output and therefore has the same "never text" property as any payload.
The delimiter is unambiguous only because ``json.dumps`` here emits ASCII with no raw newline
-- so never add ``indent`` to that call.

Every control message states the attach epoch. That is not a convention: ``ControlMessage``
takes it as a required field, so a control frame that never decided about one cannot be
constructed. The epoch is how a subscriber learns that ``seq`` restarted, and ``seq.py``'s
module docstring records why publishing it is safe.

CRITICAL: **"States" is not "carries", and the difference is a party that owns no attach.**
The App server originates ``hello`` and a refusal, and it holds no pty, mints no epoch, and
allocates no ordinals -- so there is no epoch for it to carry. It passes ``None``, which
encodes as a JSON ``null`` and decodes back to ``None``. That is a *decision*, and the field
stays required so it has to be made rather than defaulted into.

The alternative -- an empty string -- is the shape this repo rejects by name. ``tmux.py``'s
``@shellbox_incarnation`` docstring puts it exactly: an equality test two empty strings can
satisfy is not an identity check. A ``""`` epoch would compare equal to another ``""`` epoch
and report a match between two parties that each have no attach, which is the misdelivery
this mechanism exists to detect. ``None`` compares equal to nothing.

An ABSENT ``epoch`` key is still malformed, and ``decode_control`` distinguishes it from an
explicit ``null``. A sender that omitted the field did not decide; a sender that wrote
``null`` did.
"""

from __future__ import annotations

import json
import struct
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from shellbox_transport import Frame, Stream
from shellbox_transport.seq import Discontinuity, Epoch

__all__ = [
    "CLOSED_DETACHED",
    "CLOSED_TERMINAL_GONE",
    "CONTROL_CLOSED",
    "CONTROL_ERROR",
    "CONTROL_HELLO",
    "CONTROL_INPUT",
    "CONTROL_RESIZE",
    "CONTROL_RESUME",
    "CONTROL_RESYNC",
    "FIELD_ASKED_SEQ",
    "FIELD_BASE_SEQ",
    "FIELD_CODE",
    "FIELD_COLS",
    "FIELD_MESSAGE",
    "FIELD_REASON",
    "FIELD_ROWS",
    "FIELD_SESSION_ID",
    "FIELD_VIEWER_EMAIL",
    "HEADER_SIZE",
    "MAGIC",
    "UNORDERED_SEQ",
    "CodecError",
    "ControlMessage",
    "closed_message",
    "control_frame",
    "decode_control",
    "decode_frame",
    "encode_control",
    "encode_frame",
    "error_message",
    "hello_message",
    "input_message",
    "resize_message",
    "resume_message",
    "resync_message",
]

# The magic carries the version. A reader that finds anything else has either a frame from a
# future release or a byte offset it did not expect, and both must fail rather than be parsed
# on a best-effort basis: a misparsed header yields a plausible frame with a wrong `seq`,
# which is worse than no frame at all.
MAGIC = b"SBX1"

# magic, stream, seq, t, session id length, payload length. Big-endian and unaligned, so the
# header size is the same everywhere and does not depend on the platform's struct padding.
_HEADER = struct.Struct(">4sBQdHI")
HEADER_SIZE = _HEADER.size

_MAX_SESSION_ID_BYTES = 0xFFFF

CONTROL_HELLO = "hello"
CONTROL_RESYNC = "resync"
CONTROL_RESIZE = "resize"
CONTROL_ERROR = "error"
CONTROL_INPUT = "input"
CONTROL_RESUME = "resume"
CONTROL_CLOSED = "closed"

# Why the pane's stream ended, as a closed set of two. The distinction is the whole point and
# collapsing it is a session-destroying bug -- see ``closed_message``.
CLOSED_TERMINAL_GONE = "terminal_gone"
CLOSED_DETACHED = "detached"

# The ``seq`` a sender that allocates NO ordinals puts on a frame.
#
# The publisher owns the session's sequence space: one ``SeqAllocator`` per epoch is the only
# source of ordinals, which is what makes the stream gap-free by construction rather than by
# check. Two other parties originate frames -- the App server (``hello``, a refusal) and the
# subscriber (input, resize, a resume request) -- and neither holds an attach or an allocator.
#
# They must not appear to hold a position in that space. A subscriber that read a server frame
# or its own echoed input as a data ordinal would infer a gap that does not exist, and repaint
# for it. 0 is safe to mean "no position" because ``seq.FIRST_SEQ`` is 1, so no allocator ever
# issues it.
UNORDERED_SEQ = 0

FIELD_SESSION_ID = "session_id"
FIELD_VIEWER_EMAIL = "viewer_email"
FIELD_ASKED_SEQ = "asked_seq"
FIELD_BASE_SEQ = "base_seq"
FIELD_REASON = "reason"
FIELD_COLS = "cols"
FIELD_ROWS = "rows"
FIELD_CODE = "code"
FIELD_MESSAGE = "message"

_KIND = "kind"
_EPOCH = "epoch"
# `fields` is flattened into the JSON object so that a human reading a captured message sees
# one flat record. These two names are therefore reserved, and `encode_control` refuses them
# rather than letting a caller shadow the epoch with its own value.
_RESERVED_FIELDS = frozenset({_KIND, _EPOCH})


class CodecError(Exception):
    """Malformed input on the wire.

    Every decode failure is this one type, because a caller's response to all of them is the
    same: discard the message and treat the stream as untrustworthy from here. Distinguishing
    "bad magic" from "short payload" would invite a caller to recover from one of them, and
    there is no safe partial recovery from a damaged frame.
    """


def encode_frame(frame: Frame) -> bytes:
    """Encode one frame. The payload is copied verbatim and never inspected."""
    session_id = frame.session_id.encode("utf-8")
    if len(session_id) > _MAX_SESSION_ID_BYTES:
        raise CodecError(f"session_id is {len(session_id)} bytes; the header allows 65535")
    if frame.seq < 0:
        raise CodecError(f"seq {frame.seq} is negative")
    header = _HEADER.pack(
        MAGIC, int(frame.stream), frame.seq, frame.t, len(session_id), len(frame.data)
    )
    return header + session_id + frame.data


def decode_frame(raw: bytes) -> Frame:
    """Decode one frame, or raise ``CodecError``.

    The declared lengths must account for the buffer **exactly**. A trailing byte means the
    sender and this reader disagree about the format, and a short buffer means truncation --
    accepting either one delivers a payload that is not the payload that was sent, which is
    the one thing this codec exists to prevent.
    """
    if len(raw) < HEADER_SIZE:
        raise CodecError(f"frame is {len(raw)} bytes; the header alone needs {HEADER_SIZE}")
    magic, stream_value, seq, t, id_len, data_len = _HEADER.unpack_from(raw)
    if magic != MAGIC:
        raise CodecError(f"magic {magic!r} is not {MAGIC!r}")
    try:
        stream = Stream(stream_value)
    except ValueError as exc:
        # Reached by a zeroed or misaligned header, which is why `Stream` starts at 1.
        raise CodecError(f"stream {stream_value} is not a known stream") from exc
    expected = HEADER_SIZE + id_len + data_len
    if len(raw) != expected:
        raise CodecError(f"frame declares {expected} bytes and carries {len(raw)}")
    try:
        session_id = raw[HEADER_SIZE : HEADER_SIZE + id_len].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CodecError("session_id is not valid UTF-8") from exc
    return Frame(
        session_id=session_id,
        seq=seq,
        t=t,
        stream=stream,
        data=bytes(raw[HEADER_SIZE + id_len :]),
    )


@dataclass(frozen=True, slots=True)
class ControlMessage:
    """One ``control``-stream message: a kind, the epoch, JSON fields, and optional bytes.

    ``epoch`` is required, and that is the mechanism rather than the documentation of it.
    ADR-12 keeps ``Frame`` at the epic's five fields and carries the epoch here instead, so
    every control message a subscriber receives lets it answer "did ``seq`` restart?".

    ``None`` is a legal value and means **this sender holds no attach**, which is true of every
    frame the App server originates. It is required-but-nullable rather than defaulted so that
    "there is no epoch here" is something a sender wrote down. See the module docstring for why
    it is ``None`` and not ``""``.
    """

    kind: str
    epoch: str | None
    fields: Mapping[str, Any] = field(default_factory=dict)
    """JSON-safe scalars only. Nothing here may be bytes -- bytes go in ``payload``."""
    payload: bytes = b""
    """Raw bytes after the newline. A repaint, or empty."""


def encode_control(message: ControlMessage) -> bytes:
    """Encode a control message to a frame payload."""
    reserved = _RESERVED_FIELDS.intersection(message.fields)
    if reserved:
        raise CodecError(f"fields may not contain the reserved names {sorted(reserved)}")
    record: dict[str, Any] = {_KIND: message.kind, _EPOCH: message.epoch}
    record.update(message.fields)
    # NORMATIVE: compact separators, ASCII, sorted keys, and NO `indent`. The newline below is
    # the only delimiter, so a header that could contain a newline would make the payload
    # boundary depend on the message's contents.
    header = json.dumps(record, separators=(",", ":"), sort_keys=True).encode("ascii")
    return header + b"\n" + message.payload


def decode_control(raw: bytes) -> ControlMessage:
    """Decode a control frame's payload, or raise ``CodecError``.

    Splits on the FIRST newline only, so a repaint full of newlines -- which every repaint is
    -- cannot be mistaken for header structure.
    """
    header, _, payload = raw.partition(b"\n")
    if not header:
        raise CodecError("control payload has no JSON header")
    try:
        record = json.loads(header)
    except ValueError as exc:
        raise CodecError("control header is not valid JSON") from exc
    if not isinstance(record, dict):
        raise CodecError(f"control header is {type(record).__name__}, not an object")
    # Membership, not `pop(..., None)`, because an ABSENT epoch and an explicit `null` must not
    # arrive here as the same value. A sender that omitted the field never decided about the
    # epoch; a sender that wrote `null` decided it holds no attach. Only the first is malformed.
    if _EPOCH not in record:
        raise CodecError("control header has no 'epoch'; null is how a sender says it has none")
    kind = record.pop(_KIND, None)
    epoch = record.pop(_EPOCH)
    if not isinstance(kind, str):
        raise CodecError("control header needs a string 'kind'")
    if epoch is not None and not isinstance(epoch, str):
        raise CodecError(f"control header 'epoch' is {type(epoch).__name__}, not a string or null")
    return ControlMessage(kind=kind, epoch=epoch, fields=record, payload=payload)


def control_frame(session_id: str, seq: int, t: float, message: ControlMessage) -> Frame:
    """Wrap a control message in a ``control``-stream frame.

    It takes a ``seq`` from the same allocator as every data frame, on purpose: a control
    message is ordered against the bytes around it, and a renderer needs to know whether the
    stream restarted before or after the output it is holding.
    """
    return Frame(
        session_id=session_id,
        seq=seq,
        t=t,
        stream=Stream.CONTROL,
        data=encode_control(message),
    )


def hello_message(
    session_id: str, epoch: Epoch | None = None, viewer_email: str | None = None
) -> ControlMessage:
    """The handshake that gates "connected". The server sends it; the client waits for it.

    A 101 is not proof of a working transport. An unauthenticated upgrade against the
    Databricks Apps edge returned a 302, and an unauthenticated POST returned HTTP 200 with an
    HTML login body (measured, ``probe/FINDINGS.md``), so a client that treats the upgrade as
    success streams into a void and reports it as working. The client is not connected until
    this message arrives, within a bounded deadline.

    ``session_id`` is the session the server actually bound. A client that dialed for one
    session and receives a hello naming another must treat it as an ERROR, not a warning: it
    means two agents' streams could cross.

    CRITICAL: ``viewer_email`` is for DISPLAY only, never authorization. It is whatever the
    edge injected as ``X-Forwarded-Email``, and nothing in shellbox may make an access
    decision from it.

    ``epoch`` defaults to ``None`` because the App server is this message's only sender in
    Phase 3 and it holds no attach. A publisher echoing its own epoch back passes one. A client
    must therefore NOT treat a null epoch here as a mismatch: it is the expected value, and
    warning on it would put a line in the log on every reconnect the edge causes.
    """
    fields: dict[str, Any] = {FIELD_SESSION_ID: session_id}
    if viewer_email is not None:
        fields[FIELD_VIEWER_EMAIL] = viewer_email
    return ControlMessage(
        kind=CONTROL_HELLO, epoch=None if epoch is None else epoch.value, fields=fields
    )


def error_message(code: str, message: str, *, session_id: str) -> ControlMessage:
    """A refusal, named in-band: a machine-readable ``code`` and prose for a human.

    Two fields rather than one because they have different readers and different lifetimes.
    ``code`` is a closed set the client branches on -- ``publisher_conflict`` and
    ``subscriber_conflict`` are the whole of it in Phase 3 -- and ``message`` is a sentence an
    operator reads in a log, free to be reworded without breaking a branch.

    CRITICAL: **A refusal is not an authentication failure, and a client must not fold it into
    one.** Both codes are terminal for the socket they refuse and neither is terminal for the
    session: it becomes claimable the moment the other holder goes away. A client that
    classified this as ``auth_failed`` would stop retrying a session it is about to be able to
    hold.

    Sent as a frame rather than as a close code, because the edge kills healthy sockets *with
    no close frame* -- so "closed carrying no reason" is already the signature of a routine
    kill, and a refusal reported that way would read as one. ``shellbox_app.server``'s module
    docstring carries the full argument.

    The epoch is ``None``: the App server holds no attach. See the module docstring.
    """
    return ControlMessage(
        kind=CONTROL_ERROR,
        epoch=None,
        fields={FIELD_CODE: code, FIELD_MESSAGE: message, FIELD_SESSION_ID: session_id},
    )


def resync_message(discontinuity: Discontinuity, repaint: bytes) -> ControlMessage:
    """The declared discontinuity: what was lost, where the new base is, and the repaint.

    ``repaint`` is required rather than optional, so a resync that names a gap without
    carrying the state to recover from it cannot be built. It must come from ``tmux
    capture-pane -p -e`` with ``lines=0`` -- the VISIBLE pane, never scrollback. Scrollback is
    up to a 20000-line ``history_limit`` of ANSI, and every publisher in a sandbox resyncs in
    the same second after an edge kill, so a scrollback repaint turns one reconnect storm into
    an outage.

    This message must be the FIRST thing the resuming subscriber receives. A data frame ahead
    of it is the undeclared hole that ``FrameTransport.subscribe`` forbids.
    """
    return ControlMessage(
        kind=CONTROL_RESYNC,
        epoch=discontinuity.epoch.value,
        fields={
            FIELD_ASKED_SEQ: discontinuity.asked_seq,
            FIELD_BASE_SEQ: discontinuity.base_seq,
            FIELD_REASON: discontinuity.reason,
        },
        payload=repaint,
    )


def resize_message(epoch: Epoch, cols: int, rows: int) -> ControlMessage:
    """A viewer resize, travelling in-band so it is ordered against the output it reflows.

    The publisher applies it with a ``TIOCSWINSZ`` ioctl on the attach pty's master fd, which
    costs no tmux round trip.

    WARNING: Never implement the tmux side of this with a GLOBAL ``window-size manual``. A
    global ``window-size manual`` kills the tmux server on the next ``new-session`` -- 15/15
    in both measured lanes -- taking every other pooled agent's sessions with it. See
    ``tests/unit/test_no_global_window_size.py``, which greps the shipped packages for it.
    """
    if cols < 1 or rows < 1:
        raise CodecError(f"cols={cols} rows={rows}: both must be at least 1")
    return ControlMessage(
        kind=CONTROL_RESIZE, epoch=epoch.value, fields={FIELD_COLS: cols, FIELD_ROWS: rows}
    )


def input_message(data: bytes) -> ControlMessage:
    """Keystrokes travelling from the subscriber to the pane, byte-exact.

    Input is carried as a CONTROL message rather than as a fourth ``Stream`` value, and that is
    a decision rather than a convenience. ``Stream``'s wire values are normative and its
    members name what the *pane emitted*; a keystroke is not that. Adding ``STDIN`` would also
    put input in the ring, which describes what this publisher SENT -- so a resume would
    replay the viewer's typing back at them.

    The payload is the raw bytes and nothing else: no encoding, no key names, no allowlist.
    That is the point of the pty path -- ``keys.py``'s allowlist is closed by construction
    ("anything not listed is ``invalid_key``"), so an application-mode keystroke it does not
    name is unreachable through the tool surface.

    WARNING: **Byte-exact here means byte-exact to the PTY, not to the pane process.** The
    attach client is a tmux client, and tmux parses its terminal input into keys before sending
    them on. Measured against tmux 3.6b through a live attach into a raw-mode pane: plain bytes
    including ``;`` and TAB arrive verbatim, but a bracketed-paste wrapper
    (``ESC[200~`` ... ``ESC[201~``) is CONSUMED by the client and only the text between the
    markers reaches the pane. So a caller must not treat this path as a transparent pipe to the
    pane's tty, and the plan's claim that a pty "carries bracketed-paste sequences" is true only
    of the bytes inside them.

    CRITICAL: **The publisher must apply a per-line ceiling before writing this to the pty.**
    H4 is not a send-path property, it is the receiving pane's tty in canonical mode, and tmux
    forwards an attach client's input to that same tty. Measured through a live attach (spike
    F18): 8192 bytes plus a newline delivered **4096 bytes on Linux, silently truncated**, and
    0 on macOS. A truncated command is a different, still-executable command. The ceiling is
    the one the repo already ships -- ``max_send_line_bytes``, raising ``LineTooLong`` -- and
    not a new number at 4096.

    The epoch is ``None``: a subscriber holds no attach. See the module docstring.
    """
    return ControlMessage(kind=CONTROL_INPUT, epoch=None, fields={}, payload=data)


def resume_message(from_seq: int, epoch: Epoch | None = None) -> ControlMessage:
    """A subscriber asking to pick the stream back up at ``from_seq``. Answered by ADR-11.

    This is the request side of ``FrameTransport.subscribe(session_id, from_seq, epoch=...)``,
    and its two fields are exactly ``plan_resume``'s two inputs -- deliberately, so that the
    wire form and the decision function cannot drift into disagreeing about what was asked.

    It exists because the edge kills **every** open socket in the same second, so a reconnect
    is not the publisher re-dialling into a subscriber that stayed put: both ends re-dial, and
    the subscriber is the only party that knows how much of the stream it actually rendered.
    Without this message a publisher would have to assume the worst on every reconnect and
    repaint unconditionally -- which is correct but makes ADR-11's byte-exact branch dead code.

    ``epoch`` is the subscriber's LAST SEEN epoch, and ``None`` means it holds nothing. Both
    resolve safely: ``plan_resume`` checks the epoch **before** the ring floor, because ``seq``
    restarts in each epoch and a stale ``seq`` can sit comfortably above the current floor
    while naming a different position in the stream. Pass ``from_seq=0`` with no epoch for a
    fresh subscriber; ``FIRST_SEQ`` is 1, so no ring floor can satisfy 0 and it takes the
    honest branch, which is the right answer for a viewer opening a tab.
    """
    return ControlMessage(
        kind=CONTROL_RESUME,
        epoch=None if epoch is None else epoch.value,
        fields={FIELD_ASKED_SEQ: from_seq},
    )


def closed_message(epoch: Epoch, reason: str) -> ControlMessage:
    """The pane's stream ended, and **which** of the two ways it ended.

    CRITICAL: ``terminal_gone`` and ``detached`` must never be collapsed, and the cost is
    asymmetric. ``terminal_gone`` means the pane's process exited, so a viewer should stop
    reconnecting -- there is nothing left to watch. ``detached`` means only this attach client
    went away, and a viewer that read it as ``terminal_gone`` would tear down a session that is
    still running (``omnigent/terminals/ws_bridge.py:71-80`` states the same rule for the same
    reason).

    ``has-session`` cannot tell them apart. shellbox sets ``remain-on-exit on`` GLOBALLY, so a
    session deliberately outlives its pane's process -- that is what keeps the final output
    readable. The signal is ``#{pane_dead}``, measured in both directions with a live client
    attached (spike F19): on detach the pane reads ``0``, and when the process exits it reads
    ``1`` while ``has-session`` still returns rc=0.

    Sent as a frame rather than a WebSocket close code, unlike upstream's 4404/4405. Two
    reasons, both measured: the edge kills healthy sockets **with no close frame at all**, so
    "closed carrying no reason" is already the signature of a routine kill; and F19 found the
    attach client OUTLIVES the pane's process, so the publisher's socket is still up and able
    to carry a frame at the moment it has something to say.
    """
    if reason not in (CLOSED_TERMINAL_GONE, CLOSED_DETACHED):
        raise CodecError(
            f"closed reason {reason!r} is not one of "
            f"{CLOSED_TERMINAL_GONE!r} or {CLOSED_DETACHED!r}"
        )
    return ControlMessage(kind=CONTROL_CLOSED, epoch=epoch.value, fields={FIELD_REASON: reason})
