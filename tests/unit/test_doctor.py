"""``shellbox-mcp doctor`` — the diagnostic, and the two ways a diagnostic can be wrong.

A diagnostic fails in two directions and both are tested here:

* **It can lie.** Reporting a developer's ordinary `~/.databrickscfg` as a sandbox's baked
  creator PAT trains people to ignore it. So the sandbox-specific claims are asserted to be
  *conditional*.
* **It can mutate what it is inspecting.** An earlier version called `resolve_host_id`,
  which assigns and persists a uuid4 — so running the diagnostic on a machine that had
  never served minted a permanent identity in a real `$HOME`. That is now a test.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from shellbox_mcp import boot_templated, identity
from shellbox_mcp.config import Settings
from shellbox_mcp.doctor import Level, render, run_checks

CFG_PLACEHOLDER = "; placeholder — populated when credentials are provisioned\n"
CFG_PROVISIONED = "[DEFAULT]\nhost = https://x.cloud.databricks.com\ntoken = dkeaFAKEfake\n"


def _settings(tmp_path: Path, **environ: str) -> Settings:
    """Settings pointing at a temp state dir, with a deliberately SHORT socket path.

    ⚠️ Without the explicit socket, these tests fail for a reason that has nothing to do
    with `doctor`: the default socket is `$SHELLBOX_STATE_DIR/tmux.sock`, and pytest's
    `tmp_path` alone exceeds macOS's 104-byte `sun_path` limit — so `doctor` correctly
    reports FAIL and every "no failures" assertion collapses. `tests/conftest.py` documents
    the same trap for the tmux lane and solves it the same way.

    That `doctor` caught it here is a small point in its favour.
    """
    socket = environ.pop("SHELLBOX_TMUX_SOCKET", f"/tmp/sbxd{abs(hash(tmp_path)) % 10**8}.sock")
    return Settings.from_env(
        {
            "SHELLBOX_STATE_DIR": str(tmp_path / "state"),
            "SHELLBOX_TMUX_SOCKET": socket,
            **environ,
        }
    )


def _by_name(report: object, name: str) -> object:
    for check in report.checks:  # type: ignore[attr-defined]
        if check.name == name:
            return check
    raise AssertionError(
        f"no check named {name!r}; got {[c.name for c in report.checks]}"  # type: ignore[attr-defined]
    )


def _as_lakebox(monkeypatch: pytest.MonkeyPatch, marker: Path) -> None:
    """Make `lakebox_kind()` answer 'lakebox' without needing a real sandbox."""
    marker.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(identity, "_LAKEBOX_MARKERS", (marker,))


# --------------------------------------------------------------------- read-only
def test_doctor_never_assigns_an_identity(tmp_path: Path) -> None:
    """🔴 The bug found by running it: a diagnostic minted a permanent `host_id`.

    `resolve_host_id` assigns and persists when no cache exists, so merely *diagnosing* a
    machine gave it an identity — in a real `$HOME`, on a host that may never serve. A
    command you run because something is wrong must not change what it is inspecting.
    """
    settings = _settings(tmp_path)
    Path(settings.state_dir).mkdir(parents=True, exist_ok=True)

    run_checks(settings)

    assert not (Path(settings.state_dir) / identity.HOST_JSON_NAME).exists(), (
        "doctor wrote an identity cache; it must report what `serve` WOULD do, not do it"
    )


def test_an_unassigned_identity_is_reported_as_normal_not_as_an_error(tmp_path: Path) -> None:
    """A host that has never run is not broken, and saying so avoids a wild goose chase."""
    report = run_checks(_settings(tmp_path))
    check = _by_name(report, "host_id")
    assert check.level is Level.INFO  # type: ignore[attr-defined]
    assert "not yet assigned" in check.detail  # type: ignore[attr-defined]
    assert not report.failed


def test_an_existing_identity_is_read_back(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    Path(settings.state_dir).mkdir(parents=True, exist_ok=True)
    assigned = identity.resolve_host_id(settings.state_dir).host_id

    check = _by_name(run_checks(settings), "host_id")
    assert assigned in check.detail  # type: ignore[attr-defined]


def test_a_corrupt_identity_cache_is_a_FAIL_with_a_safe_remedy(tmp_path: Path) -> None:
    """The remedy must NOT be "delete it" — that re-keys every session_id on the host."""
    settings = _settings(tmp_path)
    Path(settings.state_dir).mkdir(parents=True, exist_ok=True)
    (Path(settings.state_dir) / identity.HOST_JSON_NAME).write_text("not json")

    report = run_checks(settings)
    check = _by_name(report, "host identity")
    assert check.level is Level.FAIL  # type: ignore[attr-defined]
    assert report.failed
    assert "not a safe first move" in (check.remedy or "")  # type: ignore[attr-defined]


# ---------------------------------------------------------- the exit-code contract
def test_a_failed_check_makes_the_report_fail(tmp_path: Path) -> None:
    """`doctor` exits non-zero on any FAIL. A health command that always exits 0 is one
    nobody wires into anything."""
    settings = _settings(tmp_path, SHELLBOX_TMUX_BIN="/nonexistent/tmux")
    report = run_checks(settings)

    assert report.failed
    assert _by_name(report, "tmux").level is Level.FAIL  # type: ignore[attr-defined]


def test_warnings_alone_do_not_fail(tmp_path: Path) -> None:
    """Otherwise every un-bootstrapped host is 'broken' and the signal is worthless."""
    report = run_checks(_settings(tmp_path))
    assert any(c.level is Level.WARN for c in report.checks)
    assert not report.failed


def test_an_over_long_socket_path_fails_with_the_platform_limit(tmp_path: Path) -> None:
    """A too-long path is otherwise a generic connect failure, which is why
    `SocketPathTooLong` exists as its own class."""
    settings = _settings(tmp_path, SHELLBOX_TMUX_SOCKET="/tmp/" + ("s" * 300) + ".sock")
    report = run_checks(settings)

    check = _by_name(report, "tmux socket path")
    assert check.level is Level.FAIL  # type: ignore[attr-defined]
    assert "sun_path" in check.detail and "SHELLBOX_TMUX_SOCKET" in (check.remedy or "")  # type: ignore[attr-defined]


def test_a_malformed_setting_fails_immediately_and_stops(tmp_path: Path) -> None:
    """No point reporting tmux when the process cannot resolve its own configuration."""
    import os

    from shellbox_mcp.config import ConfigError

    try:
        os.environ["SHELLBOX_HISTORY_LIMIT"] = "not-a-number"
        report = run_checks()
    except ConfigError:  # pragma: no cover - run_checks must not raise
        pytest.fail("run_checks raised instead of reporting")
    finally:
        os.environ.pop("SHELLBOX_HISTORY_LIMIT", None)

    assert report.failed
    assert _by_name(report, "configuration").level is Level.FAIL  # type: ignore[attr-defined]


# --------------------------------------------------- sandbox-conditional reporting
def test_sandbox_claims_are_not_made_on_a_laptop(tmp_path: Path) -> None:
    """🔴 The second bug found by running it.

    On a developer machine there are no `/run/lakebox` symlinks and the credential in
    `~/.databrickscfg` is that developer's own. Reporting it as "the sandbox's baked creator
    PAT that any agent can act as" is false, and false warnings are how a red doctor becomes
    background noise.
    """
    report = run_checks(_settings(tmp_path))

    templated = _by_name(report, "boot-templated files")
    assert templated.level is Level.INFO  # type: ignore[attr-defined]
    assert "not a Lakebox sandbox" in templated.detail  # type: ignore[attr-defined]

    credential = _by_name(report, "workspace credential")
    assert credential.level is Level.INFO  # type: ignore[attr-defined]
    assert "baked creator PAT" not in credential.detail  # type: ignore[attr-defined]

    assert _by_name(report, "sandbox_id").level is Level.INFO  # type: ignore[attr-defined]


def test_on_a_lakebox_every_templated_path_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """All four, because an earlier revision listed three and missed the token cache."""
    _as_lakebox(monkeypatch, tmp_path / "etc-lakebox")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir(exist_ok=True)

    report = run_checks(_settings(tmp_path))
    names = {c.name for c in report.checks}
    for templated in boot_templated.TEMPLATED_PATHS:
        assert templated.path in names, f"{templated.path} was not reported"


def test_a_dangling_token_cache_is_flagged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The measured state of a real sandbox: the link exists, the target does not."""
    _as_lakebox(monkeypatch, tmp_path / "etc-lakebox")
    home = tmp_path / "home"
    (home / ".databricks").mkdir(parents=True)
    (home / ".databricks" / "token-cache.json").symlink_to(tmp_path / "absent.json")
    monkeypatch.setenv("HOME", str(home))

    check = _by_name(run_checks(_settings(tmp_path)), "~/.databricks/token-cache.json")
    assert check.level is Level.WARN  # type: ignore[attr-defined]
    assert "DANGLING" in check.detail  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("contents", "level", "needle"),
    [
        (CFG_PLACEHOLDER, Level.WARN, "credential provisioning never landed"),
        (CFG_PROVISIONED, Level.WARN, "has NOT run since the last boot"),
        ("[DEFAULT]\nhost = https://x\n", Level.OK, "reset has run this boot"),
    ],
)
def test_the_credential_states_are_reported_distinctly_on_a_lakebox(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    contents: str,
    level: Level,
    needle: str,
) -> None:
    """Three states needing three different actions. Conflating placeholder with
    mis-provisioned is a diagnosis this project actually got wrong once."""
    _as_lakebox(monkeypatch, tmp_path / "etc-lakebox")
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    (home / ".databrickscfg").write_text(contents)
    monkeypatch.setenv("HOME", str(home))

    check = _by_name(run_checks(_settings(tmp_path)), "workspace credential")
    assert check.level is level  # type: ignore[attr-defined]
    assert needle in check.detail  # type: ignore[attr-defined]


def test_a_present_pat_is_a_WARN_and_says_not_to_reset_it_yet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """🔴 Deliberately not a FAIL.

    The PAT is a confused-deputy hazard (R6), but resetting it *today* strands the sandbox:
    the OAuth login that replaces it is Phase 3's and does not exist, and the CLI's token
    cache is boot-wiped. So the present PAT is currently the correct state, and the remedy
    has to say so rather than telling an operator to break their sandbox.
    """
    _as_lakebox(monkeypatch, tmp_path / "etc-lakebox")
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    (home / ".databrickscfg").write_text(CFG_PROVISIONED)
    monkeypatch.setenv("HOME", str(home))

    check = _by_name(run_checks(_settings(tmp_path)), "workspace credential")
    assert check.level is Level.WARN, "a present PAT must not fail the health check"  # type: ignore[attr-defined]
    assert "ONLY once an OAuth login exists" in (check.remedy or "")  # type: ignore[attr-defined]


def test_the_credential_shape_is_reported_but_never_the_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """This output is exactly what someone pastes into an issue."""
    _as_lakebox(monkeypatch, tmp_path / "etc-lakebox")
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    secret = "dkeaSUPERSECRETVALUE0123456789abcd"
    (home / ".databrickscfg").write_text(f"[DEFAULT]\nhost = https://x\ntoken = {secret}\n")
    monkeypatch.setenv("HOME", str(home))

    rendered = render(run_checks(_settings(tmp_path)))
    assert secret not in rendered, "doctor printed a credential"
    assert "dkea…" in rendered, "the 4-char prefix is useful and safe; it should be shown"
    assert str(len(secret)) in rendered


# ------------------------------------------------------ §0.6 the override variables
def test_the_config_file_override_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The variable that can make the PAT reset a silent no-op."""
    monkeypatch.setenv(boot_templated.CONFIG_FILE_VAR, "/run/lakebox/databrickscfg")

    check = _by_name(run_checks(_settings(tmp_path)), boot_templated.CONFIG_FILE_VAR)
    assert check.level is Level.WARN  # type: ignore[attr-defined]
    assert "OVERRIDES" in check.detail  # type: ignore[attr-defined]


def test_no_overrides_is_reported_as_ok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(boot_templated.CONFIG_FILE_VAR, raising=False)
    monkeypatch.delenv(boot_templated.TOKEN_CACHE_VAR, raising=False)
    check = _by_name(run_checks(_settings(tmp_path)), "DATABRICKS_*_FILE overrides")
    assert check.level is Level.OK  # type: ignore[attr-defined]


# ------------------------------------------------------------------------ rendering
def test_the_rendered_report_carries_remedies_and_a_count(tmp_path: Path) -> None:
    rendered = render(run_checks(_settings(tmp_path, SHELLBOX_TMUX_BIN="/nonexistent/tmux")))
    assert "FAIL" in rendered
    assert "->" in rendered, "a diagnostic that names a problem must name the fix"
    assert "failed," in rendered and "checks" in rendered


def test_registry_absence_is_informational_not_a_failure(tmp_path: Path) -> None:
    """Running with no inventory is a supported configuration, not a fault."""
    check = _by_name(run_checks(_settings(tmp_path)), "registry")
    assert check.level is Level.INFO  # type: ignore[attr-defined]
    assert "NullRegistry" in check.detail  # type: ignore[attr-defined]


def test_an_unreachable_registry_warns_and_redacts(tmp_path: Path) -> None:
    """Shell tools still work, so this is a WARN — and the DSN must not leak its password.

    ⚠️ The DSN is assembled from parts rather than written as a literal, and that is not
    cosmetic: a complete credential-bearing URL in source trips credential scanners (this
    project's own pre-commit hook blocked exactly that), and the habit is what eventually
    leaks a real one. `tests/integration/harness.py`'s `unreachable_dsn` exists for the same
    reason, as does `shellbox_registry.dsn.dsn_from_env`.

    Port 1 is reserved and never listening, so the connect fails immediately.
    """
    canary = "must-not-appear"  # noqa: S105 - not a credential; a marker for the redaction
    dsn = "postgresql://" + "u" + ":" + canary + "@" + "127.0.0.1:1/shellbox"
    check = _by_name(run_checks(_settings(tmp_path, SHELLBOX_DATABASE_URL=dsn)), "registry")

    assert check.level is Level.WARN  # type: ignore[attr-defined]
    assert canary not in check.detail, "the password reached the report"  # type: ignore[attr-defined]
