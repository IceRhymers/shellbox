"""Fakes for the transport lane: a scripted dial, a fake socket, and a clock that never waits.

Shared by ``test_hello_deadline.py``, ``test_auth_remint.py``, and ``test_backoff_jitter.py``
so that three files do not each grow their own socket double and then disagree about what a
dead socket does.

WARNING: These fakes deliberately do NOT simulate the edge kill. They simulate what a client
OBSERVES when it happens -- a close with no close frame -- which is a different and much
smaller claim. The kill itself is unreproducible in CI, so the DoD's reconnect clause rests on
a live run and not on this file. Anyone reading a green suite should know that.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Iterable

from shellbox_transport import Frame, Stream
from shellbox_transport.codec import control_frame, encode_frame, hello_message
from shellbox_transport.seq import Epoch
from websockets.exceptions import ConnectionClosedError

SESSION_ID = "sb-test-1"


def hello_bytes(session_id: str, epoch: str | None, *, viewer_email: str | None = None) -> bytes:
    """A server ``hello``, encoded exactly as the App half sends it.

    ``epoch=None`` is the shape ``shellbox_app`` actually sends: it holds no attach, so it
    echoes no epoch. A string is the shape a *publisher-aware* server would send, and the
    client tolerates both -- which is what ``test_hello_deadline.py`` pins.
    """
    message = hello_message(session_id, None if epoch is None else Epoch(epoch), viewer_email)
    return encode_frame(control_frame(session_id, 1, 1.0, message))


def other_kind_bytes(session_id: str, epoch: str) -> bytes:
    """A first frame that is a control frame but not a ``hello``."""
    from shellbox_transport.codec import resize_message

    return encode_frame(control_frame(session_id, 1, 1.0, resize_message(Epoch(epoch), 80, 24)))


def data_frame_bytes(session_id: str) -> bytes:
    """A first frame that is data, not control. A server must not open with this."""
    return encode_frame(
        Frame(session_id=session_id, seq=1, t=1.0, stream=Stream.STDOUT, data=b"output")
    )


def abrupt_close() -> ConnectionClosedError:
    """The MEASURED teardown signature: no close frame from either side.

    ``ConnectionClosedError(None, None)`` renders as "no close frame received or sent", which is
    the exact text the probe recorded against the real edge.
    """
    return ConnectionClosedError(None, None)


class FakeConnection:
    """A socket double. Serves queued inbound messages, then closes abruptly."""

    def __init__(self, inbound: Iterable[bytes | str] = (), *, hang: bool = False) -> None:
        self._inbound: deque[bytes | str] = deque(inbound)
        self.sent: list[bytes] = []
        self.closed = False
        self.hang = hang
        """When true, ``recv`` never returns. This is the 101-then-silence case: without a
        deadline the client waits here forever while believing it is connected."""

    async def recv(self) -> bytes | str:
        if self.hang:
            await asyncio.Event().wait()
        if not self._inbound:
            raise abrupt_close()
        return self._inbound.popleft()

    async def send(self, data: bytes) -> None:
        if self.closed:
            raise abrupt_close()
        self.sent.append(data)

    async def close(self) -> None:
        self.closed = True

    def __aiter__(self) -> FakeConnection:
        return self

    async def __anext__(self) -> bytes | str:
        if not self._inbound:
            raise StopAsyncIteration
        return self._inbound.popleft()


class ScriptedDial:
    """A dial that returns, or raises, whatever the script says -- in order.

    Records every call's URL and keyword arguments, which is how the keepalive test checks that
    ``ping_interval`` and ``ping_timeout`` reach the library rather than merely appearing in the
    source.
    """

    def __init__(self, script: Iterable[FakeConnection | BaseException]) -> None:
        self._script = deque(script)
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def __call__(self, url: str, **kwargs: object) -> FakeConnection:
        self.calls.append((url, kwargs))
        if not self._script:
            raise AssertionError(
                "the dial script ran out; the loop dialed more times than the test expected"
            )
        outcome = self._script.popleft()
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class RecordingSleep:
    """A clock that records what it was asked to wait and then does not wait.

    A test that really slept would make the backoff floor a cost rather than an assertion.
    """

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)
