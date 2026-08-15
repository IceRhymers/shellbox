"""`W51` -- measure the activity signal on the CI-pinned tmux, inside `tests/tmux/` (plan `ADR-35`).

**Version note, stated per the task's own instruction:** the machine this module was written on
reports ``tmux -V`` == ``tmux 3.6b``, not the CI-pinned ``tmux 3.4`` (`.github/workflows/ci.yml`'s
``tmux-3-4`` job, `ubuntu:24.04`). Every arm below therefore only becomes load-bearing for `W51`'s
purpose when it runs GREEN inside that container job -- `make test-tmux` is what enforces the
version (see the guard immediately below ``pytest tests/tmux`` in the ``Makefile``). A local run on
3.6b is still useful signal (it is the same major/minor lineage the original three findings were
measured on), but it does not by itself satisfy the gate.

This module re-runs `Finding 1`, `Finding 2` and `Finding 3` from the (gitignored, untracked)
`.omc/plans/phase-5-activity-signal-measurement.md`, plus the four measurements `W51` grows beyond
that file (fresh-session reading, the `#{pane_dead}` scoping question, the dead-pane window-clock
question, and the attached-silent-client question). Re-establishing the measurement here, inside
`tests/tmux/`, is the durable fix the plan calls out: the source document is the SOLE evidence for
`Findings 1-3` and lives in a directory that has already destroyed three other documents.

Seven arms, each with a criterion (plan `A36`):

* **Arm 1 -- GATE.** `#{session_activity}` does NOT advance through several seconds of continuous
  unattended output on a session-level target; it is still an attach-event clock. This is the arm
  that overturns what BOTH prior design/review passes recommended.
* **Arm 2 -- GATE.** `#{window_activity}` DOES advance on unattended output, with a silent
  unattended control session staying frozen through the same window.
* **Arm 3 -- GATE.** On a SESSION target, `#{window_activity}` resolves to the active window only;
  a background window's output is invisible to it, which is why `MAX` over windows (`W52`) is
  mandatory rather than an optimisation.
* **Arm 4 -- GATE.** A freshly created, never-used session's `#{window_activity}` reads within a
  few seconds of wall-clock `now`, not `0`/epoch.
* **Arm 5 -- RECORD ONLY, cannot fail this item.** What a session-target `#{pane_dead}` reads when
  the INACTIVE window's pane is dead (`remain-on-exit on`) and the active window's is alive.
* **Arm 6 -- GATE, the central one.** A dead pane's window clock does NOT advance across two
  samples taken a few seconds apart.
* **Arm 7 -- GATE.** An attached, SILENT client's window clock does NOT advance, compared against
  an unattended control that also produces no output.

If any GATE arm (1, 2, 3, 4, 6, 7) comes back contrary to its stated expectation, that is NOT a bug
in this test to be quietly patched around -- it means the reaping predicate in `ADR-28`/`ADR-35`
needs redesigning before `W41`/`W52` are written. The assertion messages below say what a contrary
result WOULD mean, for exactly that reason.

**Synchronization model (matches `tests/conftest.py` and `tests/tmux/test_tmux_adapter.py`): no
``sleep()``, ever.** Where a real number of wall-clock seconds legitimately has to pass -- because
the thing under test is a clock, and "did the clock move" is meaningless without letting time pass
-- this module lets a disposable tmux pane produce output on its OWN schedule (a shell loop with
its own ``sleep 1``) and polls that pane's file output with `conftest.await_file`/`await_condition`.
The wall-clock delay is therefore always a side effect of a poll succeeding, never of this Python
process calling ``time.sleep()``.
"""

from __future__ import annotations

import time
import uuid

from conftest import TmuxServer, await_condition, await_file, requires_tmux
from shellbox_mcp.attach import AttachedPty
from shellbox_mcp.tmux import TmuxAdapter

pytestmark = requires_tmux

# `Finding 2` detected `#{window_activity}` movement across an 8s window, and 6s more after that
# (`phase-5-activity-signal-measurement.md:50-60`). That is the granularity every timing-sensitive
# arm here needs, so every "let time pass" step in this module uses the same window.
_CLOCK_SECONDS = 8


def _unique(label: str) -> str:
    return f"{label}-{uuid.uuid4().hex[:8]}"


def _display(tmux_server: TmuxServer, target: str, fmt: str) -> str:
    result = tmux_server.raw("display-message", "-p", "-t", target, fmt)
    assert result.rc == 0, f"display-message failed for target {target!r}: {result.stderr}"
    return result.stdout_raw.strip()


def _session_activity(tmux_server: TmuxServer, name: str) -> int:
    return int(_display(tmux_server, f"={name}:", "#{session_activity}"))


def _window_activity(tmux_server: TmuxServer, target: str) -> int:
    return int(_display(tmux_server, target, "#{window_activity}"))


def _pane_dead(tmux_server: TmuxServer, target: str) -> str:
    return _display(tmux_server, target, "#{pane_dead}")


def _windows(tmux_server: TmuxServer, name: str) -> list[tuple[str, bool, int]]:
    """``(index, active, window_activity)`` for every window in session ``name``, freshly read."""
    result = tmux_server.raw(
        "list-windows",
        "-t",
        f"={name}:",
        "-F",
        "#{window_index}\t#{window_active}\t#{window_activity}",
    )
    assert result.rc == 0, f"list-windows failed for session {name!r}: {result.stderr}"
    rows: list[tuple[str, bool, int]] = []
    for line in result.stdout_raw.strip().split("\n"):
        if not line:
            continue
        index, active, activity = line.split("\t")
        rows.append((index, active == "1", int(activity)))
    return rows


def _ticking_producer(marker: str, ticks: int) -> list[str]:
    """A pane command that appends one line per second to ``marker``, redirected off the pane.

    For the disposable "clock" session only (`_advance_wall_clock`): that session's own
    `#{window_activity}` is never read, so its output is deliberately kept OFF the pane (a
    plain ``>>`` redirect) rather than teed onto it -- there is no reason to litter the clock
    session's pane with ticks nobody looks at. Bounded (not an infinite loop) so a hung poll
    still leaves the process exiting on its own.
    """
    return [
        "sh",
        "-c",
        f"i=0; while [ $i -lt {ticks + 6} ]; do i=$((i+1)); echo $i >> {marker}; sleep 1; done",
    ]


def _pane_output_producer(marker: str, ticks: int) -> list[str]:
    """A pane command that prints one line per second TO THE PANE, and mirrors it to ``marker``.

    Unlike `_ticking_producer`, this is for arms that measure whether pane OUTPUT moves an
    activity clock (arms 1, 2, 3) -- the output has to actually reach the pane's tty, so this
    tees rather than redirects: ``tee -a`` writes the line to its own stdout (the pane) AND
    appends it to ``marker``, which is what lets a poll on ``marker`` also prove the pane itself
    received output at that point.
    """
    return [
        "sh",
        "-c",
        f"i=0; while [ $i -lt {ticks + 6} ]; do i=$((i+1)); echo tick-$i | tee -a {marker}; "
        "sleep 1; done",
    ]


def _advance_wall_clock(
    tmux_server: TmuxServer, adapter: TmuxAdapter, tmp_path, seconds: int = _CLOCK_SECONDS
) -> None:
    """Let ``seconds`` of REAL wall-clock time pass, as a poll -- never a ``time.sleep()``.

    A disposable "clock" session appends one line per second to its own file; this polls that
    file for ``seconds`` lines to accumulate. The delay is produced entirely by the ticking
    pane's own ``sleep 1``, so from this process's point of view it is only ever polling
    (`await_file`), matching the module docstring's synchronization rule.
    """
    marker = tmp_path / f"{_unique('clock')}.tick"
    clock_name = _unique("clock")
    adapter.create(clock_name, cwd=str(tmp_path), command=_ticking_producer(str(marker), seconds))
    try:
        await_file(
            str(marker),
            lambda data: data.count(b"\n") >= seconds,
            timeout=seconds + 15,
            what=f"{seconds} clock ticks to accumulate",
        )
    finally:
        adapter.kill(clock_name)


def _attach(adapter: TmuxAdapter, name: str) -> AttachedPty:
    """The real composition: ``prepare_attach`` builds the argv, ``AttachedPty`` forks it.

    Mirrors ``tests/tmux/test_attach_pty.py``'s helper of the same name/shape.
    """
    return AttachedPty.spawn(adapter.prepare_attach(name), adapter.attach_env())


# --------------------------------------------------------------------------------------
# Arm 1 -- GATE. `#{session_activity}` is an attach-event clock, not an output clock.
# --------------------------------------------------------------------------------------


def test_arm1_session_activity_ignores_output_and_moves_only_on_attach(
    tmux_server: TmuxServer, tmp_path
) -> None:
    """Re-runs `Finding 1`. GATE: this is the option both prior review passes recommended.

    Two unattended sessions -- one producing continuous output, one silent (the control) -- must
    BOTH read `#{session_activity}` unchanged across `_CLOCK_SECONDS` of real time. If the noisy
    arm moves, `#{session_activity}` is output-aware after all on this tmux version, and `ADR-35`'s
    rejection of it needs to be revisited before `W41`/`W52` are written.

    The second half re-confirms the positive claim ("attach-event clock") the arm's name makes: a
    real attach must move the field, polled for rather than asserted after a fixed wait.
    """
    adapter = tmux_server.adapter()
    noisy = _unique("noisy")
    quiet = _unique("quiet")
    output_marker = tmp_path / "noisy.log"

    adapter.create(noisy, cwd=str(tmp_path), command=_pane_output_producer(str(output_marker), 30))
    adapter.create(quiet, cwd=str(tmp_path), command=["sh", "-c", "sleep 300"])

    t0_noisy = _session_activity(tmux_server, noisy)
    t0_quiet = _session_activity(tmux_server, quiet)

    # Confirm the noisy pane is actually producing output DURING the window we measure across --
    # otherwise a frozen reading would prove nothing.
    await_file(str(output_marker), lambda data: len(data) > 0, what="the noisy pane's first tick")
    _advance_wall_clock(tmux_server, adapter, tmp_path)

    t1_noisy = _session_activity(tmux_server, noisy)
    t1_quiet = _session_activity(tmux_server, quiet)

    assert t1_noisy == t0_noisy, (
        f"CONTRARY RESULT: #{{session_activity}} advanced ({t0_noisy} -> {t1_noisy}) through "
        "continuous unattended output. This is the fix both prior review passes recommended, and "
        "if it is now output-aware on this tmux version, ADR-35's rejection of it is wrong and "
        "the predicate needs to be redesigned before W41/W52."
    )
    assert t1_quiet == t0_quiet, (
        "control moved with no output and no attach -- not a wall-time artifact"
    )

    # Second half: it IS an attach-event clock -- a real attach moves it. Polled, not sleep-waited.
    pty = _attach(adapter, noisy)
    try:
        await_condition(
            lambda: _session_activity(tmux_server, noisy) != t1_noisy,
            what="#{session_activity} to move on attach",
        )
    finally:
        pty.close()


# --------------------------------------------------------------------------------------
# Arm 2 -- GATE. `#{window_activity}` IS output-aware while unattended.
# --------------------------------------------------------------------------------------


def test_arm2_window_activity_advances_on_unattended_output_with_frozen_control(
    tmux_server: TmuxServer, tmp_path
) -> None:
    """Re-runs `Finding 2`. GATE: this is the signal `ADR-35` chose.

    The control (silent, unattended) staying frozen is what makes this a measurement of OUTPUT
    rather than of wall time -- both sessions experience the same elapsed seconds; only one
    produces output.
    """
    adapter = tmux_server.adapter()
    noisy = _unique("noisy")
    quiet = _unique("quiet")
    output_marker = tmp_path / "noisy.log"

    adapter.create(noisy, cwd=str(tmp_path), command=_pane_output_producer(str(output_marker), 30))
    adapter.create(quiet, cwd=str(tmp_path), command=["sh", "-c", "sleep 300"])

    t0_noisy = _window_activity(tmux_server, f"={noisy}:")
    t0_quiet = _window_activity(tmux_server, f"={quiet}:")

    await_file(str(output_marker), lambda data: len(data) > 0, what="the noisy pane's first tick")
    _advance_wall_clock(tmux_server, adapter, tmp_path)

    t1_noisy = _window_activity(tmux_server, f"={noisy}:")
    t1_quiet = _window_activity(tmux_server, f"={quiet}:")

    assert t1_noisy > t0_noisy, (
        f"CONTRARY RESULT: #{{window_activity}} did NOT advance ({t0_noisy} -> {t1_noisy}) through "
        "continuous unattended output. ADR-35's chosen signal does not work on this tmux version, "
        "and the predicate has no output-aware signal left to build on -- redesign before W41/W52."
    )
    assert t1_quiet == t0_quiet, (
        "control moved with no output -- the field tracks wall time, not output"
    )


# --------------------------------------------------------------------------------------
# Arm 3 -- GATE. The trap: a SESSION target resolves to the active window only.
# --------------------------------------------------------------------------------------


def test_arm3_session_target_window_activity_sees_only_the_active_window(
    tmux_server: TmuxServer, tmp_path
) -> None:
    """Re-runs `Finding 3`. GATE: this is why `MAX(window_activity)` across windows (`W52`) is
    mandatory rather than an optimisation.

    One session, two windows. Window 0 stays active and silent; window 1 is created detached
    (`-d`, so it never becomes active) and produces output. A session-target `#{window_activity}`
    read must stay frozen at window 0's value even while window 1 visibly advances -- and the
    per-window `MAX` this test also computes must be what catches it.
    """
    adapter = tmux_server.adapter()
    name = _unique("trap")
    output_marker = tmp_path / "bg.log"

    adapter.create(name, cwd=str(tmp_path), command=["sh"])
    # `new-window`'s trailing arguments are the command to run, exactly like `new-session`'s --
    # so the same argv-shaped producer used everywhere else in this module works unchanged.
    created = tmux_server.raw(
        "new-window", "-d", "-t", f"={name}:", *_pane_output_producer(str(output_marker), 30)
    )
    assert created.rc == 0, created.stderr

    rows0 = _windows(tmux_server, name)
    assert len(rows0) == 2, f"expected 2 windows, tmux reports {rows0!r}"
    active0 = next(r for r in rows0 if r[1])
    background0 = next(r for r in rows0 if not r[1])
    assert active0[0] == "0", "window 0 must stay active -- new-window -d must not switch focus"

    t0_session = _window_activity(tmux_server, f"={name}:")
    t0_active = active0[2]
    t0_background = background0[2]
    assert t0_session == t0_active, "a session target must read the ACTIVE window's value"

    await_file(
        str(output_marker), lambda data: len(data) > 0, what="the background pane's first tick"
    )
    _advance_wall_clock(tmux_server, adapter, tmp_path)

    rows1 = _windows(tmux_server, name)
    active1 = next(r for r in rows1 if r[0] == active0[0])
    background1 = next(r for r in rows1 if r[0] == background0[0])
    t1_session = _window_activity(tmux_server, f"={name}:")
    aggregate_t1 = max(row[2] for row in rows1)

    assert t1_session == t0_session, (
        f"CONTRARY RESULT: a session-target #{{window_activity}} read moved ({t0_session} -> "
        f"{t1_session}) even though only the BACKGROUND window produced output. Finding 3's trap "
        "does not reproduce on this tmux version -- re-examine whether a session-target read is "
        "still unsafe to use directly before relying on that in W41."
    )
    assert background1[2] > t0_background, (
        "the background window's OWN target did not advance either -- arm 2's premise (output-"
        "awareness) does not hold here, which would also contradict arm 2"
    )
    assert aggregate_t1 > t0_session, (
        "MAX(window_activity) across windows failed to detect the background output that the "
        "session-target read missed -- this is the aggregate ADR-35/W52 are built on"
    )
    assert active1[2] == t0_active, "the active (silent) window's own clock must stay frozen too"


# --------------------------------------------------------------------------------------
# Arm 4 -- GATE. A fresh session's window clock reads near `now`, not epoch.
# --------------------------------------------------------------------------------------


def test_arm4_fresh_session_window_activity_reads_near_now_not_epoch(
    tmux_server: TmuxServer, tmp_path
) -> None:
    """A freshly created, never-touched session must NOT read `0`/epoch.

    The stakes (`W51`'s measurement (a)): if a new session read `0`, the reaper would treat every
    brand-new session as infinitely idle and reap it within one sweep of creation. If it reads near
    `now`, the safe direction the measurement document's control arm implied holds.
    """
    adapter = tmux_server.adapter()
    name = _unique("fresh")
    before = time.time()
    adapter.create(name, cwd=str(tmp_path), command=["sh"])
    activity = _window_activity(tmux_server, f"={name}:")
    after = time.time()

    assert activity != 0, (
        "CONTRARY RESULT: a fresh session's window_activity read epoch (0) -- the reaper would "
        "reap every new session within one sweep of creation"
    )
    assert before - 5 <= activity <= after + 5, (
        f"fresh window_activity={activity} is not within a few seconds of now "
        f"({before:.0f}..{after:.0f})"
    )


# --------------------------------------------------------------------------------------
# Arm 5 -- RECORD ONLY. Cannot fail this item.
# --------------------------------------------------------------------------------------


def test_arm5_session_target_pane_dead_scoping_is_recorded(
    tmux_server: TmuxServer, tmp_path
) -> None:
    """RECORD ONLY -- this arm cannot fail `W51`. It only records what tmux answers.

    `#{pane_dead}` is `LIST_FIELDS` index 5 and becomes `SessionRecord.alive`
    (`tmux.py:87,834`), read through a SESSION target -- structurally identical to the composition
    `Finding 3` showed was a trap for `#{window_activity}`. One session, two windows: window 0
    stays active and alive; window 1 is created detached and dies immediately. What does a
    session-target `#{pane_dead}` read?

    Whatever the answer, this test only records it (see the assertion below, which merely checks
    the field parses as a tmux boolean and does not gate on which value came back). The
    consequence, if `SessionRecord.alive` turns out to report only the active pane, is owned by
    `ADR-31`'s follow-up, not by this phase's work items -- `W51` names the fact and stops there.
    """
    adapter = tmux_server.adapter()
    name = _unique("scoping")

    adapter.create(name, cwd=str(tmp_path), command=["sh"])
    dead_window = tmux_server.raw(
        "new-window", "-d", "-t", f"={name}:", "sh", "-c", "printf 'DEAD\\n'"
    )
    assert dead_window.rc == 0, dead_window.stderr

    rows = _windows(tmux_server, name)
    assert len(rows) == 2
    background_index = next(r[0] for r in rows if not r[1])

    await_condition(
        lambda: _pane_dead(tmux_server, f"={name}:{background_index}") == "1",
        what="the background pane to die",
    )

    session_target_reading = _pane_dead(tmux_server, f"={name}:")
    active_window_reading = _pane_dead(tmux_server, f"={name}:0")
    background_window_reading = _pane_dead(tmux_server, f"={name}:{background_index}")

    # RECORDED, not asserted as an expectation: a session-target #{pane_dead} read, with an alive
    # ACTIVE window and a dead INACTIVE window, returned this value on the tmux version this ran
    # against. See this test's docstring / the appended findings entry for what it means.
    print(  # this IS the record for arm 5 -- pytest -v/-s surfaces it; see docstring above
        "W51 arm 5 RECORD: session-target #{pane_dead} = "
        f"{session_target_reading!r} (active window #0 = {active_window_reading!r}, "
        f"dead background window #{background_index} = {background_window_reading!r})"
    )
    assert session_target_reading in ("0", "1"), (
        "a boolean tmux field returned something unparseable"
    )
    assert active_window_reading == "0", "the active window's OWN pane never died in this fixture"
    assert background_window_reading == "1", (
        "the background pane must actually be dead for this arm to mean anything"
    )


# --------------------------------------------------------------------------------------
# Arm 6 -- GATE, the central one. A dead pane's window clock does not advance.
# --------------------------------------------------------------------------------------


def test_arm6_dead_panes_window_clock_does_not_advance(tmux_server: TmuxServer, tmp_path) -> None:
    """GATE -- the central measurement `W51` grows beyond the three re-run findings.

    A session whose command exits immediately (`remain-on-exit on`, set globally by the very
    first `adapter.create()` call, per `tmux.py:569` and the fixture pattern at
    `test_tmux_adapter.py:410`). Poll until `#{pane_dead}` reads `1`, take `t0`, let
    `_CLOCK_SECONDS` of real time pass (via a poll, never a sleep), take `t1`. `t1 == t0` is the
    expected result -- the two-sample comparison IS the test, not a timeout wait.

    The stakes, if this comes back "moving" instead: the canonical garbage session (a finished
    command in an abandoned session) NEVER reaps, because its window clock keeps advancing on its
    own. The feature would ship and do nothing for the case it exists to handle, and ADR-28's
    decision not to add a separate dead-pane grace key (on the grounds that the output clock
    already provides the grace) would need to be re-decided.
    """
    adapter = tmux_server.adapter()
    name = _unique("dying")
    adapter.create(name, cwd=str(tmp_path), command=["sh", "-c", "printf 'LASTLINE\\n'"])

    await_condition(
        lambda: _pane_dead(tmux_server, f"={name}:") == "1", what="the pane to die"
    )
    t0 = _window_activity(tmux_server, f"={name}:")

    _advance_wall_clock(tmux_server, adapter, tmp_path)

    t1 = _window_activity(tmux_server, f"={name}:")

    assert t1 == t0, (
        f"CONTRARY RESULT: a DEAD pane's #{{window_activity}} advanced ({t0} -> {t1}) across "
        f"{_CLOCK_SECONDS}s with nothing writing to the pane. The reaper's flagship case -- a "
        "finished command in an abandoned session -- would never age out under the output "
        "timeout, and ADR-28's decision to skip a separate dead-pane grace key needs to be "
        "re-decided before W41 is written."
    )


# --------------------------------------------------------------------------------------
# Arm 7 -- GATE. An attached, SILENT client's window clock does not advance.
# --------------------------------------------------------------------------------------


def test_arm7_attached_silent_client_window_clock_does_not_advance(
    tmux_server: TmuxServer, tmp_path
) -> None:
    """GATE -- `W51`'s measurement (d), bearing on the definition of done's FIRST clause.

    Two arms differing in exactly one variable: both sessions run an idle shell and produce NO
    output; one has a real attached client (via `AttachedPty`, the same fixture the attach lane
    uses), the other does not. If an attached client's own repaints advanced `#{window_activity}`,
    the attach-veto criterion (`A25`) would be confounded -- a session could be spared by the
    OUTPUT timeout instead of by the attach veto, and no criterion built on "the session survived"
    could tell which one did it.
    """
    adapter = tmux_server.adapter()
    attached_name = _unique("attached")
    control_name = _unique("control")

    adapter.create(attached_name, cwd=str(tmp_path), command=["sh"])
    adapter.create(control_name, cwd=str(tmp_path), command=["sh"])

    pty = _attach(adapter, attached_name)
    try:
        await_condition(
            lambda: _display(tmux_server, f"={attached_name}:", "#{session_attached}") == "1",
            what="the client to actually attach",
        )

        t0_attached = _window_activity(tmux_server, f"={attached_name}:")
        t0_control = _window_activity(tmux_server, f"={control_name}:")

        _advance_wall_clock(tmux_server, adapter, tmp_path)

        t1_attached = _window_activity(tmux_server, f"={attached_name}:")
        t1_control = _window_activity(tmux_server, f"={control_name}:")
    finally:
        pty.close()

    assert t1_attached == t0_attached, (
        f"CONTRARY RESULT: an attached but SILENT client's #{{window_activity}} advanced "
        f"({t0_attached} -> {t1_attached}) with no pane output. The attach-veto criterion (A25) "
        "is confounded by this: a session could be spared by the output timeout rather than by "
        "the attach veto, and that criterion needs a different discriminator before W41 is "
        "written -- see W51's measurement (d)."
    )
    assert t1_control == t0_control, (
        "the unattended control moved with no output -- not a wall-time artifact"
    )


# --------------------------------------------------------------------------------------
# `W52` -- `TmuxAdapter.window_activity_max` against a real multi-window session.
# --------------------------------------------------------------------------------------


def test_window_activity_max_returns_the_true_maximum_including_an_inactive_window(
    tmux_server: TmuxServer, tmp_path
) -> None:
    """`A37` real-tmux half: the MAX must include a window `#{window_activity}` cannot see.

    Same trap as Arm 3: a session-target `#{window_activity}` read resolves to the ACTIVE
    window only, so window 0 (active, silent) alone would report a frozen value while window 1
    (created detached with `-d`, so it never becomes active, and producing output the whole
    time) visibly advances underneath it. `window_activity_max` must catch that -- it is the
    method this whole trap exists to justify.
    """
    adapter = tmux_server.adapter()
    name = _unique("agg")
    output_marker = tmp_path / "agg-bg.log"

    adapter.create(name, cwd=str(tmp_path), command=["sh"])
    created = tmux_server.raw(
        "new-window", "-d", "-t", f"={name}:", *_pane_output_producer(str(output_marker), 30)
    )
    assert created.rc == 0, created.stderr

    rows0 = _windows(tmux_server, name)
    assert len(rows0) == 2, f"expected 2 windows, tmux reports {rows0!r}"
    active0 = next(r for r in rows0 if r[1])
    background0 = next(r for r in rows0 if not r[1])
    assert active0[0] == "0", "window 0 must stay active -- new-window -d must not switch focus"

    t0_max = adapter.window_activity_max(name)
    # `active0[2]` and `background0[2]` come from two separate tmux calls a moment apart, and
    # `#{window_activity}` is Unix-epoch-seconds granularity -- if creation straddles a second
    # boundary the two can differ by one tick. That's not what this fixture is proving (the
    # load-bearing assertions are below, once the background window has visibly advanced), so
    # tolerate the tick rather than asserting exact equality across two independent tmux calls.
    assert abs(int(active0[2]) - int(background0[2])) <= 1, (
        "both windows should start within a tick of each other -- not the value under test"
    )
    assert t0_max == max(int(active0[2]), int(background0[2])), (
        "the aggregate must be the max over both windows even at creation"
    )

    await_file(
        str(output_marker), lambda data: len(data) > 0, what="the background pane's first tick"
    )
    _advance_wall_clock(tmux_server, adapter, tmp_path)

    rows1 = _windows(tmux_server, name)
    active1 = next(r for r in rows1 if r[0] == active0[0])
    background1 = next(r for r in rows1 if r[0] == background0[0])
    t1_max = adapter.window_activity_max(name)

    assert active1[2] == active0[2], "the active (silent) window's own clock must stay frozen"
    assert background1[2] > background0[2], (
        "the background window did not actually advance -- this fixture proves nothing"
    )
    assert t1_max == background1[2], (
        f"window_activity_max ({t1_max}) must equal the TRUE maximum across every window "
        f"({background1[2]}), including the inactive one a session-target read cannot see -- "
        f"this is the whole reason W52 is its own list-windows call rather than a "
        f"LIST_FIELDS entry"
    )
    assert t1_max > t0_max, "the aggregate must have moved once the background window advanced"
