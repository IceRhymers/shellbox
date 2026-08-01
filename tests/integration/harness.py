"""Integration-lane infrastructure: a REAL MCP client over stdio against a REAL tmux.

Two rules this lane exists to enforce, and they are why nothing here calls a tool function
directly:

1. **Every tool is driven through the SDK's client**, over a spawned process, so the
   assertions cover argument coercion, the derived JSON schemas, and the error envelope --
   all of which are protocol surface that an in-process call skips entirely. A test that
   calls ``shell_create(...)`` as a Python function cannot fail the way a client does.
2. **The child's environment is built from scratch**, never inherited. A developer with
   ``SHELLBOX_DATABASE_URL`` or ``SHELLBOX_TMUX_SOCKET`` exported would otherwise run a
   different test than CI does -- and in the socket's case, against their own live sessions.

Synchronization follows §11.1: poll for a condition with a deadline (``await_content``),
never ``sleep`` then assert. Tests are ordinary synchronous functions -- ``run_script``
owns the event loop -- so this lane needs no async pytest plugin.

⚠️ **This is deliberately not a ``conftest.py``.** Under pytest's ``prepend`` import mode a
second ``conftest.py`` is imported under the module name ``conftest`` as well, which shadows
``tests/conftest.py`` -- so ``from conftest import TmuxServer`` inside it resolves to the
file being imported and fails as a circular import. Tests therefore call ``make_harness``
with the ``tmux_server`` and ``tmp_path`` fixtures instead of requesting a local fixture.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anyio
import mcp.types as types
from conftest import TmuxServer
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

__all__ = [
    "Harness",
    "Outcome",
    "await_content",
    "call",
    "fake_tmux",
    "RawSession",
    "make_harness",
    "raw_session",
    "run_calls",
    "run_script",
]

# A whole stdio session -- spawn, handshake, calls, teardown -- against a local tmux. Long
# enough to absorb CI scheduling noise, short enough that a hung handshake fails the test
# rather than the job.
SESSION_TIMEOUT = 60.0
_POLL_INTERVAL = 0.05

# A password that must never appear in a tool payload. Tests assert its absence, so it is
# deliberately distinctive rather than realistic.
LEAK_CANARY = "pw-must-not-leak"  # noqa: S105 - not a credential; a marker asserted against


def unreachable_dsn(*, user: str = "sbx", password: str = LEAK_CANARY) -> str:
    """A DSN guaranteed to fail fast, for exercising the registry-is-non-fatal path.

    Port 1 is reserved and never listening, so a connect attempt fails immediately rather
    than hanging on a timeout.

    Assembled from parts instead of written as a literal: a complete credential-bearing URL
    in source trips credential scanners, and the habit is what eventually leaks a real one.
    Mirrors ``shellbox_registry.dsn.dsn_from_env``, which exists for the same reason.
    """
    return f"postgresql://{user}:{password}@127.0.0.1:1/shellbox"


def blackholed_dsn(*, user: str = "sbx", password: str = LEAK_CANARY) -> str:
    """A DSN that **hangs** rather than failing fast. For the does-not-block assertions.

    ``unreachable_dsn`` above is the wrong tool for those: port 1 is closed, so a connect gets
    an immediate RST and every "did this block?" test passes without ever reaching the code path
    that could block. 198.51.100.0/24 is TEST-NET-2 (RFC 5737) and is not routed, so packets are
    dropped and the connect sits there until the driver's own timeout — which is precisely the
    condition enrollment must survive without delaying the handshake.
    """
    return f"postgresql://{user}:{password}@198.51.100.1:5432/shellbox"


@dataclass
class Outcome:
    """One ``tools/call`` result, as the client saw it.

    Deliberately keeps ``is_error`` and the payload separate rather than raising: several
    assertions here are precisely *that a call succeeded* where an earlier revision of the
    taxonomy would have failed it (an idempotent re-create, a kill with nothing to kill).
    """

    is_error: bool
    text: str
    structured: dict[str, Any] | None

    @property
    def data(self) -> dict[str, Any]:
        """The success payload. Fails the test, with the error text, if the call failed."""
        assert not self.is_error, f"expected success, got error: {self.text}"
        if self.structured is not None:
            return self.structured
        return dict(json.loads(self.text))

    @property
    def error(self) -> dict[str, Any]:
        """The §6 error body: ``{"code", "message", "session"}``.

        Asserts the failure is *structured*. An MCP tool error carrying prose would still
        set ``isError``, so a test that only checked ``is_error`` would pass against a
        server whose taxonomy had collapsed into free text.
        """
        assert self.is_error, f"expected an error, got success: {self.text}"
        body = json.loads(self.text)
        assert set(body) == {"error"}, f"unexpected error envelope: {body!r}"
        error = body["error"]
        assert set(error) == {"code", "message", "session"}, f"unexpected error body: {error!r}"
        return dict(error)

    @property
    def code(self) -> str:
        return str(self.error["code"])


@dataclass
class Harness:
    """A tmux server, a scratch state directory, and the child environment for both."""

    tmux: TmuxServer
    env: dict[str, str]
    stderr_path: Path
    tmp_path: Path
    _sequence: list[int] = field(default_factory=lambda: [0])

    def env_with(self, **overrides: str | None) -> dict[str, str]:
        """The base environment plus overrides; ``None`` removes a variable."""
        env = dict(self.env)
        for key, value in overrides.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
        return env

    def stderr(self) -> str:
        """Everything the server processes have written to stderr so far."""
        try:
            return self.stderr_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def name(self, prefix: str = "s") -> str:
        """A fresh session name, so one test's leftovers cannot decide another's outcome."""
        self._sequence[0] += 1
        return f"{prefix}{self._sequence[0]}"


def make_harness(tmux_server: TmuxServer, tmp_path: Path) -> Harness:
    """One tmux server, one scratch home, and an environment built from scratch.

    ``HOME`` points into ``tmp_path``: the server passes ``HOME`` through to the tmux
    client, and a test must not be able to touch the developer's real one. ``PATH`` is
    inherited because the pane needs a shell; the tmux binary itself is passed explicitly.
    """
    home = tmp_path / "home"
    home.mkdir()
    state = tmp_path / "state"
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "SHELLBOX_TMUX_BIN": tmux_server.tmux_bin,
        "SHELLBOX_TMUX_SOCKET": tmux_server.socket_path,
        "SHELLBOX_STATE_DIR": str(state),
        # Pinned, not derived: `shell_list` returns it and every `session` id embeds it, so
        # an assertion on either would otherwise depend on the machine running the test.
        "SHELLBOX_HOST_ID": "itest-host",
        "SHELLBOX_OWNER_EMAIL": "itest@example.com",
        "SHELLBOX_LOG_LEVEL": "INFO",
    }
    return Harness(
        tmux=tmux_server, env=env, stderr_path=tmp_path / "server-stderr.log", tmp_path=tmp_path
    )


def run_script[T](
    harness: Harness,
    script: Callable[[ClientSession], Awaitable[T]],
    *,
    env: dict[str, str] | None = None,
    timeout: float = SESSION_TIMEOUT,
) -> T:
    """Spawn ``shellbox-mcp``, handshake, run ``script`` against it, tear it down.

    ``python -m shellbox_mcp`` rather than the console script: it is the same entrypoint
    (``__main__`` calls ``cli.main``) and it is guaranteed to be the interpreter the test
    suite is running under, so the test cannot silently exercise a stale installed copy.

    The child's stderr is redirected to a file rather than inherited, which is what makes
    "logging goes to stderr" assertable at all -- and keeps a passing run quiet.
    """

    async def main() -> T:
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "shellbox_mcp"],
            env=harness.env if env is None else env,
        )
        with harness.stderr_path.open("a", encoding="utf-8") as errlog:
            with anyio.fail_after(timeout):
                async with stdio_client(params, errlog=errlog) as (read, write):
                    async with ClientSession(read, write) as client:
                        await client.initialize()
                        return await script(client)

    return anyio.run(main)


def run_calls(
    harness: Harness,
    calls: Sequence[tuple[str, dict[str, Any]]],
    *,
    env: dict[str, str] | None = None,
) -> list[Outcome]:
    """Run a sequence of tool calls in ONE stdio session. The common case.

    Sequential, and every outcome is returned rather than the last: most assertions here
    are about a *sequence* (create, then create again; kill, then read) and the earlier
    results are the setup the later ones are asserted against.
    """

    async def script(client: ClientSession) -> list[Outcome]:
        return [await call(client, name, args) for name, args in calls]

    return run_script(harness, script, env=env)


async def call(client: ClientSession, name: str, args: dict[str, Any] | None = None) -> Outcome:
    """One ``tools/call``, over the wire, as an ``Outcome``."""
    result = await client.call_tool(name, args or {})
    text = "\n".join(block.text for block in result.content if isinstance(block, types.TextContent))
    return Outcome(is_error=bool(result.isError), text=text, structured=result.structuredContent)


async def await_content(
    client: ClientSession,
    session: str,
    needle: str,
    *,
    timeout: float = 10.0,
    lines: int = 0,
) -> str:
    """Poll ``shell_read`` until ``needle`` appears on the pane, or fail.

    §11.1: tmux returns as soon as the *server* accepts a command, long before the pane's
    process has consumed anything, so this polls for the condition. It reads through the
    MCP client on purpose -- the wait and the assertion then exercise the same path the
    agent uses.
    """
    deadline = anyio.current_time() + timeout
    content = ""
    while anyio.current_time() < deadline:
        content = str(
            (await call(client, "shell_read", {"session": session, "lines": lines})).data["content"]
        )
        if needle in content:
            return content
        await anyio.sleep(_POLL_INTERVAL)
    raise AssertionError(f"{needle!r} never appeared in the pane within {timeout}s: {content!r}")


@dataclass
class RawSession:
    """What a hand-written JSON-RPC client saw: the child's raw stdout, and its stderr."""

    stdout_lines: list[str]
    stderr: str
    returncode: int | None


def raw_session(
    harness: Harness,
    requests: Sequence[dict[str, Any]],
    *,
    env: dict[str, str] | None = None,
    timeout: float = SESSION_TIMEOUT,
) -> RawSession:
    """Drive the server with hand-written JSON-RPC and return EVERY byte it wrote to stdout.

    Deliberately not the SDK client: the SDK parses stdout for us and drops what it cannot
    use, so a stray non-protocol line would be invisible in exactly the test that exists to
    catch it. Here the child's stdout is a pipe this function owns, and the assertion is over
    its complete contents.

    stderr goes to a FILE, not a pipe: the server logs to stderr, and a full 64 KiB pipe
    buffer with nobody reading it would block the process mid-session -- a hang that looks
    like a protocol bug and is not one.
    """
    argv = [sys.executable, "-m", "shellbox_mcp"]
    lines: list[str] = []
    with harness.stderr_path.open("a", encoding="utf-8") as errlog:
        process = subprocess.Popen(  # noqa: S603 -- fixed argv, no shell
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=errlog,
            env=harness.env if env is None else env,
            text=True,
            bufsize=1,
        )
        # A watchdog rather than a per-read timeout: a `readline` on a silent child blocks
        # forever, which would hang the suite instead of failing one test.
        watchdog = threading.Timer(timeout, process.kill)
        watchdog.start()
        try:
            assert process.stdin is not None and process.stdout is not None
            for request in requests:
                process.stdin.write(json.dumps(request) + "\n")
                process.stdin.flush()
                if "id" in request:
                    # One response per request, read as we go: this keeps the pipe drained
                    # and makes a missing response a short list rather than a deadlock.
                    lines.append(process.stdout.readline())
            process.stdin.close()
            lines.extend(process.stdout.readlines())
        finally:
            watchdog.cancel()
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:  # pragma: no cover
                process.kill()
    return RawSession(
        stdout_lines=[line for line in lines if line.strip()],
        stderr=harness.stderr(),
        returncode=process.returncode,
    )


def fake_tmux(tmp_path: Path, *, stderr: str, rc: int = 1) -> str:
    """A stand-in tmux that fails with a chosen stderr, for the N1 negative cases.

    Fault injection at the binary rather than through a patched runner: the property under
    test is what the *server process* returns to a client for an unrecognised tmux failure,
    and that process is a separate interpreter which no in-process monkeypatch can reach.
    """
    # Named from a digest of the message, not `hash()`: `hash` is salted per interpreter, so
    # two runs would leave differently-named scripts behind for the same fault.
    script = tmp_path / f"fake-tmux-{hashlib.sha256(stderr.encode()).hexdigest()[:8]}"
    script.write_text(f'#!/bin/sh\nprintf %s\\\\n "{stderr}" >&2\nexit {rc}\n', encoding="utf-8")
    script.chmod(0o755)
    return str(script)
