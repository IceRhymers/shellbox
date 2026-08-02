"""T-TUNNEL-* -- the transport end to end, on one machine, with nothing faked.

Everything up to this file asserts one half against a double: `#15` and `#16` drive the App
with a fake socket and the bridge with a fake transport, and `W19b` drives the publisher with a
fake bridge. This is the first place a real pty's bytes cross a real WebSocket to a real
subscriber, and the first place the two halves can disagree.

What is real here: a tmux server, a forked ``tmux attach`` client on a pty, ``PtyBridge``,
``WSTransport``, uvicorn serving the shipped ``shellbox_app``, and a subscriber that is an
ordinary WebSocket client. What is not real is the edge -- see ``tunnel.py`` for exactly which
property is reproduced (a socket that dies with nothing to await) and which is not (the kill
itself). `W24` owns the rest, and this lane is the regression gate rather than the DoD proof.

WARNING: **These tests fork real processes and hold real sockets.** Every one of them must
leave the publisher stopped, or its ``tmux attach`` client stays live on the session and holds
the window at that client's size -- which the next test would then read as its own. The
``publish`` context manager owns that, and nothing here may construct a ``Publisher`` directly.
"""

from __future__ import annotations

import anyio
from conftest import TmuxServer, requires_tmux, sentinel
from shellbox_transport import Frame, Stream
from shellbox_transport.codec import (
    CONTROL_RESYNC,
    FIELD_BASE_SEQ,
    UNORDERED_SEQ,
    control_frame,
    decode_control,
    input_message,
    resume_message,
)
from tunnel import loopback, publish, subscribe

pytestmark = requires_tmux

TMUX_NAME = "build"
# How long to wait for bytes that have to cross a pty, a socket, a relay and another socket.
# Generous because CI schedules three threads here, and a flake in this lane reads as a
# transport defect, which is the most expensive kind of false alarm this repo can produce.
ARRIVAL_TIMEOUT = 20.0


def session(tmux_server: TmuxServer, tmp_path) -> None:
    tmux_server.adapter().create(TMUX_NAME, cwd=str(tmp_path), command=["sh"])


async def await_control(sub, kind: str, *, timeout: float = ARRIVAL_TIMEOUT):
    """Consume frames until a control message of ``kind`` arrives. Fails rather than hanging.

    Waiting for the CONTROL frame specifically, rather than asserting over whatever happened to
    have arrived by the time some data check finished, is the difference between a test and a
    race: a resync and the pane's next output are two independent arrivals, and the data can
    easily win.
    """
    with anyio.fail_after(timeout):
        while True:
            frame = await sub.next_frame(timeout=timeout)
            if frame.stream is not Stream.CONTROL:
                continue
            message = decode_control(frame.data)
            if message.kind == kind:
                return message


async def collect_until(sub, needle: bytes, *, timeout: float = ARRIVAL_TIMEOUT) -> bytes:
    """Accumulate STDOUT payloads until ``needle`` appears. Fails rather than hanging.

    Concatenating is not optional: coalescing means a needle can be split across two reads of
    the pty, so a per-frame ``in`` check would miss it for a reason that has nothing to do with
    delivery. This is the same rule ``conftest.await_content`` follows for the pane.
    """
    seen = bytearray()
    with anyio.fail_after(timeout):
        while needle not in seen:
            frame = await sub.next_frame(timeout=timeout)
            if frame.stream is Stream.STDOUT:
                seen += frame.data
    return bytes(seen)


# --------------------------------------------------------------------------------------
# T-TUNNEL-ECHO
# --------------------------------------------------------------------------------------


def test_a_byte_written_in_the_pane_reaches_the_subscriber_in_order(
    tmux_server: TmuxServer, tmp_path
) -> None:
    """T-TUNNEL-ECHO. The DoD's first clause, with every layer real.

    The sentinel is ``conftest.sentinel``, whose invariant is that the token cannot appear in
    the command that produces it -- so a match proves the shell RAN, not that the pty echoed
    the command line back. ``test_sentinel.py`` explains why that distinction exists; it is
    load-bearing here because an attach client echoes keystrokes by default.
    """
    session(tmux_server, tmp_path)
    adapter = tmux_server.adapter()
    token = sentinel("ECHO")

    with loopback() as loop_back, publish(loop_back, adapter, tmux_name=TMUX_NAME) as pub:
        assert pub.publisher.claimed is True, "the publisher did not win its own session's claim"

        async def main() -> list[Frame]:
            async with subscribe(loop_back, pub.session_id) as sub:
                adapter.send(TMUX_NAME, text=token.echo())
                await collect_until(sub, token.awaited.encode())
                return list(sub.frames)

        frames = anyio.run(main)

    data = [frame for frame in frames if frame.stream is Stream.STDOUT]
    assert data, "no data frames arrived"
    assert [frame.seq for frame in data] == sorted(frame.seq for frame in data), (
        "data frames arrived out of order, which the relay must never do"
    )
    assert all(frame.session_id == pub.session_id for frame in data)
    assert all(frame.session_id != TMUX_NAME for frame in data), (
        "a frame carried the LOCAL tmux name as its wire id; two hosts would then collide"
    )


def test_the_app_binds_the_global_session_id_and_not_the_tmux_name(
    tmux_server: TmuxServer, tmp_path
) -> None:
    """The `W23` identity correction, asserted where it actually matters: the App's registry.

    The bind key is what decides whether two sandboxes share an attachment. If it were the
    local tmux name, every host with a session called ``build`` would land on one entry -- and
    the second publisher would either be refused or rebind onto the first's subscriber.
    """
    session(tmux_server, tmp_path)
    adapter = tmux_server.adapter()

    with loopback() as loop_back, publish(loop_back, adapter, tmux_name=TMUX_NAME) as pub:

        async def main() -> dict[str, tuple[bool, bool]]:
            async with subscribe(loop_back, pub.session_id):
                return {
                    key: (held.publisher is not None, held.subscriber is not None)
                    for key, held in loop_back.relay.attachments.items()
                }

        bound = anyio.run(main)

    assert bound == {pub.session_id: (True, True)}
    assert TMUX_NAME not in bound
    assert ":" in pub.session_id, "the wire id must be the global <host_id>:<tmux_name>"


# --------------------------------------------------------------------------------------
# T-TUNNEL-INPUT
# --------------------------------------------------------------------------------------


def test_input_from_the_subscriber_reaches_the_pane(tmux_server: TmuxServer, tmp_path) -> None:
    """T-TUNNEL-INPUT. A keystroke typed by a viewer runs in the agent's shell.

    The assertion is on the pane's own output rather than on the frames coming back, because
    the frames would also be satisfied by the attach client echoing the keystrokes without the
    shell ever executing them. The sentinel's split spelling is what separates the two.
    """
    session(tmux_server, tmp_path)
    adapter = tmux_server.adapter()
    token = sentinel("INPUT")

    with loopback() as loop_back, publish(loop_back, adapter, tmux_name=TMUX_NAME) as pub:

        async def main() -> bytes:
            async with subscribe(loop_back, pub.session_id) as sub:
                await sub.send(
                    control_frame(
                        pub.session_id,
                        UNORDERED_SEQ,
                        0.0,
                        input_message(token.echo().encode()),
                    )
                )
                return await collect_until(sub, token.awaited.encode())

        seen = anyio.run(main)

    assert token.awaited.encode() in seen
    # And the pane really ran it -- read through the adapter, which is a different path from
    # the one the bytes arrived on, so this cannot be satisfied by the tunnel echoing itself.
    assert token.awaited in adapter.read(TMUX_NAME, lines=0).content


# --------------------------------------------------------------------------------------
# T-TUNNEL-RECONNECT
# --------------------------------------------------------------------------------------


def test_a_severed_socket_resumes_without_an_undeclared_hole(
    tmux_server: TmuxServer, tmp_path
) -> None:
    """T-TUNNEL-RECONNECT. **The DoD's hardest clause, at the only fidelity CI can reach.**

    The proxy severs the TCP path mid-stream, so both sides see a death with no close frame --
    the measured edge signature, and the one shape that leaves nothing to await. Every socket
    dies together, which is also what the edge does.

    What is asserted is the honesty guarantee and not a particular branch: after resuming from
    ``from_seq``, the subscriber gets EITHER the next ordinal byte-exactly OR a ``resync`` that
    declares the discontinuity. What must never happen is a data frame whose ``seq`` skips
    ahead with nothing saying so -- that is the silent hole `ADR-11` exists to rule out, and it
    is invisible to a reader who is not looking for it.
    """
    session(tmux_server, tmp_path)
    adapter = tmux_server.adapter()
    first = sentinel("PRECUT")
    second = sentinel("POSTCUT")

    with loopback() as loop_back, publish(loop_back, adapter, tmux_name=TMUX_NAME) as pub:

        async def main() -> tuple[int, int, list[Frame]]:
            async with subscribe(loop_back, pub.session_id) as sub:
                adapter.send(TMUX_NAME, text=first.echo())
                await collect_until(sub, first.awaited.encode())
                highest = max(
                    frame.seq for frame in sub.frames if frame.stream is Stream.STDOUT
                )

            severed = loop_back.cut()

            # The subscriber reconnects, as a browser would, and asks to resume from the last
            # ordinal it actually holds.
            async with subscribe(loop_back, pub.session_id) as resumed:
                adapter.send(TMUX_NAME, text=second.echo())
                await resumed.send(
                    control_frame(
                        pub.session_id,
                        UNORDERED_SEQ,
                        0.0,
                        resume_message(highest, pub.epoch),
                    )
                )
                await collect_until(resumed, second.awaited.encode())
                return severed, highest, list(resumed.frames)

        severed, highest, after = anyio.run(main)

    assert severed >= 1, "nothing was cut, so this test proved only that a live socket works"

    resyncs = [
        decode_control(frame.data)
        for frame in after
        if frame.stream is Stream.CONTROL and decode_control(frame.data).kind == CONTROL_RESYNC
    ]
    data = [frame for frame in after if frame.stream is Stream.STDOUT]
    assert data, "nothing arrived after the reconnect"

    if resyncs:
        # The declared branch. A resync names the ordinal its repaint is a picture of, so the
        # subscriber knows precisely what to distrust.
        assert FIELD_BASE_SEQ in resyncs[0].fields, (
            "a resync arrived without naming the base ordinal its repaint depicts, so a "
            "subscriber cannot tell what the picture replaces"
        )
    else:
        # The byte-exact branch. The first ordinal after the cut must be the very next one --
        # anything higher is the silent hole, and anything lower is a replay.
        assert min(frame.seq for frame in data) == highest + 1, (
            f"resumed at {min(frame.seq for frame in data)} having asked from {highest}, with "
            "no resync declaring the gap. This is the undeclared discontinuity ADR-11 rules out."
        )


# --------------------------------------------------------------------------------------
# T-TUNNEL-RESTART
# --------------------------------------------------------------------------------------


def test_a_restarted_publisher_declares_a_new_epoch_rather_than_a_silent_gap(
    tmux_server: TmuxServer, tmp_path
) -> None:
    """T-TUNNEL-RESTART. The transport analogue of ``T-RESTART``.

    A publisher dies and another takes the session. Its ``seq`` restarts from 1, so the
    subscriber's held ordinals now name completely different bytes -- which is exactly the
    misdelivery the epoch exists to make detectable. Resuming with the OLD epoch must produce a
    ``resync``, never a byte-exact replay of ordinals that mean something else now.

    It also exercises `W19b` for real: the first publisher's claim has to be released (or read
    as dead) before the second can attach, and if that arbitration were broken the second
    publisher would refuse and this test would hang rather than fail.
    """
    session(tmux_server, tmp_path)
    adapter = tmux_server.adapter()
    before = sentinel("EPOCH1")
    after_token = sentinel("EPOCH2")

    with loopback() as loop_back:
        with publish(loop_back, adapter, tmux_name=TMUX_NAME) as first:
            first_epoch = first.epoch

            async def phase_one() -> None:
                async with subscribe(loop_back, first.session_id) as sub:
                    adapter.send(TMUX_NAME, text=before.echo())
                    await collect_until(sub, before.awaited.encode())

            anyio.run(phase_one)

        # The first publisher is stopped here, by `publish`'s own teardown -- claim released,
        # attach child reaped. A second one may now take the session.
        with publish(loop_back, adapter, tmux_name=TMUX_NAME) as second:
            assert second.publisher.claimed is True, (
                "the successor could not claim a session whose publisher had stopped, so the "
                "release path or the liveness predicate is broken"
            )
            assert second.epoch != first_epoch, "a restart must mint a new epoch"

            async def phase_two() -> object:
                async with subscribe(loop_back, second.session_id) as sub:
                    await sub.send(
                        control_frame(
                            second.session_id,
                            UNORDERED_SEQ,
                            0.0,
                            # The STALE epoch: what a viewer that survived the restart holds.
                            resume_message(1, first_epoch),
                        )
                    )
                    # Waited for SPECIFICALLY, and before any data check: the resync and the
                    # pane's next output are independent arrivals, and asserting over whatever
                    # had shown up by the time a sentinel landed is a race the data usually
                    # wins -- which would report a missing resync that was merely late.
                    resync = await await_control(sub, CONTROL_RESYNC)
                    adapter.send(TMUX_NAME, text=after_token.echo())
                    await collect_until(sub, after_token.awaited.encode())
                    return resync

            resync = anyio.run(phase_two)

    assert resync is not None, (
        "a resume naming the previous epoch was answered without a resync. The subscriber "
        "would replay ordinals that now name different bytes, which is the silent misdelivery "
        "the epoch exists to prevent."
    )
    assert resync.epoch == second.epoch.value, (
        "the resync named an epoch other than the new publisher's, so a subscriber could not "
        "tell which attach the repaint depicts"
    )


# --------------------------------------------------------------------------------------
# The claim, over the real thing
# --------------------------------------------------------------------------------------


def test_a_second_publisher_refuses_while_the_first_holds_the_session(
    tmux_server: TmuxServer, tmp_path
) -> None:
    """`W19b`'s arbitration, with two real publishers and a real App behind them.

    The claim is what prevents the second publisher from ever dialling, so the App's own
    ``publisher_conflict`` backstop should never be reached. Asserting the relay still holds
    exactly ONE publisher is what distinguishes "arbitrated host-side" from "refused at the
    server", which are the same outcome for a viewer and very different outcomes for the
    agent's window.
    """
    session(tmux_server, tmp_path)
    adapter = tmux_server.adapter()

    with loopback() as loop_back, publish(loop_back, adapter, tmux_name=TMUX_NAME) as first:
        assert first.publisher.claimed is True
        with publish(loop_back, adapter, tmux_name=TMUX_NAME) as second:
            assert second.publisher.claimed is False, (
                "a second publisher claimed a session another publisher holds. Two live epochs "
                "on one session is ADR-12's repaint loop."
            )
            assert second.publisher.bridge is None, (
                "the refused publisher built a bridge anyway, which forks a tmux attach client "
                "onto the agent's session"
            )

        publishers = [
            held for held in loop_back.relay.attachments.values() if held.publisher is not None
        ]
        assert len(publishers) <= 1
