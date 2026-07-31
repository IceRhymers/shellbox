"""Environment-driven settings -- the ONE place §5's table is read (plan §5, §4).

Two properties this module exists to preserve:

* **Configuration is env-only.** ``buzz-acp`` spawns MCP servers with ``args: vec![]``
  (§4, #6), so any setting that can only be expressed as a CLI flag can never be
  configured in that harness. Flags are conveniences; the environment is the interface.
* **The adapter stays a pure function of its configuration.** ``TmuxAdapter`` takes a
  ``TmuxConfig`` and never reads ``os.environ``, so its tests never mutate the
  environment. That split only works if exactly one module does the reading -- this one.

``host_id`` derivation here is deliberately the *degraded* form: §10's steps 2 and 3 (the
``$SHELLBOX_STATE_DIR/host.json`` cache and the ``lakebox:<sandbox_id>`` derivation) belong
to W7's ``identity.py``/``enroll.py``. Until W7 lands, an un-set ``SHELLBOX_HOST_ID`` lands
on §10's step 4 -- ``unknown:<machine-id>``, logged loudly -- which is exactly what §10 says
should happen and is why it is logged rather than silently accepted.
"""

from __future__ import annotations

import logging
import os
import shutil
import socket
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
    host_id: str
    history_limit: int
    default_cols: int
    default_rows: int
    max_send_bytes: int
    max_send_line_bytes: int
    log_level: LogLevel
    database_dsn: str | None = None
    owner_email: str | None = None

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
            host_id=_resolve_host_id(env),
            history_limit=_int_env(env, "SHELLBOX_HISTORY_LIMIT"),
            default_cols=_int_env(env, "SHELLBOX_DEFAULT_COLS"),
            default_rows=_int_env(env, "SHELLBOX_DEFAULT_ROWS"),
            max_send_bytes=_int_env(env, "SHELLBOX_MAX_SEND_BYTES"),
            max_send_line_bytes=_int_env(env, "SHELLBOX_MAX_SEND_LINE_BYTES"),
            log_level=log_level_from_env(env),
            # `dsn_from_env` reads the process environment itself (it also assembles a DSN
            # from SHELLBOX_PG_* parts), so it is used for the real environment only; an
            # explicit mapping resolves the one variable this module reads directly.
            database_dsn=dsn_from_env() if environ is None else env.get("SHELLBOX_DATABASE_URL"),
            owner_email=env.get("SHELLBOX_OWNER_EMAIL") or None,
        )

    def tmux_config(self) -> TmuxConfig:
        """The adapter's configuration. Constructed per call -- it is frozen and cheap."""
        return TmuxConfig(
            socket_path=self.socket_path,
            tmux_bin=self.tmux_bin,
            history_limit=self.history_limit,
            default_cols=self.default_cols,
            default_rows=self.default_rows,
            max_send_bytes=self.max_send_bytes,
            max_send_line_bytes=self.max_send_line_bytes,
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


def _resolve_host_id(env: Mapping[str, str]) -> str:
    """§10's derivation, steps 1 and 4 only. Steps 2 and 3 are W7's.

    Step 4 is logged at WARNING because §10 requires it to be loud: every host landing on
    ``kind="unknown"`` makes the ``hosts`` table useless for the Phase 4 inventory, and the
    plan's own §10 flags that as OQ-A rather than an accepted outcome.
    """
    explicit = env.get("SHELLBOX_HOST_ID")
    if explicit:
        return explicit
    host_id = f"unknown:{_machine_id()}"
    logger.warning(
        "SHELLBOX_HOST_ID is unset and identity resolution (W7) is not wired up yet; "
        "falling back to §10 step 4: %r. Set SHELLBOX_HOST_ID for a stable inventory.",
        host_id,
    )
    return host_id


def _machine_id() -> str:
    """A stable per-host string. Determinism is the requirement, not uniqueness.

    ``/etc/machine-id`` where it exists (Linux, and the sandbox image), the hostname
    otherwise. NOT ``uuid.getnode()``: it fabricates a random node id when no hardware
    address is readable, which would make ``host_id`` change across restarts -- and §10's
    whole point is that it does not.
    """
    try:
        return Path("/etc/machine-id").read_text(encoding="utf-8").strip() or socket.gethostname()
    except OSError:
        return socket.gethostname()
