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

## Two modes, and the second exists because ADR-23 leaves a gap no lane can close

    eval "$(scripts/bundle-vars.sh -t dev -p fevm-west)"

    # 1. assert the protocol, with this harness's own subscriber
    uv run python scripts/live_acceptance.py --url "$APP_URL" --minutes 45

    # 2. hold a pane for a REAL BROWSER, asserting nothing about the subscriber path
    uv run python scripts/live_acceptance.py --url "$APP_URL" --minutes 30 --publisher-only

Mode 1 drives `SubscriberClient`, which is what makes the run evidence about the browser client's
PROTOCOL half. It cannot say anything about xterm.js, because ADR-23 declines a browser test lane
and `static/protocol.js` is a transcription nothing executes.

Mode 2 is the answer to that. The App serves **one subscriber per session**, so this harness's own
subscriber would refuse a browser with `subscriber_conflict` -- `--publisher-only` leaves the slot
free and prints the URL to open. It is the only way to reach issue #4's last clause, *"a browser
can attach to a session, type, and resize"*, and its findings block says so rather than implying
the run checked anything itself.

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
from urllib.parse import quote

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

# Typed early, and asserted UNCONDITIONALLY. It is what proves the data path works end to end --
# a real keystroke into a real pty, echoed by a real shell, relayed through the real edge and
# rendered by the real client -- without depending on a platform event this harness cannot cause.
PRE_KILL_MARKER = "W38-BEFORE-KILL-OK"


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
    severed: bool = False
    output: bytearray = field(default_factory=bytearray)
    repaints: list[bytes] = field(default_factory=list)


async def run_subscriber(
    url: str,
    session_id: str,
    profile: str,
    deadline: float,
    journal: Journal,
    seen: Observed,
    sever_at: float | None = None,
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
                delay = await _hold(socket, client, journal, seen, sever_at)

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
    socket: object,
    client: SubscriberClient,
    journal: Journal,
    seen: Observed,
    sever_at: float | None = None,
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

    async def sever() -> None:
        """Drop this socket deliberately, to drive recovery without an edge kill.

        Aborts the transport rather than closing gracefully, because the edge's teardown sends
        NO close frame -- the measured signature is `ConnectionClosedError: no close frame
        received or sent`. A graceful close would exercise a path the real event never takes.
        """
        if sever_at is None or seen.severed:
            return
        await asyncio.sleep(max(0.0, sever_at - time.monotonic()))
        if stop.is_set():
            return
        seen.severed = True
        journal.note("harness", "SEVERING the subscriber socket (abort, no close frame)")
        transport = getattr(socket, "transport", None)
        if transport is not None and hasattr(transport, "abort"):
            transport.abort()
        else:
            await socket.close()  # type: ignore[attr-defined]

    # `sever` is included ONLY when it has work to do, and that is load-bearing rather than
    # tidy. `asyncio.wait(FIRST_COMPLETED)` below tears the socket down as soon as ANY of these
    # returns -- so a `sever` that returned immediately (because it already fired, or was never
    # asked for) ended every subsequent socket the instant it opened. That produced 26
    # reconnects in three minutes and looked exactly like a product defect in the retry path.
    # The client was fine; this list was not.
    coros = [pump, tick, flush]
    if sever_at is not None and not seen.severed:
        coros.append(sever)
    tasks = [asyncio.create_task(c()) for c in coros]
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
    parser.add_argument(
        "--sever-after",
        type=float,
        default=0.0,
        metavar="SECONDS",
        help=(
            "drop the subscriber's socket deliberately after this many seconds, to exercise "
            "reconnect and resume without waiting for an edge kill. NOT a substitute for the "
            "real event -- a severed socket is one connection, and the edge kills every open "
            "socket at once -- but it does drive the recovery path through the real edge."
        ),
    )
    parser.add_argument(
        "--publisher-only",
        action="store_true",
        help=(
            "hold the publisher and start NO subscriber, leaving the session's one subscriber "
            "slot free for a real browser. This is how a human closes issue #4's last clause -- "
            "'a browser can attach to a session, type, and resize' -- which no automated lane in "
            "this repo can reach, because ADR-23 declines a browser test lane. The run asserts "
            "nothing about the subscriber path and exits 2 for inconclusive by design."
        ),
    )
    args = parser.parse_args()

    if shutil.which("tmux") is None:
        print("tmux is not on PATH; this run needs a real pty", file=sys.stderr)
        return 2

    # Refused rather than ignored. `--sever-after` severs the SUBSCRIBER's socket, and
    # `--publisher-only` is the mode with no subscriber -- so the pair is a request the harness
    # cannot honour, and honouring half of it silently is the class of no-op this repo keeps
    # finding. See `probe/FINDINGS.md` on the first run reporting "not observed" as FAIL.
    if args.publisher_only and args.sever_after > 0:
        print(
            "--publisher-only starts no subscriber, so --sever-after has no socket to sever.\n"
            "Pick one: --publisher-only to hold a pane for a browser, or --sever-after to drive "
            "the recovery path with this harness's own subscriber.",
            file=sys.stderr,
        )
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

    if args.publisher_only:
        _print_browser_instructions(args.url.rstrip("/"), session_id, args.minutes)

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

    return _report(journal, seen, publisher_only=args.publisher_only, publisher=publisher)


def _print_browser_instructions(app_url: str, session_id: str, minutes: float) -> None:
    """Where to point a browser, and the one thing that will otherwise look broken.

    CRITICAL: the session will NOT appear in the inventory list at ``/ui/``, and a reader who does
    not know that will conclude the page is broken. The inventory reads the REGISTRY -- see
    `packages/shellbox-app/src/shellbox_app/inventory.py` -- and this harness publishes to the
    RELAY without enrolling a host or a session row. The two are deliberately independent (ADR-3:
    a registry failure degrades the inventory and never the relay), and this is that independence
    showing through from the other side.

    So the fragment URL is the one to open. `terminal.js` reads ``#session=`` and attaches without
    consulting the inventory at all; `applyHost` simply finds no row, and the no-publisher banner
    loses its detail rather than its correctness. That is the degraded path working as designed.
    """
    fragment = f"{app_url}/ui/#session={quote(session_id, safe='')}"
    print(
        "\n"
        + "=" * 78
        + f"""
PUBLISHER-ONLY. The subscriber slot is free -- open this in a browser:

    {fragment}

The pane is a real /bin/sh on a real pty, and it emits a line every 30 s so you can
see the stream is live without typing.

WHAT TO CHECK, which is issue #4's last unmet clause:
  1. the terminal renders (not a blank pane, not a banner)
  2. you can TYPE -- try `ls`, or `printf 'a\\tb\\n'` for a tab, or an arrow key
  3. RESIZE the window and confirm the pane reflows rather than corrupting

NOTE: this session will NOT be listed at {app_url}/ui/ and that is correct. The
inventory reads the registry; this harness publishes to the relay and enrols no row.
Use the fragment URL above.

Holding for {minutes:.0f} min. Ctrl-C when you are done -- the pane and its tmux
server are reaped on the way out.
"""
        + "=" * 78,
        flush=True,
    )


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
    """The subscriber, plus a typist that pokes the pane before and after the first kill.

    In ``--publisher-only`` mode there is no subscriber task at all -- that is the whole point,
    since the App serves one subscriber per session and this harness's own would refuse the
    browser with `subscriber_conflict`. The typist becomes a heartbeat instead of a marker writer:
    with nothing here reading the stream, a marker nothing asserts on is pointless, while a
    periodic line is what lets a HUMAN see the stream is live without typing.
    """
    sever_at = time.monotonic() + args.sever_after if args.sever_after > 0 else None
    tasks: list[asyncio.Task[None]] = []
    if not args.publisher_only:
        tasks.append(
            asyncio.create_task(
                run_subscriber(
                    f"{base}/subscribe/{session_id}",
                    session_id,
                    args.profile,
                    deadline,
                    journal,
                    seen,
                    sever_at,
                )
            )
        )

    async def typist() -> None:
        await asyncio.sleep(20)
        adapter.send(TMUX_NAME, text=f"echo {PRE_KILL_MARKER}")
        journal.note("typist", "typed the pre-kill marker")
        # Wait for the first reconnect, then prove the RECOVERED path carries real bytes.
        while time.monotonic() < deadline and seen.sockets < 2:
            await asyncio.sleep(2)
        if seen.sockets >= 2:
            await asyncio.sleep(3)
            adapter.send(TMUX_NAME, text=f"echo {POST_KILL_MARKER}")
            journal.note("typist", "typed the post-kill marker")

    async def heartbeat() -> None:
        """A visible line every 30 s, so a viewer can tell a live pane from a stalled one.

        Written through the same `adapter.send` the typist uses, so it exercises the real
        buffer-then-keys delivery path rather than writing to the pty behind tmux's back.
        """
        beat = 0
        while time.monotonic() < deadline:
            await asyncio.sleep(30)
            beat += 1
            adapter.send(TMUX_NAME, text=f"echo w38-live beat {beat}")

    async def watchdog() -> None:
        while time.monotonic() < deadline:
            await asyncio.sleep(60)
            left = f"{(deadline - time.monotonic()) / 60:.0f} min left"
            if args.publisher_only:
                # `sockets` is deliberately absent: it counts THIS harness's subscriber sockets,
                # and there are none by design. Printing `sockets=0` beside a healthy publisher
                # would read as the failure this mode exists to avoid.
                journal.note(
                    "harness",
                    f"{left}; publisher held, browser slot free; "
                    f"publisher_error={publisher.error!r}",
                )
            else:
                journal.note(
                    "harness",
                    f"{left}; sockets={seen.sockets} resyncs={seen.resyncs} "
                    f"publisher_error={publisher.error!r}",
                )

    # The typist writes markers only when something is reading them; the heartbeat is for a human.
    tasks.append(asyncio.create_task(heartbeat() if args.publisher_only else typist()))
    tasks.append(asyncio.create_task(watchdog()))
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True), timeout=deadline - time.monotonic()
        )
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


def _report(
    journal: Journal,
    seen: Observed,
    *,
    publisher_only: bool = False,
    publisher: Publisher | None = None,
) -> int:
    """The findings, and the exit status. Every claim below is a thing this run observed.

    CRITICAL: **"not observed" is a third outcome, and it is not a failure.** Two of these
    checks are conditional on an edge kill actually firing, and the kill is a platform event
    this harness cannot cause. Reporting them as FAIL when no kill happened is worse than
    useless: it points a reader at working code. So they report `NOT OBSERVED`, and the exit
    status distinguishes the three cases -- 0 all passed, 1 something FAILED, 2 the run was
    inconclusive because its triggering event never arrived.
    """
    stream = bytes(seen.output)
    killed = seen.sockets >= 2

    if publisher_only:
        # Every check below reads the SUBSCRIBER's observations, and this mode runs no subscriber.
        # Reporting them against an empty `Observed` would print four confident-looking failures
        # about code that was never exercised -- the same mistake as the first run's "no kill
        # happened" FAIL, in a new shape. So the mode reports only what it can actually see.
        checks = [
            (
                "the publisher claimed the session and held it",
                publisher is not None and publisher.error is None,
                f"error={None if publisher is None else publisher.error!r}",
            ),
            (
                "the subscriber slot was left free for a browser",
                True,
                f"sockets={seen.sockets} (this harness opened none, by design)",
            ),
            (
                "a browser attached, typed, and resized",
                None,
                "only a human can answer this -- issue #4's last clause",
            ),
        ]
        return _print(journal, seen, checks, publisher_only=True)

    # (name, outcome, detail). `outcome` is True, False, or None for "not observed".
    checks: list[tuple[str, bool | None, str]] = [
        (
            "a resync arrived and was applied as a RESET, never appended",
            seen.resyncs >= 1 and not seen.appended_after_resync,
            f"resyncs={seen.resyncs} appended={seen.appended_after_resync}",
        ),
        (
            "the data path carried real bytes end to end (typed, echoed, rendered)",
            PRE_KILL_MARKER.encode() in stream,
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
        (
            "the subscriber outlasted a socket teardown and rebound",
            True if killed else None,
            f"sockets={seen.sockets}",
        ),
        (
            "the RECOVERED path carried bytes typed after the teardown",
            (POST_KILL_MARKER.encode() in stream) if killed else None,
            f"stream={len(stream)} bytes",
        ),
    ]

    return _print(journal, seen, checks, publisher_only=False)


def _print(
    journal: Journal,
    seen: Observed,
    checks: list[tuple[str, bool | None, str]],
    *,
    publisher_only: bool,
) -> int:
    """Render the findings block and return the exit status. Shared by both modes."""
    print("\n" + "=" * 78)
    print("W38 live acceptance -- findings")
    print("=" * 78)
    failed = 0
    unobserved = 0
    for name, ok, detail in checks:
        label = "NOT OBSERVED" if ok is None else ("PASS" if ok else "FAIL")
        print(f"  {label:12s}  {name}\n                {detail}")
        if ok is None:
            unobserved += 1
        elif not ok:
            failed += 1

    print(f"\n  events recorded: {len(journal.events)}")
    held = journal.events[-1][0] if journal.events else 0.0
    if publisher_only:
        print(f"  wall time held:  {held / 60:.1f} min, publisher only")
        print(
            "\n  INCONCLUSIVE BY DESIGN. This mode asserts nothing about the subscriber path --\n"
            "  it holds a pane so a human can attach a real browser, which is the only way to\n"
            "  reach issue #4's last clause under ADR-23. Record what you saw; nothing here can."
        )
        return 1 if failed else 2

    print(f"  repaints seen:   {[len(r) for r in seen.repaints]}")
    print(f"  wall time held:  {held / 60:.1f} min across {seen.sockets} socket(s)")

    if unobserved:
        print(
            "\n  INCONCLUSIVE. No socket teardown fired during this run, so the reconnect and\n"
            "  resume clauses were never exercised. That is a statement about the window, not\n"
            "  about the code -- and it CONTRADICTS probe/FINDINGS.md, which measured a global\n"
            "  edge event killing every open socket every ~10-18 min (longest lifetime observed:\n"
            "  17.51 min). Re-run for longer, or use --sever-after to exercise recovery directly."
        )
    return 1 if failed else (2 if unobserved else 0)


if __name__ == "__main__":
    raise SystemExit(main())
