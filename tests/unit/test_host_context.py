"""``resolve_host_context`` — the seam that finally makes `identity.py` reachable.

Until this landed, `identity.py` was tested in isolation while the running server still used
`config.py`'s `unknown:<machine-id>` fallback. So these tests are about the *wiring*, which is
the part that was missing rather than the part that was wrong.
"""

from __future__ import annotations

import json
from pathlib import Path

from shellbox_mcp.config import Settings
from shellbox_mcp.identity import HOST_JSON_NAME, KIND_UNKNOWN, resolve_owner_email
from shellbox_mcp.server import resolve_host_context


def _settings(tmp_path: Path, **environ: str) -> Settings:
    return Settings.from_env({"SHELLBOX_STATE_DIR": str(tmp_path), **environ})


def test_a_host_with_no_configuration_gets_a_real_assigned_identity(tmp_path: Path) -> None:
    """The case every real sandbox is in on first boot, and the one that used to be broken."""
    context = resolve_host_context(_settings(tmp_path))

    assert context.host_id
    assert not context.host_id.startswith(("unknown:", "lakebox:")), (
        "the fleet-merging derivation is reachable again from the server path"
    )
    assert ":" not in context.host_id
    # Persisted, so the next process in this boot agrees rather than minting its own.
    assert json.loads((tmp_path / HOST_JSON_NAME).read_text())["host_id"] == context.host_id


def test_two_resolutions_in_one_process_agree(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    assert resolve_host_context(settings).host_id == resolve_host_context(settings).host_id


def test_an_explicit_override_is_honoured_and_not_persisted(tmp_path: Path) -> None:
    """Integration tests and non-Lakebox hosts rely on this; persisting it would make a
    one-off run permanent."""
    context = resolve_host_context(_settings(tmp_path, SHELLBOX_HOST_ID="itest-host"))
    assert context.host_id == "itest-host"
    assert not (tmp_path / HOST_JSON_NAME).exists()


def test_kind_is_populated_because_the_column_is_not_null(tmp_path: Path) -> None:
    """`hosts.kind` is NOT NULL and this is the only thing that fills it. On a developer laptop
    "unknown" is the honest answer, and unlike the old `unknown:<machine-id>` host id it is a
    label that collides with nothing."""
    context = resolve_host_context(_settings(tmp_path))
    assert context.kind in {"lakebox", KIND_UNKNOWN}


# --------------------------------------------------------------------------- owner_email
def test_the_owner_comes_from_the_environment_when_set(tmp_path: Path) -> None:
    context = resolve_host_context(_settings(tmp_path, SHELLBOX_OWNER_EMAIL="me@example.com"))
    assert context.owner_email == "me@example.com"


def test_the_owner_is_deferred_when_nothing_can_supply_it(tmp_path: Path) -> None:
    """E2d. `None` rather than a placeholder, so `project` can refuse to write a fake principal
    into the column #7's ACL will filter on."""
    assert resolve_host_context(_settings(tmp_path)).owner_email is None


def test_an_environment_owner_is_an_override_and_is_NOT_cached(tmp_path: Path) -> None:
    """E2c is an escape hatch, not a fact about the host, so it does not become permanent.

    Deliberately symmetric with `SHELLBOX_HOST_ID`, which is also honoured and also not
    persisted: an operator debugging one process must not silently change what the sandbox
    claims about itself forever. The value that *is* cached is the one from a credential (E2a),
    because that is evidence rather than an assertion — and `enroll.py` is what supplies it.
    """
    resolve_host_context(_settings(tmp_path, SHELLBOX_OWNER_EMAIL="from-env@example.com"))

    cached = json.loads((tmp_path / HOST_JSON_NAME).read_text())
    assert "owner_email" not in cached, (
        "an environment override was written to the cache; a one-off debug run would then "
        "outlive itself, and E2a could never tell a stale value from a real one"
    )
    # A later process not given the variable is back to deferred — correctly, since nothing
    # authoritative has ever identified this host.
    assert resolve_host_context(_settings(tmp_path)).owner_email is None


def test_a_credential_resolved_owner_IS_cached(tmp_path: Path) -> None:
    """The other half of the asymmetry, at the level `enroll.py` will call.

    E2b's "the cache is the only source after a credential-less restart" depends on this write
    having happened, which is why it lands before the `hosts` row.
    """
    resolve_host_context(_settings(tmp_path))  # establish an identity to merge into
    resolution = resolve_owner_email(
        str(tmp_path), credential_email="creator@example.com", env_email=None
    )
    assert (resolution.owner_email, resolution.source) == ("creator@example.com", "credential")

    cached = json.loads((tmp_path / HOST_JSON_NAME).read_text())
    assert cached["owner_email"] == "creator@example.com"
    # And it now serves a process that has no credential and no environment.
    assert resolve_host_context(_settings(tmp_path)).owner_email == "creator@example.com"


def test_recording_an_owner_never_disturbs_the_identity(tmp_path: Path) -> None:
    host_id = resolve_host_context(_settings(tmp_path)).host_id
    resolve_owner_email(str(tmp_path), credential_email="creator@example.com", env_email=None)
    cached = json.loads((tmp_path / HOST_JSON_NAME).read_text())
    assert cached["host_id"] == host_id, "an owner write re-keyed the host"
    assert cached["owner_email"] == "creator@example.com"
    assert resolve_host_context(_settings(tmp_path)).host_id == host_id
