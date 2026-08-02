"""T-ATTACH-CLAIM -- W19b's claim protocol against a REAL tmux server.

``tests/unit/test_publisher.py`` asserts what the protocol decides given each answer tmux can
give. This asserts that tmux actually gives those answers, which is the half a fake cannot
speak for: that the value survives a ``set-option``/``display-message`` round trip, that an
ABSENT claim reads as an empty field rather than an error, and that ``-u`` clears it.

The spike is the oracle for the tmux forms (F21), and it runs first in this lane. What is left
for this file is the half the spike cannot cover: that the SHIPPED adapter issues those forms
and that ``publisher.py`` composes them into the protocol `ADR-16` specifies.

WARNING: **No client is attached here.** The claim is arbitration, and arbitration is decided
before a pty exists -- that is the whole reason ``PtyBridge.attach`` is separate from
``PtyBridge.run``. Attaching would add a fork to a test whose subject is three tmux options.
"""

from __future__ import annotations

import pytest
from conftest import TmuxServer, departed_claim, requires_tmux
from shellbox_mcp import publisher as publisher_module
from shellbox_mcp.publisher import Claim, acquire, claim_is_live
from shellbox_mcp.tmux import PUBLISHER_OPTION

pytestmark = requires_tmux

SESSION = "build"

# The degraded lane cannot see a dead thread inside a live process -- that is the documented
# limitation of `os.kill(pid, 0)`, and it is precisely what these cases measure. Skipping is
# honest; asserting the degraded behavior instead would encode the deadlock as expected.
needs_proc = pytest.mark.skipif(
    not publisher_module._HAS_PROC,
    reason="the exact per-thread predicate needs /proc",
)


def session(tmux_server: TmuxServer, tmp_path) -> None:
    tmux_server.adapter().create(SESSION, cwd=str(tmp_path), command=["sh"])


# --------------------------------------------------------------------------------------
# The round trip the protocol is built on
# --------------------------------------------------------------------------------------


def test_a_claim_round_trips_through_the_shipped_adapter(tmux_server: TmuxServer, tmp_path) -> None:
    session(tmux_server, tmp_path)
    adapter = tmux_server.adapter()
    claim = Claim.current()

    assert adapter.claim_publisher(SESSION, str(claim)) is True
    assert adapter.read_publisher_claim(SESSION) == str(claim)

    raw = tmux_server.raw("show-options", "-t", f"={SESSION}:", "-v", PUBLISHER_OPTION)
    assert raw.stdout_raw.strip() == str(claim), "the option did not land where tmux stores it"


def test_an_absent_claim_reads_as_none_and_not_as_a_failure(
    tmux_server: TmuxServer, tmp_path
) -> None:
    """F21a's finding, asserted against the shipped read path.

    This is why the claim is read through ``display-message`` and NOT ``show-options -v``: on a
    session carrying no claim the latter exits **1** with ``invalid option``, indistinguishable
    from a real failure. An absent claim is the ORDINARY case -- it is what every first
    publisher sees -- so a read path that reports it as an error would make the common case
    look like a broken tmux.
    """
    session(tmux_server, tmp_path)
    adapter = tmux_server.adapter()

    assert adapter.read_publisher_claim(SESSION) is None

    via_show = tmux_server.raw("show-options", "-t", f"={SESSION}:", "-v", PUBLISHER_OPTION)
    assert via_show.rc != 0, (
        "show-options -v now succeeds on an unset option. If tmux changed this, the read path "
        "choice is worth revisiting -- but display-message stays correct either way."
    )


def test_a_missing_session_reads_as_no_claim_rather_than_raising(tmux_server: TmuxServer) -> None:
    """A publisher evaluating a claim on a dead session must not get an exception.

    It runs on a background thread with nobody catching for it, and "there is no session" is
    not distinguishable-from or worse-than "there is no claim" for any caller here.
    """
    assert tmux_server.adapter().read_publisher_claim("nope") is None


def test_a_malformed_claim_reads_as_absent(tmux_server: TmuxServer, tmp_path) -> None:
    """A value nobody can evaluate must not lock the session out of ever being published.

    Written raw, because the adapter refuses to write this shape -- which is itself the point:
    the guard is on both sides, and the read side has to cope with a value shellbox did not
    write, since anyone with ``set-option`` on the shared server can put one there.
    """
    session(tmux_server, tmp_path)
    adapter = tmux_server.adapter()
    tmux_server.raw("set-option", "-t", f"={SESSION}:", PUBLISHER_OPTION, "not-a-claim")

    assert adapter.read_publisher_claim(SESSION) is None

    claim = Claim.current()
    assert acquire(adapter, SESSION, claim) is True
    assert adapter.read_publisher_claim(SESSION) == str(claim)


# --------------------------------------------------------------------------------------
# The three cases T-ATTACH-CLAIM names
# --------------------------------------------------------------------------------------


def test_case_1_a_second_publisher_refuses_while_a_live_claim_holds(
    tmux_server: TmuxServer, tmp_path
) -> None:
    """Case 1: a running publisher's claim is respected, and the incumbent is not disturbed.

    The incumbent's claim is this thread's, so it is genuinely live in both lanes. The
    challenger names a different tid in the same process -- which is what a second publisher
    in one MCP process actually looks like.
    """
    session(tmux_server, tmp_path)
    adapter = tmux_server.adapter()
    incumbent = Claim.current()
    assert acquire(adapter, SESSION, incumbent) is True

    challenger = Claim(pid=incumbent.pid, tid=incumbent.tid + 1, starttime=incumbent.starttime)
    assert acquire(adapter, SESSION, challenger) is False
    assert adapter.read_publisher_claim(SESSION) == str(incumbent), (
        "the challenger overwrote a live claim. Its read-back would then certify a DOUBLE "
        "attach rather than prevent one -- two live epochs, and ADR-12's repaint loop."
    )


def test_case_2_a_claim_whose_process_is_gone_is_taken_over(
    tmux_server: TmuxServer, tmp_path
) -> None:
    """Case 2, first half: the ``/proc`` entry is gone (here, the whole process is)."""
    session(tmux_server, tmp_path)
    adapter = tmux_server.adapter()
    departed = Claim(pid=999_999_999, tid=999_999_998, starttime=1)
    assert adapter.claim_publisher(SESSION, str(departed)) is True

    claim = Claim.current()
    assert acquire(adapter, SESSION, claim) is True
    assert adapter.read_publisher_claim(SESSION) == str(claim)


@needs_proc
def test_case_2_a_recycled_id_with_a_different_start_time_is_taken_over(
    tmux_server: TmuxServer, tmp_path
) -> None:
    """Case 2, second half, and the reason ``starttime`` is in the claim at all.

    The stored claim names a **live** pid and a **live** tid -- this very thread -- and differs
    only in its start time, which is what a wrapped-around id looks like. Without this field
    the claim would read live and the session would be locked to a publisher that no longer
    exists. pid reuse is ordinary here: Linux allocates sequentially and wraps at ``pid_max``,
    and shellbox spawns a subprocess per tmux command across 1-32 rotating processes.
    """
    session(tmux_server, tmp_path)
    adapter = tmux_server.adapter()
    live = Claim.current()
    recycled = Claim(pid=live.pid, tid=live.tid, starttime=live.starttime + 1)
    assert adapter.claim_publisher(SESSION, str(recycled)) is True

    assert acquire(adapter, SESSION, live) is True
    assert adapter.read_publisher_claim(SESSION) == str(live)


@needs_proc
def test_case_3_a_crashed_thread_in_a_live_process_does_not_deadlock_that_process(
    tmux_server: TmuxServer, tmp_path
) -> None:
    """**Case 3 is the case this test exists for.**

    A publisher thread dies -- an exception in the bridge, a failed ``execve``, a raising
    ``capture-pane`` -- and its process keeps running. A new publisher **in that same process**
    must be able to attach.

    The previous plan revision's process-keyed claim deadlocked exactly here: the claim named a
    live pid, so every liveness test read "held by a live owner", and no publisher could serve
    that session again for the rest of the process's life. That is a permanent lockout, not a
    race, which is why `ADR-16` moved identity to the thread.
    """
    session(tmux_server, tmp_path)
    adapter = tmux_server.adapter()
    crashed = departed_claim()
    assert adapter.claim_publisher(SESSION, str(crashed)) is True

    assert crashed.pid == Claim.current().pid, "the fixture must model a SAME-process crash"
    assert not claim_is_live(crashed)

    successor = Claim.current()
    assert acquire(adapter, SESSION, successor) is True
    assert adapter.read_publisher_claim(SESSION) == str(successor)


# --------------------------------------------------------------------------------------
# Release, which is an optimization and must behave like one
# --------------------------------------------------------------------------------------


def test_release_clears_only_this_publishers_own_claim(tmux_server: TmuxServer, tmp_path) -> None:
    """The read-back before the unset. **This is the whole of the release.**

    A departing publisher that unset unconditionally would clobber a SUCCESSOR's fresh claim --
    and the successor has already read its own value back, so it is attached and running. The
    next publisher would then see an empty option and attach as a second one: the exact double
    attach the claim exists to prevent, caused by the code meant to prevent it.
    """
    session(tmux_server, tmp_path)
    adapter = tmux_server.adapter()
    departing = Claim.current()
    assert acquire(adapter, SESSION, departing) is True

    successor = Claim(pid=departing.pid, tid=departing.tid + 1, starttime=departing.starttime)
    assert adapter.claim_publisher(SESSION, str(successor)) is True

    assert adapter.release_publisher_claim(SESSION, str(departing)) is False
    assert adapter.read_publisher_claim(SESSION) == str(successor)

    assert adapter.release_publisher_claim(SESSION, str(successor)) is True
    assert adapter.read_publisher_claim(SESSION) is None


def test_a_released_session_can_be_claimed_again(tmux_server: TmuxServer, tmp_path) -> None:
    """What release buys: a successor attaches without waiting on a liveness probe."""
    session(tmux_server, tmp_path)
    adapter = tmux_server.adapter()
    first = Claim.current()
    assert acquire(adapter, SESSION, first) is True
    assert adapter.release_publisher_claim(SESSION, str(first)) is True

    second = Claim(pid=first.pid, tid=first.tid + 1, starttime=first.starttime)
    assert acquire(adapter, SESSION, second) is True
