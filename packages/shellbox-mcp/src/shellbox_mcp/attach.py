"""The attach child: fork onto a pty, exec ``tmux attach``, read it, resize it, reap it.

This is the byte-stream source ADR-9 chose, and every decision here is transcribed from
omnigent's ``ws_bridge.py`` at commit ``fddb9b07`` with the line cited, per ADR-15 -- the same
convention ``tmux.py`` uses for the spike and ``boot_templated.py`` for the platform's boot
script. What is NOT transcribed is the code: the argv comes from ``TmuxAdapter``, so it is
covered by the two shipped AST guards, and the fork gate had to be rewritten because upstream
keys it on a running asyncio loop that this package does not have.

## Why a fork onto a pty, and not the mechanism that looks safer

``os.forkpty()`` in a process with any live non-main thread emits ``DeprecationWarning: ... is
multi-threaded, use of forkpty() may lead to deadlocks in the child``, and ``enroll.py`` starts
daemon threads for both enrollment and the heartbeat -- so shellbox's fork is unavoidably
multithreaded. ``os.openpty()`` plus ``subprocess.Popen(start_new_session=True)`` emits nothing,
because the fork and exec happen inside ``_posixsubprocess.fork_exec`` in C with no Python
executed in the child. It looks strictly better.

CRITICAL: **It is not, and the reason is measured (spike F17).** ``Popen`` produces a client
that attaches (``#{session_attached}`` reads 1), streams correctly, and then **silently discards
every resize** -- the window stayed at 120x40 when ``TIOCSWINSZ`` moved the master to 100x30.
The kernel delivers ``SIGWINCH`` to the pty's **foreground process group**, and a child with no
controlling terminal is not in one; ``subprocess`` performs no ``TIOCSCTTY``. That is worse than
an attach that fails outright, because nothing reports it. ``preexec_fn`` is not an escape
route either: it reinstates running Python in the child after a fork, which is the hazard that
made ``Popen`` attractive.

So the fork stays, the warning is silenced deliberately, and the mitigation is upstream's and
already in force: ``os.execve`` rather than ``execvpe``, so the child allocates nothing between
the fork and the exec (``ws_bridge.py:151``).
"""

from __future__ import annotations

import asyncio
import errno
import fcntl
import logging
import os
import shutil
import signal
import struct
import termios
import threading
import time
import warnings
from collections.abc import Mapping, Sequence
from typing import Protocol

logger = logging.getLogger(__name__)

__all__ = [
    "COALESCE_INTERACTIVE_MAX",
    "COALESCE_MAX",
    "INTERACTIVE_WINDOW",
    "AttachedPty",
    "PtySource",
    "read_limit",
    "resolve_binary",
]

# The fork gate. MODULE-LEVEL and a `threading.Semaphore`, which is the one place this file
# deliberately differs from upstream rather than transcribing it.
#
# `pty.fork` copies the parent's page tables, which stalls the caller in proportion to the
# parent's heap -- so upstream serializes it (`ws_bridge.py:100-121`). But upstream keys its
# gate on `asyncio.get_running_loop()`, and `shellbox-mcp` has no loop: its six tools are
# synchronous and the package imports neither asyncio nor anyio outside the transport. A
# loop-keyed gate here would gate NOTHING -- silently, since each publisher's thread has its
# own loop and would take its own semaphore.
#
# What is being serialized is a property of the ADDRESS SPACE, not of a loop, so the gate has
# to be process-wide. A sandbox runs 1-32 agents through rotating MCP processes, and several
# publishers can start in the same second after an edge kill.
_FORK_GATE = threading.Semaphore(1)

# The exit status the child uses when `execve` never happened. Distinct from anything tmux
# itself exits with, so a reaped status of 126 says "the exec failed" rather than "tmux said
# no" -- which are different bugs with different fixes.
_EXEC_FAILED = 126

# Output coalescing, transcribed from `ws_bridge.py:56-66`.
#
# 64 KiB normally, because a full-screen repaint or a build's output should cross the socket in
# as few frames as possible. 2 KiB inside a 0.75 s window after a keystroke, because a browser
# terminal echoes synchronously and a viewer notices a coalesced echo as lag -- so the interval
# right after input is the one place a smaller read is worth the extra frames.
COALESCE_MAX = 64 * 1024
COALESCE_INTERACTIVE_MAX = 2 * 1024
INTERACTIVE_WINDOW = 0.75


def read_limit(*, now: float, last_input_at: float | None) -> int:
    """How many bytes to take from the pty in one read. The coalescing decision.

    A single ``os.read`` on a pty master returns whatever the kernel has buffered up to the
    limit, so the limit IS the coalescing -- there is no separate accumulate-and-flush loop to
    get wrong, and no timer that could hold bytes back when nothing more is coming.
    """
    if last_input_at is not None and now - last_input_at < INTERACTIVE_WINDOW:
        return COALESCE_INTERACTIVE_MAX
    return COALESCE_MAX


def resolve_binary(argv: Sequence[str]) -> list[str]:
    """Resolve ``argv[0]`` to an absolute path, because ``execve`` does not search ``PATH``.

    ``execvpe`` would search it, and upstream's reason for choosing ``execve`` instead is that
    the child must allocate nothing before the exec (``ws_bridge.py:151``) -- a ``PATH`` walk in
    a freshly forked, multithreaded child is exactly the allocation being avoided. So the walk
    happens HERE, in the parent, before the fork.

    ``TmuxConfig.tmux_bin`` defaults to the bare string ``tmux`` on purpose: ADR-1 makes it the
    single resolution point and ``PATH`` is how it resolves. Every other tmux invocation goes
    through ``subprocess.run``, which searches ``PATH`` itself, so this is the one call site
    where the default would otherwise fail -- and it would fail as ``FileNotFoundError`` from
    inside a forked child, where there is nobody left to report it to.
    """
    if not argv:
        raise ValueError("argv is empty")
    resolved = argv[0] if os.path.isabs(argv[0]) else shutil.which(argv[0])
    if resolved is None:
        raise FileNotFoundError(
            f"{argv[0]!r} is not on PATH; `execve` does not search it, so the attach child "
            "would fail after the fork with nobody to report it to"
        )
    return [resolved, *argv[1:]]


class PtySource(Protocol):
    """The seam the bridge reads and writes through, so it is testable without a real fork.

    Deliberately narrow: four operations, and nothing that leaks a file descriptor. A unit test
    supplies a queue-backed double; ``AttachedPty`` is the real thing, and the tmux lane runs
    the same bridge against it.
    """

    async def read(self, limit: int) -> bytes:
        """Up to ``limit`` bytes, waiting for at least one. ``b""`` means EOF."""
        ...

    def write(self, data: bytes) -> None:
        """Write ``data`` to the pty, byte-exact."""
        ...

    def set_window_size(self, cols: int, rows: int) -> None:
        """Resize the pty. The client sees ``SIGWINCH``."""
        ...

    def close(self) -> None:
        """Close the master and reap the child. Idempotent."""
        ...


class AttachedPty:
    """A live ``tmux attach`` client on a pty this process owns.

    Not thread-safe, and it does not need to be: one publisher owns one attach and drives it
    from one thread with one loop.
    """

    __slots__ = ("_closed", "_fd", "_grace", "_pid", "_status")

    def __init__(self, pid: int, fd: int, *, grace: float = 2.0) -> None:
        self._pid = pid
        self._fd = fd
        self._grace = grace
        """How long the child gets between SIGTERM and SIGKILL.

        A constructor argument rather than a ``close`` parameter so that ``close()`` matches
        ``PtySource`` exactly -- the bridge calls it through that protocol, and a keyword the
        protocol does not have would make a fake and the real thing differ in the one method
        whose omission leaks a tmux client."""
        self._status: int | None = None
        self._closed = False
        os.set_blocking(fd, False)

    @classmethod
    def spawn(
        cls, argv: Sequence[str], env: Mapping[str, str], *, grace: float = 2.0
    ) -> AttachedPty:
        """Fork onto a pty and exec ``argv``. The parent gets the master fd.

        ``argv`` must come from ``TmuxAdapter.prepare_attach``, which is what puts it under
        ``tests/unit/test_target.py``'s AST guard -- so the ``-t`` is the anchored ``=<name>:``
        form and the binary is ``TmuxConfig.tmux_bin``. Building an argv here instead is the
        specific mistake omnigent's bridge makes twice (``ws_bridge.py:492`` and its pane-dead
        probe), and it is why that guard's scope was widened to all of ``packages/``.

        ``env`` must come from ``TmuxAdapter.attach_env``. ``TERM`` there describes the FAR end
        and is load-bearing rather than hygiene: ``tmux attach`` under ``TERM=dumb`` is refused
        outright with ``open terminal failed: terminal does not support clear`` (spike F17), and
        a headless host has no tty so bash substitutes exactly that.
        """
        resolved = resolve_binary(argv)
        child_env = dict(env)

        # The gate is held across the fork only. Nothing inside it does I/O or takes another
        # lock, so it cannot become a place two publishers deadlock.
        with _FORK_GATE:
            with warnings.catch_warnings():
                # Silenced DELIBERATELY, per spike F17's action, and not because the warning is
                # wrong. CPython emits it whenever `threading.active_count() > 1` regardless of
                # the call site, and shellbox's fork is unavoidably multithreaded because
                # `enroll.py` runs an enrollment thread and a heartbeat thread. The standard
                # mitigation is child-minimality, which is exactly the `os.execve` below.
                #
                # The alternative -- `os.openpty()` + `Popen` -- emits nothing and then silently
                # discards every resize (F17). Measured, both lanes. A quiet warning is worth
                # far less than a working `TIOCSWINSZ`.
                #
                # `catch_warnings` mutates process-global filter state, so a warning raised by
                # another thread in this microsecond-wide window would also be swallowed. That
                # is accepted over a module-level `filterwarnings`, which would swallow it for
                # the process's whole life.
                warnings.simplefilter("ignore", DeprecationWarning)
                pid, fd = os.forkpty()

            if pid == 0:
                # ================= CHILD =================
                # Allocate NOTHING here. `os.execve`, not `execvpe`: a PATH walk in a freshly
                # forked child of a multithreaded parent is the allocation the whole
                # child-minimality mitigation exists to avoid, so `resolve_binary` did it in the
                # parent. `os._exit`, not `sys.exit`: the latter raises SystemExit, which would
                # unwind through the parent's `finally` blocks in a copy of the parent's stack
                # and could flush the parent's buffers a second time.
                try:
                    os.execve(resolved[0], resolved, child_env)
                except BaseException:  # noqa: BLE001 - there is nobody left to report to
                    os._exit(_EXEC_FAILED)
                os._exit(_EXEC_FAILED)  # unreachable; exec does not return
                # =========================================

        logger.info("attach client pid %d on pty fd %d: %s", pid, fd, " ".join(resolved))
        return cls(pid, fd, grace=grace)

    @property
    def pid(self) -> int:
        return self._pid

    @property
    def status(self) -> int | None:
        """The child's ``waitpid`` status, or ``None`` while it is still running.

        ``_EXEC_FAILED`` in the exit code means the fork happened and the exec did not, which is
        a different bug from tmux refusing to attach and is worth being able to tell apart.
        """
        return self._status

    def fileno(self) -> int:
        return self._fd

    async def read(self, limit: int) -> bytes:
        """Up to ``limit`` bytes from the pty master. ``b""`` at EOF.

        CRITICAL: **EOF on a pty master is two different things depending on the platform**, and
        a reader that handles only one of them hangs on the other. When the slave side closes,
        Linux fails the read with ``EIO`` while macOS returns zero bytes. Both mean the attach
        client is gone, which is the event the whole detach-versus-dead classification hangs
        off -- so both are folded into ``b""`` here rather than one of them escaping as an
        ``OSError`` the bridge would classify as a transport failure.
        """
        loop = asyncio.get_running_loop()
        while True:
            try:
                data = os.read(self._fd, limit)
            except BlockingIOError:
                await self._wait_readable(loop)
                continue
            except OSError as exc:
                if exc.errno == errno.EIO:
                    return b""  # Linux's spelling of "the slave closed".
                raise
            return data  # b"" is macOS's spelling of the same thing.

    async def _wait_readable(self, loop: asyncio.AbstractEventLoop) -> None:
        """Suspend until the master has something to read.

        ``add_reader`` rather than a thread or a poll interval: this is a publisher's hot path,
        a poll would add latency to every keystroke's echo, and a reader thread would put the
        pty behind a queue that the resize and close paths would then also have to cross.
        """
        future: asyncio.Future[None] = loop.create_future()

        def _ready() -> None:
            if not future.done():
                future.set_result(None)

        loop.add_reader(self._fd, _ready)
        try:
            await future
        finally:
            loop.remove_reader(self._fd)

    def write(self, data: bytes) -> None:
        """Write to the pty, looping until every byte is accepted.

        The loop is not optional. The fd is non-blocking, so a large write can be partially
        accepted, and a caller that ignored the return value would deliver a **prefix** of the
        viewer's keystrokes -- the silent-truncation failure the per-line ceiling exists to
        prevent, reintroduced one layer down.
        """
        view = memoryview(data)
        while view:
            try:
                written = os.write(self._fd, view)
            except BlockingIOError:
                # The pty's input buffer is full. Rare for interactive input, and yielding here
                # would need this method to be async for one pathological case; a short spin is
                # the smaller cost.
                time.sleep(0.001)
                continue
            view = view[written:]

    def set_window_size(self, cols: int, rows: int) -> None:
        """``TIOCSWINSZ`` on the master. The kernel raises ``SIGWINCH`` in the client.

        No tmux round trip, which is the fourth of ADR-9's drivers. It works only because the
        child holds the pty as its **controlling terminal** -- see this module's docstring and
        spike F17 for the mechanism that made the ``Popen`` alternative fail here.

        The struct is ``ws_row, ws_col, ws_xpixel, ws_ypixel``, so rows come FIRST. Swapping
        them produces a client that renders at a transposed size, which looks like a rendering
        bug rather than an ioctl bug.
        """
        if cols < 1 or rows < 1:
            raise ValueError(f"cols={cols} rows={rows}: both must be at least 1")
        fcntl.ioctl(self._fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    def close(self) -> None:
        """Close the master, then reap the child. Idempotent. Read ``status`` for the result.

        CRITICAL: **An unreaped attach child is not untidiness.** It stays a live tmux client on
        that session indefinitely, and a live client holds the window at that client's size --
        which is PM3's reflow made permanent by a crash, on a session an agent is still using.
        That is why this escalates rather than sending one signal and hoping.

        The sequence is upstream's (``ws_bridge.py:204``): close the master so the client sees
        its terminal go away, ``SIGTERM``, poll ``waitpid(WNOHANG)`` for a bounded grace period,
        then ``SIGKILL`` and wait. Closing first matters -- a tmux client whose terminal has
        gone exits on its own, so the common case is reaped before the signal is even needed.
        """
        if self._closed:
            return
        self._closed = True

        try:
            os.close(self._fd)
        except OSError as exc:
            logger.debug("closing attach pty fd %d: %s", self._fd, exc)

        if self._reap_nowait() is not None:
            return

        self._signal(signal.SIGTERM)
        deadline = time.monotonic() + self._grace
        while time.monotonic() < deadline:
            if self._reap_nowait() is not None:
                return
            time.sleep(0.01)

        logger.warning(
            "attach child pid %d survived SIGTERM for %.1fs; escalating to SIGKILL",
            self._pid,
            self._grace,
        )
        self._signal(signal.SIGKILL)
        try:
            _, status = os.waitpid(self._pid, 0)
        except ChildProcessError:
            # Already reaped, by this process or by an outer waiter. Not an error: the
            # obligation was that the child not survive, and it did not.
            self._status = self._status if self._status is not None else 0
        else:
            self._status = status

    def _reap_nowait(self) -> int | None:
        """Reap without blocking. ``None`` while the child is still running."""
        if self._status is not None:
            return self._status
        try:
            pid, status = os.waitpid(self._pid, os.WNOHANG)
        except ChildProcessError:
            self._status = 0
            return self._status
        if pid == 0:
            return None
        self._status = status
        return status

    def _signal(self, number: int) -> None:
        try:
            os.kill(self._pid, number)
        except ProcessLookupError:
            # It exited between the poll and the signal. The whole point of the poll.
            return
