"""``TmuxAdapter`` against a REAL tmux server.

Green in BOTH lanes or §7 is not normative: local tmux 3.6b/macOS, and tmux 3.4 on
ubuntu:24.04 (the sandbox's version) in CI. Half the composition defects this adapter exists
to avoid are only observable on a real server, and two of them made their own tests pass
green -- so the assertions here are deliberately about the PANE and about raw tmux output,
not about what the adapter believes it did.

Synchronization is always sentinel-and-poll (``tests/conftest.py``), never ``sleep``.
"""

from __future__ import annotations

import os

import pytest
from conftest import TmuxServer, await_file, await_file_bytes, raw_reader, requires_tmux
from shellbox_mcp.errors import BadCwd, InvalidName, NotFound
from shellbox_mcp.tmux import LIST_FORMAT, TmuxAdapter

pytestmark = requires_tmux

# The raw-mode reader lives in conftest.py NEXT TO the canonical one, so the two cannot be
# quietly made to look alike -- which is the single way the split H4 oracle breaks (§11.3).
_raw_reader = raw_reader


# --------------------------------------------------------------------------------------
# §7.2 -- the create chain, verified where it matters: at the pane
# --------------------------------------------------------------------------------------


def test_create_chain_reaches_the_pane_and_a_second_create_succeeds(
    tmux_server: TmuxServer, tmp_path
) -> None:
    adapter = tmux_server.adapter()
    created = adapter.create("build", cwd=str(tmp_path), command=["sh"])
    assert created.created is True
    assert created.incarnation

    # 🔴 The PANE's history_limit is the only valid oracle. A pane fixes its limit at
    # creation, so the earlier form -- global set AFTER new-session -- left every pane at
    # tmux's 2000 default while `show-options -g` reported 20000 and the acceptance test
    # passed green.
    pane_limit = tmux_server.raw(
        "display-message", "-p", "-t", "=build:", "#{history_limit}"
    ).stdout_raw.strip()
    assert pane_limit == "20000", f"pane reported history_limit={pane_limit!r}"
    assert adapter.read("build").history_limit == 20000

    assert "screen-256color" in tmux_server.raw("show-options", "-g", "default-terminal").stdout_raw

    # The F1 regression: a SECOND create on the same server must succeed. This is the
    # assertion a global `window-size manual` fails, and it fails by killing the server and
    # taking the first session with it -- which is why one create is not enough to catch it.
    second = adapter.create("other", cwd=str(tmp_path), command=["sh"])
    assert second.created is True
    assert sorted(tmux_server.sessions()) == ["build", "other"]
    assert (
        tmux_server.raw(
            "display-message", "-p", "-t", "=other:", "#{history_limit}"
        ).stdout_raw.strip()
        == "20000"
    )


def test_the_session_is_created_bare_named_and_reachable(tmux_server: TmuxServer, tmp_path) -> None:
    """``-s`` takes a bare name, and the result is addressable through ``target()``.

    ``new-session -s '=build'`` succeeds too -- and creates a session literally NAMED
    ``=build``, which the adapter's own helper can then never reach.
    """
    adapter = tmux_server.adapter()
    adapter.create("build", cwd=str(tmp_path), command=["sh"])
    assert tmux_server.sessions() == ["build"]
    assert adapter.exists("build") is True


def test_incarnation_round_trips_non_empty_through_list_sessions(
    tmux_server: TmuxServer, tmp_path
) -> None:
    """Non-empty is the assertion. An empty incarnation is never a match.

    The earlier half-anchored ``set-option -t '=<name>'`` returned rc=1 and stored nothing,
    so the incarnation was ``""``, and the restart/concurrency tests compared ``"" == ""``
    and reported green over a completely inert mechanism.
    """
    adapter = tmux_server.adapter()
    created = adapter.create("build", cwd=str(tmp_path), command=["sh"])

    raw = tmux_server.raw("list-sessions", "-F", LIST_FORMAT).stdout_raw
    assert created.incarnation in raw
    assert len(created.incarnation) == 36

    listed = adapter.list_sessions()
    assert len(listed) == 1
    assert listed[0].incarnation == created.incarnation
    assert listed[0].incarnation, "an empty incarnation must never be treated as a match"
    assert listed[0].foreign is False
    assert listed[0].stamped_cwd == os.path.realpath(str(tmp_path))
    assert listed[0].cwd  # pane_current_path, read per session


def test_env_survives_lf_and_semicolon_values(tmux_server: TmuxServer, tmp_path) -> None:
    """``-e K=V`` with an LF and a ``;`` in the value, read back FROM THE PANE.

    One ``echo`` per variable, deliberately: BSD ``printenv`` accepts only ONE variable name
    and silently ignores the rest, which once looked like a macOS/Linux tmux difference and
    was not.
    """
    out = str(tmp_path / "env-readback")
    adapter = tmux_server.adapter()
    adapter.create(
        "build",
        cwd=str(tmp_path),
        env={"FOO": "a\nb", "BAR": "x;y"},
        command=["sh", "-c", f'echo "$FOO" > {out}; echo "$BAR" >> {out}'],
    )
    contents = await_file(out, lambda data: data.count(b"\n") >= 3, what="both env readbacks")
    assert contents == b"a\nb\nx;y\n"


def test_dimensions_are_applied(tmux_server: TmuxServer, tmp_path) -> None:
    adapter = tmux_server.adapter()
    adapter.create("build", cwd=str(tmp_path), cols=100, rows=40, command=["sh"])
    listed = adapter.list_sessions()[0]
    assert (listed.cols, listed.rows) == (100, 40)


# --------------------------------------------------------------------------------------
# The 8-field format, stamped and unstamped
# --------------------------------------------------------------------------------------


def test_list_format_yields_eight_raw_fields(tmux_server: TmuxServer, tmp_path) -> None:
    adapter = tmux_server.adapter()
    adapter.create("build", cwd=str(tmp_path), command=["sh"])
    raw = tmux_server.raw("list-sessions", "-F", LIST_FORMAT).stdout_raw
    line = raw.split("\n")[0]
    assert len(line.split("\t")) == 8


def test_an_unstamped_session_yields_eight_raw_fields_with_two_empty(
    tmux_server: TmuxServer, tmp_path
) -> None:
    """A foreign session: 8 raw fields, the last two empty -- and 6 after ``.strip()``.

    Both halves matter and they are LAYERED, not alternatives. The field count is 8 whether
    or not an incarnation is present, so the count check cannot detect a missing
    incarnation; and a parser that stripped first would see 6, drop the record, and silently
    discard exactly the sessions orphan reconciliation exists to find.
    """
    tmux_server.raw(
        "new-session", "-d", "-s", "foreign", "-x", "80", "-y", "24", "-c", str(tmp_path), "sh"
    )
    raw = tmux_server.raw("list-sessions", "-F", LIST_FORMAT).stdout_raw
    line = raw.split("\n")[0]
    fields = line.split("\t")
    assert len(fields) == 8, f"raw fields: {fields!r}"
    assert fields[6] == "" and fields[7] == ""
    assert len(line.strip().split("\t")) == 6, "the strip() trap, measured here too"

    # The adapter must still SEE it -- and must call it foreign rather than owned.
    listed = tmux_server.adapter().list_sessions()
    assert [s.tmux_name for s in listed] == ["foreign"]
    assert listed[0].incarnation is None
    assert listed[0].foreign is True


def test_the_record_parses_when_the_process_has_no_locale(
    tmux_server: TmuxServer, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """🔴 The regression test for the defect W2 nearly shipped, end to end on real tmux.

    With a non-UTF-8 ctype locale, tmux encodes the TAB in format output as ``_`` -- in
    ``list-sessions -F`` as well as ``display-message`` -- so all eight fields collapse into
    one, every record is dropped as malformed, and ``shell_list`` returns an EMPTY inventory
    on a host full of live sessions. Orphan reconciliation then marks every one ``orphaned``.

    A locale is normally ABSENT in a container, a systemd unit and a sandbox, which is exactly
    where shellbox runs -- and it was invisible to every earlier measurement because those
    invoked tmux with a developer's full environment. Passing ``LANG`` through does not fix it;
    the adapter forces ``LC_CTYPE``.

    This test deletes every locale variable from the process, which is the sandbox's condition.
    """
    for var in ("LANG", "LC_ALL", "LC_CTYPE"):
        monkeypatch.delenv(var, raising=False)

    # Built AFTER the env is stripped: SubprocessRunner snapshots the environment.
    adapter = tmux_server.adapter()
    created = adapter.create("build", cwd=str(tmp_path), command=["sh"])

    listed = adapter.list_sessions()
    assert [s.tmux_name for s in listed] == ["build"]
    assert listed[0].incarnation == created.incarnation
    assert listed[0].cwd
    assert adapter.read("build").history_limit == 20000
    assert adapter.send("build", keys=["Enter"]).incarnation == created.incarnation

    # And the raw form, to show what the adapter is protecting against: the same tmux, the
    # same server, invoked WITHOUT the forced locale, mangles the separator.
    import subprocess

    raw = subprocess.run(
        [
            tmux_server.tmux_bin,
            "-S",
            tmux_server.socket_path,
            "-f",
            "/dev/null",
            "list-sessions",
            "-F",
            LIST_FORMAT,
        ],
        capture_output=True,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "TERM": "xterm-256color"},
    ).stdout.decode()
    assert "\t" not in raw, "expected the un-forced locale to mangle the TAB"
    assert len(raw.split("\n")[0].split("\t")) == 1


def test_a_foreign_session_cannot_be_mutated(tmux_server: TmuxServer, tmp_path) -> None:
    """The §9.2 stamp window as a DEFINED state: empty incarnation => ``not_found``."""
    tmux_server.raw("new-session", "-d", "-s", "foreign", "-x", "80", "-y", "24", "sh")
    adapter = tmux_server.adapter()
    for call in (
        lambda: adapter.send("foreign", text="rm -rf /\n"),
        lambda: adapter.resize("foreign", 100, 40),
        lambda: adapter.kill("foreign"),
    ):
        with pytest.raises(NotFound):
            call()
    # Untouched: still there, still its original size.
    assert tmux_server.sessions() == ["foreign"]


# --------------------------------------------------------------------------------------
# §7.1's adapter-level assertions (the raw-tmux half is in test_raw_tmux_targeting.py)
# --------------------------------------------------------------------------------------


def test_a_prefix_name_is_not_found_not_a_silent_hit(tmux_server: TmuxServer, tmp_path) -> None:
    """``bui`` with only ``build`` present: ``not_found``.

    Raw tmux would resolve ``-t bui`` to ``build`` by prefix and send into it, or kill it.
    The anchored form is what turns that into an honest miss.
    """
    adapter = tmux_server.adapter()
    adapter.create("build", cwd=str(tmp_path), command=["sh"])
    for call in (
        lambda: adapter.send("bui", text="echo pwned\n"),
        lambda: adapter.read("bui"),
        lambda: adapter.resize("bui", 100, 40),
    ):
        with pytest.raises(NotFound):
            call()
    assert adapter.kill("bui") is False, "an absent session is an idempotent no-op"
    assert tmux_server.sessions() == ["build"], "`build` must be untouched"
    assert adapter.list_sessions()[0].cols == 80, "and unresized"


def test_an_anchored_name_is_invalid_name_and_never_reaches_tmux(
    tmux_server: TmuxServer, tmp_path
) -> None:
    """``=bui`` is ``invalid_name``, not ``not_found`` -- the other half of the split."""
    adapter = tmux_server.adapter()
    adapter.create("build", cwd=str(tmp_path), command=["sh"])
    for name in ("=bui", "=build", "=build:"):
        with pytest.raises(InvalidName):
            adapter.send(name, text="x\n")
        with pytest.raises(InvalidName):
            adapter.kill(name)
    assert tmux_server.sessions() == ["build"]


# --------------------------------------------------------------------------------------
# display-message
# --------------------------------------------------------------------------------------


def test_display_message_returns_rc_zero_for_a_missing_target(tmux_server: TmuxServer) -> None:
    """The finding no prose review caught, re-asserted against real tmux.

    ``display-message`` is the only targeting verb that returns rc=0 for a target that does
    not exist, and §7.4 uses it to read cwd and liveness. An rc check on it is worthless;
    empty output is the signal.
    """
    tmux_server.raw("new-session", "-d", "-s", "build", "-x", "80", "-y", "24", "sh")
    probe = tmux_server.raw("display-message", "-p", "-t", "=bui:", "#{session_name}")
    assert probe.rc == 0
    assert probe.stdout_raw.strip() == ""

    # And the case that breaks the naive rule: with more than one field, the placeholders
    # expand empty but the literal TAB survives, so stdout is NOT empty for a session that
    # does not exist. Hence every format leads with #{session_name}.
    multi = tmux_server.raw(
        "display-message", "-p", "-t", "=bui:", "#{history_limit}\t#{pane_dead}"
    )
    assert multi.rc == 0
    assert multi.stdout_raw.strip("\n") != "", "the literal separator survives"
    assert multi.stdout_raw.replace("\t", "").strip() == ""

    with pytest.raises(NotFound):
        tmux_server.adapter().read("bui")


# --------------------------------------------------------------------------------------
# cwd validation, at the boundary
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("char", ["\t", "\r", "\n"])
def test_a_cwd_with_a_record_breaking_character_is_rejected_before_tmux(
    tmux_server: TmuxServer, tmp_path, char: str
) -> None:
    directory = tmp_path / f"a{char}b"
    directory.mkdir()
    with pytest.raises(BadCwd):
        tmux_server.adapter().create("build", cwd=str(directory))
    # Nothing was created, and no server was even started.
    assert tmux_server.sessions() == []


def test_the_cwd_hazard_is_real(tmux_server: TmuxServer, tmp_path) -> None:
    """Prove the guard is necessary by reproducing what it prevents, with raw tmux.

    Without this, the validation above looks like defensive padding and the next person
    removes it. A TAB breaks the field count (the record is dropped -- and a dropped record
    marks a LIVE session ``orphaned``); an LF keeps 8 fields on line 1 with a silently
    TRUNCATED cwd, so it passes validation carrying wrong data.
    """
    tmux_server.raw("new-session", "-d", "-s", "build", "-x", "80", "-y", "24", "sh")

    tmux_server.raw("set-option", "-t", "=build:", "@shellbox_cwd", str(tmp_path / "a\tb"))
    tabbed = tmux_server.raw("list-sessions", "-F", LIST_FORMAT).stdout_raw.split("\n")[0]
    assert len(tabbed.split("\t")) == 9, "a TAB adds a field"

    tmux_server.raw("set-option", "-t", "=build:", "@shellbox_cwd", str(tmp_path / "a\nb"))
    raw = tmux_server.raw("list-sessions", "-F", LIST_FORMAT).stdout_raw
    lines = [line for line in raw.split("\n") if line]
    assert len(lines) == 2, "an LF adds a spurious record"
    assert len(lines[0].split("\t")) == 8, "while line 1 still LOOKS well formed"
    assert lines[0].split("\t")[7] != str(tmp_path / "a\nb"), "carrying a truncated cwd"


# --------------------------------------------------------------------------------------
# send / read / resize / kill
# --------------------------------------------------------------------------------------


def test_send_text_is_byte_exact_at_the_pane_process(tmux_server: TmuxServer, tmp_path) -> None:
    """Byte-exactness is read from the FILE the pane process wrote, not from capture-pane.

    A screen scrape is not an oracle for delivered bytes: the pane renders, wraps and
    normalises. (W3 owns the full byte-exactness matrix; this asserts the plumbing works,
    including the lone ``;`` that ``send-keys -l`` silently swallows.)
    """
    out = str(tmp_path / "delivered")
    adapter = tmux_server.adapter()
    adapter.create("build", cwd=str(tmp_path), command=_raw_reader(out))

    payload = "a;b ; -n é\n"
    sent = adapter.send("build", text=payload)
    assert sent.submitted_bytes == len(payload.encode())
    assert sent.incarnation
    assert sent.delivery == "unverified"
    assert await_file_bytes(out, len(payload.encode())) == payload.encode()


def test_send_keys_reaches_the_pane(tmux_server: TmuxServer, tmp_path) -> None:
    out = str(tmp_path / "keys")
    adapter = tmux_server.adapter()
    adapter.create("build", cwd=str(tmp_path), command=_raw_reader(out))
    adapter.send("build", text="hello", keys=["Enter"])
    # Text first, then keys -- the ordering the schema guarantees.
    assert await_file_bytes(out, 6) == b"hello\n"


def test_send_leaves_no_buffer_behind(tmux_server: TmuxServer, tmp_path) -> None:
    """``paste-buffer -d`` drops the payload, so agent input does not linger server-side.

    ``buffer-limit`` is 50 server-wide across every pooled agent, so a leaked buffer would
    both evict another agent's buffers and retain arbitrary agent input.
    """
    out = str(tmp_path / "delivered")
    adapter = tmux_server.adapter()
    adapter.create("build", cwd=str(tmp_path), command=_raw_reader(out))
    adapter.send("build", text="hi\n")
    await_file_bytes(out, 3)
    assert tmux_server.raw("list-buffers").stdout_raw.strip() == ""


def test_read_preserves_ansi(tmux_server: TmuxServer, tmp_path) -> None:
    """``capture-pane -p -e``: the escapes xterm.js needs, which stripping loses silently."""
    adapter = tmux_server.adapter()
    marker = str(tmp_path / "printed")
    adapter.create(
        "build",
        cwd=str(tmp_path),
        command=["sh", "-c", f"printf '\\033[31mRED\\033[0m\\n'; touch {marker}; sleep 30"],
    )
    await_file(marker, lambda _: True, what="the pane to print")
    content = adapter.read("build").content
    assert "RED" in content
    assert "\x1b[31m" in content, "ANSI must be preserved (-e)"


def test_read_reports_a_dead_pane_and_keeps_its_output(tmux_server: TmuxServer, tmp_path) -> None:
    """``remain-on-exit on`` is what keeps a finished process's last output readable.

    Without it the session is destroyed the moment the pane's process exits, the server
    exits, and every later call fails ``no server running`` -- losing the output. The cost is
    that ``has-session`` no longer implies "alive", so liveness reads ``#{pane_dead}``.

    Note ``lines=100``: tmux's own "Pane is dead" banner pushes the final output into
    scrollback, so the visible pane alone is not where it ends up.
    """
    adapter = tmux_server.adapter()
    adapter.create("build", cwd=str(tmp_path), command=["sh", "-c", "printf 'LASTLINE\\n'"])

    def dead() -> bool:
        return adapter.read("build").alive is False

    from conftest import await_condition

    await_condition(dead, what="the pane to die")
    read = adapter.read("build", lines=100)
    assert read.alive is False
    assert "LASTLINE" in read.content
    assert adapter.exists("build") is True, "has-session still reports it: existence != liveness"


def test_resize(tmux_server: TmuxServer, tmp_path) -> None:
    adapter = tmux_server.adapter()
    adapter.create("build", cwd=str(tmp_path), command=["sh"])
    assert adapter.resize("build", 100, 30) == (100, 30)
    assert (
        tmux_server.raw(
            "list-sessions", "-F", "#{session_name}\t#{window_width}x#{window_height}"
        ).stdout_raw.strip()
        == "build\t100x30"
    )
    assert (adapter.read("build").cols, adapter.read("build").rows) == (100, 30)


def test_kill_is_idempotent(tmux_server: TmuxServer, tmp_path) -> None:
    """A kill race must not produce a spurious error for the loser.

    Killing the last session exits the server, so the second call's stderr is
    ``no server running`` rather than ``can't find session`` -- both mean "gone".
    """
    adapter = tmux_server.adapter()
    adapter.create("build", cwd=str(tmp_path), command=["sh"])
    assert adapter.kill("build") is True
    assert adapter.kill("build") is False
    assert adapter.exists("build") is False


def test_no_server_is_an_empty_list_not_an_error(tmux_server: TmuxServer) -> None:
    """A socket with no server behind it: ``shell_list`` succeeds with nothing in it."""
    adapter = tmux_server.adapter()
    assert adapter.list_sessions() == []
    assert adapter.exists("build") is False


def test_socket_path_is_validated_when_the_adapter_is_constructed(tmp_path) -> None:
    from shellbox_mcp.errors import SocketPathTooLong
    from shellbox_mcp.naming import max_socket_path_bytes

    over_limit = "/tmp/" + "s" * max_socket_path_bytes()
    with pytest.raises(SocketPathTooLong):
        TmuxAdapter(tmux_server_config(over_limit))


def tmux_server_config(socket_path: str):
    from shellbox_mcp.tmux import TmuxConfig

    return TmuxConfig(socket_path=socket_path)
