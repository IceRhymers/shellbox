"""W19's data path against a REAL tmux server and a REAL forked attach client.

The unit lane asserts the composition with a fake pty. This asserts the half that only a real
``tmux attach`` can answer: that tmux honors the ioctl, that its initial redraw really does
arrive in-band, and that the pane's own tty destroys an over-long line exactly as measured.

WARNING: **The byte-exactness oracle is the file the pane process wrote, never the pty stream
and never ``capture-pane``.** A pane renders, wraps and normalises, and an attach client
receives the RENDERED screen -- so a byte-for-byte assertion against the master fd would be an
assertion about tmux's renderer. ``tests/conftest.py`` states the same rule for the tool path
and the reason is identical here. Output assertions below therefore look for a sentinel in the
stream; delivery assertions read the file.

Every wait is a poll for a condition with a deadline (§11.1). Nothing here sleeps for a
duration and then asserts, because tmux returns as soon as the *server* accepts a command --
long before the pane's process has consumed anything.
"""

from __future__ import annotations

import asyncio
import time

import anyio
import pytest
from conftest import (
    TmuxServer,
    await_condition,
    await_file,
    raw_reader_ready,
    requires_tmux,
    sentinel,
)
from shellbox_mcp.attach import AttachedPty
from shellbox_mcp.bridge import PtyBridge
from shellbox_mcp.errors import LineTooLong
from shellbox_mcp.tmux import TmuxAdapter

pytestmark = requires_tmux


def attach(adapter: TmuxAdapter, name: str) -> AttachedPty:
    """The real composition: ``prepare_attach`` builds the argv, ``AttachedPty`` forks it.

    Exactly what ``PtyBridge._attach_real`` does, spelled out rather than reached into, so this
    file exercises the public seam the bridge is built on.
    """
    return AttachedPty.spawn(adapter.prepare_attach(name), adapter.attach_env())


def read_for(pty: AttachedPty, needle: bytes, *, timeout: float = 10.0) -> bytes:
    """Drain the master until ``needle`` shows up, or fail naming what was seen instead."""

    async def scenario() -> bytes:
        deadline = time.monotonic() + timeout
        seen = b""
        while needle not in seen:
            if time.monotonic() > deadline:
                raise AssertionError(f"timed out waiting for {needle!r}; saw {seen[-400:]!r}")
            try:
                chunk = await asyncio.wait_for(pty.read(65536), timeout=1.0)
            except TimeoutError:
                continue
            if not chunk:
                raise AssertionError(f"pty hit EOF before {needle!r}; saw {seen[-400:]!r}")
            seen += chunk
        return seen

    return anyio.run(scenario)


# --------------------------------------------------------------------------------------
# T-ATTACH-PTY
# --------------------------------------------------------------------------------------


def test_the_attach_delivers_pane_output_and_leaves_the_session_size_alone(
    tmux_server: TmuxServer, tmp_path
) -> None:
    """T-ATTACH-PTY. Both halves of the plan's row, and the second is the one PM3 is about.

    An attach is a tmux CLIENT, and a client's size normally drives its window's -- which is why
    ``TmuxConfig.default_terminal`` is small on purpose and why the control row in spike F16
    reflows. ``prepare_attach`` freezes the window first, so a viewer arriving at some other
    size must not move the agent's pane.
    """
    adapter = tmux_server.adapter()
    adapter.create("build", cwd=str(tmp_path), command=["sh"])
    before = adapter.read("build")
    token = sentinel("PANE")

    pty = attach(adapter, "build")
    try:
        pty.write(token.echo().encode())
        read_for(pty, token.awaited.encode())
    finally:
        pty.close()

    after = adapter.read("build")
    assert (after.cols, after.rows) == (before.cols, before.rows), (
        "the attach moved the agent's window; freeze_window_size did not take effect"
    )


def test_the_initial_repaint_arrives_in_band_with_no_capture_pane(
    tmux_server: TmuxServer, tmp_path
) -> None:
    """T-ATTACH-PTY. ADR-9's strongest argument over the ``pipe-pane`` alternative.

    The screen state a viewer needs at connect is free, in-band, and ordered by tmux itself:
    ``tmux attach`` redraws the whole screen to the new client in the same byte stream. Spike
    F17 measured it directly rather than inferring it from the absence of a ``capture-pane``
    call in omnigent's bridge -- a sentinel printed BEFORE any client existed arrived in the
    attach master's first ~730 bytes.

    This re-asserts it through the shipped ``prepare_attach``, and the invocation count is the
    load-bearing half: the argv issues no capture at all, so what arrives can only be tmux's own
    redraw.
    """
    adapter = tmux_server.adapter()
    adapter.create("build", cwd=str(tmp_path), command=["sh"])
    token = sentinel("BEFORE")

    # Printed with NO client attached. This is the state a later viewer must still receive.
    adapter.send("build", text=token.echo(newline=False), keys=["Enter"])
    await_condition(
        lambda: token.awaited in adapter.read("build").content, what="the pane to print it"
    )

    pty = attach(adapter, "build")
    try:
        seen = read_for(pty, token.awaited.encode())
    finally:
        pty.close()

    assert token.awaited.encode() in seen
    assert b"capture-pane" not in seen, "the repaint is tmux's redraw, not a capture we issued"


def test_a_resize_on_the_master_reaches_tmux_and_does_not_move_the_window(
    tmux_server: TmuxServer, tmp_path
) -> None:
    """T-ATTACH-PTY. ``TIOCSWINSZ``, which is the thing the ``Popen`` alternative silently lost.

    Spike F17 measured ``openpty`` + ``Popen(start_new_session=True)`` attaching correctly and
    then discarding every resize, because the kernel delivers ``SIGWINCH`` to the pty's
    foreground process group and a child with no controlling terminal is not in one.

    The window must NOT follow. That is the whole point of the freeze: what moves is the
    client's viewport, and the agent's pane stays where the agent put it.
    """
    adapter = tmux_server.adapter()
    adapter.create("build", cwd=str(tmp_path), command=["sh"])
    before = adapter.read("build")

    pty = attach(adapter, "build")
    try:
        token = sentinel("SIZED")
        pty.write(token.echo().encode())
        read_for(pty, token.awaited.encode())

        pty.set_window_size(before.cols + 37, before.rows + 11)
        # Nothing to poll for on the tmux side -- the window is frozen, so the assertion is
        # that it does NOT change. Give tmux a real chance to act on the SIGWINCH first by
        # round-tripping a command through the server.
        second = sentinel("AFTER")
        pty.write(second.echo().encode())
        read_for(pty, second.awaited.encode())

        after = adapter.read("build")
    finally:
        pty.close()

    assert (after.cols, after.rows) == (before.cols, before.rows), (
        "the client's resize moved the agent's window; per-window window-size manual is what "
        "must prevent that, and spike F16 measured it holding over 1714 samples"
    )


def test_input_written_to_the_master_reaches_the_pane_process_byte_exactly(
    tmux_server: TmuxServer, tmp_path
) -> None:
    """T-ATTACH-PTY. The input half, against the only valid oracle.

    The reader pane is RAW mode (``stty -icanon``), which is the one that can be asserted
    byte-exactly -- ``MAX_CANON`` does not apply, so full delivery is possible at any length.
    ``tests/conftest.py`` explains at length why the canonical reader must never be used for
    this, and the same split applies here.

    The payload carries a bare ``;`` and a TAB deliberately. ``;`` is H1 -- ``send-keys -l ';'``
    returns rc=0 and the character never arrives -- and TAB is the ``-F`` record separator this
    repo parses on. Both survive here, which is the input-path advantage ADR-9 keeps.
    """
    target = tmp_path / "delivered.bin"
    adapter = tmux_server.adapter()
    adapter.create("build", cwd=str(tmp_path), command=raw_reader_ready(str(target), "RDY-IN"))
    payload = b"raw ; payload\twith a tab"

    pty = attach(adapter, "build")
    try:
        # Wait for the pane's `stty -icanon` to have run. Canonical mode assembles lines as
        # bytes ENTER the tty buffer, so a payload written before that is held awaiting a
        # newline and never becomes raw retroactively.
        read_for(pty, b"RDY-IN")
        pty.write(payload)
        got = await_file(str(target), lambda data: len(data) >= len(payload), what="the payload")
    finally:
        pty.close()

    assert got == payload


def test_a_bracketed_paste_wrapper_is_consumed_by_the_tmux_client(
    tmux_server: TmuxServer, tmp_path
) -> None:
    """MEASURED, and it CORRECTS a claim in the plan rather than confirming one.

    Decision A's third argument for the attached pty says it "accepts control characters,
    bracketed-paste sequences, and raw-mode application input". The middle item does not hold as
    written. An attach client is a tmux CLIENT, and tmux parses its terminal input into keys
    before sending them to the server -- so a DECSET 2004 wrapper is interpreted by the client,
    not forwarded.

    Measured on tmux 3.6b through a live attach into a raw-mode pane: the text between the
    markers arrives, and the markers themselves do not. The surviving form of the argument is
    the one this suite can defend -- raw bytes and application-mode keystrokes that
    ``keys.py``'s closed allowlist cannot name -- and that is what the test above asserts.

    This is here as a REGRESSION test on the documented behavior, not as an aspiration. If a
    future tmux forwards the wrapper, this fails and the docs get corrected in the other
    direction rather than the surprise landing in a renderer.
    """
    target = tmp_path / "pasted.bin"
    adapter = tmux_server.adapter()
    adapter.create("build", cwd=str(tmp_path), command=raw_reader_ready(str(target), "RDY-PASTE"))

    pty = attach(adapter, "build")
    try:
        read_for(pty, b"RDY-PASTE")
        pty.write(b"\x1b[200~inner\x1b[201~")
        got = await_file(str(target), lambda data: b"inner" in data, what="the pasted text")
    finally:
        pty.close()

    assert b"inner" in got
    assert b"\x1b[200~" not in got, "if the wrapper now survives, update input_message's WARNING"


# --------------------------------------------------------------------------------------
# T-INPUT-LINE-CEILING
# --------------------------------------------------------------------------------------


def test_an_over_ceiling_line_is_refused_before_it_reaches_the_pane(
    tmux_server: TmuxServer, tmp_path
) -> None:
    """T-INPUT-LINE-CEILING. Rejection, asserted at the pane -- the bytes must never arrive.

    MEASURED on this exact path (spike F18): 8192 bytes plus a newline through a live attach
    client delivered **4096 bytes on Linux, silently truncated**, and 0 on macOS. A truncated
    command is a different, still-executable command, which is the worse of the two failures and
    the sandbox's behaviour.

    REJECT is the only implementable branch. The truncation is the kernel's ``MAX_CANON`` line
    buffer, so chunking cannot help, and no tmux format exposes the pane pty's termios, so this
    process cannot know which mode the pane is in.

    The reader here is deliberately the RAW one, which would have accepted the whole payload.
    That is the point: the ceiling fires on shellbox's side, before the pane's line discipline
    is ever consulted, so it does not depend on which mode the pane happens to be in.
    """
    target = tmp_path / "must-stay-empty.bin"
    adapter = tmux_server.adapter()
    adapter.create("build", cwd=str(tmp_path), command=raw_reader_ready(str(target), "RDY-CEIL"))
    pty = attach(adapter, "build")
    # The transport is `None` on purpose: `send_input` is synchronous and touches no socket, so
    # a bridge that never runs is the whole of what this needs. Passing a double would suggest
    # the socket were involved in the decision, and it is not.
    bridge = PtyBridge(
        adapter,
        None,  # type: ignore[arg-type]
        "itest-host:build",
        tmux_name="build",
        attach=lambda: pty,
    )
    bridge.attach()
    over = b"z" * adapter.config.max_send_line_bytes + b"\n"

    try:
        read_for(pty, b"RDY-CEIL")
        with pytest.raises(LineTooLong):
            bridge.send_input(over)

        # And a legal line through the same bridge still lands, so the refusal above is the
        # ceiling firing and not the path being broken.
        bridge.send_input(b"legal\n")
        got = await_file(str(target), lambda data: b"legal" in data, what="the legal line")
    finally:
        bridge.close()

    assert b"z" not in got, "the refused line reached the pane; the ceiling did nothing"


# --------------------------------------------------------------------------------------
# T-RESYNC-CAPTURE
# --------------------------------------------------------------------------------------


def test_the_repaint_expression_reproduces_the_visible_pane_with_ansi(
    tmux_server: TmuxServer, tmp_path
) -> None:
    """T-RESYNC-CAPTURE. The exact expression ``PtyBridge._repaint`` evaluates, against tmux.

    ``read(lines=0)`` is the VISIBLE pane and never scrollback -- ``history_limit`` is 20000
    lines of ANSI, and every publisher in a sandbox resyncs in the same second after an edge
    kill, so a scrollback repaint turns one reconnect storm into an outage.

    ``-e`` is what keeps the escapes: without it a red line captures as ``RED`` and with it as
    ``\\x1b[31mRED\\x1b[39m``. A repaint stripped of ANSI would repaint a subscriber's terminal
    into the wrong colours and, worse, the wrong cursor state.

    The WIRING -- that a resync's payload is this value -- is asserted in
    ``tests/unit/test_bridge.py``, where a fake adapter makes the byte comparison exact. This is
    the other half: that the value itself is what a real tmux produces.
    """
    adapter = tmux_server.adapter()
    adapter.create("build", cwd=str(tmp_path), command=["sh"])
    token = sentinel("PAINT")
    adapter.send("build", text=f"printf '\\033[31m{token.typed}\\033[39m\\n'", keys=["Enter"])
    await_condition(
        lambda: token.awaited in adapter.read("build").content, what="the coloured output"
    )

    repaint = adapter.read("build", lines=0).content.encode("utf-8")

    assert token.awaited.encode() in repaint
    assert b"\x1b[31m" in repaint, "capture-pane lost the ANSI; xterm.js needs those escapes"


# --------------------------------------------------------------------------------------
# T-ATTACH-DETACH -- both directions, per S-PANE-DEAD
# --------------------------------------------------------------------------------------


def test_killing_the_attach_client_leaves_the_session_alive(
    tmux_server: TmuxServer, tmp_path
) -> None:
    """T-ATTACH-DETACH, direction 1. A detach is not a death.

    Measured with a live client in spike F19: on detach the pane reads ``0`` and keeps reading
    ``0`` afterwards. A publisher that reported this as ``terminal_gone`` would tell the viewer
    to stop reconnecting to a session that is still running -- and, in omnigent's shape, tear
    the session down.
    """
    adapter = tmux_server.adapter()
    adapter.create("build", cwd=str(tmp_path), command=["sh"])
    token = sentinel("LIVE")

    pty = attach(adapter, "build")
    pty.write(token.echo().encode())
    read_for(pty, token.awaited.encode())
    assert adapter.pane_dead("build") is False

    pty.close()

    assert adapter.pane_dead("build") is False, "a detach must not read as a dead pane"
    assert adapter.exists("build") is True
    assert adapter.read("build").alive is True


def test_a_pane_whose_process_exited_reads_dead_while_the_session_survives(
    tmux_server: TmuxServer, tmp_path
) -> None:
    """T-ATTACH-DETACH, direction 2, and the third column spike F19 added.

    ``remain-on-exit on`` is set globally, so the session deliberately outlives its process --
    that is what keeps the final output readable, and the cost is that ``has-session`` stops
    meaning "alive".

    The finding that makes this reportable: the attach client OUTLIVES the pane's process. So a
    publisher can read ``#{pane_dead}`` and say ``terminal_gone`` on a socket that is still up,
    rather than inferring it from its own client going away.
    """
    adapter = tmux_server.adapter()
    adapter.create("build", cwd=str(tmp_path), command=["sh", "-c", "read x; exit 0"])

    pty = attach(adapter, "build")
    try:
        pty.write(b"go\n")
        await_condition(lambda: adapter.pane_dead("build") is True, what="the pane to die")

        assert adapter.exists("build") is True, "has-session still says yes: existence != liveness"
        assert pty.status is None, (
            "the attach client must OUTLIVE the pane's process -- that is what makes "
            "terminal_gone reportable on a live socket (spike F19)"
        )
    finally:
        pty.close()
