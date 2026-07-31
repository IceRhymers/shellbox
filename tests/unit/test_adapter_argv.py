"""``TmuxAdapter`` argv construction and output parsing, with no tmux involved.

The unit lane exists to assert things a real-tmux test cannot: the exact argv shellbox
BUILDS, and -- just as important -- that some inputs cause **no tmux invocation at all**.
"tmux was never called" is not observable against a real server.

The tmux output fixtures in this file are literal captures from a real tmux, not invented
strings; ``tests/tmux/`` re-asserts the same properties against the real thing.
"""

from __future__ import annotations

import logging
import os
import re

import pytest
from conftest import RecordingRunner, result
from shellbox_mcp.errors import (
    AlreadyExists,
    InvalidName,
    LineTooLong,
    NoPayload,
    NotFound,
    TmuxError,
    TooLarge,
)
from shellbox_mcp.tmux import LIST_FORMAT, TmuxAdapter, TmuxConfig

SOCKET = "/tmp/sbx-unit.sock"


def adapter(runner: RecordingRunner, **overrides: object) -> TmuxAdapter:
    settings: dict[str, object] = {"socket_path": SOCKET}
    settings.update(overrides)
    return TmuxAdapter(TmuxConfig(**settings), runner=runner)  # type: ignore[arg-type]


def _after_verb(argv: tuple[str, ...], verb: str) -> tuple[str, ...]:
    """The argv from the tmux VERB onwards.

    Necessary because the base argv already contains ``-S <socket>``, so an assertion about
    ``capture-pane``'s ``-S <range>`` would otherwise be answered by the socket flag.
    """
    return argv[argv.index(verb) :]


# --------------------------------------------------------------------------------------
# The §7.2 create chain
# --------------------------------------------------------------------------------------


def test_create_builds_the_section_7_2_chain_verbatim_as_one_invocation(tmp_path) -> None:
    runner = RecordingRunner()
    created = adapter(runner).create("build", cwd=str(tmp_path), command=["sh"])

    assert len(runner.calls) == 1, "the create chain must be ONE invocation, not several"
    real_cwd = os.path.realpath(str(tmp_path))
    assert runner.argvs[0] == (
        "tmux",
        "-S",
        SOCKET,
        "-f",
        "/dev/null",
        "start-server",
        ";",
        "set-option",
        "-g",
        "history-limit",
        "20000",
        ";",
        "set-option",
        "-g",
        "status",
        "off",
        ";",
        "set-option",
        "-g",
        "default-terminal",
        "screen-256color",
        ";",
        "set-option",
        "-g",
        "remain-on-exit",
        "on",
        ";",
        "new-session",
        "-d",
        "-s",
        "build",
        "-x",
        "80",
        "-y",
        "24",
        "-c",
        real_cwd,
        "sh",
        ";",
        "set-option",
        "-t",
        "=build:",
        "@shellbox_incarnation",
        created.incarnation,
        ";",
        "set-option",
        "-t",
        "=build:",
        "@shellbox_cwd",
        real_cwd,
    )
    assert created.created is True
    assert created.incarnation and re.fullmatch(r"[0-9a-f-]{36}", created.incarnation)


def test_globals_precede_new_session_in_the_chain(tmp_path) -> None:
    """The ordering that makes ``history-limit`` reach the PANE.

    A pane fixes its history limit at creation. Set the global afterwards and the pane stays
    at tmux's 2000 default while ``show-options -g`` reports 20000 -- which is how a previous
    revision passed its own acceptance test while every real pane ran at 2000.
    """
    runner = RecordingRunner()
    adapter(runner).create("build", cwd=str(tmp_path))
    argv = runner.argvs[0]
    assert argv.index("start-server") < argv.index("history-limit") < argv.index("new-session")


def test_create_never_sets_window_size_at_any_scope(tmp_path) -> None:
    runner = RecordingRunner()
    adapter(runner).create("build", cwd=str(tmp_path))
    assert "window-size" not in runner.argvs[0]
    assert "manual" not in runner.argvs[0]


def test_create_passes_env_as_dash_e_arguments(tmp_path) -> None:
    runner = RecordingRunner()
    adapter(runner).create("build", cwd=str(tmp_path), env={"FOO": "a\nb", "BAR": "x;y"})
    argv = runner.argvs[0]
    assert "-e" in argv
    assert "FOO=a\nb" in argv
    assert "BAR=x;y" in argv
    # `-e` must land on new-session, not after the trailing set-options.
    assert argv.index("FOO=a\nb") > argv.index("new-session")
    assert argv.index("FOO=a\nb") < argv.index("@shellbox_incarnation")


def test_create_uses_configured_history_limit_and_terminal(tmp_path) -> None:
    runner = RecordingRunner()
    adapter(runner, history_limit=5000, default_terminal="tmux-256color").create(
        "build", cwd=str(tmp_path)
    )
    argv = runner.argvs[0]
    assert argv[argv.index("history-limit") + 1] == "5000"
    assert argv[argv.index("default-terminal") + 1] == "tmux-256color"


# --------------------------------------------------------------------------------------
# Validation happens BEFORE tmux is invoked
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["=bui", "=build:", "bad:name", "-flag", ""])
def test_invalid_names_never_reach_tmux(name: str) -> None:
    """A name of ``=bui`` is ``invalid_name``, and tmux is not invoked at all."""
    runner = RecordingRunner()
    for call in (
        lambda: adapter(runner).create(name),
        lambda: adapter(runner).send(name, text="x"),
        lambda: adapter(runner).read(name),
        lambda: adapter(runner).resize(name, 80, 24),
        lambda: adapter(runner).kill(name),
        lambda: adapter(runner).exists(name),
    ):
        with pytest.raises(InvalidName):
            call()
    assert runner.calls == [], "tmux must not be invoked for an invalid name"


@pytest.mark.parametrize("char", ["\t", "\r", "\n"])
def test_bad_cwd_never_reaches_tmux(tmp_path, char: str) -> None:
    from shellbox_mcp.errors import BadCwd

    directory = tmp_path / f"a{char}b"
    directory.mkdir()
    runner = RecordingRunner()
    with pytest.raises(BadCwd):
        adapter(runner).create("build", cwd=str(directory))
    assert runner.calls == []


def test_line_too_long_is_returned_without_invoking_tmux() -> None:
    """§8's correctness boundary, and it fires before the server is touched.

    The limit is on bytes since the last newline, not total bytes, because that is the
    quantity the pty line discipline destroys: over it, macOS discards the entire line and
    Linux silently TRUNCATES it into a different, still-executable command.
    """
    runner = RecordingRunner()
    with pytest.raises(LineTooLong):
        adapter(runner).send("build", text="x" * 1000)
    assert runner.calls == []


def test_a_long_payload_split_across_lines_is_allowed() -> None:
    """Total bytes are not the boundary: 10 x 999-byte lines is fine, one 1000 is not."""
    runner = RecordingRunner(default=result(rc=0, stdout="build\tINC-1\n"))
    payload = "\n".join(["x" * 999] * 10)
    sent = adapter(runner).send("build", text=payload)
    assert sent.submitted_bytes == len(payload.encode())


def test_too_large_total_payload() -> None:
    runner = RecordingRunner()
    with pytest.raises(TooLarge):
        adapter(runner, max_send_bytes=100).send("build", text="\n".join(["x" * 10] * 20))
    assert runner.calls == []


def test_send_requires_a_payload() -> None:
    runner = RecordingRunner()
    with pytest.raises(NoPayload):
        adapter(runner).send("build")
    assert runner.calls == []


def test_invalid_key_never_reaches_tmux() -> None:
    from shellbox_mcp.errors import InvalidKey

    runner = RecordingRunner()
    with pytest.raises(InvalidKey):
        adapter(runner).send("build", keys=["kill-session"])
    assert runner.calls == []


# --------------------------------------------------------------------------------------
# display-message: empty output is not_found
# --------------------------------------------------------------------------------------


def test_display_message_empty_stdout_is_not_found() -> None:
    """``display-message`` returns rc=0 for a nonexistent target, so rc is worthless (F6)."""
    runner = RecordingRunner(default=result(rc=0, stdout=""))
    with pytest.raises(NotFound):
        adapter(runner).resize("build", 80, 24)
    # It resolved and stopped; no resize-window was issued at a session that is not there.
    assert all("resize-window" not in argv for argv in runner.argvs)


def test_display_message_literal_separators_are_still_not_found() -> None:
    """The naive emptiness check is not enough, and this is the case that breaks it.

    Measured while building the adapter (and added to the spike as F11): for a MULTI-field
    format the placeholders expand empty but the literal TABs SURVIVE, so a nonexistent
    target yields ``"\\t\\t\\n"`` -- rc=0 AND non-empty stdout. Every format therefore leads
    with ``#{session_name}``, and resolution is decided by that field alone.
    """
    runner = RecordingRunner(default=result(rc=0, stdout="\t\t\t\t\t\n"))
    with pytest.raises(NotFound):
        adapter(runner).read("build")


def test_a_session_with_an_empty_incarnation_is_never_a_match() -> None:
    """Present, but unstamped: ``not_found``, never a silent success (§9.1).

    An empty ``@shellbox_incarnation`` means either a create is in flight or the session is
    foreign. Both are "cannot confirm identity", and an equality test two empty strings can
    satisfy is not an identity check.
    """
    runner = RecordingRunner(default=result(rc=0, stdout="build\t\n"))
    with pytest.raises(NotFound) as excinfo:
        adapter(runner).send("build", text="hi\n")
    assert "carries no @shellbox_incarnation" in excinfo.value.message
    assert all("paste-buffer" not in argv for argv in runner.argvs)


def test_kill_distinguishes_absent_from_unowned() -> None:
    absent = RecordingRunner(default=result(rc=0, stdout="\t\n"))
    # Absent: idempotent success with killed=false, so a kill race has no loser.
    assert adapter(absent).kill("build") is False
    assert all("kill-session" not in argv for argv in absent.argvs)

    # Present but unstamped: not_found. Killing a session shellbox cannot prove it owns
    # must fail rather than succeed silently.
    foreign = RecordingRunner(default=result(rc=0, stdout="build\t\n"))
    with pytest.raises(NotFound):
        adapter(foreign).kill("build")
    assert all("kill-session" not in argv for argv in foreign.argvs)


# --------------------------------------------------------------------------------------
# The buffer send path (W3 owns the correctness matrix; this is the plumbing)
# --------------------------------------------------------------------------------------


def test_send_text_uses_load_buffer_from_stdin_and_paste_buffer_dash_d() -> None:
    runner = RecordingRunner(default=result(rc=0, stdout="build\tINC-1\n"))
    sent = adapter(runner).send("build", text="echo hi\n")

    load_argv, load_stdin = next(c for c in runner.calls if "load-buffer" in c[0])
    # `-` reads STDIN, so agent input never touches disk.
    assert load_argv[-1] == "-"
    assert load_stdin == b"echo hi\n"
    buffer_name = load_argv[load_argv.index("-b") + 1]
    assert buffer_name.startswith("shellbox-")

    paste_argv = runner.sub_argv("paste-buffer")
    # `-d` drops the buffer on success, so payloads do not linger in the server's buffer
    # stack where any other pane could paste them back.
    assert "-d" in paste_argv
    assert paste_argv[paste_argv.index("-b") + 1] == buffer_name
    assert paste_argv[paste_argv.index("-t") + 1] == "=build:"
    assert sent.submitted_bytes == len(b"echo hi\n")
    assert sent.incarnation == "INC-1"
    # Nothing here may be read as a delivery receipt: per H4 the bytes reaching the pane
    # PROCESS are not knowable to shellbox.
    assert sent.delivery == "unverified"


def test_buffer_names_are_unique_per_call() -> None:
    """Required, not stylistic: a shared name lets pooled agents paste into each other."""
    runner = RecordingRunner(default=result(rc=0, stdout="build\tINC-1\n"))
    ad = adapter(runner)
    ad.send("build", text="a\n")
    ad.send("build", text="b\n")
    names = [argv[argv.index("-b") + 1] for argv in runner.argvs if "load-buffer" in argv]
    assert len(names) == 2
    assert names[0] != names[1]


def test_failed_paste_deletes_the_buffer_and_raises() -> None:
    """``-d`` only fires on success, so the failure path must delete the buffer itself.

    ``buffer-limit`` defaults to 50, server-wide across every pooled agent, so a leaked
    buffer evicts another agent's buffers *and* retains arbitrary agent input.
    """
    runner = RecordingRunner(
        results=[
            result(rc=0, stdout="build\tINC-1\n"),  # resolve
            result(rc=0),  # load-buffer
            result(rc=1, stderr="some paste failure"),  # paste-buffer
            result(rc=0),  # delete-buffer
        ]
    )
    with pytest.raises(TmuxError):
        adapter(runner).send("build", text="hi\n")
    load_argv = runner.sub_argv("load-buffer")
    delete_argv = runner.sub_argv("delete-buffer")
    assert delete_argv[delete_argv.index("-b") + 1] == load_argv[load_argv.index("-b") + 1]


def test_cleanup_failure_does_not_mask_the_paste_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    runner = RecordingRunner(
        results=[
            result(rc=0, stdout="build\tINC-1\n"),
            result(rc=0),
            result(rc=1, stderr="paste exploded"),
            result(rc=1, stderr="unknown buffer: shellbox-x"),
        ]
    )
    with caplog.at_level(logging.WARNING), pytest.raises(TmuxError) as excinfo:
        adapter(runner).send("build", text="hi\n")
    assert "paste exploded" in excinfo.value.message
    assert "could not delete tmux buffer" in caplog.text


def test_send_keys_uses_a_double_dash_and_preserves_order() -> None:
    runner = RecordingRunner(default=result(rc=0, stdout="build\tINC-1\n"))
    sent = adapter(runner).send("build", keys=["C-c", "Enter"])
    argv = runner.sub_argv("send-keys")
    assert argv[argv.index("-t") + 1] == "=build:"
    assert argv[-3:] == ("--", "C-c", "Enter")
    assert sent.keys_sent == ("C-c", "Enter")


def test_text_is_delivered_before_keys() -> None:
    """Ordering is guaranteed (M18) and callers depend on it: paste, then Enter."""
    runner = RecordingRunner(default=result(rc=0, stdout="build\tINC-1\n"))
    adapter(runner).send("build", text="echo hi", keys=["Enter"])
    order = [argv for argv in runner.argvs if "paste-buffer" in argv or "send-keys" in argv]
    assert "paste-buffer" in order[0]
    assert "send-keys" in order[1]


# --------------------------------------------------------------------------------------
# read
# --------------------------------------------------------------------------------------


def test_read_preserves_ansi_and_omits_dash_j() -> None:
    runner = RecordingRunner(
        results=[
            result(rc=0, stdout="build\t80\t24\t0\t7\t20000\n"),
            result(rc=0, stdout="\x1b[31mRED\x1b[39m\n"),
        ]
    )
    read = adapter(runner).read("build")
    # Only the part after the verb: `-S <socket>` is in every base argv, so a naive
    # `"-S" not in argv` would assert about the socket flag instead of the capture range.
    argv = _after_verb(runner.sub_argv("capture-pane"), "capture-pane")
    # `-e` is what makes the Phase 4 renderer possible; without it ANSI is stripped and
    # nothing notices until then. `-J` would destroy the wrapping the terminal needs.
    assert "-e" in argv
    assert "-J" not in argv
    assert "-S" not in argv  # lines=0 is the visible pane
    assert read.content == "\x1b[31mRED\x1b[39m\n"
    assert (read.cols, read.rows) == (80, 24)
    assert read.alive is True
    assert read.scrollback_lines == 7
    assert read.history_limit == 20000


def test_read_with_lines_uses_dash_s_negative() -> None:
    runner = RecordingRunner(
        results=[result(rc=0, stdout="build\t80\t24\t0\t7\t20000\n"), result(rc=0, stdout="x\n")]
    )
    adapter(runner).read("build", lines=100)
    argv = _after_verb(runner.sub_argv("capture-pane"), "capture-pane")
    assert argv[argv.index("-S") + 1] == "-100"


def test_read_reports_a_dead_pane_as_not_alive() -> None:
    runner = RecordingRunner(
        results=[
            result(rc=0, stdout="build\t80\t24\t1\t3\t20000\n"),
            result(rc=0, stdout="LASTLINE\n"),
        ]
    )
    read = adapter(runner).read("build")
    # With `remain-on-exit on`, has-session no longer implies alive, so liveness reads the
    # PANE. `alive` is the single source; there is no separate dead-pane field.
    assert read.alive is False
    assert "LASTLINE" in read.content


# --------------------------------------------------------------------------------------
# list-sessions parsing
# --------------------------------------------------------------------------------------

# A literal capture: `build` stamped, `foreign` unstamped. Note the trailing TABs on the
# second record -- those are the four characters the strip() trap eats.
LIST_OUTPUT = (
    "build\t1785477220\t1785477230\t80\t24\t0\t11660705-aaaa-bbbb-cccc-ddddddddeeee\t/tmp\n"
    "foreign\t1785477240\t1785477250\t80\t24\t0\t\t\n"
)


def _list_runner(stdout: str, cwd: str = "/private/tmp") -> RecordingRunner:
    lines = [line for line in stdout.split("\n") if line]
    results = [result(rc=0, stdout=stdout)]
    for line in lines:
        name = line.split("\t")[0]
        stamped = line.split("\t")[7] if len(line.split("\t")) > 7 else ""
        results.append(result(rc=0, stdout=f"{name}\t{cwd}\n"))
        results.append(result(rc=0, stdout=f"{name}\t{stamped}\n"))
    return RecordingRunner(results=results)


def test_list_uses_the_eight_field_format() -> None:
    runner = _list_runner(LIST_OUTPUT)
    adapter(runner).list_sessions()
    argv = runner.argvs[0]
    fmt = argv[argv.index("-F") + 1]
    assert fmt == LIST_FORMAT
    assert fmt.count("\t") == 7
    # The two fields that may legitimately be EMPTY go LAST. With `incarnation` earlier, a
    # parser reads `session_created` -- always non-empty -- as the incarnation, and every
    # session is then misidentified as shellbox-owned with a bogus incarnation, silently.
    assert fmt.split("\t")[0] == "#{session_name}"
    assert fmt.split("\t")[-2:] == ["#{@shellbox_incarnation}", "#{@shellbox_cwd}"]
    assert "#{pane_current_path}" not in fmt


def test_list_parses_stamped_and_unstamped_sessions() -> None:
    sessions = adapter(_list_runner(LIST_OUTPUT)).list_sessions()
    assert [s.tmux_name for s in sessions] == ["build", "foreign"]

    build, foreign = sessions
    assert build.incarnation == "11660705-aaaa-bbbb-cccc-ddddddddeeee"
    assert build.foreign is False
    assert (build.cols, build.rows, build.alive) == (80, 24, True)
    assert build.created_at == 1785477220

    # An unstamped session is reported, with incarnation None and foreign true -- a DEFINED
    # state (the §9.2 stamp window), not an accident, and never a match.
    assert foreign.incarnation is None
    assert foreign.foreign is True


def test_the_strip_trap_is_what_this_parser_avoids() -> None:
    """An unstamped record has 8 raw fields and 6 stripped ones (spike S10).

    Trailing empty fields end the line in TABs and ``.strip()`` eats them. A parser that
    stripped first would see 6, apply "field count != 8 => drop", and silently discard
    exactly the unstamped sessions -- which is the set orphan reconciliation exists to find.
    """
    unstamped = LIST_OUTPUT.split("\n")[1]
    assert len(unstamped.split("\t")) == 8
    assert len(unstamped.strip().split("\t")) == 6  # the trap, demonstrated

    sessions = adapter(_list_runner(LIST_OUTPUT)).list_sessions()
    assert len(sessions) == 2, "the unstamped record must survive parsing"


def test_a_record_with_the_wrong_field_count_is_dropped_with_a_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Nine fields means a TAB got into a value: drop it, never partially parse it."""
    broken = "build\t1\t2\t80\t24\t0\tINC\t/tmp/a\tb\n" + LIST_OUTPUT.split("\n")[1] + "\n"
    with caplog.at_level(logging.WARNING):
        sessions = adapter(_list_runner(broken)).list_sessions()
    assert [s.tmux_name for s in sessions] == ["foreign"]
    assert "dropping malformed list-sessions record with 9 fields" in caplog.text


def test_list_enriches_cwd_from_pane_current_path() -> None:
    """``cwd`` is where the shell IS, not where it was created (M20).

    ``#{session_path}`` would show the directory the session was created in and go stale
    after any ``cd``; on macOS the two also differ by ``/private`` normalisation.
    """
    runner = _list_runner(LIST_OUTPUT, cwd="/private/tmp/sub")
    sessions = adapter(runner).list_sessions()
    assert sessions[0].cwd == "/private/tmp/sub"

    formats = [argv[-1] for argv in runner.argvs if "display-message" in argv]
    # Each user-controlled value is read in its own invocation, so a TAB inside one cannot
    # shift the other; and every format leads with #{session_name} so a nonexistent target
    # is detectable despite display-message's rc=0.
    assert "#{session_name}\t#{pane_current_path}" in formats
    assert "#{session_name}\t#{@shellbox_cwd}" in formats
    assert all(fmt.startswith("#{session_name}") for fmt in formats)
    assert all("#{session_path}" not in fmt for fmt in formats)


def test_list_logs_when_the_stamped_cwd_disagrees_with_the_per_session_read(
    caplog: pytest.LogCaptureFixture,
) -> None:
    runner = RecordingRunner(
        results=[
            result(rc=0, stdout=LIST_OUTPUT.split("\n")[0] + "\n"),
            result(rc=0, stdout="build\t/private/tmp\n"),  # pane_current_path
            result(rc=0, stdout="build\t/somewhere/else\n"),  # @shellbox_cwd per session
        ]
    )
    with caplog.at_level(logging.WARNING):
        sessions = adapter(runner).list_sessions()
    assert sessions[0].stamped_cwd == "/somewhere/else", "the per-session value wins"
    assert "the per-session value wins" in caplog.text


def test_a_session_that_vanishes_mid_enrichment_is_dropped() -> None:
    runner = RecordingRunner(
        results=[
            result(rc=0, stdout=LIST_OUTPUT),
            result(rc=0, stdout="\t\n"),  # build vanished
            result(rc=0, stdout="foreign\t/private/tmp\n"),
            result(rc=0, stdout="foreign\t\n"),
        ]
    )
    assert [s.tmux_name for s in adapter(runner).list_sessions()] == ["foreign"]


def test_no_server_running_is_an_empty_list() -> None:
    runner = RecordingRunner(default=result(rc=1, stderr="no server running on /tmp/sbx"))
    assert adapter(runner).list_sessions() == []


@pytest.mark.parametrize(
    "stderr",
    ["some unknown tmux failure", "error connecting to /tmp/sbx (File name too long)", ""],
)
def test_unknown_stderr_never_becomes_an_empty_list(stderr: str) -> None:
    """🔴 The mapping that must not exist.

    A permissive "probably no sessions" fallback would report a broken tmux as a healthy
    empty inventory, and orphan reconciliation would mark every live session on the host
    ``orphaned`` on the strength of it.
    """
    runner = RecordingRunner(default=result(rc=1, stderr=stderr))
    with pytest.raises(TmuxError):
        adapter(runner).list_sessions()


def test_records_that_all_fail_to_parse_raise_instead_of_returning_an_empty_list() -> None:
    """🔴 The same rule one layer down: tmux SUCCEEDED but nothing parsed.

    This is the shape of the locale bug -- a non-UTF-8 ctype locale makes tmux encode the
    record's TABs as ``_``, so all eight fields collapse into one and every record is
    "malformed". Dropping them one by one and returning ``[]`` would report a broken
    environment as a healthy empty inventory, which is the input orphan reconciliation acts
    on. The adapter forces ``LC_CTYPE``, so this should be unreachable -- which is exactly
    why it must be loud.
    """
    mangled = "build_1785477220_1785477230_80_24_0_11660705-aaaa_/tmp\n"
    runner = RecordingRunner(default=result(rc=0, stdout=mangled))
    with pytest.raises(TmuxError) as excinfo:
        adapter(runner).list_sessions()
    assert "ctype locale" in excinfo.value.message

    # An genuinely empty server is still an empty list, not an error.
    assert adapter(RecordingRunner(default=result(rc=0, stdout=""))).list_sessions() == []


def test_one_bad_record_among_good_ones_is_still_only_dropped() -> None:
    """The loud failure above must not fire when tmux is fine and one record is odd."""
    stdout = "build_mangled_only_this_one\n" + LIST_OUTPUT
    sessions = adapter(_list_runner(stdout)).list_sessions()
    assert [s.tmux_name for s in sessions] == ["build", "foreign"]


@pytest.mark.parametrize(
    "stderr",
    [
        "no server running on /tmp/sbx",
        # The cold-start signature: the socket FILE does not exist yet. Measured in both
        # lanes, and absent from the plan's N1 table.
        "error connecting to /tmp/sbx (No such file or directory)",
    ],
)
def test_no_server_is_not_found_everywhere_except_list(stderr: str) -> None:
    """N1, literally: ``no server running`` is an empty list for ``shell_list``, and
    ``not_found`` elsewhere.

    A session on a server that is not running does not exist. Reporting the infrastructure
    instead of answering the question would also make ``shell_kill`` non-idempotent, which
    §9.2 requires it to be.
    """
    assert adapter(RecordingRunner(default=result(rc=1, stderr=stderr))).list_sessions() == []
    with pytest.raises(NotFound):
        adapter(RecordingRunner(default=result(rc=1, stderr=stderr))).read("build")
    with pytest.raises(NotFound):
        adapter(RecordingRunner(default=result(rc=1, stderr=stderr))).send("build", text="x\n")
    assert adapter(RecordingRunner(default=result(rc=1, stderr=stderr))).kill("build") is False
    assert adapter(RecordingRunner(default=result(rc=1, stderr=stderr))).exists("build") is False


def test_a_too_long_socket_path_is_an_error_not_an_empty_inventory() -> None:
    """Same ``error connecting to`` prefix, opposite meaning -- so both parts must match.

    A misconfigured socket path reported as a healthy empty inventory is how one process
    with a bad ``SHELLBOX_TMUX_SOCKET`` would mark every live session on the host
    ``orphaned``.
    """
    stderr = "error connecting to /tmp/ssss… (File name too long)"
    with pytest.raises(TmuxError):
        adapter(RecordingRunner(default=result(rc=1, stderr=stderr))).list_sessions()


# --------------------------------------------------------------------------------------
# create failure handling
# --------------------------------------------------------------------------------------


def test_duplicate_session_with_a_matching_cwd_is_an_idempotent_reuse(tmp_path) -> None:
    """tmux resolves the create race atomically; the only question is whether it is ours."""
    real = os.path.realpath(str(tmp_path))
    runner = RecordingRunner(
        results=[
            result(rc=1, stderr="duplicate session: build"),
            result(rc=0, stdout="build\tINC-1\n"),  # existing incarnation
            result(rc=0, stdout=f"build\t{real}\n"),  # existing @shellbox_cwd
        ]
    )
    created = adapter(runner).create("build", cwd=str(tmp_path))
    assert created.created is False
    assert created.incarnation == "INC-1"


def test_duplicate_session_with_a_conflicting_cwd_is_already_exists(tmp_path) -> None:
    """Silently reusing a session pointed at another directory is the dangerous failure."""
    runner = RecordingRunner(
        results=[
            result(rc=1, stderr="duplicate session: build"),
            result(rc=0, stdout="build\tINC-1\n"),
            result(rc=0, stdout="build\t/somewhere/else\n"),
        ]
    )
    with pytest.raises(AlreadyExists):
        adapter(runner).create("build", cwd=str(tmp_path))


def test_duplicate_session_that_is_unstamped_is_already_exists(tmp_path) -> None:
    """A name held by a session shellbox cannot prove it owns is an error, not a reuse."""
    runner = RecordingRunner(
        results=[
            result(rc=1, stderr="duplicate session: build"),
            result(rc=0, stdout="build\t\n"),
            result(rc=0, stdout="build\t/tmp\n"),
        ]
    )
    with pytest.raises(AlreadyExists) as excinfo:
        adapter(runner).create("build", cwd=str(tmp_path))
    assert "@shellbox_incarnation" in excinfo.value.message


def test_create_failure_other_than_duplicate_propagates(tmp_path) -> None:
    runner = RecordingRunner(default=result(rc=1, stderr="server exited unexpectedly"))
    with pytest.raises(TmuxError):
        adapter(runner).create("build", cwd=str(tmp_path))


# --------------------------------------------------------------------------------------
# base argv invariants
# --------------------------------------------------------------------------------------


def test_every_invocation_carries_the_socket_and_ignores_user_config(tmp_path) -> None:
    runner = RecordingRunner(default=result(rc=0, stdout="build\tINC-1\n"))
    ad = adapter(runner)
    ad.create("build", cwd=str(tmp_path))
    ad.exists("build")
    ad.resize("build", 90, 30)
    for argv in runner.argvs:
        assert argv[:5] == ("tmux", "-S", SOCKET, "-f", "/dev/null")


def test_every_dash_t_value_is_anchored_at_runtime(tmp_path) -> None:
    """The structural test in ``test_target.py`` proved the source; this proves the argv."""
    # One default that satisfies both readers: `_display_tail` takes everything after the
    # first TAB as the value (non-empty => owned), and `_display_numeric` sees 6 fields.
    runner = RecordingRunner(default=result(rc=0, stdout="build\t80\t24\t0\t7\t20000\n"))
    ad = adapter(runner)
    ad.create("build", cwd=str(tmp_path))
    ad.send("build", text="hi\n", keys=["Enter"])
    ad.read("build", lines=10)
    ad.resize("build", 90, 30)
    ad.kill("build")
    ad.exists("build")
    seen = 0
    for argv in runner.argvs:
        for index, item in enumerate(argv[:-1]):
            if item == "-t":
                assert argv[index + 1] == "=build:"
                seen += 1
    assert seen >= 8, f"expected -t on most verbs, saw {seen}"
