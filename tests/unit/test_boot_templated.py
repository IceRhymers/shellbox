"""`boot_templated.py` — writing to files the platform re-templates at every boot (W8).

Every test builds a **fixture tree that mimics the sandbox's real layout**: a `run/`
directory standing in for `/run/lakebox` and a `home/` directory whose files are symlinks
into it. That shape is the whole point — the failure this module exists to prevent is a
write that *follows* the symlink, lands in `/run`, appears to work, and vanishes at the
next boot. A test against a plain regular file could not see it.
"""

from __future__ import annotations

import stat
import tomllib
from dataclasses import dataclass
from pathlib import Path

import pytest
from shellbox_mcp import boot_templated
from shellbox_mcp.boot_templated import (
    PathState,
    ResetIncomplete,
    cfg_carries_a_token,
    codex_mcp_registration,
    credential_less_cfg,
    describe_cfg,
    inspect_path,
    replace_templated,
    reset_pat,
)

# The real placeholder the boot script writes, byte for byte.
CFG_PLACEHOLDER = "; placeholder — populated when credentials are provisioned\n"

# A provisioned config, in the shape measured on a live sandbox.
CFG_PROVISIONED = """[DEFAULT]
host = https://fevm-tanner-west.cloud.databricks.com
token = dkeaFAKEfakeFAKEfakeFAKEfakeFAKEfake
"""

# The Codex template, carrying the keys that make Codex able to reach its model at all.
CODEX_TEMPLATE = """# placeholder — populated when credentials are provisioned
model_provider = "databricks"

[model_providers.databricks]
name = "Databricks"
base_url = "https://example.cloud.databricks.com/serving-endpoints"
"""


@dataclass
class SandboxTree:
    """A `$HOME` whose files are symlinks into a `/run`-like directory."""

    home: Path
    run: Path

    def templated(self, name: str, contents: str, *, link_at: str | None = None) -> Path:
        """Create `run/<name>` and a symlink to it, exactly as the boot script does."""
        target = self.run / name
        target.write_text(contents)
        target.chmod(0o600)
        link = self.home / (link_at or name)
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(target)
        return link

    def dangling(self, name: str, link_at: str) -> Path:
        """A symlink whose target does NOT exist — the token cache's measured state."""
        link = self.home / link_at
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(self.run / name)
        return link


@pytest.fixture
def tree(tmp_path: Path) -> SandboxTree:
    home = tmp_path / "home"
    run = tmp_path / "run"
    home.mkdir()
    run.mkdir()
    return SandboxTree(home=home, run=run)


# ------------------------------------------------------------------------- inspection
def test_a_boot_templated_symlink_is_recognised(tree: SandboxTree) -> None:
    link = tree.templated("databrickscfg", CFG_PROVISIONED)
    found = inspect_path(link)
    assert found.state is PathState.SYMLINK
    assert found.target == str(tree.run / "databrickscfg")
    assert found.is_boot_templated


def test_a_dangling_symlink_is_distinguished_from_a_working_one(tree: SandboxTree) -> None:
    """CRITICAL: Not a nicety — this is the measured state of `~/.databricks/token-cache.json`.

    The link exists and its target does not, so the OAuth cache is not merely emptied at
    boot: it is absent. Code that only asked "is this a symlink?" would report it healthy,
    and `os.path.exists` (which follows) would report it missing entirely.
    """
    link = tree.dangling("token-cache.json", ".databricks/token-cache.json")
    found = inspect_path(link)
    assert found.state is PathState.DANGLING
    assert found.is_boot_templated
    assert link.is_symlink() and not link.exists()


def test_a_regular_file_and_an_absent_path_are_not_templated(tree: SandboxTree) -> None:
    regular = tree.home / "plain"
    regular.write_text("x")
    assert inspect_path(regular).state is PathState.REGULAR
    assert not inspect_path(regular).is_boot_templated
    assert inspect_path(tree.home / "nope").state is PathState.ABSENT


# --------------------------------------------------------- the contract: never follow
def test_the_symlink_is_replaced_and_its_target_left_byte_unchanged(tree: SandboxTree) -> None:
    """CRITICAL: The property the whole module exists for.

    Writing *through* the link would put the new content in `/run` — which is wiped between
    boots — and leave the caller believing it succeeded. The proof is not "the file now has
    the right content" (true either way) but "the former target is byte-identical".
    """
    link = tree.templated("databrickscfg", CFG_PROVISIONED)
    target = tree.run / "databrickscfg"
    before = target.read_bytes()

    result = replace_templated(link, credential_less_cfg)

    assert result.unlinked_symlink and result.target_preserved
    assert target.read_bytes() == before, "the write followed the symlink into /run"
    assert not link.is_symlink(), "the path is still a symlink"
    assert link.is_file()
    assert "token" not in link.read_text()


def test_the_replacement_is_0600_and_never_briefly_wider(tree: SandboxTree) -> None:
    """It holds a workspace credential's neighbourhood; the mode is set at `open()`."""
    link = tree.templated("databrickscfg", CFG_PROVISIONED)
    replace_templated(link, credential_less_cfg)
    assert stat.S_IMODE(link.stat().st_mode) == 0o600


def test_a_pre_existing_regular_file_is_handled(tree: SandboxTree) -> None:
    """The second boot-time run, and the non-sandbox case, both land here."""
    path = tree.home / ".databrickscfg"
    path.write_text(CFG_PROVISIONED)

    result = replace_templated(path, credential_less_cfg)

    assert result.before is PathState.REGULAR
    assert result.changed and not result.unlinked_symlink
    assert not cfg_carries_a_token(path.read_text())


def test_running_twice_is_a_no_op(tree: SandboxTree) -> None:
    """Idempotence within a boot. `changed` is False the second time — not merely
    "the content happens to match", but a reported fact a caller can log."""
    link = tree.templated("databrickscfg", CFG_PROVISIONED)
    first = replace_templated(link, credential_less_cfg)
    contents = link.read_text()
    second = replace_templated(link, credential_less_cfg)

    assert first.changed and not second.changed
    assert link.read_text() == contents


def test_a_replaced_file_whose_content_drifted_is_corrected(tree: SandboxTree) -> None:
    """ "Already a regular file" must not be mistaken for "already correct" — otherwise a
    credential written back by hand would survive a reset that reported success."""
    path = tree.home / ".databrickscfg"
    path.write_text(CFG_PROVISIONED)
    replace_templated(path, credential_less_cfg)
    path.write_text(CFG_PROVISIONED)  # someone put the credential back

    assert replace_templated(path, credential_less_cfg).changed
    assert not cfg_carries_a_token(path.read_text())


# --------------------------------------------------------------- merge mode 1: the cfg
def test_the_reset_keeps_the_host_and_drops_the_credential(tree: SandboxTree) -> None:
    """Wholesale replacement would lose `host`, and an operator inside a sandbox has no
    obvious way to recover it — every later CLI call becomes "which workspace?"."""
    link = tree.templated("databrickscfg", CFG_PROVISIONED)
    replace_templated(link, credential_less_cfg)
    body = link.read_text()

    assert "host = https://fevm-tanner-west.cloud.databricks.com" in body
    assert "[DEFAULT]" in body
    assert not cfg_carries_a_token(body)
    assert body.count("[DEFAULT]") == 1, "exactly one profile"


def test_the_reset_says_in_the_file_that_it_is_per_boot(tree: SandboxTree) -> None:
    """The next person to read this file is debugging why the PAT came back."""
    link = tree.templated("databrickscfg", CFG_PROVISIONED)
    replace_templated(link, credential_less_cfg)
    assert "PER-BOOT" in link.read_text().upper()


# ------------------------------------------------------------- merge mode 2: the Codex
def test_codex_registration_preserves_the_harness_model_config(tree: SandboxTree) -> None:
    """CRITICAL: The case a wholesale writer would have shipped broken.

    `model_provider` and `model_providers` are how Codex reaches its model. An earlier
    version of ADR-7 specified "unlink and write a regular file in its place", which is
    right for the credential file and would have **discarded Codex's ability to run** — and
    the criteria only tested the replace case, so it would have shipped untested.
    """
    link = tree.templated("codex-config.toml", CODEX_TEMPLATE, link_at=".codex/config.toml")

    replace_templated(link, codex_mcp_registration())

    parsed = tomllib.loads(link.read_text())
    assert parsed["model_provider"] == "databricks", "the harness's model provider was lost"
    assert parsed["model_providers"]["databricks"]["base_url"].endswith("serving-endpoints")
    assert parsed["mcp_servers"]["shellbox"] == {"command": "shellbox-mcp", "args": []}


def test_codex_registration_preserves_comments_byte_for_byte(tree: SandboxTree) -> None:
    """Appending rather than round-tripping through a parser. The template is *mostly*
    comments — a rewrite would silently strip the one line explaining what the file is."""
    link = tree.templated("codex-config.toml", CODEX_TEMPLATE, link_at=".codex/config.toml")
    replace_templated(link, codex_mcp_registration())
    assert link.read_text().startswith(CODEX_TEMPLATE)


def test_codex_registration_is_idempotent(tree: SandboxTree) -> None:
    """Appending twice would produce a duplicate table and make the file unparseable."""
    link = tree.templated("codex-config.toml", CODEX_TEMPLATE, link_at=".codex/config.toml")
    replace_templated(link, codex_mcp_registration())
    once = link.read_text()
    replace_templated(link, codex_mcp_registration())

    assert link.read_text() == once
    tomllib.loads(link.read_text())  # still parses: no duplicate [mcp_servers.shellbox]


def test_codex_registration_into_an_empty_placeholder(tree: SandboxTree) -> None:
    """The measured starting state of a sandbox that has never been provisioned."""
    link = tree.templated("codex-config.toml", "# placeholder\n", link_at=".codex/config.toml")
    replace_templated(link, codex_mcp_registration())
    assert tomllib.loads(link.read_text())["mcp_servers"]["shellbox"]["command"] == "shellbox-mcp"


def test_an_unparseable_codex_config_is_not_made_worse(tree: SandboxTree) -> None:
    """A file we cannot parse still must not gain a duplicate section."""
    broken = "this is not [ valid toml\n[mcp_servers.shellbox]\ncommand = 'x'\n"
    link = tree.templated("codex-config.toml", broken, link_at=".codex/config.toml")
    replace_templated(link, codex_mcp_registration())
    assert link.read_text().count("[mcp_servers.shellbox]") == 1


# ------------------------------------------------- the cfg's three states (W8 / doctor)
@pytest.mark.parametrize(
    ("contents", "expected"),
    [
        (CFG_PLACEHOLDER, "placeholder"),
        (CFG_PROVISIONED, "credentialed"),
        ("[DEFAULT]\nhost = https://x\n", "reset"),
        (None, "absent"),
    ],
)
def test_the_config_has_three_meaningful_states_not_two(
    contents: str | None, expected: str
) -> None:
    """Each needs a different action, and conflating the first two is a real mistake that
    was made: a comment-only stub was diagnosed as "mis-provisioned" when it is the boot
    script's own `write_placeholder` output."""
    state, explanation = describe_cfg(contents)
    assert state == expected
    assert explanation


def test_the_placeholder_diagnosis_names_the_actual_cause() -> None:
    _, explanation = describe_cfg(CFG_PLACEHOLDER)
    assert "credential provisioning never landed" in explanation
    assert "Restart the sandbox" in explanation or "restart" in explanation.lower()


# --------------------------------------------------- §0.6: the override that hides a reset
def test_reset_pat_also_resets_the_overridden_path(tree: SandboxTree, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """CRITICAL: The measured hazard: `DATABRICKS_CONFIG_FILE` overrides the `~/` path.

    PID 1 and `ttyd` both export it in a Lakebox, pointing at `/run/lakebox/databrickscfg`.
    Where an agent inherits it, resetting only `~/.databrickscfg` leaves the baked PAT in
    use — and reports success. So the reset handles the variable **unconditionally**,
    because whether `login` scrubs it is a util-linux detail an image bump could change.
    """
    home_cfg = tree.home / ".databrickscfg"
    home_cfg.write_text(CFG_PROVISIONED)
    override = tree.run / "databrickscfg"
    override.write_text(CFG_PROVISIONED)

    monkeypatch.setenv("HOME", str(tree.home))
    monkeypatch.setenv(boot_templated.CONFIG_FILE_VAR, str(override))

    outcome = reset_pat()

    assert override in outcome.paths_checked, "the overridden path was not even considered"
    assert not cfg_carries_a_token(override.read_text()), (
        "the override still holds the baked PAT, so the reset was a no-op where it counted"
    )
    assert not cfg_carries_a_token(home_cfg.read_text())


def test_reset_pat_refuses_to_report_success_while_a_credential_survives(
    tree: SandboxTree, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    """CRITICAL: The criterion, stated as its own test: it must NEVER report success while the
    baked PAT is still in use.

    Simulated by making the overridden path un-writable, which is the shape of every real
    way this could fail — a read-only mount, a permissions problem, a path we did not know
    to reset. The outcome must be an exception, not a log line.
    """
    (tree.home / ".databrickscfg").write_text(CFG_PROVISIONED)
    override = tree.run / "readonly-cfg"
    override.write_text(CFG_PROVISIONED)
    override.chmod(0o400)
    tree.run.chmod(0o500)  # cannot unlink or recreate within the directory

    monkeypatch.setenv("HOME", str(tree.home))
    monkeypatch.setenv(boot_templated.CONFIG_FILE_VAR, str(override))

    try:
        with pytest.raises(ResetIncomplete, match="still present"):
            reset_pat()
    finally:
        tree.run.chmod(0o700)
        override.chmod(0o600)


def test_reset_pat_is_clean_when_only_the_home_config_exists(
    tree: SandboxTree, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    """The ordinary case: no override set, one file, credential gone."""
    home_cfg = tree.home / ".databrickscfg"
    home_cfg.write_text(CFG_PROVISIONED)
    monkeypatch.setenv("HOME", str(tree.home))
    monkeypatch.delenv(boot_templated.CONFIG_FILE_VAR, raising=False)

    outcome = reset_pat()

    assert outcome.changed
    assert not outcome.overrides
    assert not cfg_carries_a_token(home_cfg.read_text())


def test_config_file_overrides_reports_both_variables() -> None:
    env = {
        boot_templated.CONFIG_FILE_VAR: "/run/lakebox/databrickscfg",
        boot_templated.TOKEN_CACHE_VAR: "/run/lakebox/token-cache.json",
        "UNRELATED": "x",
    }
    assert boot_templated.config_file_overrides(env) == {
        boot_templated.CONFIG_FILE_VAR: "/run/lakebox/databrickscfg",
        boot_templated.TOKEN_CACHE_VAR: "/run/lakebox/token-cache.json",
    }
    assert boot_templated.config_file_overrides({}) == {}


def test_the_templated_set_matches_the_boot_script() -> None:
    """Four, not three. The fourth is the OAuth token cache, whose boot-wipe is the finding
    that lands on Phase 3 — an earlier revision listed three and missed it."""
    paths = {t.path for t in boot_templated.TEMPLATED_PATHS}
    assert paths == {
        "~/.databrickscfg",
        "~/.codex/config.toml",
        "~/.claude/settings.json",
        "~/.databricks/token-cache.json",
    }
    assert all(t.target.startswith("/run/lakebox/") for t in boot_templated.TEMPLATED_PATHS)
    assert "~/.claude.json" not in paths, (
        "~/.claude.json is a REAL file in persistent $HOME holding the harness's own state; "
        "routing it through a symlink-aware writer would destroy `projects` and `mcpServers`"
    )


def test_a_write_that_follows_the_symlink_is_detected(tree: SandboxTree) -> None:
    """The guard on the guard: if `replace_templated` ever regressed to writing through the
    link, `target_preserved` must go False rather than the caller believing it worked."""
    link = tree.templated("databrickscfg", CFG_PROVISIONED)
    target = tree.run / "databrickscfg"

    def merge_that_also_scribbles_on_the_target(prior: str | None) -> str:
        target.write_text("scribbled")  # simulates a write that followed the link
        return credential_less_cfg(prior)

    result = replace_templated(link, merge_that_also_scribbles_on_the_target)
    assert not result.target_preserved
