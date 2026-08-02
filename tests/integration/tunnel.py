"""W23's loopback infrastructure: a real App, a severable TCP path, and a real subscriber.

Everything in `#15`, `#16` and `W19b` is asserted against a double -- a fake socket on the
server side, a fake transport on the publisher side. This module is what makes the real thing
run end to end on one machine: uvicorn serving the shipped ``shellbox_app``, a publisher
holding a real pty on a real tmux session, and a subscriber that is an ordinary WebSocket
client decoding the shipped frame codec.

## Why there is a TCP proxy in front of the server

`T-TUNNEL-RECONNECT` needs the socket to die **the way the edge kills it**, and the measured
signature is specific: an abrupt TCP termination with **no close frame**
(``ConnectionClosedError: no close frame received or sent``, `probe/FINDINGS.md`). Calling
``websocket.close()`` on the server sends a close frame, which is the one shape the edge never
produces -- and it is the shape the publisher's detector would handle differently.

So the path is ``client -> proxy -> uvicorn``, and ``cut()`` drops the proxy's sockets
mid-stream. That puts the severance at the layer the edge actually operates at, rather than
asking the application to simulate its own network failing.

CRITICAL: **This still is not the edge kill, and the DoD's reconnect clause does not rest on
it.** What is reproduced here is the *observable* -- a socket that dies with nothing to await.
The kill's real properties (global, wall-clock, every socket in the same second) are
unreproducible in CI and remain `W24`'s to measure. `wsfakes.py` carries the same warning one
layer down, and it is worth repeating here because a green loopback lane is much more
convincing than a green unit lane and proves only a little more.

## Threads

Three, and each is the shape the component really has:

* **the server thread** hosts uvicorn and the proxy on one loop -- in production the App is a
  separate process entirely, so it must not share the publisher's loop;
* **the publisher thread** is `W19b`'s ``Publisher``, which is what a publisher is;
* **the test's own thread** runs the subscriber under ``anyio.run``, standing in for a browser.

A test that ran all three on one loop would be a different program from the one that ships.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field

import uvicorn
from shellbox_app.server import Relay, build_app
from shellbox_mcp import naming
from shellbox_mcp.bridge import PtyBridge
from shellbox_mcp.publisher import Publisher
from shellbox_mcp.tmux import TmuxAdapter
from shellbox_mcp.transport import WSTransport, WSTransportConfig
from shellbox_transport import Frame
from shellbox_transport.codec import decode_frame, encode_frame
from shellbox_transport.seq import Epoch
from websockets.asyncio.client import ClientConnection, connect

__all__ = [
    "Loopback",
    "Publishing",
    "Subscriber",
    "loopback",
    "publish",
    "subscribe",
    "wait_bound",
]

# Long enough to absorb CI scheduling noise, short enough that a socket which never arrives
# fails one test rather than hanging the lane.
STARTUP_TIMEOUT = 10.0


@dataclass
class _Link:
    """One proxied connection: the socket the client holds and the one uvicorn holds."""

    downstream: asyncio.StreamWriter
    upstream: asyncio.StreamWriter

    def sever(self) -> None:
        """Drop both halves without a close frame, which is the whole point of this class.

        ``transport.abort()`` rather than ``close()``: ``close()`` flushes and sends FIN
        politely, and anything already queued -- including a close frame the application layer
        wrote a moment ago -- would still be delivered. ``abort()`` discards it, which is what
        makes the peer see a death with nothing to await.
        """
        for writer in (self.downstream, self.upstream):
            try:
                writer.transport.abort()
            except Exception:  # noqa: BLE001 - already gone is the outcome being asked for
                continue


@dataclass
class Loopback:
    """A running App, reachable over a path a test can sever.

    ``relay`` is the server's own ``Relay``, exposed so a test can assert what the server
    believes -- which publisher is bound, whether a subscriber survived a kill -- without
    inferring it from frames.
    """

    url: str
    """``ws://127.0.0.1:<proxy port>``. Append ``/publish/<id>`` or ``/subscribe/<id>``."""

    relay: Relay
    _links: list[_Link]
    _loop: asyncio.AbstractEventLoop
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def publish_url(self, session_id: str) -> str:
        return f"{self.url}/publish/{session_id}"

    def subscribe_url(self, session_id: str) -> str:
        return f"{self.url}/subscribe/{session_id}"

    def cut(self) -> int:
        """Sever every live proxied connection. Returns how many were cut.

        Called from the TEST's thread while the sockets live on the server thread's loop, so
        the severance is marshalled with ``call_soon_threadsafe``. Returning the count is what
        keeps a test honest: cutting zero connections would otherwise look exactly like a
        publisher that reconnected instantly.
        """
        with self._lock:
            links = list(self._links)
            self._links.clear()
        if not links:
            return 0
        done = threading.Event()

        def sever_all() -> None:
            for link in links:
                link.sever()
            done.set()

        self._loop.call_soon_threadsafe(sever_all)
        done.wait(STARTUP_TIMEOUT)
        return len(links)


@contextmanager
def loopback() -> Iterator[Loopback]:
    """Serve ``shellbox_app`` behind a severable proxy for the duration of the block.

    A fresh ``Relay`` per call, never the module-level ``app``: ``server.py`` makes the relay
    per-app precisely so two apps share nothing, and a lane that reused one would let a
    leftover attachment from one test decide another's outcome.
    """
    relay = Relay()
    api = build_app(relay)
    # port=0 lets the kernel choose, so parallel test runs and a developer's own services
    # cannot collide with a hardcoded number.
    config = uvicorn.Config(api, host="127.0.0.1", port=0, log_level="warning", access_log=False)
    server = uvicorn.Server(config)

    links: list[_Link] = []
    ready = threading.Event()
    holder: dict[str, object] = {}
    failure: list[BaseException] = []

    async def pump(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Copy one direction until EOF, then propagate the half-close.

        CRITICAL: **``write_eof`` is not tidiness, it is what makes a graceful close finish.**
        A WebSocket close is a handshake -- close frame out, close frame back, then TCP FIN --
        and ``websockets``' ``close()`` waits for all of it. A proxy that reads EOF on one
        direction and simply stops leaves the peer waiting on a FIN that never arrives, so
        every graceful close burns its full timeout and the publisher's shutdown looks slow for
        a reason that lives entirely in the test harness. Measured while building `W23`: 2 s per
        shutdown, exactly the ``close_timeout``.
        """
        try:
            while True:
                chunk = await reader.read(65536)
                if not chunk:
                    break
                writer.write(chunk)
                await writer.drain()
            if writer.can_write_eof():
                writer.write_eof()
        except (ConnectionResetError, BrokenPipeError, OSError):
            return

    async def handle(
        downstream_reader: asyncio.StreamReader, downstream_writer: asyncio.StreamWriter
    ) -> None:
        upstream_port = int(holder["port"])  # type: ignore[call-overload]
        try:
            upstream_reader, upstream_writer = await asyncio.open_connection(
                "127.0.0.1", upstream_port
            )
        except OSError:
            downstream_writer.transport.abort()
            return
        link = _Link(downstream=downstream_writer, upstream=upstream_writer)
        links.append(link)
        try:
            await asyncio.gather(
                pump(downstream_reader, upstream_writer),
                pump(upstream_reader, downstream_writer),
            )
        finally:
            link.sever()
            if link in links:
                links.remove(link)

    async def main() -> None:
        serve = asyncio.create_task(server.serve())
        # uvicorn publishes its bound sockets only once `serve()` has started, so this waits
        # for the port rather than assuming one.
        deadline = asyncio.get_running_loop().time() + STARTUP_TIMEOUT
        while asyncio.get_running_loop().time() < deadline:
            if server.started and server.servers:
                break
            await asyncio.sleep(0.01)
        else:  # pragma: no cover - a uvicorn that never binds
            raise AssertionError("uvicorn did not start within the deadline")
        holder["port"] = server.servers[0].sockets[0].getsockname()[1]

        proxy = await asyncio.start_server(handle, "127.0.0.1", 0)
        holder["proxy_port"] = proxy.sockets[0].getsockname()[1]
        holder["loop"] = asyncio.get_running_loop()
        ready.set()

        async with proxy:
            await serve

    def run() -> None:
        try:
            asyncio.run(main())
        except BaseException as exc:  # noqa: BLE001 - reported to the test thread below
            failure.append(exc)
            ready.set()

    thread = threading.Thread(target=run, name="shellbox-loopback", daemon=True)
    thread.start()
    if not ready.wait(STARTUP_TIMEOUT) or failure:
        raise AssertionError(f"the loopback app did not start: {failure!r}")

    handle_obj = Loopback(
        url=f"ws://127.0.0.1:{holder['proxy_port']}",
        relay=relay,
        _links=links,
        _loop=holder["loop"],  # type: ignore[arg-type]
    )
    try:
        yield handle_obj
    finally:
        server.should_exit = True
        thread.join(timeout=STARTUP_TIMEOUT)


def wait_bound(loop_back: Loopback, session_id: str, *, timeout: float = STARTUP_TIMEOUT) -> None:
    """Block until the App has a live publisher socket for ``session_id``.

    Polls the server's own registry with a deadline rather than sleeping, which is
    ``tests/conftest.py``'s rule applied to a different kind of server. The registry is the
    right thing to watch because it is what the relay consults per message: a publisher that
    has dialled but is not yet bound is exactly a publisher whose peer lookup returns ``None``.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        held = loop_back.relay.attachments.get(session_id)
        if held is not None and held.publisher is not None:
            return
        time.sleep(0.02)
    raise AssertionError(
        f"no publisher bound for {session_id!r} within {timeout}s; "
        f"the App holds {sorted(loop_back.relay.attachments)}"
    )


@dataclass
class Publishing:
    """A live publisher: the thread that hosts it, the bridge it drives, and its identity."""

    publisher: Publisher
    bridge: PtyBridge
    session_id: str
    tmux_name: str
    epoch: Epoch


@contextmanager
def publish(
    loop_back: Loopback,
    adapter: TmuxAdapter,
    *,
    tmux_name: str,
    host_id: str = "itest-host",
    epoch: Epoch | None = None,
) -> Iterator[Publishing]:
    """A real ``PtyBridge`` on a real pty, hosted by `W19b`'s ``Publisher``, dialling the App.

    The wire id is the GLOBAL ``naming.session_id(host_id, tmux_name)`` and the tmux name is
    the bare local one -- the split `W23` had to introduce, and the reason this helper takes
    both. The App binds the global id, so two hosts with a session of the same local name no
    longer collide on one server.

    Backoff is compressed to milliseconds. The shipped floor exists so a publisher cannot
    hot-loop into an imminent edge kill (`probe` constraint 2), which is a property of a real
    edge and not of a loopback proxy; leaving it at half a second would add that to every
    reconnect assertion for nothing.
    """
    attach_epoch = Epoch.new() if epoch is None else epoch
    session_id = naming.session_id(host_id, tmux_name)
    transport = WSTransport(
        WSTransportConfig(
            url=loop_back.publish_url(session_id),
            session_id=session_id,
            epoch=attach_epoch.value,
            backoff_floor=0.01,
            backoff_cap=0.05,
            hello_deadline=STARTUP_TIMEOUT,
        )
    )
    bridge = PtyBridge(adapter, transport, session_id, tmux_name=tmux_name, epoch=attach_epoch)
    publisher = Publisher(adapter, tmux_name, lambda: bridge)
    publisher.start(timeout=STARTUP_TIMEOUT)
    if publisher.claimed:
        # CRITICAL: **``start`` returning is not "the publisher is on the socket".** It returns
        # once the CLAIM is decided, which is three tmux round trips before the dial has
        # happened. A test that proceeded here would send input into an App whose ``_pump``
        # finds no peer and DROPS it -- silently, because dropping is correct when a publisher
        # is mid-reconnect. Every symptom of that is a timeout somewhere unrelated.
        #
        # A publisher that LOST its claim never dials, so waiting for it would time out on the
        # one case that is behaving correctly. Hence the branch.
        wait_bound(loop_back, session_id)
    try:
        yield Publishing(
            publisher=publisher,
            bridge=bridge,
            session_id=session_id,
            tmux_name=tmux_name,
            epoch=attach_epoch,
        )
    finally:
        # Not optional tidying: an unstopped publisher leaves a live `tmux attach` client on
        # the session, and the next test's assertions about window size would then be reading
        # this test's leftovers.
        publisher.stop(timeout=STARTUP_TIMEOUT)


class Subscriber:
    """A viewer's end of the tunnel: decoded frames in, control messages out.

    ``frames`` is every frame received including the server's ``hello``, for tests that assert
    over the whole stream. ``next_frame`` hands out the ones after it, in order.
    """

    def __init__(self, connection: ClientConnection, hello: Frame) -> None:
        self._connection = connection
        self.hello = hello
        self.frames: list[Frame] = [hello]
        self._inbox: asyncio.Queue[Frame] = asyncio.Queue()

    async def _read_forever(self) -> None:
        try:
            async for raw in self._connection:
                if isinstance(raw, bytes):
                    frame = decode_frame(raw)
                    self.frames.append(frame)
                    await self._inbox.put(frame)
        except Exception:  # noqa: BLE001 - a dead socket ends this reader, not the test
            return

    async def next_frame(self, *, timeout: float = 10.0) -> Frame:
        """The next frame not yet handed out. Fails on a deadline rather than hanging."""
        return await asyncio.wait_for(self._inbox.get(), timeout=timeout)

    async def send(self, frame: Frame) -> None:
        """Send one frame to the publisher, through the App's relay."""
        await self._connection.send(encode_frame(frame))


@asynccontextmanager
async def subscribe(loop_back: Loopback, session_id: str) -> AsyncIterator[Subscriber]:
    """An ordinary WebSocket client on ``/subscribe/<session_id>``.

    Deliberately NOT ``WSTransport``: that class is the publisher half, with a dial loop, a
    ``hello`` deadline and a reconnect policy of its own. A subscriber driven through it would
    make a test's failure ambiguous between the two halves. What a viewer is, is a socket that
    reads frames -- so that is what this is.

    The server's ``hello`` is consumed here, so a test's first ``next_frame`` is the first
    thing the PUBLISHER sent rather than the handshake.
    """
    async with connect(loop_back.subscribe_url(session_id)) as connection:
        first = await asyncio.wait_for(connection.recv(), timeout=STARTUP_TIMEOUT)
        assert isinstance(first, bytes), "the App's hello must be binary"
        subscriber = Subscriber(connection, decode_frame(first))
        pump_task = asyncio.create_task(subscriber._read_forever())  # noqa: SLF001
        try:
            yield subscriber
        finally:
            pump_task.cancel()
