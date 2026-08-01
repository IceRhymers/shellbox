"""`doctor` and `bootstrap` at the CLI boundary — exit codes and argument refusals.

The unit tests for these live next to their modules; what is asserted here is the contract
an *operator* and a *script* see: what exits non-zero, what refuses, and what stdout stays
clean of.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from shellbox_mcp import identity
from shellbox_mcp.cli import main


@pytest.fixture(autouse=True)
def _short_socket(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A state dir under `tmp_path` and a SHORT socket path.

    The short socket is not incidental: the default is `$SHELLBOX_STATE_DIR/tmux.sock`, and
    pytest's `tmp_path` alone exceeds macOS's 104-byte `sun_path` limit, so every command
    here would fail on a socket-length check unrelated to what is being tested.
    """
    monkeypatch.setenv("SHELLBOX_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SHELLBOX_TMUX_SOCKET", f"/tmp/sbxc{abs(hash(tmp_path)) % 10**8}.sock")
    monkeypatch.delenv("SHELLBOX_DATABASE_URL", raising=False)
    monkeypatch.delenv("SHELLBOX_HOST_ID", raising=False)


def _cache(tmp_path: Path) -> dict[str, object]:
    return json.loads((tmp_path / "state" / identity.HOST_JSON_NAME).read_text())


# ------------------------------------------------------------------------- doctor
def test_doctor_exits_zero_when_nothing_failed(capsys: pytest.CaptureFixture[str]) -> None:
    main(["doctor"])
    captured = capsys.readouterr()
    assert "shellbox-mcp doctor" in captured.err
    assert captured.out == "", "doctor wrote to stdout, which may be a JSON-RPC stream"


def test_doctor_exits_non_zero_on_a_failed_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """The criterion: a health command that always exits 0 is one nobody wires up."""
    monkeypatch.setenv("SHELLBOX_TMUX_BIN", "/nonexistent/tmux")
    with pytest.raises(SystemExit) as exit_info:
        main(["doctor"])
    assert exit_info.value.code == 1


def test_doctor_rejects_arguments() -> None:
    with pytest.raises(SystemExit, match="unexpected arguments"):
        main(["doctor", "--verbose"])


# ---------------------------------------------------------------------- bootstrap
def test_bootstrap_records_the_sandbox_id(tmp_path: Path) -> None:
    """ADR-8: a sandbox cannot learn its own id, so an outside caller injects it."""
    main(["bootstrap", "--sandbox-id", "realistic-phoenix-2742"])
    assert _cache(tmp_path)["sandbox_id"] == "realistic-phoenix-2742"


def test_bootstrap_records_the_gateway_host_too(tmp_path: Path) -> None:
    main(["bootstrap", "--sandbox-id", "sbx-1", "--gateway-host", "gw.example.com"])
    cached = _cache(tmp_path)
    assert (cached["sandbox_id"], cached["gateway_host"]) == ("sbx-1", "gw.example.com")


def test_bootstrap_refuses_to_reset_without_recording_which_sandbox() -> None:
    """🔴 ADR-8's "one invocation does both", in the direction that matters.

    A reset-only run produces a host that has lost its credential AND cannot be named in
    the inventory — `doctor` would report "sandbox_id is NULL: never bootstrapped" about a
    host that demonstrably *was*. Refusing is better than either half.
    """
    with pytest.raises(SystemExit, match="requires --sandbox-id"):
        main(["bootstrap", "--reset-pat"])


def test_bootstrap_with_no_options_does_nothing_and_says_so() -> None:
    """Silence would be indistinguishable from success."""
    with pytest.raises(SystemExit, match="nothing to do"):
        main(["bootstrap"])


def test_bootstrap_rejects_unknown_options() -> None:
    with pytest.raises(SystemExit, match="bad arguments"):
        main(["bootstrap", "--reset-everything"])


def test_bootstrap_registers_codex_preserving_the_model_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The merge, exercised through the CLI: Codex must still be able to reach its model."""
    import tomllib

    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    template = '# placeholder\nmodel_provider = "databricks"\n'
    (home / ".codex" / "config.toml").write_text(template)
    monkeypatch.setenv("HOME", str(home))

    main(["bootstrap", "--register-codex"])

    parsed = tomllib.loads((home / ".codex" / "config.toml").read_text())
    assert parsed["model_provider"] == "databricks"
    assert parsed["mcp_servers"]["shellbox"] == {"command": "shellbox-mcp", "args": []}


def test_bootstrap_reset_pat_removes_the_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / ".databrickscfg").write_text(
        "[DEFAULT]\nhost = https://x.cloud.databricks.com\ntoken = dkeaFAKE\n"
    )
    monkeypatch.setenv("HOME", str(home))

    main(["bootstrap", "--sandbox-id", "sbx-1", "--reset-pat"])

    body = (home / ".databrickscfg").read_text()
    assert "token" not in body
    assert "host = https://x.cloud.databricks.com" in body, "the workspace host was lost"
    assert _cache(tmp_path)["sandbox_id"] == "sbx-1", "the stamp must land even alongside a reset"


# --------------------------------------------------------------------------- usage
def test_enroll_explains_that_serve_already_does_it() -> None:
    """`enroll.py` runs E1-E7 automatically at every start, so a standalone command would
    add a diagnosis path, not correctness. The message must say that rather than
    "not implemented", which sends a reader to the plan looking for missing work."""
    with pytest.raises(SystemExit, match="serve` runs enrollment automatically"):
        main(["enroll"])


def test_help_goes_to_stderr_and_names_the_new_commands(
    capsys: pytest.CaptureFixture[str],
) -> None:
    main(["--help"])
    captured = capsys.readouterr()
    assert captured.out == "", "usage on stdout would corrupt a JSON-RPC stream"
    for expected in ("doctor", "bootstrap", "--sandbox-id", "--reset-pat", "PER-BOOT"):
        assert expected in captured.err


def test_doctor_works_when_the_mcp_sdk_is_unavailable(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """🔴 `doctor` must not need the thing it is diagnosing.

    `cli.py` used to import `server.serve` at module level, which pulls in the MCP SDK and
    pydantic — so `shellbox-mcp doctor` died on an unrelated `ModuleNotFoundError` in
    exactly the situation it exists for. Found in a real sandbox, where a corrupt cached
    wheel left pydantic uninstalled.

    Asserted by making the import genuinely fail rather than by reading the source, because
    the property is "does it run", not "where is the import written".
    """
    import sys

    class _NoMcp:
        def find_module(self, name: str, path: object = None) -> object | None:
            return self if name == "mcp" or name.startswith("mcp.") else None

        def load_module(self, name: str) -> object:
            raise ImportError("mcp is not installed")

    blocker = _NoMcp()
    sys.meta_path.insert(0, blocker)
    for name in [m for m in sys.modules if m == "mcp" or m.startswith("mcp.")]:
        del sys.modules[name]
    try:
        main(["doctor"])
    finally:
        sys.meta_path.remove(blocker)

    assert "shellbox-mcp doctor" in capsys.readouterr().err
