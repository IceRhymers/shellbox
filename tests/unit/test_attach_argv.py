"""T-ATTACH-ARGV -- the attach path's argv, environment, and ordering, with no tmux involved.

Three things the unit lane can assert here that a real-tmux test cannot: the exact argv
shellbox BUILDS, the ORDER two commands are issued in, and that some inputs cause no tmux
invocation at all. The tmux lane re-asserts the behavior against a real server; this file
asserts the construction.

The measurements behind these assertions are in `spike/FINDINGS.md`, and the two that decide
the shape of this code are worth naming here:

* **F16** -- per-window ``window-size manual`` holds a window's size under a live attached
  client on tmux 3.4, and the control row (no option) REFLOWS. So the freeze is required, and
  the ``before_attach`` placement's exposure window measured empty over 1714 samples.
* **F19** -- ``#{pane_dead}`` reads ``0`` on detach and ``1`` when the pane's process exits,
  while ``has-session`` returns rc=0 in both cases. That is the whole detach-versus-dead
  distinction, and it is why liveness cannot be ``has-session``.
"""

from __future__ import annotations

import pytest
from conftest import RecordingRunner, result
from shellbox_mcp.errors import InvalidName, NotFound, TmuxError
from shellbox_mcp.tmux import TmuxAdapter, TmuxConfig, client_env

SOCKET = "/tmp/sbx-attach.sock"
INCARNATION = "11111111-2222-4333-8444-555555555555"


def adapter(runner: RecordingRunner, **overrides: object) -> TmuxAdapter:
    settings: dict[str, object] = {"socket_path": SOCKET}
    settings.update(overrides)
    return TmuxAdapter(TmuxConfig(**settings), runner=runner)  # type: ignore[arg-type]


def owned() -> RecordingRunner:
    """A runner whose session resolves and carries an incarnation shellbox owns."""
    return RecordingRunner(default=result(stdout=f"build\t{INCARNATION}\n"))


# --------------------------------------------------------------------------------------
# attach_argv -- the pure builder
# --------------------------------------------------------------------------------------


def test_attach_argv_is_the_base_argv_plus_an_anchored_attach() -> None:
    """T-ATTACH-ARGV. ``-S <socket>`` and ``-f /dev/null`` come along, and the target is anchored.

    omnigent's bridge passes ``-t tmux_target`` unanchored (``ws_bridge.py:492``) and spawns a
    bare ``"tmux"``. Both are what this argv exists to not do: without ``-S`` it would talk to
    the DEFAULT tmux server rather than shellbox's private socket, and without ``-f /dev/null``
    it would inherit a ``~/.tmux.conf`` that can rebind keys under the renderer.
    """
    runner = RecordingRunner()

    argv = adapter(runner).attach_argv("build")

    assert argv == ["tmux", "-S", SOCKET, "-f", "/dev/null", "attach", "-t", "=build:"]
    assert runner.calls == [], "attach_argv is a pure builder and must invoke nothing"


def test_attach_argv_uses_the_configured_binary() -> None:
    """T-ATTACH-ARGV. ADR-1: the binary is resolved in one place."""
    argv = adapter(RecordingRunner(), tmux_bin="/opt/tmux/bin/tmux").attach_argv("build")

    assert argv[0] == "/opt/tmux/bin/tmux"


def test_attach_argv_rejects_a_bad_session_name_without_invoking_tmux() -> None:
    """T-ATTACH-ARGV. Validation is at the boundary, before anything is spawned."""
    runner = RecordingRunner()

    with pytest.raises(InvalidName):
        adapter(runner).attach_argv("../etc/passwd")

    assert runner.calls == []


# --------------------------------------------------------------------------------------
# attach_env -- TERM describes the far end
# --------------------------------------------------------------------------------------


def test_attach_env_forces_a_term_that_tmux_will_accept() -> None:
    """T-ATTACH-ARGV. MEASURED (F17): ``tmux attach`` under ``TERM=dumb`` is refused outright.

    ``open terminal failed: terminal does not support clear``, the child exits, zero clients
    attach -- both lanes. A headless host has no tty so bash substitutes ``TERM=dumb``, which
    makes inheriting the environment the failing case rather than the safe one. The renderer
    would then paint that error message instead of the pane.
    """
    env = adapter(RecordingRunner()).attach_env()

    assert env["TERM"] == "xterm-256color"
    assert env["TERM"] != "dumb"


def test_the_attach_child_gets_the_same_environment_as_every_other_tmux_client() -> None:
    """T-ATTACH-ARGV. One construction, so the two cannot drift.

    An attach IS a tmux client. The property that matters most here is ``LC_CTYPE``: without a
    UTF-8 ctype locale tmux visually encodes TABs in format output, which collapses the whole
    8-field record into one and makes ``shell_list`` report an empty inventory.
    """
    config = TmuxConfig(socket_path=SOCKET)

    assert adapter(RecordingRunner()).attach_env() == client_env(config)
    assert "LC_ALL" not in adapter(RecordingRunner()).attach_env()


# --------------------------------------------------------------------------------------
# freeze_window_size -- the scope that must never be global
# --------------------------------------------------------------------------------------


def test_freeze_window_size_sets_the_per_window_scope_against_an_anchored_target() -> None:
    """T-ATTACH-ARGV. The exact form measured safe at 0/15, and no other.

    The global form SIGSEGVs the server on the NEXT ``new-session`` -- 15/15 in both lanes --
    and it lands on the second create, by which time other pooled agents hold sessions on that
    server. ``tests/unit/test_no_global_window_size.py`` enforces the shape structurally; this
    asserts the argv that actually reaches tmux.
    """
    runner = owned()

    adapter(runner).freeze_window_size("build")

    argv = runner.calls[-1][0]
    assert argv[-6:] == ("set-option", "-w", "-t", "=build:", "window-size", "manual")
    assert "-g" not in argv, "a global window-size destroys every pooled agent's sessions"


def test_freeze_window_size_refuses_a_session_shellbox_cannot_prove_it_owns() -> None:
    """T-ATTACH-ARGV. The same rule ``kill`` follows, for the same reason.

    A session carrying no incarnation is foreign or mid-create. Freezing a foreign session's
    window size would silently change another tool's terminal, so it is refused rather than
    attempted.
    """
    runner = RecordingRunner(default=result(stdout="build\t\n"))

    with pytest.raises(NotFound):
        adapter(runner).freeze_window_size("build")

    assert all("set-option" not in argv for argv, _ in runner.calls)


def test_freeze_window_size_reports_a_tmux_failure_rather_than_continuing() -> None:
    """T-ATTACH-ARGV. A silent failure here means the agent's pane reflows and nobody knows."""
    runner = RecordingRunner(
        respond=lambda argv: (
            result(rc=1, stderr="can't set option: window-size")
            if "set-option" in argv
            else result(stdout=f"build\t{INCARNATION}\n")
        )
    )

    with pytest.raises(TmuxError):
        adapter(runner).freeze_window_size("build")


# --------------------------------------------------------------------------------------
# prepare_attach -- the ordering IS the method
# --------------------------------------------------------------------------------------


def test_prepare_attach_freezes_the_window_before_it_hands_back_the_argv() -> None:
    """T-ATTACH-ARGV. Order first, argv second.

    F16 consequence 3 measured what the other order costs: setting the option once the client
    is live reflows the agent's pane to the viewer's size and then reverts it. Self-healing,
    but visible -- and entirely avoidable by doing it first. The publisher cannot get this
    wrong if it calls one method, which is the reason this method exists.
    """
    runner = owned()

    argv = adapter(runner).prepare_attach("build")

    issued = [call[0] for call in runner.calls]
    assert any("set-option" in call for call in issued), "the freeze must have happened"
    assert argv == ["tmux", "-S", SOCKET, "-f", "/dev/null", "attach", "-t", "=build:"]
    assert not any("attach" in call for call in issued), (
        "prepare_attach returns the argv for the caller to fork; it must not spawn the client "
        "itself, which happens on a pty in the publisher"
    )


def test_prepare_attach_refuses_an_unowned_session_before_freezing_anything() -> None:
    """T-ATTACH-ARGV. shellbox does not attach a pty to a session it cannot prove it owns."""
    runner = RecordingRunner(default=result(stdout=""))

    with pytest.raises(NotFound):
        adapter(runner).prepare_attach("build")


# --------------------------------------------------------------------------------------
# pane_dead -- detach versus a dead pane
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("stdout", "expected"),
    [("build\t0\n", False), ("build\t1\n", True), ("", None), ("\t\n", None)],
    ids=["alive", "dead", "no-output", "unresolved-target-literals"],
)
def test_pane_dead_reports_the_three_states_it_has_to_distinguish(
    stdout: str, expected: bool | None
) -> None:
    """T-ATTACH-ARGV. Dead, alive, and "the session did not resolve" are three answers.

    The last two must not collapse. ``display-message`` returns **rc=0 for every nonexistent
    target** (spike F6), and for a multi-field format the placeholders expand empty while the
    literal separators survive -- so ``"\\t\\n"`` is what a missing session prints. Leading the
    format with ``#{session_name}`` is what makes that distinguishable, and a probe that read
    the empty field as ``0`` would report a vanished session as alive.
    """
    runner = RecordingRunner(default=result(stdout=stdout))

    assert adapter(runner).pane_dead("build") is expected


def test_pane_dead_reads_the_shipped_display_message_path_and_not_list_panes() -> None:
    """T-ATTACH-ARGV. Not a new tmux form -- ``#{pane_dead}`` is already read three ways.

    Principle 5 says a new command form goes into the spike first. ``list-panes`` would be
    one, and it is unnecessary: the field is already in ``LIST_FIELDS`` and ``_READ_FIELDS``,
    and ``read()`` already returns it as ``ReadResult.alive``.

    F21 makes the choice of read path load-bearing in the neighbouring case: ``show-options
    -v`` reports an ABSENT value as rc=1, indistinguishable from a real failure, while the
    shipped ``display-message`` path reports it as an empty field at rc=0.
    """
    runner = RecordingRunner(default=result(stdout="build\t0\n"))

    adapter(runner).pane_dead("build")

    argv = runner.calls[-1][0]
    assert "display-message" in argv
    assert "list-panes" not in argv
    assert argv[-1] == "#{session_name}\t#{pane_dead}", (
        "the format must lead with #{session_name}, or a missing session's surviving TAB "
        "separators parse as a real record"
    )


def test_pane_dead_costs_one_round_trip_and_no_capture_pane() -> None:
    """T-ATTACH-ARGV. Why this is its own accessor rather than a call to ``read()``.

    ``read()`` also performs a ``capture-pane`` and raises ``NotFound`` on a session carrying
    no incarnation. A liveness probe wants neither: it runs on every close, and it must be able
    to report "gone" rather than raise.
    """
    runner = RecordingRunner(default=result(stdout="build\t0\n"))

    adapter(runner).pane_dead("build")

    assert len(runner.calls) == 1
    assert all("capture-pane" not in argv for argv, _ in runner.calls)


def test_pane_dead_does_not_raise_on_a_session_carrying_no_incarnation() -> None:
    """T-ATTACH-ARGV. The distinction from ``read()``, asserted.

    An unstamped session is foreign or mid-create. A liveness probe reports what tmux says
    about the pane rather than refusing to answer -- refusing would make the publisher's close
    path raise at exactly the moment it is trying to shut down cleanly.
    """
    runner = RecordingRunner(default=result(stdout="build\t0\n"))

    assert adapter(runner).pane_dead("build") is False
