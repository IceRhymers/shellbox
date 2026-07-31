"""The ShellboxError taxonomy and the tmux-stderr classifier (plan §6, §12 N1).

Two things live here, and they are deliberately separate:

1. **The closed public error set** (§6) that MCP tool payloads carry.
2. **``classify_stderr``** — the N1 table: tmux's stderr text -> an *internal* code. It
   returns the internal ``no_server`` classification, which the tool boundary maps to
   ``tmux_unavailable`` (or, for ``shell_list`` only, to an empty list).

🔴 **Unknown stderr must NEVER map to "empty list."** Only the exact ``no server running``
signature means "there are no sessions"; every other non-zero exit is an error. A
permissive "probably no sessions" fallback would report a broken tmux as a healthy empty
inventory, and orphan reconciliation would then mark every live session on the host
``orphaned`` on the strength of it. ``TmuxAdapter.list_sessions`` is the one place that
inspects the classification, and ``tests/unit/test_errors.py`` asserts the negative case.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

__all__ = [
    "STDERR_SIGNATURES",
    "AlreadyExists",
    "BadCwd",
    "InvalidDimensions",
    "InvalidKey",
    "InvalidName",
    "LineTooLong",
    "NoPayload",
    "NotFound",
    "ShellboxError",
    "SocketPathTooLong",
    "TmuxError",
    "TmuxUnavailable",
    "TooLarge",
    "classify_stderr",
    "public_code",
    "tmux_failure",
]

# The internal classification for "no tmux server on this socket". It is NOT a public
# code: `public_code` maps it to `tmux_unavailable`, and `shell_list` alone treats it as
# an empty inventory (§6, N1).
NO_SERVER = "no_server"


class ShellboxError(Exception):
    """Base class for every error that reaches an MCP tool payload.

    ``code`` is from the closed set in §6; ``session`` is the tmux session name when the
    error is about a specific session, so the payload can name it.
    """

    code = "tmux_error"

    def __init__(
        self, message: str, *, session: str | None = None, internal_code: str | None = None
    ) -> None:
        super().__init__(message)
        self.message = message
        self.session = session
        # The N1 classification behind this error, when it came from tmux stderr. It is NOT
        # part of the payload: it exists so a caller can act on `no_server` (which surfaces
        # publicly as `not_found`) -- specifically, so §9.2's E5 reconciliation can be
        # triggered by a tool call that failed for that reason.
        self.internal_code = internal_code or self.code

    def payload(self) -> dict[str, str | None]:
        return {"code": self.code, "message": self.message, "session": self.session}


class InvalidName(ShellboxError):
    """Session name fails ``naming.py`` -- includes a caller passing ``=bui``."""

    code = "invalid_name"


class NotFound(ShellboxError):
    """Session absent, OR present with an EMPTY incarnation (§9.1).

    The second case is not defensive padding: an empty ``@shellbox_incarnation`` means
    either a create is in flight or the session is foreign, and both are "cannot confirm
    identity". Acting on it would let a send land in a session shellbox does not own.
    """

    code = "not_found"


class AlreadyExists(ShellboxError):
    """Name taken with a CONFLICTING cwd (a matching cwd is an idempotent re-create)."""

    code = "already_exists"


class BadCwd(ShellboxError):
    """Not a directory, or contains TAB/CR/LF (§7.4, spike S11)."""

    code = "bad_cwd"


class NoPayload(ShellboxError):
    code = "no_payload"


class InvalidKey(ShellboxError):
    code = "invalid_key"


class TooLarge(ShellboxError):
    """Total-bytes guard -- a tmux-server memory guard, NOT a delivery guarantee."""

    code = "too_large"


class LineTooLong(ShellboxError):
    """Bytes since the last newline exceeded the limit (§8, H4). The real boundary."""

    code = "line_too_long"


class InvalidDimensions(ShellboxError):
    code = "invalid_dimensions"


class TmuxUnavailable(ShellboxError):
    """No usable tmux server: the only public code for that condition (§6)."""

    code = "tmux_unavailable"


class SocketPathTooLong(TmuxUnavailable):
    """The socket path exceeds the PLATFORM ``sun_path`` limit (§7, M13).

    A subclass rather than a code of its own: tmux can never be reached over this socket,
    which is exactly what ``tmux_unavailable`` means. The distinct class exists so
    ``doctor`` can report the actionable cause instead of a generic connect failure.
    """


class TmuxError(ShellboxError):
    """Anything else, including unknown stderr."""

    code = "tmux_error"


# The N1 table, in match order. Each signature is the set of substrings that must ALL be
# present, because tmux appends context (`no server running on /tmp/sbx…`) and because one
# family of messages needs two parts to be told apart -- see the socket entries below.
#
# Every signature here was observed from a real tmux (3.6b local / 3.4 container) by
# `spike/tmux_spike.py::check_stderr_signatures`, which is what keeps this table honest: a
# mapping table transcribed from prose is a guess, and a wrong guess here degrades
# `not_found` into `tmux_error` (or worse, per the module docstring, into "empty list").
STDERR_SIGNATURES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("server exited unexpectedly",), "tmux_error"),
    (("can't find session:",), "not_found"),
    (("can't find pane:",), "not_found"),
    (("can't find window:",), "not_found"),
    (("no such session:",), "not_found"),
    (("duplicate session:",), "already_exists"),
    (("no server running",), NO_SERVER),
    # ⚠️ NOT in the plan's N1 table -- the cold-start case, which nothing had run. Measured
    # in both lanes: when the socket FILE does not exist (no tmux server has ever started on
    # this host), every verb fails with `error connecting to <path> (No such file or
    # directory)`, NOT with `no server running`. Without this entry the very first
    # `shell_list` on a fresh host is a `tmux_error` instead of an empty inventory.
    #
    # It is a second EXACT signature, not a widening of the rule: the next entry is the
    # reason both parts are required.
    (("error connecting to", "No such file or directory"), NO_SERVER),
    # A too-long socket path produces the SAME `error connecting to` prefix with a different
    # cause, and it is a misconfiguration rather than an empty host. Matching the prefix
    # alone would classify it as `no_server` -- i.e. report a broken configuration as a
    # healthy empty inventory, which is exactly the failure this module exists to prevent.
    (("error connecting to", "File name too long"), "tmux_error"),
    (("open terminal failed",), "tmux_error"),
)

# ⚠️ A residual ambiguity that CANNOT be resolved here, recorded so it is not mistaken for
# solved: a cold start and a WRONG socket path both produce `No such file or directory`, so
# `no_server` cannot distinguish "this host has no sessions" from "this process is looking in
# the wrong place". That is why orphaning authority is guarded elsewhere (§9.2): a process may
# only mark sessions `orphaned` when its own resolved socket path matches the one recorded for
# the host. Classification is not the place for that check, and a debounce does not help --
# the condition is persistent, not transient.

# The `server exited unexpectedly` case gets a CRITICAL log, not just a code: per spike
# F1/F9 it means a GLOBAL `window-size manual` is set on this server, which kills the
# server on the NEXT `new-session` and takes every other agent's sessions with it. It
# should be unreachable -- shellbox sets that option nowhere, at any scope -- so if it is
# observed, a global-scope option is back in a create path.
_CRITICAL_SIGNATURE = "server exited unexpectedly"

_CODE_TO_EXC: dict[str, type[ShellboxError]] = {
    "not_found": NotFound,
    "already_exists": AlreadyExists,
    "tmux_error": TmuxError,
    "tmux_unavailable": TmuxUnavailable,
}


def classify_stderr(stderr: str) -> str:
    """Map tmux stderr to an internal code per the N1 table.

    Returns ``no_server`` only for the two measured signatures that mean it; ``tmux_error``
    for unrecognised stderr. Never returns anything a caller could read as "success".
    """
    text = stderr.strip()
    for signature, code in STDERR_SIGNATURES:
        if all(part in text for part in signature):
            if _CRITICAL_SIGNATURE in signature:
                logger.critical(
                    "tmux reported %r: a GLOBAL `window-size manual` is set on this "
                    "server (spike F1/F9). Every session on it is lost. shellbox must "
                    "never set that option at global scope; check the create path.",
                    _CRITICAL_SIGNATURE,
                )
            return code
    return "tmux_error"


def public_code(internal_code: str) -> str:
    """Map an internal classification to the closed public set (§6).

    ``no_server`` is internal only; at the tool boundary it is ``tmux_unavailable``.
    """
    return "tmux_unavailable" if internal_code == NO_SERVER else internal_code


def tmux_failure(
    stderr: str,
    *,
    session: str | None = None,
    context: str = "",
    no_server_as: str = "not_found",
) -> ShellboxError:
    """Build the exception for a failed tmux invocation, classified per N1.

    ``no_server_as`` defaults to ``not_found`` because that is what N1 says literally:
    ``no server running`` means an empty list for ``shell_list`` and **not_found elsewhere**.
    A session on a server that is not running does not exist, and telling the agent
    ``tmux_unavailable`` for it would describe the infrastructure instead of answering the
    question it asked. The classification is still carried on ``internal_code`` so E5 can be
    triggered by it.
    """
    internal = classify_stderr(stderr)
    code = no_server_as if internal == NO_SERVER else internal
    exc_type = _CODE_TO_EXC.get(code, TmuxError)
    detail = stderr.strip() or "tmux failed with no stderr"
    message = f"{context}: {detail}" if context else detail
    return exc_type(message, session=session, internal_code=internal)
