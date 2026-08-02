"""W15's attach support against a REAL tmux server: the freeze, and the liveness probe.

The unit lane asserts the argv this code builds. This asserts what tmux does with it, which
is where every composition defect in this adapter has lived -- two of them made their own
tests pass green.

WARNING: **This file does not attach a client.** Spawning ``tmux attach`` needs a pty, and the
forkpty discipline (fork gate, ``os.execve``, child reaping, ``TIOCSWINSZ``) is `W19`'s
deliverable, not this one. The attach-side properties are measured in
[`spike/tmux_spike.py`](../../spike/tmux_spike.py) -- which is the repo's oracle for a tmux
form by convention, and which `make test-tmux` runs FIRST for that reason. `F16` covers the
size holding under a live 120x40 client over 1714 samples, and `F19` covers `#{pane_dead}` in
both directions with a client attached.

What is left for this file is the half the spike cannot speak for: that the SHIPPED adapter
issues those forms, that the option lands at the scope it claims, and that the mitigation does
not break the tools already built on ``resize-window``.
"""

from __future__ import annotations

import pytest
from conftest import TmuxServer, await_condition, requires_tmux
from shellbox_mcp.errors import NotFound

pytestmark = requires_tmux


# --------------------------------------------------------------------------------------
# freeze_window_size -- the scope, and the server that must survive it
# --------------------------------------------------------------------------------------


def test_the_freeze_lands_on_the_window_and_not_on_the_server(
    tmux_server: TmuxServer, tmp_path
) -> None:
    """The scope IS the variable (F1/F9). Read back at both scopes, because only one is safe.

    A per-window option that silently applied globally would pass every functional test in
    this file and then kill the server on some other agent's ``shell_create``.
    """
    adapter = tmux_server.adapter()
    adapter.create("build", cwd=str(tmp_path), command=["sh"])

    adapter.freeze_window_size("build")

    per_window = tmux_server.raw("show-options", "-w", "-t", "=build:", "window-size")
    assert per_window.stdout_raw.strip() == "window-size manual"

    global_scope = tmux_server.raw("show-options", "-g", "window-size").stdout_raw.strip()
    assert global_scope != "window-size manual", (
        "the option reached GLOBAL scope. The next new-session on this server will SIGSEGV in "
        "clients_calculate_size -- 15/15, both lanes -- taking every pooled agent's sessions."
    )


def test_a_second_create_still_succeeds_after_a_window_has_been_frozen(
    tmux_server: TmuxServer, tmp_path
) -> None:
    """THE F1 regression, in the one place Phase 3 makes it reachable again.

    The global form's failure lands on the *second* create, not the first, which is what makes
    it so expensive: by then other pooled agents hold sessions on this server. One create
    cannot catch it, so this test creates, freezes, and creates again -- the exact sequence a
    sandbox performs when one agent opens a browser and another starts work.
    """
    adapter = tmux_server.adapter()
    adapter.create("build", cwd=str(tmp_path), command=["sh"])
    adapter.freeze_window_size("build")

    second = adapter.create("other", cwd=str(tmp_path), command=["sh"])

    assert second.created is True
    assert sorted(tmux_server.sessions()) == ["build", "other"]


def test_freezing_one_window_does_not_freeze_another(
    tmux_server: TmuxServer, tmp_path
) -> None:
    """Per-window means per window. A viewer on one session must not pin another's size."""
    adapter = tmux_server.adapter()
    adapter.create("watched", cwd=str(tmp_path), command=["sh"])
    adapter.create("untouched", cwd=str(tmp_path), command=["sh"])

    adapter.freeze_window_size("watched")

    other = tmux_server.raw("show-options", "-w", "-t", "=untouched:", "window-size")
    assert other.stdout_raw.strip() != "window-size manual"


def test_resize_still_works_while_the_size_is_frozen(tmux_server: TmuxServer, tmp_path) -> None:
    """F16 consequence 4: the mitigation must not break a shipped tool.

    ``shell_resize`` is built on ``resize-window``, and an option that silently froze it would
    be a regression introduced BY the fix. Measured rc=0 in both lanes; asserted here through
    the adapter, at the pane.

    Note the size does not revert when the last client detaches. That is what ``manual`` means,
    and it is the intended behavior rather than a leak.
    """
    adapter = tmux_server.adapter()
    adapter.create("build", cwd=str(tmp_path), command=["sh"])
    adapter.freeze_window_size("build")

    assert adapter.resize("build", 100, 30) == (100, 30)
    assert (adapter.read("build").cols, adapter.read("build").rows) == (100, 30)


def test_the_freeze_is_idempotent(tmux_server: TmuxServer, tmp_path) -> None:
    """A publisher reconnects every 10 to 18 minutes and freezes on each attach.

    So this runs thousands of times over a session's life, and it must be a no-op after the
    first.
    """
    adapter = tmux_server.adapter()
    adapter.create("build", cwd=str(tmp_path), command=["sh"])

    for _ in range(3):
        adapter.freeze_window_size("build")

    assert adapter.resize("build", 90, 28) == (90, 28)


def test_the_freeze_refuses_a_session_shellbox_does_not_own(
    tmux_server: TmuxServer, tmp_path
) -> None:
    """A session carrying no incarnation is foreign. Changing its window size is not shellbox's
    to do, and the refusal is the same one ``kill`` gives."""
    tmux_server.raw("new-session", "-d", "-s", "foreign", "-c", str(tmp_path), "sh")
    adapter = tmux_server.adapter()

    with pytest.raises(NotFound):
        adapter.freeze_window_size("foreign")

    left = tmux_server.raw("show-options", "-w", "-t", "=foreign:", "window-size")
    assert left.stdout_raw.strip() != "window-size manual"


def test_prepare_attach_returns_an_argv_that_a_real_tmux_accepts(
    tmux_server: TmuxServer, tmp_path
) -> None:
    """The argv is well-formed against a real server, checked without spawning a client.

    ``attach`` needs a pty, so it cannot be run here (see this module's docstring). But
    ``has-session`` takes the identical target, so the same anchored ``-t`` can be proven to
    resolve -- which is the half of "is this argv right" that does not need a terminal.
    """
    adapter = tmux_server.adapter()
    adapter.create("build", cwd=str(tmp_path), command=["sh"])

    argv = adapter.prepare_attach("build")

    assert argv[-3:] == ["attach", "-t", "=build:"]
    assert tmux_server.raw("has-session", "-t", argv[-1]).rc == 0
    assert tmux_server.raw("has-session", "-t", "=buil:").rc != 0, (
        "the anchored form must not prefix-match, or an attach could reach another session"
    )


# --------------------------------------------------------------------------------------
# pane_dead -- the detach-versus-dead distinction, minus the detach half
# --------------------------------------------------------------------------------------


def test_pane_dead_is_false_for_a_live_pane(tmux_server: TmuxServer, tmp_path) -> None:
    adapter = tmux_server.adapter()
    adapter.create("build", cwd=str(tmp_path), command=["sh"])

    assert adapter.pane_dead("build") is False


def test_pane_dead_becomes_true_when_the_process_exits_and_the_session_survives(
    tmux_server: TmuxServer, tmp_path
) -> None:
    """F19, through the shipped path. ``remain-on-exit on`` is why both halves are true at once.

    The session outliving its process is deliberate -- it keeps the final output readable -- and
    the cost is that ``has-session`` stops meaning "alive". This is the assertion that
    ``pane_dead`` picks up that cost rather than leaving it to a caller.
    """
    adapter = tmux_server.adapter()
    adapter.create("build", cwd=str(tmp_path), command=["sh", "-c", "printf 'DONE\\n'"])

    await_condition(lambda: adapter.pane_dead("build") is True, what="the pane to die")

    assert adapter.exists("build") is True, "has-session still says yes: existence != liveness"
    assert adapter.read("build", lines=100).alive is False, "and read() must agree"


def test_pane_dead_reports_none_for_a_session_that_does_not_exist(
    tmux_server: TmuxServer, tmp_path
) -> None:
    """The third state, and the one ``display-message`` makes easy to get wrong.

    It returns **rc=0 for every nonexistent target** (F6), and a multi-field format's literal
    separators survive the empty expansion -- so a missing session prints ``"\\t\\n"``, which is
    non-empty stdout. Leading with ``#{session_name}`` is what keeps "gone" distinguishable
    from "alive", and reading the empty field as ``0`` would report a vanished session as live.
    """
    adapter = tmux_server.adapter()
    adapter.create("build", cwd=str(tmp_path), command=["sh"])

    assert adapter.pane_dead("nonexistent") is None


def test_pane_dead_answers_for_a_foreign_session_rather_than_raising(
    tmux_server: TmuxServer, tmp_path
) -> None:
    """Unlike ``read()``, which raises on a session carrying no incarnation.

    A liveness probe runs on the close path. Raising there would make a publisher's shutdown
    fail at the moment it is trying to be clean, and "I cannot prove I own this" is not an
    answer to "has the pane exited".
    """
    tmux_server.raw("new-session", "-d", "-s", "foreign", "-c", str(tmp_path), "sh")

    assert tmux_server.adapter().pane_dead("foreign") is False
