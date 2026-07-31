"""stdout carries ONLY valid JSON-RPC, for a full session (W4 criterion 3).

Why this test exists in this form: stdout *is* the MCP transport, and a single stray byte on
it -- a ``print``, a library's banner, a progress bar, a logging handler defaulting to
stdout -- corrupts the stream. Client-side that appears as an unintelligible parse error
with no indication of which process wrote what, which is close to undiagnosable from the
agent's end. So the assertion is mechanical: capture the child's stdout, parse EVERY line,
and fail on any line that is not a JSON-RPC message.

The session driven here is deliberately a busy one -- all six tools, plus failures, plus a
registry that cannot be reached -- because the interesting stray writes come from error and
warning paths, not from the happy path.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from conftest import TmuxServer, requires_tmux
from harness import make_harness, raw_session, unreachable_dsn
from mcp.types import LATEST_PROTOCOL_VERSION

pytestmark = requires_tmux


def _request(id_: int, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}}


def _tool_call(id_: int, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return _request(id_, "tools/call", {"name": name, "arguments": arguments})


def assert_jsonrpc(line: str) -> dict[str, Any]:
    """Parse one stdout line as a JSON-RPC 2.0 message, or fail with the offending line.

    Checks the envelope, not just "it is JSON": a bare JSON value (``42``, a string, an
    array) parses fine and is not a protocol message, and that is exactly the shape a
    debugging ``print(json.dumps(...))`` leaves behind.
    """
    try:
        message = json.loads(line)
    except json.JSONDecodeError as exc:
        raise AssertionError(f"non-JSON line on stdout: {line!r} ({exc})") from exc
    assert isinstance(message, dict), f"stdout line is JSON but not a JSON-RPC object: {line!r}"
    assert message.get("jsonrpc") == "2.0", f"line is not JSON-RPC 2.0: {line!r}"
    if "method" in message:
        return message  # a request or a notification from the server
    assert "id" in message, f"response carries no id: {line!r}"
    assert ("result" in message) != ("error" in message), (
        f"a JSON-RPC response must carry exactly one of result/error: {line!r}"
    )
    return message


def test_a_full_session_emits_only_valid_jsonrpc_on_stdout(
    tmux_server: TmuxServer, tmp_path: Path
) -> None:
    harness = make_harness(tmux_server, tmp_path)
    name = "protocol"
    requests: list[dict[str, Any]] = [
        _request(
            1,
            "initialize",
            {
                "protocolVersion": LATEST_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "shellbox-raw-probe", "version": "0"},
            },
        ),
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        _request(2, "tools/list"),
        _tool_call(3, "shell_create", {"name": name, "cwd": str(tmp_path)}),
        _tool_call(4, "shell_send", {"session": name, "text": "echo PROTOCOL-OK\n"}),
        _tool_call(5, "shell_read", {"session": name}),
        _tool_call(6, "shell_resize", {"session": name, "cols": 90, "rows": 30}),
        _tool_call(7, "shell_list", {}),
        # Failure paths, on purpose: they are the ones that log, and a logging handler
        # pointed at stdout is the defect this test exists to catch.
        _tool_call(8, "shell_read", {"session": "absent"}),
        _tool_call(9, "shell_send", {"session": name}),
        _tool_call(10, "shell_create", {"name": "=bui"}),
        _tool_call(11, "shell_kill", {"session": name}),
    ]
    # A registry that cannot be reached, so the per-call `registry_warning` path runs too.
    env = harness.env_with(SHELLBOX_DATABASE_URL=unreachable_dsn(user="u", password="p"))

    session = raw_session(harness, requests, env=env)

    responses = [assert_jsonrpc(line) for line in session.stdout_lines]
    ids = [message["id"] for message in responses if "id" in message]
    assert ids == [request["id"] for request in requests if "id" in request], (
        f"one response per request, in order; got {ids}"
    )
    # Every tool call answered; `isError` is fine here -- four of them are meant to fail.
    assert all("result" in message for message in responses), responses

    # And the counterpart: the diagnostics DID happen, they just went to stderr. Without
    # this, a server that emitted nothing anywhere would pass the assertions above.
    assert "shellbox-mcp serving on stdio" in session.stderr
    assert "registry projection failed" in session.stderr


def test_stderr_carries_the_logs_and_stdout_carries_none_of_them(
    tmux_server: TmuxServer, tmp_path: Path
) -> None:
    """``SHELLBOX_LOG_LEVEL=DEBUG`` makes the server chatty; stdout must not notice.

    The log level is the interesting variable: a handler misconfigured onto stdout is
    invisible at ``ERROR`` on a healthy run and catastrophic at ``DEBUG``, which is precisely
    the setting an operator turns on when something is already wrong.
    """
    harness = make_harness(tmux_server, tmp_path)
    requests = [
        _request(
            1,
            "initialize",
            {
                "protocolVersion": LATEST_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "shellbox-raw-probe", "version": "0"},
            },
        ),
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        _tool_call(2, "shell_create", {"name": "debuglevel", "cwd": str(tmp_path)}),
        _tool_call(3, "shell_list", {}),
    ]
    session = raw_session(harness, requests, env=harness.env_with(SHELLBOX_LOG_LEVEL="DEBUG"))

    for line in session.stdout_lines:
        assert_jsonrpc(line)
    assert len(session.stdout_lines) == 3
    assert "DEBUG" in session.stderr, "SHELLBOX_LOG_LEVEL=DEBUG produced no DEBUG records"
