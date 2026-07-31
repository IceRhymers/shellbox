"""Boundary validation: session names, cwd, env, dimensions, socket paths (§7.4, §7.5).

Everything here runs BEFORE tmux is invoked. That ordering is the point: several of these
inputs corrupt tmux's own output format in ways that are invisible downstream, so a check
that ran "somewhere in the adapter" would be too late.
"""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Mapping

from shellbox_mcp.errors import BadCwd, InvalidDimensions, InvalidName, SocketPathTooLong

__all__ = [
    "MAX_DIMENSION",
    "SESSION_NAME_RE",
    "max_socket_path_bytes",
    "session_id",
    "sun_path_limit",
    "validate_cwd",
    "validate_dimensions",
    "validate_env",
    "validate_session_name",
    "validate_socket_path",
]

# `:` is tmux's session:window.pane separator and is rejected rather than sanitised: a
# silently rewritten name would differ from the name the agent sees in `shell_list`.
SESSION_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

# Characters that corrupt the `list-sessions -F` TAB record. Measured (spike S11, both
# lanes) with a TAB and an LF in @shellbox_cwd:
#
#   TAB -> line 1 carries 9 fields, so the record is DROPPED, and under orphan
#          reconciliation a dropped record means a LIVE session is marked `orphaned`.
#   LF  -> TWO output lines whose first still has 8 fields with a SILENTLY TRUNCATED cwd,
#          i.e. it passes the field-count check carrying wrong data, plus a spurious
#          second record.
#
# CR is included because tmux ships records line-wise and a bare CR is indistinguishable
# from a line ending to enough consumers to not be worth the argument.
FORBIDDEN_PATH_CHARS = ("\t", "\r", "\n")

# Upper bound for `-x/-y`. tmux itself accepts much larger, but a 100k-column window is a
# memory multiplier on the server every pooled agent shares (§7.3).
MAX_DIMENSION = 10_000

# `sun_path` in `struct sockaddr_un` is a FIXED array, and its size is platform-specific:
# 104 bytes on macOS/BSD, 108 on Linux. The limit INCLUDES the NUL terminator, so the
# longest usable path is one byte shorter -- verified empirically in
# `tests/unit/test_naming.py::test_sun_path_limit_matches_the_platform`, which binds real
# sockets at increasing lengths and asserts the boundary this table claims. That test is
# the reason this is a table and not a guess: it fails loudly on any platform whose real
# limit differs from the entry below.
_SUN_PATH_BYTES: tuple[tuple[str, int], ...] = (
    ("linux", 108),
    ("darwin", 104),
    ("freebsd", 104),
    ("openbsd", 104),
    ("netbsd", 104),
)
# Unknown platforms get the SMALLER of the two known limits. Being conservative fails a
# too-long path early with an actionable message; being permissive produces tmux's
# baffling `error connecting to <sock> (File name too long)` at first use (M13).
_SUN_PATH_FALLBACK = 104


def validate_session_name(name: str) -> str:
    """Return ``name`` unchanged, or raise ``invalid_name``.

    This is where a caller-supplied ``=bui`` dies (§7.1's two-level split): at the adapter
    boundary an anchored-looking name is ``invalid_name``, never ``not_found``, and tmux is
    never invoked with it.
    """
    if not isinstance(name, str) or not SESSION_NAME_RE.match(name):
        raise InvalidName(
            f"session name {name!r} must match {SESSION_NAME_RE.pattern} "
            "(no ':', no leading '=' or '.', 1-64 chars)",
            session=name if isinstance(name, str) else None,
        )
    return name


def validate_cwd(cwd: str, *, session: str | None = None) -> str:
    """Validate and canonicalise a working directory. Raises ``bad_cwd``.

    Checks run in this order deliberately: the character check comes first so a hostile
    path never reaches the filesystem, and it is repeated on the resolved path because a
    symlink can resolve INTO a directory whose name contains a TAB or LF.
    """
    if not isinstance(cwd, str) or not cwd:
        raise BadCwd(f"cwd {cwd!r} must be a non-empty string", session=session)
    _reject_forbidden_chars(cwd, "cwd", session)
    real = os.path.realpath(cwd)
    _reject_forbidden_chars(real, "resolved cwd", session)
    if not os.path.isdir(real):
        raise BadCwd(f"cwd {cwd!r} is not a directory", session=session)
    return real


def _reject_forbidden_chars(value: str, label: str, session: str | None) -> None:
    for char in FORBIDDEN_PATH_CHARS:
        if char in value:
            raise BadCwd(
                f"{label} {value!r} contains {char!r}, which corrupts the "
                "`list-sessions -F` TAB record (spike S11)",
                session=session,
            )


def validate_env(env: Mapping[str, str] | None, *, session: str | None = None) -> list[str]:
    """Turn an env mapping into ``["-e", "K=V", ...]`` arguments.

    Values are NOT restricted: measured (M21, and re-run inside the verbatim §7.2 chain),
    ``-e 'FOO=a<LF>b'`` and ``-e 'BAR=x;y'`` arrive in the pane byte-intact. Keys are
    restricted, because a key containing ``=`` would silently move part of itself into the
    value. NUL is rejected explicitly: it cannot cross an argv boundary at all, and the
    error Python raises for it names neither the variable nor the reason.
    """
    if not env:
        return []
    args: list[str] = []
    for key, value in env.items():
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key or ""):
            raise InvalidName(f"env key {key!r} is not a valid shell identifier", session=session)
        if "\0" in key or "\0" in value:
            raise InvalidName(f"env value for {key!r} contains NUL", session=session)
        args += ["-e", f"{key}={value}"]
    return args


def validate_dimensions(cols: int, rows: int, *, session: str | None = None) -> tuple[int, int]:
    """Validate ``cols``/``rows``. Raises ``invalid_dimensions``."""
    for label, value in (("cols", cols), ("rows", rows)):
        if isinstance(value, bool) or not isinstance(value, int):
            raise InvalidDimensions(f"{label} must be an int, got {value!r}", session=session)
        if not 1 <= value <= MAX_DIMENSION:
            raise InvalidDimensions(
                f"{label}={value} out of range 1..{MAX_DIMENSION}", session=session
            )
    return cols, rows


def sun_path_limit() -> int:
    """The PLATFORM ``sun_path`` size in bytes, INCLUDING the NUL terminator."""
    for prefix, size in _SUN_PATH_BYTES:
        if sys.platform.startswith(prefix):
            return size
    return _SUN_PATH_FALLBACK


def max_socket_path_bytes() -> int:
    """Longest usable socket path in bytes: ``sun_path_limit() - 1`` for the NUL."""
    return sun_path_limit() - 1


def validate_socket_path(path: str) -> str:
    """Validate a tmux socket path against the platform limit. Raises ``tmux_unavailable``.

    Called at startup (and by ``doctor``) rather than lazily, because
    ``SHELLBOX_STATE_DIR`` is user-overridable and a deep override otherwise produces
    ``error connecting to <sock> (File name too long)`` at first use, on every call, with
    nothing pointing at the cause.
    """
    encoded = len(os.fsencode(path))
    limit = max_socket_path_bytes()
    if encoded > limit:
        raise SocketPathTooLong(
            f"tmux socket path is {encoded} bytes, over this platform's limit of {limit} "
            f"(sun_path is {sun_path_limit()} bytes on {sys.platform}, NUL included): "
            f"{path!r}. Set SHELLBOX_TMUX_SOCKET or SHELLBOX_STATE_DIR to a shorter path."
        )
    return path


def session_id(host_id: str, tmux_name: str) -> str:
    """``f"{host_id}:{tmux_name}"`` -- deterministic, so it needs no lookup table (§7.5).

    Stable across MCP restarts and identical from every concurrent instance with no
    coordination. A random UUID would require state, which Principle 1 forbids.
    """
    return f"{host_id}:{tmux_name}"
