"""Environment-driven settings -- the ONE place §5's table is read (plan §5, §4).

Two properties this module exists to preserve:

* **Configuration is env-only.** ``buzz-acp`` spawns MCP servers with ``args: vec![]``
  (§4, #6), so any setting that can only be expressed as a CLI flag can never be
  configured in that harness. Flags are conveniences; the environment is the interface.
* **The adapter stays a pure function of its configuration.** ``TmuxAdapter`` takes a
  ``TmuxConfig`` and never reads ``os.environ``, so its tests never mutate the
  environment. That split only works if exactly one module does the reading -- this one.

WARNING: **This module does not resolve ``host_id``, and must not start.** It reads
``SHELLBOX_HOST_ID`` as an *override* and stops there; the identity itself is assigned and
cached by ``identity.py``, called explicitly by ``server.py``. Three reasons that split is
load-bearing rather than tidiness:

* Assigning an identity **writes a file** under ``$HOME`` (and takes a lock to do it). This
  module is a pure function of an injected ``Mapping``, unit-tested with synthetic
  environments; a write behind it would make those tests touch a real home directory.
* The assignment is arbitrated across 1-32 concurrent processes. Putting that inside a frozen
  dataclass's constructor hides a multi-process transaction behind an attribute access.
* ``ensure_state_dir()`` is called *after* configuration resolves, so resolving identity here
  would need the state directory before anything had created it.

An earlier version derived ``unknown:<machine-id>`` here as a last resort. That was not a weak
identity but a **fleet-merging** one: ``/etc/machine-id`` is baked into the sandbox image, so
every host would have shared one ``hosts`` row, each overwriting the others' ``owner_email``.
It is deleted rather than deprecated -- see ``docs/sandbox-environment.md`` §2.
"""

from __future__ import annotations

import logging
import os
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from shellbox_registry.dsn import dsn_from_env

from shellbox_mcp.tmux import TmuxConfig

logger = logging.getLogger(__name__)

__all__ = ["DEFAULTS", "ConfigError", "LogLevel", "Settings"]

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

# §5's table, as data. Kept together so a default can be checked against the plan in one
# place rather than being spread over a constructor's keyword arguments.
DEFAULTS = {
    "SHELLBOX_HISTORY_LIMIT": 20_000,
    "SHELLBOX_DEFAULT_COLS": 80,
    "SHELLBOX_DEFAULT_ROWS": 24,
    "SHELLBOX_MAX_SEND_BYTES": 1 << 20,
    "SHELLBOX_MAX_SEND_LINE_BYTES": 1000,
    "SHELLBOX_IDLE_TIMEOUT_SECONDS": 1800,
    "SHELLBOX_REAP_INTERVAL_SECONDS": 60,
}

_LOG_LEVELS: frozenset[str] = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})


class ConfigError(Exception):
    """A malformed environment variable.

    NOT a ``ShellboxError``: this cannot reach a tool payload, because a process that
    cannot resolve its own configuration must fail at startup instead of answering every
    tool call with the same error. ``cli.py`` turns this into a stderr message and a
    non-zero exit.
    """


@dataclass(frozen=True)
class Settings:
    """Resolved configuration for one process. Immutable, and never re-read per call."""

    tmux_bin: str
    socket_path: str
    state_dir: str
    history_limit: int
    default_cols: int
    default_rows: int
    max_send_bytes: int
    max_send_line_bytes: int
    idle_timeout_seconds: int
    reap_interval_seconds: int
    log_level: LogLevel
    database_dsn: str | None = None
    owner_email: str | None = None
    host_id_override: str | None = None
    """``$SHELLBOX_HOST_ID``: an override, NOT a resolved identity.

    ``None`` is the normal case and means "assign or read one from the cache" -- which
    ``identity.resolve_host_id`` does. Named ``_override`` so no caller can mistake it for the
    host's identity and skip that call."""

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Settings:
        """Resolve §5's table. Raises ``ConfigError`` on a malformed value.

        Raising rather than falling back to the default is deliberate: a typo'd
        ``SHELLBOX_MAX_SEND_LINE_BYTES`` that silently reverts to 1000 is a correctness
        boundary the operator believes they moved.
        """
        env = os.environ if environ is None else environ
        state_dir = env.get("SHELLBOX_STATE_DIR") or str(Path.home() / ".shellbox")
        return cls(
            # `which` at resolution time, not at call time: the one resolution point for
            # ADR-1, so a later PATH change cannot make two calls in one process disagree
            # about which tmux binary they are talking to. A missing binary is left as the
            # bare name -- `subprocess` then raises, and the tool boundary reports
            # `tmux_unavailable`, which is the honest code for it.
            tmux_bin=env.get("SHELLBOX_TMUX_BIN") or shutil.which("tmux") or "tmux",
            socket_path=env.get("SHELLBOX_TMUX_SOCKET") or str(Path(state_dir) / "tmux.sock"),
            state_dir=state_dir,
            history_limit=_int_env(env, "SHELLBOX_HISTORY_LIMIT"),
            default_cols=_int_env(env, "SHELLBOX_DEFAULT_COLS"),
            default_rows=_int_env(env, "SHELLBOX_DEFAULT_ROWS"),
            max_send_bytes=_int_env(env, "SHELLBOX_MAX_SEND_BYTES"),
            max_send_line_bytes=_int_env(env, "SHELLBOX_MAX_SEND_LINE_BYTES"),
            idle_timeout_seconds=_bounded_int_env(
                env, "SHELLBOX_IDLE_TIMEOUT_SECONDS", minimum=60, maximum=86400
            ),
            reap_interval_seconds=_bounded_int_env(
                env, "SHELLBOX_REAP_INTERVAL_SECONDS", minimum=10, maximum=3600
            ),
            log_level=log_level_from_env(env),
            # `dsn_from_env` reads the process environment itself (it also assembles a DSN
            # from SHELLBOX_PG_* parts), so it is used for the real environment only; an
            # explicit mapping resolves the one variable this module reads directly.
            database_dsn=dsn_from_env() if environ is None else env.get("SHELLBOX_DATABASE_URL"),
            owner_email=env.get("SHELLBOX_OWNER_EMAIL") or None,
            host_id_override=env.get("SHELLBOX_HOST_ID") or None,
        )

    def tmux_config(self, *, timeout: float | None = None) -> TmuxConfig:
        """The adapter's configuration. Constructed per call -- it is frozen and cheap.

        ``timeout`` is ``None`` for every ordinary caller, which leaves ``TmuxConfig.timeout``
        at its own default and every shipped verb byte-identical (`ADR-37`). The reaper
        (`reaper.py`, `W41`) is the one caller that passes a concrete bound, so its own
        subprocess calls cannot wedge a sweep forever.
        """
        return TmuxConfig(
            socket_path=self.socket_path,
            tmux_bin=self.tmux_bin,
            history_limit=self.history_limit,
            default_cols=self.default_cols,
            default_rows=self.default_rows,
            max_send_bytes=self.max_send_bytes,
            max_send_line_bytes=self.max_send_line_bytes,
            timeout=timeout,
        )

    def ensure_state_dir(self) -> None:
        """Create ``state_dir`` (mode 0700) if absent. tmux will not create the socket's
        parent directory itself, so without this the default configuration fails at first
        use with a connect error that names the socket and not the missing directory."""
        Path(self.state_dir).mkdir(mode=0o700, parents=True, exist_ok=True)


def log_level_from_env(env: Mapping[str, str] | None = None) -> LogLevel:
    """Resolve ``SHELLBOX_LOG_LEVEL``, defaulting to ``INFO``.

    Separate from ``Settings.from_env`` because ``cli.py`` needs it *before* it may import
    anything else (stderr logging is configured as the module's first statement), and an
    unrecognised value must not be a startup failure: stderr is the only diagnostic
    channel, and refusing to start is a worse answer than starting at ``INFO``.
    """
    raw = (os.environ if env is None else env).get("SHELLBOX_LOG_LEVEL", "").strip().upper()
    if not raw:
        return "INFO"
    if raw not in _LOG_LEVELS:
        logger.warning("SHELLBOX_LOG_LEVEL=%r is not a known level; using INFO", raw)
        return "INFO"
    # The membership check above is the guarantee; mypy cannot narrow a str to the Literal.
    return raw  # type: ignore[return-value]


def _int_env(env: Mapping[str, str], key: str) -> int:
    raw = env.get(key)
    if raw is None or raw == "":
        return int(DEFAULTS[key])
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{key}={raw!r} is not an integer") from exc
    if value <= 0:
        raise ConfigError(f"{key}={raw!r} must be positive")
    return value


def _bounded_int_env(env: Mapping[str, str], key: str, *, minimum: int, maximum: int) -> int:
    """Like ``_int_env``, but also enforces ``[minimum, maximum]``.

    Guards the environment-resolution path only -- see the module docstring's callers.
    Out-of-range is a ``ConfigError``, never a clamp: a silently-clamped value is the same
    "correctness boundary the operator believes they moved" that ``Settings.from_env``
    refuses to cross for a malformed one.
    """
    value = _int_env(env, key)
    if value < minimum or value > maximum:
        raise ConfigError(f"{key}={value!r} must be between {minimum} and {maximum}")
    return value
