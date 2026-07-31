"""Shared test infrastructure -- above all, the synchronization model (plan §11.1).

**Every tmux assertion needs a synchronization model, and a fixed ``sleep`` is not one.**
tmux commands return as soon as the *server* accepts them, long before the pane's process
has consumed anything, so ``sleep``-based tmux suites pass locally and fail under CI load.
Everything here polls for a *condition* with a deadline. Nothing here sleeps for a duration
and then asserts.

⚠️ **A warning for whoever maintains this suite.** Byte-exactness is asserted against the
**file the pane process wrote**, never against ``capture-pane``: the pane renders, wraps and
normalises, so a screen scrape is not an oracle for delivered bytes.

⚠️ **And the trap that would make CI green while production drops bytes:** the canonical-mode
and raw-mode reader panes test *different things* and must never be unified. A canonical-mode
pane exists to prove the ``line_too_long`` guard fires before tmux is touched; a raw-mode pane
(``stty -icanon``) exists to prove shellbox's delivery path is byte-exact. "Fixing" a flaky
byte-exactness test by adding ``stty -icanon`` to the canonical case deletes the first
property and keeps the test green -- and over-long lines are DROPPED on macOS and silently
TRUNCATED on Linux, so nothing else would notice.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
import uuid
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass

import pytest
from shellbox_mcp.tmux import CommandResult, TmuxAdapter, TmuxConfig

TMUX_BIN = os.environ.get("SHELLBOX_TMUX_BIN") or shutil.which("tmux")

# Applied by every module under tests/tmux/. A missing tmux skips rather than fails: the
# unit lane must stay runnable anywhere, and CI's tmux-3.4 container lane is where the real
# gate lives.
requires_tmux = pytest.mark.skipif(TMUX_BIN is None, reason="tmux binary not available")

# Sockets live directly under /tmp with short names: `sun_path` is 104 bytes on macOS and
# 108 on Linux (naming.sun_path_limit), and pytest's own tmp_path is long enough to blow
# through that on its own.
_SOCKET_ROOT = "/tmp"

DEFAULT_TIMEOUT = 5.0
_POLL_INTERVAL = 0.02


def await_condition(
    predicate: Callable[[], bool], *, timeout: float = DEFAULT_TIMEOUT, what: str = "condition"
) -> None:
    """Poll ``predicate`` until true, or fail the test. The primitive everything else uses."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(_POLL_INTERVAL)
    raise AssertionError(f"timed out after {timeout}s waiting for {what}")


def await_file(
    path: str,
    predicate: Callable[[bytes], bool],
    *,
    timeout: float = DEFAULT_TIMEOUT,
    what: str = "file contents",
) -> bytes:
    """Poll a file written by a pane process until ``predicate`` holds; return its bytes.

    This is the byte-exactness oracle: the pane *process* wrote these bytes, so unlike
    ``capture-pane`` they have not been rendered, wrapped or normalised.
    """
    contents = b""

    def check() -> bool:
        nonlocal contents
        try:
            with open(path, "rb") as handle:
                contents = handle.read()
        except OSError:
            return False
        return predicate(contents)

    await_condition(check, timeout=timeout, what=f"{what} at {path}")
    return contents


def await_file_bytes(path: str, minimum: int, *, timeout: float = DEFAULT_TIMEOUT) -> bytes:
    """Poll until ``path`` holds at least ``minimum`` bytes."""
    return await_file(
        path, lambda data: len(data) >= minimum, timeout=timeout, what=f">= {minimum} bytes"
    )


@dataclass
class TmuxServer:
    """One tmux server on its own short socket, torn down after the test."""

    socket_path: str
    tmux_bin: str

    def raw(self, *args: str, stdin: bytes | None = None) -> CommandResult:
        """Invoke tmux directly, bypassing the adapter.

        Only for the raw-tmux regression assertions (§7.1's two-level split), which have to
        exercise the very target forms the adapter forbids -- proving they are unsafe is the
        point, so they cannot go through ``target()``.
        """
        argv = [self.tmux_bin, "-S", self.socket_path, "-f", "/dev/null", *args]
        proc = subprocess.run(argv, input=stdin, capture_output=True, shell=False)
        return CommandResult(
            argv=tuple(argv),
            rc=proc.returncode,
            stdout_raw=proc.stdout.decode("utf-8", errors="replace"),
            stderr=proc.stderr.decode("utf-8", errors="replace"),
        )

    def config(self, **overrides: object) -> TmuxConfig:
        settings: dict[str, object] = {
            "socket_path": self.socket_path,
            "tmux_bin": self.tmux_bin,
        }
        settings.update(overrides)
        return TmuxConfig(**settings)  # type: ignore[arg-type]

    def adapter(self, **overrides: object) -> TmuxAdapter:
        return TmuxAdapter(self.config(**overrides))

    def sessions(self) -> list[str]:
        """Session names straight from tmux, for assertions about what the adapter did."""
        result = self.raw("list-sessions", "-F", "#{session_name}")
        if result.rc != 0:
            return []
        return [line for line in result.stdout_raw.split("\n") if line]

    def kill(self) -> None:
        subprocess.run(
            [self.tmux_bin, "-S", self.socket_path, "kill-server"],
            capture_output=True,
            shell=False,
        )
        try:
            os.unlink(self.socket_path)
        except OSError:
            pass


@pytest.fixture(autouse=True)
def _keep_shellbox_loggers_capturable() -> None:
    """Undo ``logging.config.fileConfig``'s ``disable_existing_loggers`` default.

    ``shellbox-registry``'s alembic ``env.py`` calls ``fileConfig(config_file_name)``, which
    defaults to ``disable_existing_loggers=True`` and therefore DISABLES every logger created
    before it -- including ``shellbox_mcp.*``. Once a migration test has run, every later
    ``caplog`` assertion in the session sees nothing, so several tests here pass or fail
    depending on collection order.

    This makes those assertions order-independent. It is not the real fix: alembic's
    ``env.py`` should pass ``disable_existing_loggers=False``, which matters beyond the test
    suite -- running a migration in-process would otherwise silence the MCP server's logging,
    and stderr is its only diagnostic channel.
    """
    # Every logger, not just the package roots: `fileConfig` disables each existing logger
    # object individually, and `shellbox_mcp.tmux` is a different object from `shellbox_mcp`.
    for name, logger in logging.Logger.manager.loggerDict.items():
        if name.startswith("shellbox") and isinstance(logger, logging.Logger):
            logger.disabled = False
            logger.propagate = True


@pytest.fixture
def tmux_server() -> Iterator[TmuxServer]:
    """A private, empty tmux server per test.

    Per test, not per session: several assertions here are about what a SECOND create does
    to a server that already holds sessions, which is where the server-killing global option
    was found. Sharing a server across tests would let one test's leftovers decide another
    test's outcome.
    """
    assert TMUX_BIN is not None, "tmux_server fixture requires tmux; use @requires_tmux"
    server = TmuxServer(
        socket_path=os.path.join(_SOCKET_ROOT, f"sbxt{uuid.uuid4().hex[:8]}"),
        tmux_bin=TMUX_BIN,
    )
    try:
        yield server
    finally:
        server.kill()


@dataclass
class RecordingRunner:
    """A fake ``Runner`` that records argv and replays scripted results.

    The unit lane's whole point: assert the argv shellbox BUILDS, with no tmux involved, so
    "the adapter never invoked tmux" is itself assertable.
    """

    results: list[CommandResult] | None = None
    default: CommandResult | None = None

    def __post_init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], bytes | None]] = []
        self._queue = list(self.results or [])

    def __call__(self, argv: Sequence[str], stdin: bytes | None = None) -> CommandResult:
        self.calls.append((tuple(argv), stdin))
        if self._queue:
            scripted = self._queue.pop(0)
            return CommandResult(
                argv=tuple(argv),
                rc=scripted.rc,
                stdout_raw=scripted.stdout_raw,
                stderr=scripted.stderr,
            )
        if self.default is not None:
            return CommandResult(
                argv=tuple(argv),
                rc=self.default.rc,
                stdout_raw=self.default.stdout_raw,
                stderr=self.default.stderr,
            )
        return CommandResult(argv=tuple(argv), rc=0, stdout_raw="", stderr="")

    @property
    def argvs(self) -> list[tuple[str, ...]]:
        return [argv for argv, _ in self.calls]

    def sub_argv(self, verb: str) -> tuple[str, ...]:
        """The first recorded argv containing ``verb``, for per-verb assertions."""
        for argv in self.argvs:
            if verb in argv:
                return argv
        raise AssertionError(f"no recorded tmux invocation contained {verb!r}: {self.argvs}")


def result(rc: int = 0, stdout: str = "", stderr: str = "") -> CommandResult:
    """Terse ``CommandResult`` constructor for scripted runners."""
    return CommandResult(argv=(), rc=rc, stdout_raw=stdout, stderr=stderr)
