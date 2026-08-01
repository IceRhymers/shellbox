"""How shellbox reaches tmux: argv lists, ``shell=False``, a forced ``TERM``, a minimal env.

Two distinct properties, both easy to lose in a refactor and neither visible in a passing
functional test:

* **``TERM`` is forced on every invocation.** tmux is claimed to refuse to start under an
  unset or dumb ``TERM`` in these sandboxes (still unverified by us -- it is a sandbox-only
  test), so this is set unconditionally rather than inherited.
* **The MCP process's environment is NOT handed to the tmux server.** The harness injects
  credentials into that environment, and a tmux server inherits its environment from the
  client that started it and passes it on to every pane it later spawns. So the pass-through
  list is an allowlist.
"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path

import pytest
from shellbox_mcp.tmux import SubprocessRunner, TmuxAdapter, TmuxConfig

PACKAGES = Path(__file__).resolve().parents[2] / "packages"
CONFIG = TmuxConfig(socket_path="/tmp/sbx-hygiene.sock")


def test_term_is_forced(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TERM", "dumb")
    assert SubprocessRunner(CONFIG).env["TERM"] == "xterm-256color"
    monkeypatch.delenv("TERM", raising=False)
    assert SubprocessRunner(CONFIG).env["TERM"] == "xterm-256color"


def test_the_environment_is_an_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABRICKS_TOKEN", "dkea-secret")
    monkeypatch.setenv("HOME", "/home/agent")
    env = SubprocessRunner(CONFIG).env
    assert "DATABRICKS_TOKEN" not in env, "a credential must not reach the tmux server"
    assert env["HOME"] == "/home/agent"
    assert set(env) <= {"TERM", "LC_CTYPE", *CONFIG.passthrough_env}


def test_a_utf8_ctype_locale_is_forced(monkeypatch: pytest.MonkeyPatch) -> None:
    """CRITICAL: The whole 8-field record depends on this, and it is the bug W2 nearly shipped.

    Measured in both lanes: with a non-UTF-8 ctype locale, tmux encodes the TAB in format
    output as ``_`` -- in ``list-sessions -F`` as well as ``display-message`` -- so all eight
    fields collapse into one, every record is dropped as malformed, ``shell_list`` reports an
    EMPTY inventory, and orphan reconciliation marks every live session ``orphaned``.

    A locale is normally absent in a container, a systemd unit and a sandbox, so passing
    ``LANG`` through is not a fix: there is often nothing to pass. It must be forced.
    """
    for var in ("LANG", "LC_ALL", "LC_CTYPE"):
        monkeypatch.delenv(var, raising=False)
    env = SubprocessRunner(CONFIG).env
    assert env["LC_CTYPE"] == "C.UTF-8"

    # And a hostile LC_ALL must not reach tmux: LC_ALL overrides LC_CTYPE, which would
    # silently reinstate the mangling.
    monkeypatch.setenv("LC_ALL", "C")
    assert "LC_ALL" not in SubprocessRunner(CONFIG).env
    assert "LC_ALL" not in CONFIG.passthrough_env


def test_every_invocation_uses_the_forced_env_an_argv_list_and_shell_false(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Asserted at the ``subprocess.run`` boundary, so it covers every adapter method."""
    calls: list[tuple[object, dict[str, object]]] = []

    class FakeCompleted:
        returncode = 0
        stdout = b"build\t00000000-0000-4000-8000-000000000001\n"
        stderr = b""

    def fake_run(argv, **kwargs):  # type: ignore[no-untyped-def]
        calls.append((argv, kwargs))
        return FakeCompleted()

    monkeypatch.setattr(subprocess, "run", fake_run)
    adapter = TmuxAdapter(CONFIG)
    adapter.create("build", cwd=str(tmp_path))
    adapter.send("build", text="hi\n")
    adapter.exists("build")

    assert calls
    for argv, kwargs in calls:
        assert isinstance(argv, list), "argv must be a LIST, never a string"
        assert all(isinstance(item, str) for item in argv)
        assert kwargs["shell"] is False
        assert kwargs["env"]["TERM"] == "xterm-256color"  # type: ignore[index]


def _subprocess_run_calls() -> list[tuple[Path, ast.Call]]:
    found = []
    for path in sorted(PACKAGES.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "run"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "subprocess"
            ):
                found.append((path, node))
    return found


def test_no_shipped_subprocess_call_uses_a_shell() -> None:
    """``shell=False`` is passed explicitly, and ``shell=True`` appears nowhere.

    Explicitly, not by default: the default is the correct value, and a reader should not
    have to know that to audit the call.
    """
    calls = _subprocess_run_calls()
    assert calls, "expected at least one subprocess.run in the packages"
    for path, call in calls:
        where = f"{path.relative_to(PACKAGES)}:{call.lineno}"
        keywords = {kw.arg: kw.value for kw in call.keywords}
        assert "shell" in keywords, f"{where}: pass shell=False explicitly"
        shell = keywords["shell"]
        assert isinstance(shell, ast.Constant) and shell.value is False, (
            f"{where}: shell must be False"
        )
        first = call.args[0] if call.args else None
        assert not isinstance(first, ast.Constant), f"{where}: argv must not be a string literal"

    for path in sorted(PACKAGES.rglob("*.py")):
        text = path.read_text()
        assert "shell=True" not in text, f"{path}: shell=True"
        assert "os.system" not in text, f"{path}: os.system"
        assert "os.popen" not in text, f"{path}: os.popen"
