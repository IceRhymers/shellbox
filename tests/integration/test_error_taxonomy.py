"""Every documented error code is REACHABLE and asserted, through a real client (W4 §6).

§6's table is a closed set, and this module is its inventory. ``ASSERTED_BY`` names the test
that reaches each code, and ``test_the_taxonomy_inventory_is_complete`` fails if the table and
that mapping disagree -- so adding a code to §6 without a test that reaches it breaks this
lane rather than passing silently. The mapping is checked statically rather than by
accumulating a module global, so the check survives ``-k``, a single-test run, and any future
parallel or randomised ordering.

Two negatives matter as much as the positives, and both were near-misses in earlier plan
revisions:

* ``no_server`` is an INTERNAL classification (N1) and must never appear as a public code.
* An unrecognised tmux failure must never become an EMPTY LIST. Reporting a broken tmux as a
  healthy empty inventory is what would let orphan reconciliation mark every live session on
  the host dead.
"""

from __future__ import annotations

from pathlib import Path

from conftest import TmuxServer, requires_tmux
from harness import Outcome, fake_tmux, make_harness, run_calls

pytestmark = requires_tmux

# §6's closed set, transcribed. Every entry must be reached by a test in this module.
DOCUMENTED_CODES = {
    "invalid_name",
    "not_found",
    "already_exists",
    "bad_cwd",
    "no_payload",
    "invalid_key",
    "too_large",
    "line_too_long",
    "invalid_dimensions",
    "tmux_unavailable",
    "tmux_error",
}

# Which test reaches which code. Checked against DOCUMENTED_CODES below.
ASSERTED_BY = {
    "invalid_name": "test_invalid_name_for_a_target_shaped_name",
    "not_found": "test_not_found_for_send_read_resize",
    "already_exists": "test_already_exists_only_on_a_conflicting_cwd",
    "bad_cwd": "test_bad_cwd_for_a_non_directory_and_for_a_tab",
    "no_payload": "test_no_payload_and_invalid_key",
    "invalid_key": "test_no_payload_and_invalid_key",
    "too_large": "test_too_large_and_line_too_long",
    "line_too_long": "test_too_large_and_line_too_long",
    "invalid_dimensions": "test_invalid_dimensions_for_resize_and_read",
    "tmux_unavailable": "test_tmux_unavailable_when_the_binary_cannot_be_run",
    "tmux_error": "test_unknown_tmux_stderr_is_tmux_error_and_never_an_empty_list",
}


def covers(outcome: Outcome, code: str) -> Outcome:
    """Assert an outcome carries ``code`` -- and that ``code`` is one §6 documents.

    The second half is the closed-set check: a tool answering with a code outside the table
    is as much a taxonomy break as a code nothing can reach, and it would otherwise pass
    unnoticed here.
    """
    assert code in DOCUMENTED_CODES, f"{code!r} is not in §6's table"
    assert outcome.code == code, f"expected {code}, got {outcome.error}"
    return outcome


def test_invalid_name_for_a_target_shaped_name(tmux_server: TmuxServer, tmp_path: Path) -> None:
    """``=bui`` is ``invalid_name`` at the boundary, and tmux is never invoked with it.

    §7.1's two-level split: an anchored-looking name from a caller is a malformed NAME, not
    a missing session. Answering ``not_found`` would tell the caller to create it, and
    creating it would make a session unreachable through the only safe target form.
    """
    harness = make_harness(tmux_server, tmp_path)
    outcomes = run_calls(
        harness,
        [
            ("shell_create", {"name": "=bui", "cwd": str(tmp_path)}),
            ("shell_read", {"session": "not a valid name"}),
            ("shell_kill", {"session": "still:not:valid"}),
        ],
    )
    covers(outcomes[0], "invalid_name")
    covers(outcomes[1], "invalid_name")
    covers(outcomes[2], "invalid_name")
    assert tmux_server.sessions() == []


def test_not_found_for_send_read_resize(tmux_server: TmuxServer, tmp_path: Path) -> None:
    """An absent session is ``not_found`` for every non-create mutating tool."""
    harness = make_harness(tmux_server, tmp_path)
    outcomes = run_calls(
        harness,
        [
            # A server exists, so this is a genuine `can't find session`, not the cold start.
            ("shell_create", {"name": "present", "cwd": str(tmp_path)}),
            ("shell_send", {"session": "absent", "text": "x\n"}),
            ("shell_read", {"session": "absent"}),
            ("shell_resize", {"session": "absent", "cols": 80, "rows": 24}),
        ],
    )
    for outcome in outcomes[1:]:
        covers(outcome, "not_found")
    assert outcomes[1].error["session"] == "absent", "the error names the session it is about"


def test_not_found_when_the_session_has_no_incarnation(
    tmux_server: TmuxServer, tmp_path: Path
) -> None:
    """A FOREIGN session -- one shellbox cannot prove it owns -- is ``not_found`` (§9.1).

    Created here with raw tmux, so it carries no ``@shellbox_incarnation``: the same state a
    concurrent create is briefly in. ``shell_send`` into it would put an agent's input in
    someone else's shell, and ``shell_kill`` would destroy it, so both must refuse. An empty
    incarnation is never a match.
    """
    harness = make_harness(tmux_server, tmp_path)
    assert tmux_server.raw("new-session", "-d", "-s", "outsider").rc == 0
    outcomes = run_calls(
        harness,
        [
            ("shell_send", {"session": "outsider", "text": "rm -rf /\n"}),
            ("shell_kill", {"session": "outsider"}),
        ],
    )
    covers(outcomes[0], "not_found")
    covers(outcomes[1], "not_found")
    assert "outsider" in tmux_server.sessions(), "a foreign session must not be killed"


def test_already_exists_only_on_a_conflicting_cwd(tmux_server: TmuxServer, tmp_path: Path) -> None:
    """A second create with a DIFFERENT cwd is ``already_exists``.

    The matching-cwd case is a success (``created: false``) and is asserted in
    ``test_tools_over_stdio``; this is the other half. Handing back a shell pointed at
    another directory is the more dangerous answer, which is why only this one errors.
    """
    harness = make_harness(tmux_server, tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    outcomes = run_calls(
        harness,
        [
            ("shell_create", {"name": "conflict", "cwd": str(tmp_path)}),
            ("shell_create", {"name": "conflict", "cwd": str(elsewhere)}),
        ],
    )
    assert outcomes[0].data["created"] is True
    covers(outcomes[1], "already_exists")


def test_already_exists_for_a_name_taken_by_a_foreign_session(
    tmux_server: TmuxServer, tmp_path: Path
) -> None:
    """A name held by an UNSTAMPED session is ``already_exists``, never a silent reuse.

    Reusing it would hand the caller a shell shellbox knows nothing about -- possibly
    another tool's, possibly mid-create by a concurrent agent.
    """
    harness = make_harness(tmux_server, tmp_path)
    assert tmux_server.raw("new-session", "-d", "-s", "squatter").rc == 0
    (outcome,) = run_calls(harness, [("shell_create", {"name": "squatter", "cwd": str(tmp_path)})])
    covers(outcome, "already_exists")


def test_bad_cwd_for_a_non_directory_and_for_a_tab(tmux_server: TmuxServer, tmp_path: Path) -> None:
    """``bad_cwd`` covers both a missing directory and a path with a TAB in it.

    The TAB case is not pedantry: ``@shellbox_cwd`` rides in the TAB-separated
    ``list-sessions`` record, so a TAB in the path makes that session's record unparseable
    and drops it from the inventory (spike S11) -- and a dropped record is a live session
    that reconciliation would mark orphaned.
    """
    harness = make_harness(tmux_server, tmp_path)
    outcomes = run_calls(
        harness,
        [
            ("shell_create", {"name": "nodir", "cwd": str(tmp_path / "does-not-exist")}),
            ("shell_create", {"name": "tabbed", "cwd": f"{tmp_path}/has\ttab"}),
        ],
    )
    covers(outcomes[0], "bad_cwd")
    covers(outcomes[1], "bad_cwd")


def test_no_payload_and_invalid_key(tmux_server: TmuxServer, tmp_path: Path) -> None:
    """``shell_send`` with nothing to send, and with a key outside the allowlist.

    ``C-C`` (upper-case) is used deliberately: it is what a caller writes when they mean
    ``C-c``, and tmux's key parser accepts constructs far outside "a key", so the allowlist
    rejects anything not on it rather than guessing.
    """
    harness = make_harness(tmux_server, tmp_path)
    outcomes = run_calls(
        harness,
        [
            ("shell_create", {"name": "payload", "cwd": str(tmp_path)}),
            ("shell_send", {"session": "payload"}),
            ("shell_send", {"session": "payload", "keys": ["C-C"]}),
            ("shell_send", {"session": "payload", "keys": ["Enter", "run-shell"]}),
        ],
    )
    covers(outcomes[1], "no_payload")
    covers(outcomes[2], "invalid_key")
    covers(outcomes[3], "invalid_key")


def test_too_large_and_line_too_long(tmux_server: TmuxServer, tmp_path: Path) -> None:
    """The two size guards, which are different guards for different failures.

    ``too_large`` is a tmux-SERVER memory guard over the whole payload;
    ``line_too_long`` is the correctness boundary -- bytes since the last newline -- because
    that is the quantity the pty line discipline destroys (dropped on macOS, TRUNCATED on
    Linux, and a truncated command is a different, still-executable command).

    ``SHELLBOX_MAX_SEND_BYTES`` is lowered for this run so the total-bytes guard can be
    tripped without pushing a megabyte through the protocol. Enforcement lives in the
    adapter (W3); what is asserted here is that both codes reach a client.
    """
    harness = make_harness(tmux_server, tmp_path)
    small_total = harness.env_with(SHELLBOX_MAX_SEND_BYTES="64")
    outcomes = run_calls(
        harness,
        [
            ("shell_create", {"name": "sizes", "cwd": str(tmp_path)}),
            ("shell_send", {"session": "sizes", "text": "x" * 200}),
        ],
        env=small_total,
    )
    covers(outcomes[1], "too_large")

    long_line = run_calls(
        harness,
        [("shell_send", {"session": "sizes", "text": "y" * 1200})],
        env=small_total | {"SHELLBOX_MAX_SEND_BYTES": str(1 << 20)},
    )
    covers(long_line[0], "line_too_long")


def test_invalid_dimensions_for_resize_and_read(tmux_server: TmuxServer, tmp_path: Path) -> None:
    """Zero/oversized dimensions, and a negative ``lines``."""
    harness = make_harness(tmux_server, tmp_path)
    outcomes = run_calls(
        harness,
        [
            ("shell_create", {"name": "dims", "cwd": str(tmp_path)}),
            ("shell_resize", {"session": "dims", "cols": 0, "rows": 24}),
            ("shell_resize", {"session": "dims", "cols": 80, "rows": 99_999}),
            ("shell_read", {"session": "dims", "lines": -1}),
        ],
    )
    for outcome in outcomes[1:]:
        covers(outcome, "invalid_dimensions")


def test_tmux_unavailable_when_the_binary_cannot_be_run(
    tmux_server: TmuxServer, tmp_path: Path
) -> None:
    """A ``SHELLBOX_TMUX_BIN`` that does not exist is ``tmux_unavailable``, not a traceback."""
    harness = make_harness(tmux_server, tmp_path)
    outcomes = run_calls(
        harness,
        [("shell_list", {}), ("shell_create", {"name": "nobin", "cwd": str(tmp_path)})],
        env=harness.env_with(SHELLBOX_TMUX_BIN=str(tmp_path / "no-such-tmux")),
    )
    covers(outcomes[0], "tmux_unavailable")
    covers(outcomes[1], "tmux_unavailable")


def test_tmux_unavailable_for_a_socket_path_over_the_platform_limit(
    tmux_server: TmuxServer, tmp_path: Path
) -> None:
    """A too-long socket path is ``tmux_unavailable`` per call -- and the process still runs.

    ``sun_path`` is a fixed 104/108-byte array, so tmux can never be reached over such a
    path. Failing the whole handshake instead would give the agent nothing to report but "the
    server did not start"; this way the answer names the cause and every other tool call gets
    the same honest error.
    """
    harness = make_harness(tmux_server, tmp_path)
    too_long = "/tmp/" + "s" * 200
    (outcome,) = run_calls(
        harness, [("shell_list", {})], env=harness.env_with(SHELLBOX_TMUX_SOCKET=too_long)
    )
    covers(outcome, "tmux_unavailable")
    assert "socket path" in outcome.error["message"]


def test_unknown_tmux_stderr_is_tmux_error_and_never_an_empty_list(
    tmux_server: TmuxServer, tmp_path: Path
) -> None:
    """🔴 The one that matters most: an unrecognised tmux failure is NOT an empty inventory.

    ``shell_list`` returns ``[]`` for exactly two measured stderr signatures ("no server
    running" and the cold-start "error connecting to ... (No such file or directory)"). Any
    other failure must be an error, because an empty list is indistinguishable from "this
    host has no sessions" -- and E5 would then mark every live session on the host
    ``orphaned`` on the strength of it.
    """
    harness = make_harness(tmux_server, tmp_path)
    broken = fake_tmux(tmp_path, stderr="tmux: something nobody has ever seen before")
    (outcome,) = run_calls(
        harness, [("shell_list", {})], env=harness.env_with(SHELLBOX_TMUX_BIN=broken)
    )
    covers(outcome, "tmux_error")
    # The point of the test, stated as an assertion: NO inventory came back -- not an empty
    # one, and not a partial one.
    assert outcome.structured is None
    assert '"sessions"' not in outcome.text


def test_a_too_long_socket_path_is_not_reported_as_no_server(
    tmux_server: TmuxServer, tmp_path: Path
) -> None:
    """``error connecting to ... (File name too long)`` shares a prefix with the cold start.

    Matching the prefix alone would classify a misconfigured path as "no server", i.e. as an
    empty inventory. Driven here through a fake tmux emitting the real message, because the
    two signatures differ only in their parenthesised cause.
    """
    harness = make_harness(tmux_server, tmp_path)
    misconfigured = fake_tmux(
        tmp_path, stderr="error connecting to /tmp/whatever (File name too long)"
    )
    (outcome,) = run_calls(
        harness, [("shell_list", {})], env=harness.env_with(SHELLBOX_TMUX_BIN=misconfigured)
    )
    covers(outcome, "tmux_error")


def test_no_server_is_never_a_public_code(tmux_server: TmuxServer, tmp_path: Path) -> None:
    """The internal N1 classification does not leak into a tool payload.

    ⚠️ **Where §6 and N1 disagree, and how it is pinned.** §6's table calls
    ``tmux_unavailable`` "the only code for 'no tmux server'", while N1 says ``no server
    running`` means "an empty list for ``shell_list``; **not_found elsewhere**". This lane
    asserts N1's reading, which is the one ``errors.py`` implements (``tmux_failure``'s
    ``no_server_as="not_found"``, W2, with the rationale in place): a session on a server that
    is not running does not exist, and answering ``tmux_unavailable`` would describe the
    infrastructure instead of the question the caller asked. The classification is still
    carried internally on ``internal_code`` so §9.2's E5 can be triggered by it.

    ``tmux_unavailable`` remains reachable and is asserted by the two tests above -- an
    un-runnable binary and a socket path over the platform limit -- which are the cases where
    tmux genuinely cannot be reached at all.

    Either way ``no_server`` itself must never appear: it is not in §6's table, so an agent
    could only branch on it by accident.
    """
    harness = make_harness(tmux_server, tmp_path)
    absent_server = fake_tmux(tmp_path, stderr="no server running on /tmp/nothing")
    outcomes = run_calls(
        harness,
        [
            ("shell_list", {}),
            ("shell_read", {"session": "anything"}),
            ("shell_send", {"session": "anything", "text": "x\n"}),
            ("shell_kill", {"session": "anything"}),
        ],
        env=harness.env_with(SHELLBOX_TMUX_BIN=absent_server),
    )
    assert outcomes[0].data == {"host_id": "itest-host", "sessions": []}
    covers(outcomes[1], "not_found")
    covers(outcomes[2], "not_found")
    # A kill with nothing to kill is idempotent success, even here.
    assert outcomes[3].data["killed"] is False
    for outcome in outcomes:
        assert "no_server" not in outcome.text, f"internal code leaked: {outcome.text}"


def test_foreign_sessions_are_listed_with_a_null_incarnation(
    tmux_server: TmuxServer, tmp_path: Path
) -> None:
    """``foreign: true`` + ``incarnation: null`` for an unstamped session (§9.1, T-CONC-4).

    An empty ``@shellbox_incarnation`` is NEVER an incarnation match. It is reported as a
    defined state rather than omitted, because the Phase 4 inventory and orphan
    reconciliation both have to tell "not mine" apart from "mine, and I forgot".
    """
    harness = make_harness(tmux_server, tmp_path)
    assert tmux_server.raw("new-session", "-d", "-s", "stranger").rc == 0
    outcomes = run_calls(
        harness,
        [("shell_create", {"name": "mine", "cwd": str(tmp_path)}), ("shell_list", {})],
    )
    entries = {entry["tmux_name"]: entry for entry in outcomes[1].data["sessions"]}
    assert entries["stranger"]["foreign"] is True
    assert entries["stranger"]["incarnation"] is None
    assert entries["mine"]["foreign"] is False
    assert entries["mine"]["incarnation"] == outcomes[0].data["incarnation"]


def test_the_taxonomy_inventory_is_complete() -> None:
    """Every code in §6's table is claimed by a test that exists in this module.

    A ratchet, not a behavioral test: it fails when §6 gains a code nothing here reaches, or
    when the test that reached one is renamed or deleted. That is the failure mode "every
    documented error code is reachable and asserted" exists to prevent.
    """
    assert set(ASSERTED_BY) == DOCUMENTED_CODES, (
        f"unclaimed codes: {sorted(DOCUMENTED_CODES - set(ASSERTED_BY))}; "
        f"claimed but undocumented: {sorted(set(ASSERTED_BY) - DOCUMENTED_CODES)}"
    )
    for code, test_name in ASSERTED_BY.items():
        assert callable(globals().get(test_name)), (
            f"{code} claims to be asserted by {test_name}, which does not exist here"
        )
