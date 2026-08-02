"""``AttachedPty`` against a REAL fork onto a REAL pty -- with no tmux involved.

tmux is not needed to prove any of this. What ``AttachedPty`` owns is a forked child on a pty
master: the exec, the ``TIOCSWINSZ`` ioctl, the two platform spellings of EOF, and the reap. A
plain ``/bin/sh`` exercises every one of them, so these run in the unit lane on every machine
rather than only where the tmux gate runs.

The one property that genuinely needs tmux -- that a ``tmux attach`` client honors the ioctl --
is measured in [`spike/tmux_spike.py`](../../spike/tmux_spike.py) (F17) and asserted through the
shipped adapter in `tests/tmux/`. This file asserts the half underneath it: that the ioctl
reaches the child at all, which is precisely what the ``Popen`` alternative failed to do.
"""

from __future__ import annotations

import os
import signal
import time

import anyio
import pytest
from shellbox_mcp.attach import (
    COALESCE_INTERACTIVE_MAX,
    COALESCE_MAX,
    INTERACTIVE_WINDOW,
    AttachedPty,
    read_limit,
    resolve_binary,
)

SH = "/bin/sh"


def spawn(script: str, **kwargs: object) -> AttachedPty:
    """A shell on a pty, with the reduced environment a tmux client would get."""
    return AttachedPty.spawn(
        [SH, "-c", script],
        {"PATH": "/usr/bin:/bin", "TERM": "xterm-256color", "LC_CTYPE": "C.UTF-8"},
        **kwargs,  # type: ignore[arg-type]
    )


async def drain(pty: AttachedPty, *, limit: int = 4096) -> bytes:
    """Read until EOF. The loop a publisher runs, minus the framing."""
    out = b""
    while True:
        chunk = await pty.read(limit)
        if not chunk:
            return out
        out += chunk


async def read_until(pty: AttachedPty, marker: bytes, timeout: float = 5.0) -> bytes:
    """Read until ``marker`` appears, or fail. §11.1's rule, applied to a pty.

    Every teardown assertion below needs this. ``close`` races the child's startup otherwise,
    and a child killed before it installed its signal handlers proves nothing about what happens
    to one that installed them -- which is the difference between measuring the escalation and
    measuring a startup race.
    """
    deadline = time.monotonic() + timeout
    out = b""
    while marker not in out:
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {marker!r}; read {out!r}")
        chunk = await pty.read(4096)
        if not chunk:
            raise AssertionError(f"pty hit EOF before {marker!r} arrived; read {out!r}")
        out += chunk
    return out


# --------------------------------------------------------------------------------------
# read_limit -- the coalescing decision
# --------------------------------------------------------------------------------------


def test_the_read_limit_is_64_kib_when_nobody_has_typed() -> None:
    """Bulk output should cross the socket in as few frames as possible."""
    assert read_limit(now=1000.0, last_input_at=None) == COALESCE_MAX
    assert read_limit(now=1000.0, last_input_at=1000.0 - INTERACTIVE_WINDOW) == COALESCE_MAX
    assert read_limit(now=1000.0, last_input_at=0.0) == COALESCE_MAX


def test_the_read_limit_tightens_to_2_kib_just_after_a_keystroke() -> None:
    """A browser terminal echoes synchronously, and a coalesced echo reads as lag.

    The window is the one place a smaller read is worth the extra frames, and it is bounded so
    that a session which is merely producing output is not billed for it forever.
    """
    assert read_limit(now=1000.0, last_input_at=999.99) == COALESCE_INTERACTIVE_MAX
    assert read_limit(now=1000.0, last_input_at=1000.0) == COALESCE_INTERACTIVE_MAX


def test_the_interactive_cap_is_smaller_than_the_bulk_cap() -> None:
    """Stated as a relation, so a future tuning pass cannot invert them by editing one line."""
    assert 0 < COALESCE_INTERACTIVE_MAX < COALESCE_MAX
    assert INTERACTIVE_WINDOW > 0


# --------------------------------------------------------------------------------------
# resolve_binary -- the parent does the PATH walk, because execve will not
# --------------------------------------------------------------------------------------


def test_a_bare_binary_name_is_resolved_to_an_absolute_path() -> None:
    """``TmuxConfig.tmux_bin`` defaults to the bare string ``tmux``, and ADR-1 keeps it that way.

    Every other invocation goes through ``subprocess.run``, which searches ``PATH`` itself. This
    is the one call site where it would not, so the walk happens here -- in the parent, where a
    failure can still be reported.
    """
    resolved = resolve_binary(["sh", "-c", "true"])

    assert os.path.isabs(resolved[0])
    assert os.path.basename(resolved[0]) == "sh"
    assert resolved[1:] == ["-c", "true"]


def test_an_absolute_path_is_left_alone() -> None:
    assert resolve_binary([SH, "attach"]) == [SH, "attach"]


def test_a_binary_that_is_not_on_path_fails_in_the_parent() -> None:
    """The failure has to happen BEFORE the fork.

    After it, the only thing left to report with is an exit status from a child that has no
    stderr anyone is reading -- so the error would present as an attach that silently never
    produced a byte.
    """
    with pytest.raises(FileNotFoundError, match="execve"):
        resolve_binary(["definitely-not-a-real-binary-8f3a", "attach"])


def test_an_empty_argv_is_refused() -> None:
    with pytest.raises(ValueError, match="empty"):
        resolve_binary([])


# --------------------------------------------------------------------------------------
# The fork, the exec, and the stream
# --------------------------------------------------------------------------------------


def test_the_child_execs_and_its_output_arrives_on_the_master() -> None:
    """The whole mechanism end to end, at its smallest."""
    pty = spawn("printf 'HELLO-FROM-THE-CHILD'")
    try:
        out = anyio.run(drain, pty)
    finally:
        pty.close()

    assert b"HELLO-FROM-THE-CHILD" in out


def test_eof_arrives_as_empty_bytes_on_both_platforms() -> None:
    """CRITICAL: Linux raises ``EIO`` on a pty master when the slave closes; macOS returns 0.

    Both mean the attach client is gone, which is the event the entire detach-versus-dead
    classification hangs off. A reader that handled only one of them would hang forever on the
    other, holding a publisher open on a session nobody is attached to -- and it would do it on
    exactly one of the two lanes, so half of CI would stay green.
    """
    pty = spawn("printf 'done'")
    try:
        anyio.run(drain, pty)

        async def after_eof() -> bytes:
            return await pty.read(1024)

        assert anyio.run(after_eof) == b"", "EOF must stay EOF, not raise on the second read"
    finally:
        pty.close()


def test_a_write_reaches_the_child_byte_exactly() -> None:
    """The input half. ``cat`` echoes the pty's line discipline output back at us."""
    pty = spawn("read line; printf 'GOT:%s' \"$line\"")
    try:

        async def scenario() -> bytes:
            pty.write(b"payload-9f2\n")
            deadline = time.monotonic() + 5.0
            out = b""
            while b"GOT:payload-9f2" not in out and time.monotonic() < deadline:
                chunk = await pty.read(4096)
                if not chunk:
                    break
                out += chunk
            return out

        assert b"GOT:payload-9f2" in anyio.run(scenario)
    finally:
        pty.close()


def test_the_ioctl_reaches_the_child_and_rows_are_not_transposed_with_cols() -> None:
    """T-ATTACH-ARGV's sibling: ``TIOCSWINSZ``, which is what the ``Popen`` route lost.

    Spike F17 measured ``openpty`` + ``Popen(start_new_session=True)`` attaching correctly and
    then discarding every resize, because the kernel delivers ``SIGWINCH`` to the pty's
    foreground process group and a child with no controlling terminal is not in one. That is a
    worse outcome than a failed attach, because nothing reports it. This is the assertion that
    the mechanism actually chosen does not have that hole.

    The size is deliberately non-square (90x37). A square one would pass whether or not the
    ``winsize`` struct's ``ws_row, ws_col`` order was respected, and a transposed viewport looks
    like a renderer bug rather than an ioctl bug.

    The child blocks on ``read`` first, so the ioctl is applied before ``stty`` runs. Waiting on
    a condition rather than sleeping for a duration is §11.1's rule, and it applies to a pty as
    much as to tmux.
    """
    pty = spawn("read go; stty size")
    try:

        async def scenario() -> bytes:
            pty.set_window_size(90, 37)
            pty.write(b"go\n")
            deadline = time.monotonic() + 5.0
            out = b""
            while b"37 90" not in out and time.monotonic() < deadline:
                chunk = await pty.read(4096)
                if not chunk:
                    break
                out += chunk
            return out

        out = anyio.run(scenario)
        assert b"37 90" in out, f"stty reported {out!r}; expected rows=37 cols=90"
        assert b"90 37" not in out, "rows and cols are transposed in the winsize struct"
    finally:
        pty.close()


@pytest.mark.parametrize(("cols", "rows"), [(0, 24), (80, 0), (-1, 24)])
def test_a_degenerate_window_size_is_refused(cols: int, rows: int) -> None:
    """A zero-column terminal is not a terminal, and the ioctl would accept it silently."""
    pty = spawn("read x")
    try:
        with pytest.raises(ValueError, match="at least 1"):
            pty.set_window_size(cols, rows)
    finally:
        pty.close()


# --------------------------------------------------------------------------------------
# The reap -- an unreaped child is a permanent reflow, not untidiness
# --------------------------------------------------------------------------------------


def test_close_reaps_a_child_that_has_already_finished() -> None:
    """The common case: closing the master is enough, and no signal is needed.

    A tmux client whose terminal has gone exits on its own, which is why ``close`` closes the fd
    before it reaches for ``SIGTERM``.
    """
    pty = spawn("printf 'bye'")
    anyio.run(drain, pty)

    pty.close()

    assert pty.status == 0
    with pytest.raises(ChildProcessError):
        os.waitpid(pty.pid, os.WNOHANG)


def test_closing_the_master_hangs_up_a_child_that_is_still_running() -> None:
    """The case that matters, and MEASURED here rather than assumed: the close does the work.

    An orphaned ``tmux attach`` is a live client on the session, and a live client holds the
    window at that client's size -- so an unreaped child is PM3's reflow made permanent on a
    session an agent is still using.

    What actually kills it is ``SIGHUP``, not the ``SIGTERM`` below it in ``close``. Closing the
    master hangs up the controlling terminal, and the kernel signals the pty's foreground
    process group -- which is exactly the group the child is in, and only because ``forkpty``
    gave it a controlling terminal. So the ordering in ``close`` is load-bearing twice over: the
    same property that makes ``TIOCSWINSZ`` work is what makes the cheap teardown work, and the
    ``Popen`` route (spike F17) would have had neither.
    """
    pty = spawn("printf READY; read forever")
    anyio.run(read_until, pty, b"READY")
    assert pty.status is None, "the child must still be running for this test to mean anything"

    pty.close()

    assert pty.status is not None
    assert os.WIFSIGNALED(pty.status) and os.WTERMSIG(pty.status) == signal.SIGHUP
    with pytest.raises(ChildProcessError):
        os.waitpid(pty.pid, os.WNOHANG)


def test_close_escalates_to_sigkill_when_the_child_ignores_everything_catchable() -> None:
    """A child that traps its way out must not be able to keep an agent's window hostage.

    ``HUP`` is trapped as well as ``TERM``, because the test above establishes that ``HUP`` is
    what normally does it -- so trapping ``TERM`` alone would never reach the escalation and
    this test would pass while asserting nothing about it. ``SIGKILL`` cannot be trapped, which
    is the whole reason the ladder ends there.

    The grace period is shortened so this costs a fraction of a second; the shipped default is
    2 s, which is a real tmux client's time to notice its terminal went away.

    The child loops rather than blocking in ``read``: with the master closed, ``read`` fails and
    a shell that traps every signal still exits on its own -- so the trap would be untested and
    this test would pass on a path that never reached the escalation at all.
    """
    pty = spawn("trap '' HUP TERM; printf READY; while :; do sleep 0.05; done", grace=0.15)
    anyio.run(read_until, pty, b"READY")
    started = time.monotonic()

    pty.close()

    assert pty.status is not None
    assert os.WIFSIGNALED(pty.status), f"expected a signalled exit, got status {pty.status}"
    assert os.WTERMSIG(pty.status) == signal.SIGKILL
    assert time.monotonic() - started < 5.0, "the escalation must be bounded, not indefinite"


def test_close_is_idempotent() -> None:
    """``run``'s ``finally`` calls it, and so does ``W19b``'s shutdown path. Both must be safe."""
    pty = spawn("printf 'x'")
    anyio.run(drain, pty)

    pty.close()
    first = pty.status
    pty.close()

    assert pty.status == first


def test_a_failed_exec_is_distinguishable_from_a_refused_attach() -> None:
    """126 says the fork happened and the exec did not. A different bug, a different fix.

    Without the distinct code, a missing binary and a tmux that declined to attach would both
    surface as "the child exited and produced nothing".
    """
    pty = AttachedPty.spawn(["/nonexistent/binary-a71f", "attach"], {})
    try:
        anyio.run(drain, pty)
    finally:
        pty.close()

    assert pty.status is not None
    assert os.WIFEXITED(pty.status)
    assert os.WEXITSTATUS(pty.status) == 126
