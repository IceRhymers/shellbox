"""T-FRAME-SIZE -- the inbound frame cap is a decision, passed to the library and enforced by it.

`websockets` bounds the largest inbound message with `max_size`, and its default (1 MiB on both
14.2 and 15.0.1) happens to equal this publisher's ring size. "Happens to" is the whole problem:
the dependency range (`>=14.2,<16`) spans two versions, so a default nobody here chose must not
be what decides the frame boundary this module operates at. So `max_size` is pinned to
`DEFAULT_RING_BYTES` and passed EXPLICITLY at the dial, the same treatment the keepalive values
get and for the same reason (`tests/unit/test_no_keepalive.py`).

Three claims, and the third is measured against the real library rather than a double:

1. the cap defaults to the ring size, so the two are sized from one constant;
2. that value reaches the library on EVERY dial, not merely the first -- asserted on what the
   dial received, in `test_no_keepalive.py`'s shape;
3. a real inbound frame OVER the cap tears the socket down with a 1009 close that both carries
   that code and classifies transient, and a substantial frame UNDER it is delivered intact --
   so the teardown is the cap firing and not some other close.

The last pair runs a real `websockets` server on the loopback and dials it with the module's own
default `connect` (`_NoRedirect`), because a hand-built `ConnectionClosedError` would assert the
classifier's mapping while proving nothing about whether `max_size` is wired to the library at
all. The over-cap frame is what proves the wiring: at the default 1 MiB it would arrive fine.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import anyio
from shellbox_mcp.transport import Failure, WSTransport, WSTransportConfig, classify_failure
from shellbox_transport import Frame, Stream
from shellbox_transport.codec import decode_frame, encode_frame
from shellbox_transport.seq import DEFAULT_RING_BYTES
from websockets.asyncio.client import ClientConnection
from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed
from wsfakes import SESSION_ID, FakeConnection, RecordingSleep, ScriptedDial, hello_bytes

EPOCH = str(uuid.uuid4())

# A small cap, so an "over the cap" frame is a few KiB rather than a megabyte -- the loopback
# tests below need only prove the boundary is enforced, not what its production value is.
_TEST_CAP = 4096


def config(**overrides: object) -> WSTransportConfig:
    base = {"url": "wss://app.example/publish", "session_id": SESSION_ID, "epoch": EPOCH}
    return WSTransportConfig(**{**base, **overrides})


def data_frame(size: int) -> bytes:
    """An encoded STDOUT frame carrying `size` payload bytes -- the thing the cap measures."""
    payload = b"x" * size
    frame = Frame(session_id=SESSION_ID, seq=2, t=1.0, stream=Stream.STDOUT, data=payload)
    return encode_frame(frame)


# --------------------------------------------------------------------------------------
# 1. The cap and the ring are sized from one constant
# --------------------------------------------------------------------------------------


def test_the_cap_defaults_to_the_ring_size() -> None:
    """T-FRAME-SIZE. The pin the module comment describes, asserted as an equality.

    The ring bounds the bytes this side sends; the cap bounds the largest single frame it will
    accept back. Sizing them from the same constant is what stops them drifting apart when one is
    tuned and the other is forgotten -- so this is an equality, not two independent literals.
    """
    assert config().max_size == DEFAULT_RING_BYTES
    assert config().max_size == 1 << 20, "an explicit 1 MiB, taken from the ring's constant"


# --------------------------------------------------------------------------------------
# 2. The value reaches the library, on every dial
# --------------------------------------------------------------------------------------


def test_the_cap_reaches_the_dial_on_every_attempt() -> None:
    """T-FRAME-SIZE. Passed, not defaulted -- and on the retry, not merely the first try.

    Asserting on what the dial RECEIVED rather than on what the source says is what makes the
    version safety real: a `max_size` default that differed between 14.2 and 15.0.1 could not
    then change how this transport behaves. The first dial fails transiently so the second dial
    is a real reconnect, and both are checked -- a value that reached only the first attempt
    would leave every reconnect at the library's mercy.
    """
    dial = ScriptedDial(
        [
            FakeConnection([]),  # hello recv hits an abrupt close -> transient -> retry
            FakeConnection([hello_bytes(SESSION_ID, EPOCH)]),  # the reconnect succeeds
        ]
    )
    transport = WSTransport(config(), dial=dial, sleep=RecordingSleep())

    async def scenario() -> None:
        stream = transport.connect_forever()
        try:
            await stream.__anext__()
        finally:
            await stream.aclose()

    anyio.run(scenario)

    assert len(dial.calls) == 2, "one failed dial then one successful reconnect"
    for _url, kwargs in dial.calls:
        assert kwargs["max_size"] == DEFAULT_RING_BYTES


# --------------------------------------------------------------------------------------
# 3. The library enforces it -- driven over a real socket
# --------------------------------------------------------------------------------------


@asynccontextmanager
async def _live_socket(
    server_frames: list[bytes], cap: int | None
) -> AsyncIterator[ClientConnection]:
    """Serve `hello` then `server_frames` on a real loopback socket; yield the live connection.

    A real server and the module's own default dial (`_NoRedirect`), so `max_size` is exercised
    where it actually lives -- in `websockets` -- rather than in a fake that would have to
    re-implement the bound. The connection is the one the transport confirmed with `hello`, so a
    recv on it reads the next frame the server sent, which is what the cap acts on.
    """

    async def handler(connection: ServerConnection) -> None:
        await connection.send(hello_bytes(SESSION_ID, EPOCH))
        for raw in server_frames:
            await connection.send(raw)
        try:
            await connection.wait_closed()
        except ConnectionClosed:
            pass

    async with serve(handler, "127.0.0.1", 0) as server:
        host, port = next(iter(server.sockets)).getsockname()[:2]
        transport = WSTransport(config(url=f"ws://{host}:{port}/publish", max_size=cap))
        stream = transport.connect_forever()
        connected = await stream.__anext__()
        try:
            yield connected.connection
        finally:
            await stream.aclose()


def test_an_inbound_frame_over_the_cap_is_a_transient_teardown() -> None:
    """T-FRAME-SIZE. The frame driven OVER the cap, and the classification it produces.

    An inbound frame larger than the cap is one a peer should not send in normal operation -- this
    publisher receives only small control frames inbound. `websockets` fails the socket with a
    1009 (message too big) close rather than buffering it, surfacing as `ConnectionClosed`. This
    test recv's on the connection directly and asserts two things: the close carries the 1009 (so
    the teardown is provably the CAP and not an unrelated close), and `classify_failure` maps it
    to `TRANSIENT` -- the same disposition it gives the edge kill, so a live socket re-dials. It
    does NOT drive the re-dial loop itself; the fakes-based tests above cover that.
    """
    over = data_frame(_TEST_CAP * 2)

    async def scenario() -> None:
        async with _live_socket([over], cap=_TEST_CAP) as connection:
            try:
                raw = await connection.recv()
            except ConnectionClosed as exc:
                assert classify_failure(exc) is Failure.TRANSIENT
                assert exc.sent is not None and exc.sent.code == 1009, "the cap is what fired"
            else:  # pragma: no cover - a delivered over-cap frame is the bug this test exists for
                raise AssertionError(f"an over-cap frame was delivered: {len(raw)} bytes")

    anyio.run(scenario)


def test_a_substantial_frame_under_the_cap_is_delivered_intact() -> None:
    """T-FRAME-SIZE. The inverse, so the teardown above is the cap and not any large frame.

    A frame comfortably larger than a control message but under the cap must arrive and decode.
    Without this, the over-cap test alone could pass against a socket that rejected every frame,
    and the cap would be an outage wearing the shape of a bound.
    """
    payload = b"x" * (_TEST_CAP // 2)
    under = data_frame(_TEST_CAP // 2)

    async def scenario() -> None:
        async with _live_socket([under], cap=_TEST_CAP) as connection:
            raw = await connection.recv()
            assert not isinstance(raw, str), "frames are binary"
            assert decode_frame(raw).data == payload, "delivered byte for byte, under the cap"

    anyio.run(scenario)
