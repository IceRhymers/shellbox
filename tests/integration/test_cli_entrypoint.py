"""The console-script entrypoint: zero-args serve, and every diagnostic on stderr.

No tmux needed, so this runs in every lane. It covers the constraints §4 puts on the
entrypoint rather than the tool surface:

* ``{"command": "shellbox-mcp", "args": []}`` must be a complete registration -- ``buzz-acp``
  spawns MCP servers with ``args: vec![]`` (#6), so a design needing a flag could never be
  used there. That is asserted by every other module in this lane, which launches the server
  with no arguments at all.
* **Nothing but protocol on stdout, ever** -- including usage text and startup failures. By
  the time a bad argument is reported, a client may already be reading stdout for a handshake,
  and a usage message there is indistinguishable from a corrupt stream.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ARGV = [sys.executable, "-m", "shellbox_mcp"]


def _run(args: list[str], tmp_path: Path, **env_overrides: str) -> subprocess.CompletedProcess[str]:
    """Run the entrypoint with an environment built from scratch and no stdin.

    ``stdin=DEVNULL`` matters: ``serve`` would otherwise block waiting for a JSON-RPC
    handshake, and every case here is expected to exit before serving.
    """
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path),
        "SHELLBOX_STATE_DIR": str(tmp_path / "state"),
        "SHELLBOX_HOST_ID": "cli-host",
        **env_overrides,
    }
    return subprocess.run(  # noqa: S603 -- fixed argv, no shell
        [*ARGV, *args],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_help_goes_to_stderr_and_stdout_stays_empty(tmp_path: Path) -> None:
    result = _run(["--help"], tmp_path)
    assert result.returncode == 0
    assert result.stdout == "", f"usage text reached stdout: {result.stdout!r}"
    assert "usage: shellbox-mcp" in result.stderr


def test_a_deferred_subcommand_says_so_and_exits_non_zero(tmp_path: Path) -> None:
    """``doctor``/``enroll``/``bootstrap`` are named, so the error is "not yet", not "no such".

    The difference decides where the reader looks next: at the plan, or at their spelling.
    """
    for command, work_item in (("doctor", "W8"), ("enroll", "W7"), ("bootstrap", "W8")):
        result = _run([command], tmp_path)
        assert result.returncode != 0, command
        assert result.stdout == ""
        assert "not implemented yet" in result.stderr
        assert work_item in result.stderr


def test_an_unknown_subcommand_exits_non_zero_with_usage_on_stderr(tmp_path: Path) -> None:
    result = _run(["frobnicate"], tmp_path)
    assert result.returncode != 0
    assert result.stdout == ""
    assert "unknown command" in result.stderr
    assert "usage: shellbox-mcp" in result.stderr


def test_a_malformed_setting_fails_at_startup(tmp_path: Path) -> None:
    """A typo'd limit is a startup failure, NOT a silent revert to the default.

    ``SHELLBOX_MAX_SEND_LINE_BYTES`` is the correctness boundary of §8: an operator who set it
    to nonsense believes they moved that boundary, and serving happily at 1000 would be the
    server disagreeing with them in silence.
    """
    result = _run([], tmp_path, SHELLBOX_MAX_SEND_LINE_BYTES="lots")
    assert result.returncode != 0
    assert result.stdout == ""
    assert "SHELLBOX_MAX_SEND_LINE_BYTES" in result.stderr


def test_an_unrecognised_log_level_warns_but_still_serves(tmp_path: Path) -> None:
    """The opposite call for ``SHELLBOX_LOG_LEVEL``: warn and start at INFO.

    stderr is the only diagnostic channel this process has. Refusing to start because its
    verbosity was misspelled would remove the channel in order to complain about it.
    """
    result = _run([], tmp_path, SHELLBOX_LOG_LEVEL="CHATTY")
    # stdin is /dev/null, so `serve` reaches EOF immediately and exits cleanly -- which is
    # itself the assertion that it got as far as serving.
    assert result.returncode == 0, result.stderr
    assert "is not a known level" in result.stderr
    assert "shellbox-mcp serving on stdio" in result.stderr
