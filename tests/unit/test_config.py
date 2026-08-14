"""``Settings`` — §5's table, and the purity that makes the identity split work.

The point of most of this file is what `Settings` **does not** do. It used to derive a
`host_id`, falling back to ``unknown:<machine-id>`` — which is not a weak identity but a
fleet-merging one, since `/etc/machine-id` is baked into the sandbox image and every host would
have shared one `hosts` row. That derivation is deleted, and `identity.py` assigns instead.

Deleted code needs a test more than live code does: nothing else stops it coming back.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from shellbox_mcp.config import DEFAULTS, ConfigError, Settings, log_level_from_env


def test_the_defaults_are_ss_table() -> None:
    settings = Settings.from_env({})
    assert settings.history_limit == DEFAULTS["SHELLBOX_HISTORY_LIMIT"]
    assert settings.default_cols == DEFAULTS["SHELLBOX_DEFAULT_COLS"]
    assert settings.default_rows == DEFAULTS["SHELLBOX_DEFAULT_ROWS"]
    assert settings.max_send_bytes == DEFAULTS["SHELLBOX_MAX_SEND_BYTES"]
    assert settings.max_send_line_bytes == DEFAULTS["SHELLBOX_MAX_SEND_LINE_BYTES"]
    assert settings.log_level == "INFO"


# --------------------------------------------------------------- the deleted derivation
def test_settings_has_no_host_id_at_all() -> None:
    """CRITICAL: `Settings` must not carry a resolved identity, only an override.

    Asserted as the *absence* of the attribute rather than by grepping for `_machine_id`,
    because the failure mode to prevent is someone re-adding a convenient `settings.host_id`
    — at which point every caller silently stops going through `identity.py`, and with it
    through the arbitration that keeps 1-32 concurrent processes on one identity.
    """
    settings = Settings.from_env({})
    assert not hasattr(settings, "host_id"), (
        "Settings grew a host_id again. Identity is assigned by identity.resolve_host_id, "
        "which writes a file under a lock; a Settings attribute cannot do that safely."
    )
    assert settings.host_id_override is None


def test_no_environment_produces_an_unknown_or_machine_derived_id() -> None:
    """With nothing set, the answer is "no override", not a fabricated identity.

    The old code returned ``unknown:<machine-id>`` here and logged a warning, which reads as
    caution but was the actively harmful path: identical on every sandbox from one image.
    """
    for environ in ({}, {"SHELLBOX_HOST_ID": ""}, {"SHELLBOX_STATE_DIR": "/tmp/x"}):
        override = Settings.from_env(environ).host_id_override
        assert override is None, f"{environ} produced an id: {override!r}"


def test_an_explicit_host_id_becomes_an_override(tmp_path: Path) -> None:
    settings = Settings.from_env({"SHELLBOX_HOST_ID": "host-under-test"})
    assert settings.host_id_override == "host-under-test"


def test_resolving_settings_touches_no_filesystem(tmp_path: Path) -> None:
    """`from_env` is a pure function of its mapping — the property the identity split protects.

    `identity.py` writes `host.json` under a lock. Resolving configuration must not, or the
    unit lane starts writing to a real `$HOME` and a frozen dataclass's constructor starts
    hiding a multi-process transaction.
    """
    state = tmp_path / "state"
    settings = Settings.from_env({"SHELLBOX_STATE_DIR": str(state)})
    assert settings.state_dir == str(state)
    assert not state.exists(), "from_env created the state directory; it must stay pure"

    # And the explicit step that IS allowed to write is the one that does.
    settings.ensure_state_dir()
    assert state.is_dir()
    assert oct(state.stat().st_mode & 0o777) == "0o700"


# ------------------------------------------------------------------- malformed values
@pytest.mark.parametrize(
    "key", ["SHELLBOX_HISTORY_LIMIT", "SHELLBOX_DEFAULT_COLS", "SHELLBOX_MAX_SEND_LINE_BYTES"]
)
@pytest.mark.parametrize("value", ["not-a-number", "0", "-1", "1.5"])
def test_a_malformed_integer_refuses_to_start(key: str, value: str) -> None:
    """Raising beats defaulting: a typo'd `SHELLBOX_MAX_SEND_LINE_BYTES` that silently reverts
    to 1000 is a correctness boundary the operator believes they moved (R13/R15)."""
    with pytest.raises(ConfigError, match=key):
        Settings.from_env({key: value})


# --------------------------------------------------------------- bounded integer keys
@pytest.mark.parametrize("key", ["SHELLBOX_IDLE_TIMEOUT_SECONDS", "SHELLBOX_REAP_INTERVAL_SECONDS"])
@pytest.mark.parametrize("value", ["not-a-number", "0", "-1", "1.5"])
def test_a_malformed_bounded_integer_refuses_to_start(key: str, value: str) -> None:
    """The two reaper-config keys raise, not revert to a default, on a malformed value --
    same house style as the plain integer keys above."""
    with pytest.raises(ConfigError, match=key):
        Settings.from_env({key: value})


@pytest.mark.parametrize(
    "key, out_of_range",
    [
        ("SHELLBOX_IDLE_TIMEOUT_SECONDS", "59"),
        ("SHELLBOX_IDLE_TIMEOUT_SECONDS", "86401"),
        ("SHELLBOX_REAP_INTERVAL_SECONDS", "9"),
        ("SHELLBOX_REAP_INTERVAL_SECONDS", "3601"),
    ],
)
def test_an_out_of_range_bounded_integer_refuses_to_start(key: str, out_of_range: str) -> None:
    """Out-of-range is a `ConfigError`, never a clamp -- a clamped value is a correctness
    boundary the operator believes they moved, same as a malformed one."""
    with pytest.raises(ConfigError, match=key):
        Settings.from_env({key: out_of_range})


def test_the_reaper_config_defaults_are_ss_table() -> None:
    settings = Settings.from_env({})
    assert settings.idle_timeout_seconds == DEFAULTS["SHELLBOX_IDLE_TIMEOUT_SECONDS"]
    assert settings.reap_interval_seconds == DEFAULTS["SHELLBOX_REAP_INTERVAL_SECONDS"]


def test_an_unrecognised_log_level_warns_and_uses_info() -> None:
    """The one setting that must NOT refuse to start: stderr is the only diagnostic channel, so
    starting at INFO beats an opaque handshake failure."""
    assert log_level_from_env({"SHELLBOX_LOG_LEVEL": "LOUD"}) == "INFO"
    assert log_level_from_env({"SHELLBOX_LOG_LEVEL": "debug"}) == "DEBUG"
    assert log_level_from_env({}) == "INFO"
