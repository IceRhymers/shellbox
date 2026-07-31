"""The N1 stderr -> error-code table, and the one mapping that must never exist.

Every ``stderr`` string here was captured from a real tmux (3.6b local, 3.4 in the
container lane) by ``spike/tmux_spike.py::check_stderr_signatures``, including the context
tmux appends. Testing against invented strings would prove only that the table matches
itself.
"""

from __future__ import annotations

import logging

import pytest
from shellbox_mcp.errors import (
    NO_SERVER,
    AlreadyExists,
    NotFound,
    TmuxError,
    TmuxUnavailable,
    classify_stderr,
    public_code,
    tmux_failure,
)
from shellbox_mcp.errors import (
    STDERR_SIGNATURES as SIGNATURES,
)

# (observed stderr, expected internal code, the N1 signature it must match)
OBSERVED = [
    ("can't find session: nosuch", "not_found", ("can't find session:",)),
    ("can't find pane: =build", "not_found", ("can't find pane:",)),
    ("can't find window: bad", "not_found", ("can't find window:",)),
    ("no such session: =nosuch:", "not_found", ("no such session:",)),
    ("duplicate session: build", "already_exists", ("duplicate session:",)),
    ("no server running on /tmp/sbxa1b2c3d4", NO_SERVER, ("no server running",)),
    ("open terminal failed: not a terminal", "tmux_error", ("open terminal failed",)),
    ("server exited unexpectedly", "tmux_error", ("server exited unexpectedly",)),
    # Beyond the plan's N1 table, both measured in both lanes. Same prefix, opposite
    # meanings, which is why each signature is a SET of required substrings: a cold start
    # (no socket file yet) is an empty host, while a too-long path is a misconfiguration
    # that must never be reported as a healthy empty inventory.
    (
        "error connecting to /tmp/sbx (No such file or directory)",
        NO_SERVER,
        ("error connecting to", "No such file or directory"),
    ),
    (
        "error connecting to /tmp/ssss (File name too long)",
        "tmux_error",
        ("error connecting to", "File name too long"),
    ),
]

OBSERVED = [(stderr, code, tuple(sig)) for stderr, code, sig in OBSERVED]


@pytest.mark.parametrize(("stderr", "expected", "signature"), OBSERVED)
def test_n1_table(stderr: str, expected: str, signature: tuple[str, ...]) -> None:
    assert all(part in stderr for part in signature)
    assert classify_stderr(stderr) == expected


def test_every_n1_signature_is_covered_by_a_test() -> None:
    """Adding a signature to ``errors.py`` without a case here fails.

    The table is the kind of code that rots silently: a wrong or missing row degrades
    ``not_found`` into ``tmux_error`` and nothing else notices.
    """
    assert {signature for signature, _ in SIGNATURES} == {sig for _, _, sig in OBSERVED}


@pytest.mark.parametrize(
    "stderr",
    [
        "",
        "   ",
        "some future tmux error nobody has seen",
        "error connecting to /tmp/sbx (File name too long)",
        "lost server",
        "sessions should be nested with care, unset $TMUX to force",
    ],
)
def test_unknown_stderr_is_tmux_error_never_no_server(stderr: str) -> None:
    """🔴 Unknown stderr must NEVER be classified as "no server", i.e. as an empty list.

    ``shell_list`` returns an empty inventory for exactly one classification. If an
    unrecognised failure reached it, a broken tmux would be reported as a healthy empty
    inventory -- and orphan reconciliation would then mark every live session on the host
    ``orphaned`` on the strength of it.
    """
    assert classify_stderr(stderr) == "tmux_error"
    assert classify_stderr(stderr) != NO_SERVER


def test_only_the_exact_no_server_signatures_classify_as_no_server() -> None:
    assert classify_stderr("no server running on /tmp/x") == NO_SERVER
    # Near-misses are not the signature and must not be treated as one.
    for near_miss in ("no server", "server not running", "no servers running"):
        assert classify_stderr(near_miss) == "tmux_error"
    # Half of a two-part signature is not the signature either.
    assert classify_stderr("error connecting to /tmp/x") == "tmux_error"
    assert classify_stderr("No such file or directory") == "tmux_error"


def test_no_server_is_internal_and_surfaces_as_not_found_by_default() -> None:
    """``no_server`` is an internal classification, never a public code (§6).

    Its default public form is ``not_found``, which is N1 read literally: an empty list for
    ``shell_list``, ``not_found`` elsewhere. The internal classification stays on the
    exception so E5 reconciliation can be triggered by it (§9.2).
    """
    assert public_code(NO_SERVER) == "tmux_unavailable"
    assert public_code("not_found") == "not_found"

    default = tmux_failure("no server running on /tmp/x")
    assert isinstance(default, NotFound)
    assert default.internal_code == NO_SERVER

    as_list = tmux_failure("no server running on /tmp/x", no_server_as="tmux_unavailable")
    assert isinstance(as_list, TmuxUnavailable)
    assert as_list.internal_code == NO_SERVER


@pytest.mark.parametrize(
    ("stderr", "exc_type"),
    [
        ("can't find session: build", NotFound),
        ("duplicate session: build", AlreadyExists),
        ("something unknown", TmuxError),
    ],
)
def test_tmux_failure_builds_the_right_exception(stderr: str, exc_type: type) -> None:
    error = tmux_failure(stderr, session="build", context="kill-session failed")
    assert isinstance(error, exc_type)
    assert error.session == "build"
    assert "kill-session failed" in error.message
    assert stderr in error.message


def test_server_exited_unexpectedly_logs_critical(caplog: pytest.LogCaptureFixture) -> None:
    """F1's signature gets a CRITICAL log, because it names a specific, fatal misconfiguration.

    It means a GLOBAL ``window-size manual`` is set on this server, which kills the server
    on the next ``new-session`` and takes every pooled agent's sessions with it. It should be
    unreachable; if it is ever seen, a global-scope option is back in a create path.
    """
    with caplog.at_level(logging.CRITICAL, logger="shellbox_mcp.errors"):
        assert classify_stderr("server exited unexpectedly") == "tmux_error"
    assert any(r.levelno == logging.CRITICAL for r in caplog.records)
    assert "window-size manual" in caplog.text


def test_error_payload_shape() -> None:
    error = NotFound("gone", session="build")
    assert error.payload() == {"code": "not_found", "message": "gone", "session": "build"}


def test_empty_stderr_still_produces_a_message() -> None:
    """A failure with no stderr must not produce an empty error message."""
    error = tmux_failure("", session="build")
    assert error.message
    assert isinstance(error, TmuxError)
