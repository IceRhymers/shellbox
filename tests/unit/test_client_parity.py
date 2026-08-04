"""`static/protocol.js` and `shellbox_app.client` must agree about the numbers and the codes.

The browser runs a transcription of `packages/shellbox-app/src/shellbox_app/client.py`, and
nothing executes both. This file is the whole of what CI does about that, and its reach is
worth being precise about:

* It CATCHES a deadline, a backoff bound, a notice code or a stop code changed on one side only
  -- which is the realistic drift, because those are the values someone tunes.
* It does NOT catch divergent logic. A branch deleted from the JavaScript, a `reset` turned into
  a `write`, an ordering mistake in the refusal path: every one of those passes here.
  `tests/unit/test_client_protocol.py` asserts the BEHAVIOUR, and it asserts it of the Python
  only. Above the protocol layer, `W38`'s live run is the only gate that exists.

That asymmetry is ADR-23 working as decided, not an oversight. It is written here because this
is the file whose green tick would otherwise be read as "the browser client is tested".
"""

from __future__ import annotations

import pytest
from jsconst import js_constants
from shellbox_app.client import (
    BACKOFF_CAP_SECONDS,
    BACKOFF_FLOOR_SECONDS,
    CODE_PUBLISHER_CONFLICT,
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
    Phase,
)
from shellbox_app.server import WS_PING_INTERVAL_SECONDS, WS_PING_TIMEOUT_SECONDS
from shellbox_app.ui import STATIC_ROOT

# Written out rather than discovered, so that DELETING a constant from the JavaScript fails here
# instead of shrinking the comparison to whatever happened to survive.
SHARED: dict[str, object] = {
    "HELLO_DEADLINE_SECONDS": HELLO_DEADLINE_SECONDS,
    "NO_PUBLISHER_DEADLINE_SECONDS": NO_PUBLISHER_DEADLINE_SECONDS,
    "SUBSCRIBER_CONFLICT_BOUND_SECONDS": SUBSCRIBER_CONFLICT_BOUND_SECONDS,
    "BACKOFF_FLOOR_SECONDS": BACKOFF_FLOOR_SECONDS,
    "BACKOFF_CAP_SECONDS": BACKOFF_CAP_SECONDS,
    "NO_PUBLISHER_MESSAGE": NO_PUBLISHER_MESSAGE,
    "NOTICE_NO_PUBLISHER": NOTICE_NO_PUBLISHER,
    "NOTICE_DETACHED": NOTICE_DETACHED,
    "NOTICE_STREAM_GAP": NOTICE_STREAM_GAP,
    "CODE_SUBSCRIBER_CONFLICT": CODE_SUBSCRIBER_CONFLICT,
    "CODE_PUBLISHER_CONFLICT": CODE_PUBLISHER_CONFLICT,
    "CODE_TERMINAL_GONE": CODE_TERMINAL_GONE,
    "CODE_SESSION_MISMATCH": CODE_SESSION_MISMATCH,
    # The phases, so the two state machines cannot come to disagree about what a state is
    # CALLED even where they still agree about what it means.
    "PHASE_DIALING": Phase.DIALING.value,
    "PHASE_AWAITING_HELLO": Phase.AWAITING_HELLO.value,
    "PHASE_LIVE": Phase.LIVE.value,
    "PHASE_STOPPED": Phase.STOPPED.value,
}


@pytest.fixture(scope="module")
def declared() -> dict[str, object]:
    return js_constants(STATIC_ROOT / "protocol.js", js_constants(STATIC_ROOT / "codec.js"))


@pytest.mark.parametrize("name", sorted(SHARED))
def test_the_browser_client_declares_the_same_value(name: str, declared: dict[str, object]) -> None:
    assert name in declared, (
        f"static/protocol.js no longer declares {name}. The browser and the Python twin would "
        "then be free to disagree about it, silently."
    )
    assert declared[name] == SHARED[name], (
        f"static/protocol.js declares {name} = {declared[name]!r}, and shellbox_app.client "
        f"says {SHARED[name]!r}. Change both, in the same commit."
    )


def test_the_browsers_conflict_bound_also_outlasts_the_apps_reaper() -> None:
    """The relationship, asserted against the JAVASCRIPT's own number rather than the Python's.

    `tests/unit/test_client_protocol.py` asserts this for `shellbox_app.client`. Asserting it
    again here is not duplication: this is the number the browser actually retries on, and the
    two files agreeing is exactly what the parametrised case above could stop being true.
    """
    declared = js_constants(STATIC_ROOT / "protocol.js", js_constants(STATIC_ROOT / "codec.js"))
    reaper = WS_PING_INTERVAL_SECONDS + WS_PING_TIMEOUT_SECONDS
    assert isinstance(declared["SUBSCRIBER_CONFLICT_BOUND_SECONDS"], float)
    assert declared["SUBSCRIBER_CONFLICT_BOUND_SECONDS"] > reaper


def test_the_comparison_is_not_vacuous() -> None:
    assert len(SHARED) >= 17
