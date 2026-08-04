"""`static/codec.js` and `shellbox_transport.codec` must agree about the wire.

Two implementations of one format, and nothing executes both -- ADR-23's cost, paid here in the
narrowest way that is still worth something. Every constant the two share is compared, so a
magic, a header size, a stream number, a message kind, a field name or a closed-reason value
cannot move on one side alone.

WHAT THIS DOES NOT CATCH, stated plainly because the file's existence invites the opposite
assumption: it does not execute the JavaScript, so a byte-order mistake, an off-by-one offset or
a mis-sorted JSON key would pass every assertion here. The encoders were compared byte-for-byte
by hand when they were written; nothing in CI repeats that. `W38`'s live run is what exercises
the JavaScript at all.
"""

from __future__ import annotations

import pytest
from jsconst import js_constants
from shellbox_app.ui import STATIC_ROOT
from shellbox_transport import Stream
from shellbox_transport.codec import (
    CLOSED_DETACHED,
    CLOSED_TERMINAL_GONE,
    CONTROL_CLOSED,
    CONTROL_ERROR,
    CONTROL_HELLO,
    CONTROL_INPUT,
    CONTROL_RESIZE,
    CONTROL_RESUME,
    CONTROL_RESYNC,
    FIELD_ASKED_SEQ,
    FIELD_BASE_SEQ,
    FIELD_CODE,
    FIELD_COLS,
    FIELD_MESSAGE,
    FIELD_REASON,
    FIELD_ROWS,
    FIELD_SESSION_ID,
    FIELD_VIEWER_EMAIL,
    HEADER_SIZE,
    MAGIC,
    UNORDERED_SEQ,
)

# Every name both halves declare, mapped to the Python value it must equal. Written out rather
# than discovered, so that DELETING a constant from the JavaScript fails this test instead of
# shrinking the comparison to whatever survived.
SHARED: dict[str, object] = {
    "MAGIC": MAGIC.decode("ascii"),
    "HEADER_SIZE": HEADER_SIZE,
    "UNORDERED_SEQ": UNORDERED_SEQ,
    "STREAM_STDOUT": int(Stream.STDOUT),
    "STREAM_STDERR": int(Stream.STDERR),
    "STREAM_CONTROL": int(Stream.CONTROL),
    "CONTROL_HELLO": CONTROL_HELLO,
    "CONTROL_RESYNC": CONTROL_RESYNC,
    "CONTROL_RESIZE": CONTROL_RESIZE,
    "CONTROL_ERROR": CONTROL_ERROR,
    "CONTROL_INPUT": CONTROL_INPUT,
    "CONTROL_RESUME": CONTROL_RESUME,
    "CONTROL_CLOSED": CONTROL_CLOSED,
    "CLOSED_TERMINAL_GONE": CLOSED_TERMINAL_GONE,
    "CLOSED_DETACHED": CLOSED_DETACHED,
    "FIELD_SESSION_ID": FIELD_SESSION_ID,
    "FIELD_VIEWER_EMAIL": FIELD_VIEWER_EMAIL,
    "FIELD_ASKED_SEQ": FIELD_ASKED_SEQ,
    "FIELD_BASE_SEQ": FIELD_BASE_SEQ,
    "FIELD_REASON": FIELD_REASON,
    "FIELD_COLS": FIELD_COLS,
    "FIELD_ROWS": FIELD_ROWS,
    "FIELD_CODE": FIELD_CODE,
    "FIELD_MESSAGE": FIELD_MESSAGE,
}


@pytest.fixture(scope="module")
def declared() -> dict[str, object]:
    return js_constants(STATIC_ROOT / "codec.js")


@pytest.mark.parametrize("name", sorted(SHARED))
def test_the_browser_codec_declares_the_same_value(name: str, declared: dict[str, object]) -> None:
    assert name in declared, (
        f"static/codec.js no longer declares {name}. The browser and the App would then be "
        "free to disagree about the wire, which is what this file exists to prevent."
    )
    assert declared[name] == SHARED[name], (
        f"static/codec.js declares {name} = {declared[name]!r}, and "
        f"shellbox_transport.codec says {SHARED[name]!r}."
    )


def test_the_header_size_matches_the_struct_it_describes() -> None:
    """The one value a reader is most likely to hand-count wrong. 4 + 1 + 8 + 8 + 2 + 4."""
    assert HEADER_SIZE == 27


def test_the_comparison_is_not_vacuous() -> None:
    """A `SHARED` that quietly emptied would make every parametrised case above disappear."""
    assert len(SHARED) >= 24
