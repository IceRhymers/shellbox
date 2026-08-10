"""The App lane's socket double, and the two decoders every test of it needs.

Shared by `tests/unit/test_app_server.py` and `tests/unit/test_app_database.py` so that two
files do not each grow their own socket double and then disagree about what a dead socket
does. `tests/unit/wsfakes.py` is the same idea for the CLIENT half of the transport, and the
two are deliberately separate: that one doubles a ``websockets`` connection, and this one
doubles Starlette's ``WebSocket``.

The methods this class defines are exactly the surface `shellbox_app.server`'s handlers use.
A handler reaching for anything else fails here with an ``AttributeError``, which is a cheap
way to notice the accept path growing a dependency on the framework.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import anyio
from shellbox_transport.codec import (
    CONTROL_HELLO,
    ControlMessage,
    decode_control,
    decode_frame,
)
from starlette.websockets import WebSocketState

# Short enough that a handler waiting on a queued message is not the slow part of the suite.
POLL_INTERVAL = 0.001


@dataclass
class FakeSocket:
    """A ``WebSocket`` stand-in that records what was sent and can be held open.

    ``hold`` is the mechanism the concurrency assertions turn on. With it set, ``receive`` waits
    for ``hang_up()`` instead of reporting a disconnect, so a handler stays suspended inside its
    relay loop and the registry can be inspected while a publisher is genuinely live. Without it
    a handler runs to completion immediately, which is what the single-socket assertions want.
    """

    headers: dict[str, str] = field(default_factory=dict)
    hold: bool = False
    client_state: WebSocketState = WebSocketState.CONNECTED

    def __post_init__(self) -> None:
        self.accepted = False
        self.sent: list[bytes] = []
        self.closed: int | None = None
        self._inbox: list[dict[str, object]] = []
        self._hung_up = False

    # -- the surface the handlers use ---------------------------------------------------

    async def accept(self) -> None:
        self.accepted = True

    async def send_bytes(self, data: bytes) -> None:
        if self.client_state is not WebSocketState.CONNECTED:
            raise RuntimeError("cannot send on a closed socket")
        self.sent.append(data)

    async def receive(self) -> dict[str, object]:
        """The next queued message, or a disconnect.

        WARNING: This POLLS the inbox rather than waiting on an event, and the difference is not
        a style choice. A test queues messages *after* the handler is already suspended here --
        that is the only way to assert on a socket whose peer bound later. An implementation that
        waited on a single event would return the disconnect on release and silently never
        deliver anything queued in the meantime, so the relay assertions would pass by never
        running.
        """
        while True:
            if self._inbox:
                return self._inbox.pop(0)
            if not self.hold or self._hung_up:
                return {"type": "websocket.disconnect", "code": 1006}
            await anyio.sleep(POLL_INTERVAL)

    async def close(self, code: int = 1000) -> None:
        self.closed = code
        self.client_state = WebSocketState.DISCONNECTED

    # -- test-side controls ------------------------------------------------------------

    def queue_bytes(self, *payloads: bytes) -> None:
        self._inbox += [{"type": "websocket.receive", "bytes": payload} for payload in payloads]

    def queue_text(self, text: str) -> None:
        """A protocol violation on purpose: the frame protocol is binary."""
        self._inbox.append({"type": "websocket.receive", "text": text})

    def hang_up(self) -> None:
        """Report a disconnect once the inbox drains, as an edge kill would."""
        self._hung_up = True

    def die(self) -> None:
        """Make every later send fail, without reporting a disconnect to this socket's handler.

        The shape of a peer dying: the *other* handler's send is what discovers it.
        """
        self.client_state = WebSocketState.DISCONNECTED


def controls(socket: FakeSocket) -> list[ControlMessage]:
    """Every control message the server originated on ``socket``, decoded through the codec.

    Decoded rather than compared as bytes on purpose: this asserts the server's frames are
    readable by the same codec the client half uses, so a header change on either side fails
    here rather than at the first live reconnect.
    """
    return [decode_control(decode_frame(raw).data) for raw in socket.sent]


def hello_of(socket: FakeSocket) -> ControlMessage:
    """The ``hello`` message, asserting exactly one was sent."""
    found = [message for message in controls(socket) if message.kind == CONTROL_HELLO]
    assert len(found) == 1, f"expected exactly one hello, got {found!r}"
    return found[0]
