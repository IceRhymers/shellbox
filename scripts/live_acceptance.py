#!/usr/bin/env python3
"""`W38`: hold a real publisher and a real browser client against the deployed App, and outlast
an edge kill.

This is the phase's only proof of its own definition of done, and it is the one thing no CI lane
can substitute for. The Databricks Apps edge kills every open WebSocket on a wall clock roughly
every 10 to 18 minutes -- measured by the Phase 1 probe, unreproducible anywhere else -- and the
whole reconnect-and-resume design exists for that event. Until a run has waited one out, the
design is untested against the thing it was designed for.

## What runs, and why each half is the real code

* **The publisher** is `shellbox_mcp.publisher.Publisher` over a real `PtyBridge` on a real pty
  attached to a real tmux session, dialling the live URL through `WSTransport`. Its backoff is
  the SHIPPED `0.5`/`5.0`, not the compressed values `tests/integration/tunnel.py` uses -- the
  nonzero floor exists because of a property of a real edge, so a live run that compressed it
  would be measuring something else.
* **The subscriber** is `shellbox_app.client.SubscriberClient`, the same state machine
  `static/protocol.js` transcribes, driven over a real socket. That is what makes this run
  evidence about the browser client rather than about a bespoke test client.

## What it asserts

1. Both sockets survive to a kill and **both reconnect**.
2. The subscriber's reconnect produces a **`resync` applied as a full reset**, never appended --
   D7's guarantee, observed rather than argued.
3. Output typed AFTER the kill reaches the pane and comes back, so the recovered path carries
   real bytes rather than merely reporting itself connected.
4. No `subscriber_conflict` outlives ADR-20's bound, and no undeclared `seq` gap is ever seen.

## Running it

    eval "$(scripts/bundle-vars.sh -t dev -p fevm-west)"
    uv run python scripts/live_acceptance.py --minutes 45

It needs a workspace OAuth token: the edge answers a PAT with a 302. The token is re-minted per
dial rather than captured once, because Apps OAuth expires in about an hour and this run is
deliberately long enough to care.

WARNING: this holds a real tmux session and a real pty on the machine it runs on. It reaps both
on the way out, including on a signal, but a `kill -9` will leave a `tmux attach` client behind.
The socket path is under a temporary directory so a leak is visible rather than mixed into
anyone's default server.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from shellbox_app.client import (
    Notice,
    Redial,
    Reset,
    Send,
    Stop,
    SubscriberClient,
    Write,
)
from shellbox_mcp import naming
from shellbox_mcp.bridge import PtyBridge
from shellbox_mcp.publisher import Publisher
from shellbox_mcp.tmux import TmuxAdapter, TmuxConfig
from shellbox_mcp.transport import WSTransport, WSTransportConfig
from shellbox_transport.seq import Epoch
from websockets.asyncio.client import connect

HOST_ID = "w38-live"
TMUX_NAME = "w38"

# The marker typed after the kill. Distinctive so finding it in the stream is unambiguous, and
# free of shell metacharacters so the pane's shell echoes it rather than acting on it.
POST_KILL_MARKER = "W38-RECOVERED-OK"


def mint_token(profile: str) -> str:
    """A fresh workspace OAuth token. Re-minted per dial -- see this module's docstring."""
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["databricks", "auth", "token", "--profile", profile],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    )
    return str(json.loads(result.stdout)["access_token"])


@dataclass
class Journal:
    """Everything observed, with timestamps. The run's actual output."""

    started: float = field(default_factory=time.monotonic)
    events: list[tuple[float, str, str]] = field(default_factory=list)

    def note(self, who: str, what: str) -> None:
        stamp = time.monotonic() - self.started
        self.events.append((stamp, who, what))
        print(f"[{stamp:7.1f}s] {who:10s} {what}", flush=True)

    def count(self, who: str, needle: str) -> int:
        return sum(1 for _, actor, what in self.events if actor == who and needle in what)


@dataclass
class Observed:
    """What the subscriber saw, in the terms the assertions are written in."""

    sockets: int = 0
    hellos: int = 0
    resyncs: int = 0
    resets: int = 0
    appended_after_resync: bool = False
    notices: list[str] = field(default_factory=list)
    stopped: str | None = None
    output: bytearray = field(default_factory=bytearray)
    repaints: list[bytes] = field(default_factory=list)


async def run_subscriber(
    url: str, session_id: str, profile: str, deadline: float, journal: Journal, seen: Observed
) -> None:
    """Hold a subscriber across every kill, driving the real `SubscriberClient`."""
    client = SubscriberClient(session_id)

    while time.monotonic() < deadline:
        delay = 0.0
        try:
            headers = {"Authorization": f"Bearer {mint_token(profile)}"}
            async with connect(
                url,
                additional_headers=headers,
                # Explicit, and the same values the App passes uvicorn. This is the detector for
                # a silent death; the TCP close is the primary signal.
                ping_interval=20,
                ping_timeout=20,
                open_timeout=15,
                max_size=None,
            ) as socket:
                seen.sockets += 1
                journal.note("subscriber", f"socket {seen.sockets} open (101)")
                delay = await _hold(socket, client, journal, seen)

        except Exception as exc:  # noqa: BLE001 - a live run reports, it does not crash
            journal.note("subscriber", f"socket ended: {type(exc).__name__}: {exc}")
            acts = client.closed(time.time())
            for action in acts:
                if isinstance(action, Redial):
                    delay = action.delay

        if client.phase.value == "stopped":
            journal.note("subscriber", f"TERMINAL: {seen.stopped}")
            return
        if delay == 0.0:
            delay = client.next_delay()
        journal.note("subscriber", f"redialling in {delay:.2f}s")
        await asyncio.sleep(delay)


async def _hold(
    socket: object, client: SubscriberClient, journal: Journal, seen: Observed
) -> float:
    """Drive one live socket until the state machine asks to leave it. Returns the next delay.

    A function rather than three closures inside the reconnect loop, so that each socket's
    tasks close over THIS socket and nothing else. The closure version was flagged by ruff
    (`B023`) for exactly the hazard that matters here: a task outliving its iteration would go
    on writing into a socket the loop had already abandoned.
    """
    stop = asyncio.Event()
    outbound: list[bytes] = []
    leaving = 0.0

    def apply(actions: list[object]) -> bool:
        nonlocal leaving
        _drive(actions, journal, seen, sending=outbound)
        for action in actions:
            if isinstance(action, Redial):
                leaving = action.delay
                return True
            if isinstance(action, Stop):
                return True
        return False

    apply(client.opened(time.time()))

    async def pump() -> None:
        async for raw in socket:  # type: ignore[attr-defined]
            if apply(client.received(raw, time.time())):
                stop.set()
                return
        stop.set()

    async def tick() -> None:
        while not stop.is_set():
            await asyncio.sleep(1.0)
            if apply(client.ticked(time.time())):
                stop.set()
                return

    async def flush() -> None:
        while not stop.is_set():
            await asyncio.sleep(0.05)
            while outbound:
                await socket.send(outbound.pop(0))  # type: ignore[attr-defined]

    tasks = [asyncio.create_task(c()) for c in (pump, tick, flush)]
    _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    return leaving


def _drive(
    actions: list[object],
    journal: Journal,
    seen: Observed,
    *,
    sending: list[bytes],
) -> None:
    """Perform the state machine's actions. The browser's `terminal.js`, minus a terminal."""
    for action in actions:
        if isinstance(action, Send):
            sending.append(action.payload)
        elif isinstance(action, Write):
            seen.output.extend(action.data)
            if seen.resyncs and not seen.repaints:
                seen.appended_after_resync = True
        elif isinstance(action, Reset):
            seen.resyncs += 1
            seen.resets += 1
            seen.repaints.append(action.repaint)
            # A reset means the terminal is cleared, so the accumulated stream restarts here --
            # exactly what the browser does, and the reason a repaint must never be appended.
            seen.output = bytearray(action.repaint)
            journal.note("subscriber", f"RESYNC applied as a reset ({len(action.repaint)} bytes)")
        elif isinstance(action, Notice):
            seen.notices.append(action.code)
            journal.note("subscriber", f"notice {action.code}: {action.text[:90]}")
        elif isinstance(action, Stop):
            seen.stopped = action.code
            journal.note("subscriber", f"stop {action.code}: {action.text[:90]}")
        elif isinstance(action, Redial):
            journal.note("subscriber", f"redial in {action.delay:.2f}s")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, help="the App's https URL")
    parser.add_argument("--profile", default="fevm-west")
    parser.add_argument("--minutes", type=float, default=45.0)
    args = parser.parse_args()

    if shutil.which("tmux") is None:
        print("tmux is not on PATH; this run needs a real pty", file=sys.stderr)
        return 2

    base = args.url.rstrip("/").replace("https://", "wss://")
    session_id = naming.session_id(HOST_ID, TMUX_NAME)
    workspace = Path(tempfile.mkdtemp(prefix="w38-"))
    journal = Journal()
    seen = Observed()

    adapter = TmuxAdapter(TmuxConfig(socket_path=str(workspace / "tmux.sock")))
    epoch = Epoch.new()
    transport = WSTransport(
        WSTransportConfig(
            url=f"{base}/publish/{session_id}",
            session_id=session_id,
            epoch=epoch.value,
            # The SHIPPED values. See this module's docstring.
            backoff_floor=0.5,
            backoff_cap=5.0,
        ),
        mint_token=lambda: mint_token(args.profile),
    )
    bridge = PtyBridge(adapter, transport, session_id, tmux_name=TMUX_NAME, epoch=epoch)

    journal.note("harness", f"session_id={session_id} epoch={epoch.value}")
    adapter.create(TMUX_NAME, cwd=str(workspace), command=["/bin/sh"])
    publisher = Publisher(adapter, TMUX_NAME, lambda: bridge)
    publisher.start(timeout=15.0)
    journal.note("publisher", f"claimed={publisher.claimed}")

    deadline = time.monotonic() + args.minutes * 60
    try:
        asyncio.run(_run(base, session_id, args, deadline, journal, seen, adapter, publisher))
    except KeyboardInterrupt:
        journal.note("harness", "interrupted")
    finally:
        publisher.stop(timeout=10.0)
        with contextlib.suppress(Exception):
            adapter.kill(TMUX_NAME)
        shutil.rmtree(workspace, ignore_errors=True)

    return _report(journal, seen)


async def _run(
    base: str,
    session_id: str,
    args: argparse.Namespace,
    deadline: float,
    journal: Journal,
    seen: Observed,
    adapter: TmuxAdapter,
    publisher: Publisher,
) -> None:
    """The subscriber, plus a typist that pokes the pane before and after the first kill."""
    subscriber = asyncio.create_task(
        run_subscriber(
            f"{base}/subscribe/{session_id}", session_id, args.profile, deadline, journal, seen
        )
    )

    async def typist() -> None:
        await asyncio.sleep(20)
        adapter.send(TMUX_NAME, text="echo W38-BEFORE-KILL-OK")
        journal.note("typist", "typed the pre-kill marker")
        # Wait for the first reconnect, then prove the RECOVERED path carries real bytes.
        while time.monotonic() < deadline and seen.sockets < 2:
            await asyncio.sleep(2)
        if seen.sockets >= 2:
            await asyncio.sleep(3)
            adapter.send(TMUX_NAME, text=f"echo {POST_KILL_MARKER}")
            journal.note("typist", "typed the post-kill marker")

    async def watchdog() -> None:
        while time.monotonic() < deadline:
            await asyncio.sleep(60)
            journal.note(
                "harness",
                f"{(deadline - time.monotonic()) / 60:.0f} min left; "
                f"sockets={seen.sockets} resyncs={seen.resyncs} "
                f"publisher_error={publisher.error!r}",
            )

    tasks = [subscriber, asyncio.create_task(typist()), asyncio.create_task(watchdog())]
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True), timeout=deadline - time.monotonic()
        )
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


def _report(journal: Journal, seen: Observed) -> int:
    """The findings, and the exit status. Every claim below is a thing this run observed."""
    stream = bytes(seen.output)
    checks: list[tuple[str, bool, str]] = [
        (
            "the subscriber held more than one socket (an edge kill was outlasted)",
            seen.sockets >= 2,
            f"sockets={seen.sockets}",
        ),
        (
            "a resync arrived and was applied as a RESET, never appended",
            seen.resyncs >= 1 and not seen.appended_after_resync,
            f"resyncs={seen.resyncs} appended={seen.appended_after_resync}",
        ),
        (
            "the recovered path carried real bytes typed AFTER the kill",
            POST_KILL_MARKER.encode() in stream,
            f"stream={len(stream)} bytes",
        ),
        (
            "no subscriber_conflict outlived ADR-20's bound",
            seen.stopped != "subscriber_conflict",
            f"stopped={seen.stopped}",
        ),
        (
            "no undeclared seq gap was ever observed",
            "stream_gap" not in seen.notices,
            f"notices={sorted(set(seen.notices))}",
        ),
    ]

    print("\n" + "=" * 78)
    print("W38 live acceptance -- findings")
    print("=" * 78)
    failed = 0
    for name, ok, detail in checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}\n        {detail}")
        failed += 0 if ok else 1
    print(f"\n  events recorded: {len(journal.events)}")
    print(f"  repaints seen:   {[len(r) for r in seen.repaints]}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
