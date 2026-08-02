"""W19b's lifecycle: the claim protocol, the thread that hosts a bridge, and the reap.

``tests/tmux/test_attach_claim.py`` runs the claim against a real tmux server, which is where
the round trip and the read-back are proved. What this file asserts is the part a real server
makes *harder* to see: the decisions the protocol takes given each answer tmux can give, and
the shutdown ordering -- which needs a bridge that can be made to hang, and a claim whose owner
can be made to look dead on demand.

Three properties here fail silently rather than loudly, so each has a test that names it:

1. **The pre-check and the read-back are both required.** Deleting either leaves a protocol
   that passes the ordinary case and double-attaches in the case it exists for.
2. **A claim nobody can evaluate must not lock a session forever.** Malformed reads as absent.
3. **``stop()`` reaps the child even when the thread will not stop.** The one case the shutdown
   path exists for is the one where every cooperative mechanism has already failed.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import threading

import pytest
from conftest import await_condition, departed_claim
from shellbox_mcp import publisher as publisher_module
from shellbox_mcp.publisher import Claim, Publisher, acquire, claim_is_live, tid_starttime

# A claim whose owner is not running in EITHER lane. The pid is above `pid_max` on every
# platform this runs on, so `/proc` has no entry for it and `os.kill` raises
# `ProcessLookupError` -- deliberately NOT pid 1, which exists everywhere and would read live
# in the degraded lane.
FOREIGN = Claim(pid=999_999_999, tid=999_999_998, starttime=1)


class FakeAdapter:
    """The three claim verbs, with the stored value visible and every call recorded.

    ``stored`` is the tmux session option. ``on_write`` lets a test simulate the one thing a
    single-threaded test otherwise cannot: another publisher writing between this one's write
    and its read-back, which is R33's interleaving and the reason the read-back exists.
    """

    def __init__(self, stored: str | None = None, *, writes: bool = True) -> None:
        self.stored = stored
        self.writes = writes
        self.reads = 0
        self.written: list[str] = []
        self.released: list[str] = []
        self.on_write: object | None = None

    def read_publisher_claim(self, name: str) -> str | None:
        self.reads += 1
        return self.stored

    def claim_publisher(self, name: str, claim: str) -> bool:
        self.written.append(claim)
        if not self.writes:
            return False
        self.stored = claim
        if callable(self.on_write):
            self.on_write()
        return True

    def release_publisher_claim(self, name: str, claim: str) -> bool:
        if self.stored != claim:
            return False
        self.released.append(claim)
        self.stored = None
        return True


@pytest.fixture(autouse=True)
def _no_leaked_publishers():
    """Fail loudly if a test leaves a publisher in the module's shutdown registry.

    The registry is what ``atexit`` iterates, so a leak here is a leak in the mechanism that
    exists to prevent leaks -- and it would show up as a hang at interpreter exit rather than
    as a failing test.
    """
    yield
    assert not publisher_module._LIVE, "a test left a publisher registered for shutdown"


def mine() -> Claim:
    """A claim whose owner IS running: this process, this thread."""
    return Claim.current()


# --------------------------------------------------------------------------------------
# The claim's shape and its liveness predicate
# --------------------------------------------------------------------------------------


def test_a_claim_round_trips_through_its_stored_form() -> None:
    claim = Claim(pid=1234, tid=5678, starttime=115518170)
    assert str(claim) == "1234:5678:115518170"
    assert Claim.parse(str(claim)) == claim


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "1234",
        "1234:5678",
        "1234:5678:9:0",
        "1234:5678:abc",
        "-1:5678:9",
        "1234:5678:9\t10",
    ],
)
def test_an_unparseable_claim_is_none_rather_than_an_exception(raw: str) -> None:
    """A junk value must not raise on a background thread nobody catches for."""
    assert Claim.parse(raw) is None


def test_the_stored_form_carries_only_digits_and_colons() -> None:
    """What keeps the claim safe in the TAB-separated ``display-message`` read path.

    ``@shellbox_cwd`` is deliberately kept OUT of that group because a path can contain a TAB
    or an LF; this value may join it only because its alphabet cannot.
    """
    assert set(str(Claim.current())) <= set("0123456789:")


def test_a_live_thread_reads_live_and_a_departed_one_does_not() -> None:
    """The predicate the whole design rests on, in both directions."""
    assert claim_is_live(mine())
    assert not claim_is_live(FOREIGN)


def test_the_degraded_predicate_reads_an_unsignalable_process_as_ALIVE(monkeypatch) -> None:
    """The macOS lane's rule, asserted on every platform because only one platform runs it.

    ``os.kill(pid, 0)`` raises ``PermissionError`` for a process this user may not signal --
    pid 1, for any non-root caller -- and that means the process EXISTS. Catching ``OSError``
    wholesale reads it as gone, which is the single direction this predicate must never fail
    in: the publisher would attach over a live one and produce `ADR-12`'s repaint loop.

    Forced rather than skipped, because the exact predicate is what CI runs, so a regression
    here would ship unobserved and surface only on a developer's laptop.
    """
    monkeypatch.setattr(publisher_module, "_HAS_PROC", False)

    assert claim_is_live(Claim(pid=1, tid=1, starttime=0))
    assert not claim_is_live(FOREIGN)


@pytest.mark.skipif(not publisher_module._HAS_PROC, reason="the exact predicate needs /proc")
def test_a_dead_thread_in_a_live_process_reads_dead() -> None:
    """T-ATTACH-CLAIM case 3's predicate, which is the reason identity is a tid and not a pid.

    A process-keyed claim would answer True here -- the process IS alive, it is running this
    assertion -- and no publisher would serve that session again for the rest of its life.
    That is a permanent lockout, not a race, and it is what `ADR-16` corrected.
    """
    departed = departed_claim()

    assert not claim_is_live(departed)
    assert departed.pid == mine().pid, "the fixture must model a SAME-process crash"
    assert claim_is_live(mine()), "the process itself must still read live"


@pytest.mark.skipif(not publisher_module._HAS_PROC, reason="the exact predicate needs /proc")
def test_a_recycled_id_reads_dead_because_the_start_time_differs() -> None:
    """``starttime`` is the half that makes pid/tid reuse detectable.

    Reuse is ordinary here rather than exotic: Linux allocates ids sequentially and wraps at
    ``pid_max``, and shellbox spawns a subprocess per tmux command across 1-32 processes.
    """
    live = mine()
    assert not claim_is_live(Claim(pid=live.pid, tid=live.tid, starttime=live.starttime + 1))


@pytest.mark.skipif(not publisher_module._HAS_PROC, reason="the exact predicate needs /proc")
def test_the_start_time_is_per_thread_and_not_per_process() -> None:
    """Spike F21c, asserted against the shipped reader rather than the spike's copy.

    If this read a neighbouring field instead, every claim in a process would compare equal and
    the tid would stop discriminating -- silently, since both values are plausible ints.
    """
    here = mine()
    main = tid_starttime(here.pid, here.tid)
    other: list[int | None] = []

    def record() -> None:
        other.append(tid_starttime(os.getpid(), threading.get_native_id()))

    thread = threading.Thread(target=record)
    thread.start()
    thread.join()

    assert main is not None and other[0] is not None
    assert main != other[0]


# --------------------------------------------------------------------------------------
# acquire: claim -> read back -> attach, or exit
# --------------------------------------------------------------------------------------


def test_an_unclaimed_session_is_claimed_and_the_write_is_read_back() -> None:
    adapter = FakeAdapter(stored=None)
    claim = mine()

    assert acquire(adapter, "s", claim) is True
    assert adapter.written == [str(claim)]
    assert adapter.stored == str(claim)
    assert adapter.reads == 2, "the pre-check and the read-back are two distinct reads"


def test_a_session_claimed_by_a_running_publisher_is_refused_without_a_write() -> None:
    """The pre-check. **Deleting it is what the read-back cannot cover.**

    Without this, the second publisher overwrites the first's claim, reads back its own, and
    attaches -- so the read-back would certify the double attach rather than prevent it. The
    ``written == []`` assertion is the real subject: refusing after writing is not refusing.
    """
    live = mine()
    adapter = FakeAdapter(stored=str(live))
    other = Claim(pid=live.pid, tid=live.tid + 1, starttime=live.starttime)

    assert acquire(adapter, "s", other) is False
    assert adapter.written == []
    assert adapter.stored == str(live), "the incumbent's claim must be untouched"


def test_a_claim_whose_publisher_is_gone_is_taken_over() -> None:
    """The self-clearing property, which is what lets a design with no shutdown path be safe."""
    adapter = FakeAdapter(stored=str(FOREIGN))
    claim = mine()

    assert acquire(adapter, "s", claim) is True
    assert adapter.stored == str(claim)


def test_a_malformed_claim_does_not_lock_the_session_out_forever() -> None:
    """Property 2. A value nobody can evaluate is not self-clearing.

    Treating it as a live owner would let one junk ``set-option`` make a session unpublishable
    until it is killed, which is a worse failure than the double attach the claim prevents.
    """
    adapter = FakeAdapter(stored="not-a-claim")
    claim = mine()

    assert acquire(adapter, "s", claim) is True
    assert adapter.stored == str(claim)


def test_losing_the_read_back_race_is_a_refusal() -> None:
    """R33's single interleaving, and the ONLY place it is observable.

    Both publishers passed the pre-check inside one tmux round trip -- there is no
    compare-and-swap, the same limit ``_resolve_owned`` documents for the send path (R12). The
    protocol is detection, not exclusion: last-writer-wins converged on the other publisher, so
    this one reads a foreign value back and exits.
    """
    adapter = FakeAdapter(stored=None)
    claim = mine()
    adapter.on_write = lambda: setattr(adapter, "stored", str(FOREIGN))

    assert acquire(adapter, "s", claim) is False


def test_a_write_that_fails_is_a_refusal_rather_than_an_attach() -> None:
    """A failed ``set-option`` means the session or the server is gone.

    Attaching anyway would mean attaching to something this publisher could not address a
    moment earlier.
    """
    adapter = FakeAdapter(stored=None, writes=False)
    assert acquire(adapter, "s", mine()) is False


def test_re_acquiring_a_session_this_publisher_already_holds_succeeds() -> None:
    """Restart-in-place: the same thread re-running ``acquire`` must not refuse itself."""
    claim = mine()
    adapter = FakeAdapter(stored=str(claim))
    assert acquire(adapter, "s", claim) is True


# --------------------------------------------------------------------------------------
# The publisher thread, and the shutdown path
# --------------------------------------------------------------------------------------


class FakeBridge:
    """A bridge whose ``run`` blocks until cancelled, recording its closes.

    ``close`` counts rather than flags because the ordering claim is about it being called at
    all under each failure -- and because it is documented idempotent, so being called twice is
    correct rather than a defect worth failing on.
    """

    def __init__(self, *, hangs: bool = False, returns: bool = False) -> None:
        self.closes = 0
        self.started = threading.Event()
        self._hangs = hangs
        self._returns = returns

    async def run(self) -> None:
        self.started.set()
        if self._returns:
            return  # the pane's stream ended, which is how a publisher normally finishes
        if self._hangs:
            # Uncancellable on purpose: a `capture-pane` against a wedged tmux server, which
            # is the case `stop`'s timeout exists for.
            threading.Event().wait(30)
            return
        await asyncio.Event().wait()

    def close(self) -> None:
        self.closes += 1


def test_the_publisher_claims_before_it_builds_a_bridge() -> None:
    """Ordering, and the reason ``PtyBridge.attach`` is separate from ``PtyBridge.run``.

    A bridge built before the claim is decided has already forked a ``tmux attach``, so a
    publisher that then loses the claim has already done the damage the claim prevents.
    """
    built: list[FakeBridge] = []
    live = mine()
    adapter = FakeAdapter(stored=str(Claim(pid=live.pid, tid=live.tid, starttime=live.starttime)))

    def factory() -> FakeBridge:
        built.append(FakeBridge())
        return built[-1]

    # The stored claim is this very thread's, but the publisher's claim is its OWN thread's,
    # so it sees a foreign live claim and must refuse.
    publisher = Publisher(adapter, "s", factory)  # type: ignore[arg-type]
    try:
        assert publisher.start(timeout=5.0) is False
        assert built == [], "no bridge may be built by a publisher that lost the claim"
    finally:
        publisher.stop(timeout=2.0)


def test_a_publisher_that_wins_runs_the_bridge_and_stop_closes_the_pty() -> None:
    """The ordinary shutdown.

    ``error is None`` is the anti-drift assertion. ``_run`` records anything ``_serve`` raises
    rather than propagating it, so a teardown that raises leaves every other assertion here
    intact; this is the only thing that would notice.
    """
    adapter = FakeAdapter(stored=None)
    bridge = FakeBridge()
    publisher = Publisher(adapter, "s", lambda: bridge)  # type: ignore[arg-type]

    assert publisher.start(timeout=5.0) is True
    assert bridge.started.wait(5.0), "the bridge's run never started"

    publisher.stop(timeout=5.0)
    assert bridge.closes >= 1, "the attach child was never reaped"
    assert publisher.error is None, f"the publisher's teardown raised: {publisher.error!r}"


def test_stop_releases_the_claim_so_a_successor_does_not_wait_on_a_probe() -> None:
    adapter = FakeAdapter(stored=None)
    bridge = FakeBridge()
    publisher = Publisher(adapter, "s", lambda: bridge)  # type: ignore[arg-type]
    publisher.start(timeout=5.0)
    assert bridge.started.wait(5.0)

    publisher.stop(timeout=5.0)
    assert adapter.released and adapter.stored is None


def test_stop_reaps_the_child_even_when_the_thread_will_not_stop() -> None:
    """Property 3, and the case the whole shutdown path exists for.

    A shutdown that can hang is one an operator kills, and a killed shutdown orphans exactly
    the ``tmux attach`` client this is meant to reap. So ``stop`` returns after its timeout and
    closes the pty from the MAIN thread -- which it can do because ``close`` is synchronous and
    does not need the publisher's loop to still be running.
    """
    adapter = FakeAdapter(stored=None)
    bridge = FakeBridge(hangs=True)
    publisher = Publisher(adapter, "s", lambda: bridge)  # type: ignore[arg-type]
    publisher.start(timeout=5.0)
    assert bridge.started.wait(5.0)

    publisher.stop(timeout=0.2)

    assert bridge.closes >= 1, "stop returned without reaping a child it could not join"


def test_stop_is_idempotent() -> None:
    adapter = FakeAdapter(stored=None)
    bridge = FakeBridge()
    publisher = Publisher(adapter, "s", lambda: bridge)  # type: ignore[arg-type]
    publisher.start(timeout=5.0)
    assert bridge.started.wait(5.0)

    publisher.stop(timeout=5.0)
    publisher.stop(timeout=5.0)


def test_a_publisher_that_lost_the_claim_starts_no_thread_work_and_leaks_nothing() -> None:
    live = mine()
    adapter = FakeAdapter(stored=str(live))
    other_thread_claim = Claim(pid=live.pid, tid=live.tid + 1, starttime=live.starttime)
    publisher = Publisher(
        adapter,
        "s",
        lambda: FakeBridge(),  # type: ignore[arg-type, return-value]
        claim=lambda: other_thread_claim,
    )

    assert publisher.start(timeout=5.0) is False
    publisher.stop(timeout=2.0)
    assert publisher.bridge is None


def test_stopping_before_the_loop_starts_still_stops_the_publisher() -> None:
    """The race ``_serve``'s ``_stop.is_set()`` check closes.

    ``stop`` cancels through the run task, and between ``start`` and the loop's first tick
    there is no task to cancel. Without the re-check the request is simply lost and the
    publisher runs on after a shutdown that believed it had stopped everything.

    The bridge is asserted NEVER BUILT rather than built-and-closed: building it forks a real
    ``tmux attach``, so a publisher that constructs one after being told to stop has attached a
    client to the agent's session and reflowed it, which no later ``close`` undoes.
    """
    adapter = FakeAdapter(stored=None)
    built: list[FakeBridge] = []

    def factory() -> FakeBridge:
        built.append(FakeBridge())
        return built[-1]

    publisher = Publisher(adapter, "s", factory)  # type: ignore[arg-type]
    publisher._stop.set()
    publisher.start(timeout=5.0)
    publisher.stop(timeout=5.0)

    assert built == [], "a publisher told to stop forked an attach client anyway"


def test_a_publisher_whose_pane_exits_leaves_the_shutdown_registry_on_its_own() -> None:
    """Otherwise the registry grows one entry per session ever published, for the process's life.

    ``shutdown_all`` walks it at exit, so the cost is not only memory: a long-lived server
    would spend its shutdown iterating publishers that finished hours ago.
    """
    adapter = FakeAdapter(stored=None)
    bridge = FakeBridge(returns=True)
    publisher = Publisher(adapter, "s", lambda: bridge)  # type: ignore[arg-type]

    assert publisher.start(timeout=5.0) is True
    await_condition(
        lambda: publisher not in publisher_module._LIVE,
        what="the finished publisher to leave the shutdown registry",
    )
    assert bridge.closes >= 1, "and its pty must still have been closed"


def test_an_exception_from_the_claim_is_recorded_rather_than_raised() -> None:
    """ADR-3/R7: a transport or tmux failure may not fail the tool call that started this."""

    class Exploding(FakeAdapter):
        def read_publisher_claim(self, name: str) -> str | None:
            raise RuntimeError("tmux is gone")

    publisher = Publisher(Exploding(), "s", lambda: FakeBridge())  # type: ignore[arg-type]
    assert publisher.start(timeout=5.0) is False
    assert isinstance(publisher.error, RuntimeError)
    publisher.stop(timeout=2.0)


def test_interpreter_shutdown_reaps_a_running_publishers_child(tmp_path) -> None:
    """**The claim W19b exists to make, and the only test that can make it.**

    ``bridge.py``'s docstring states the defect precisely: "a daemon thread killed at
    interpreter shutdown never reaches a ``finally``". Every in-process test here stops the
    publisher deliberately, so none of them exercises that -- the process has to actually end
    while a publisher is still running.

    It works because of an ordering in CPython's finalizer: daemon threads are **not** joined,
    they are frozen the next time they take the GIL, while ``atexit`` handlers run on the main
    thread during finalization. So the reap has to happen from the main thread, which is what
    ``_register`` installs and what this asserts.

    The child stands in for the ``tmux attach`` client. If this test fails, what leaks in
    production is a live tmux client holding the agent's window at the last viewer's size --
    PM3's reflow made permanent, and invisible until someone looks at the pane.
    """
    marker = tmp_path / "reaped"
    program = f"""
import asyncio, threading
from shellbox_mcp.publisher import Publisher

running = threading.Event()

class Bridge:
    async def run(self):
        running.set()
        await asyncio.Event().wait()                 # never returns on its own
    def close(self):
        open({str(marker)!r}, "w").write("reaped")

class Adapter:
    stored = None
    def read_publisher_claim(self, name): return self.stored
    def claim_publisher(self, name, claim):
        self.stored = claim          # so the read-back sees it, as tmux would
        return True
    def release_publisher_claim(self, name, claim): return True

publisher = Publisher(Adapter(), "s", lambda: Bridge())
assert publisher.start(timeout=5.0) is True

# Wait for the loop to be RUNNING, not merely claimed. `start` returns once the claim is
# decided, which is before the bridge exists -- exiting there would test a publisher that had
# nothing to reap, and would pass whether or not the exit hook works.
assert running.wait(5.0), "the bridge's loop never started"

# Now exit, with the publisher thread mid-loop. Nothing calls stop().
"""
    proc = subprocess.run(
        [sys.executable, "-c", program], capture_output=True, text=True, timeout=30
    )

    assert proc.returncode == 0, f"the child interpreter failed: {proc.stderr}"
    assert marker.read_text() == "reaped", (
        "the attach child was NOT reaped at interpreter shutdown. A publisher thread killed "
        "mid-loop leaves a live tmux client on the agent's session indefinitely."
    )


def test_shutdown_all_stops_every_registered_publisher() -> None:
    """What ``atexit`` runs. One wedged publisher must not strand the others' children."""
    bridges = [FakeBridge(), FakeBridge()]
    publishers = [
        Publisher(FakeAdapter(stored=None), f"s{n}", lambda b=b: b)  # type: ignore[arg-type, misc]
        for n, b in enumerate(bridges)
    ]
    for publisher in publishers:
        publisher.start(timeout=5.0)
    for bridge in bridges:
        assert bridge.started.wait(5.0)

    publisher_module.shutdown_all(timeout=5.0)

    assert all(bridge.closes >= 1 for bridge in bridges)
    assert not publisher_module._LIVE
