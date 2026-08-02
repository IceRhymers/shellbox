"""The publisher's lifecycle: who may attach, what hosts the loop, and what stops it.

``bridge.py`` is the data path and deliberately does not decide **whether** it may attach.
This module does, and it does three more things that only exist once a bridge has to live
somewhere: it hosts the loop on a daemon thread (Decision F.1), it stops that thread from the
main thread (`ADR-16`), and it guarantees the attach child is reaped either way.

## Why these four things are one module and not four

They share a single failure: **an orphaned ``tmux attach`` child.** It stays a live tmux client
on the session indefinitely, and a live client holds the window at that client's size -- PM3's
reflow made permanent, on a session an agent is still using. Every piece here is a different
way of arriving at it. Two publishers attach and one of them is never tracked; the thread dies
and nothing closes its pty; the interpreter exits and a daemon thread never reaches a
``finally``. Splitting them across modules would leave each looking like tidying.

## The claim, in one paragraph

A publisher writes ``<pid>:<tid>:<tid_starttime>`` into the session's ``@shellbox_publisher``,
re-reads it, and attaches only if what it reads is its own. It writes at all only if the prior
claim is absent, malformed, or names a thread that is no longer running. Identity is the
**thread's**, not the process's, because a publisher IS a thread: a claim naming a process
would survive its own publisher's death and lock that session out for the rest of the
process's life, which is a permanent lockout rather than a transient race. ``/proc/<pid>/task/
<tid>`` disappears when a thread dies while its process lives (measured, spike F21c), so the
claim is **self-clearing**, and any of the 1-32 MCP processes can evaluate it -- which a uuid
could not.

CRITICAL: **This is detection, not mutual exclusion.** tmux has no compare-and-swap, so two
publishers can pass the pre-check inside one round trip; last-writer-wins converges and the
loser's read-back tells it so (`R33`). Spike F21b measured 24 racing trials across both tmux
versions: 24/24 one-own-one-foreign, 0/24 both-own, 0/24 torn. That **bounds** the race rather
than excluding it, and the residual is a brief double-attach costing one reflow and one
repaint -- the same residual ``_resolve_owned`` accepts on the send path (R12), accepted here
for the same reason.

## The degraded lane, stated rather than silently different

``/proc`` does not exist on macOS, so there the only probe available is ``os.kill(pid, 0)``.
That **reinstates the exact hazard the ``tid`` was introduced for**: a publisher thread that
died inside a still-running process leaves a claim naming a live pid, and no publisher serves
that session again for that process's life. It is acceptable only because macOS is a developer
lane and the sandbox is Ubuntu 24.04. It would not be acceptable in production, and
``T-ATTACH-CLAIM``'s case 3 is the test that would otherwise pass on Linux while the degraded
predicate deadlocked.
"""

from __future__ import annotations

import asyncio
import atexit
import logging
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from shellbox_mcp.bridge import PtyBridge

# Deliberately a TYPE_CHECKING import: nothing here touches `PtyBridge` at runtime -- it is
# built by a factory the caller supplies and driven through two methods. So the lifecycle does
# not drag in the data path, and with it `websockets` and the whole transport, for a process
# that only ever wants to evaluate a claim. It also keeps the dependency pointing one way:
# `bridge.py` names `W19b` in its docstring but imports nothing from here, and this module
# imports nothing from there.

logger = logging.getLogger(__name__)

__all__ = [
    "Claim",
    "Publisher",
    "claim_is_live",
    "shutdown_all",
    "start_publisher",
    "tid_starttime",
]

# Whether this kernel exposes per-thread identity at all. Checked once, structurally, rather
# than by branching on `sys.platform`: what the predicate needs is the FILE, and a platform
# name is a proxy for it that can be wrong in both directions (a Linux container can mount
# /proc elsewhere; a future macOS could grow one).
_HAS_PROC = os.path.isdir("/proc/self/task")


def tid_starttime(pid: int, tid: int) -> int | None:
    """Field 22 of ``/proc/<pid>/task/<tid>/stat``, or ``None`` where ``/proc`` is not there.

    This is the **thread's** start time in clock ticks, measured rather than taken from the
    documentation (spike F21c): two threads started 0.4 s apart read 40 ticks apart at
    ``SC_CLK_TCK`` = 100, while the process's own main-thread entry read a different value
    again. It is what makes a reused pid or tid read as a different owner -- and pid reuse is
    ordinary here rather than exotic, because Linux allocates pids sequentially and wraps at
    ``pid_max`` while shellbox spawns a subprocess **per tmux command** across 1-32 rotating
    MCP processes.

    CRITICAL: **The split is after the LAST ``)``, not the first.** Field 2 is the thread's
    ``comm``, which is parenthesised and may itself contain spaces and a ``)``. Splitting on
    whitespace from the start reads a neighbouring counter instead, and the claim then compares
    two numbers that are both plausible ints and neither of which is a start time -- so it
    fails silently, in the direction that lets a second publisher attach.
    """
    try:
        with open(f"/proc/{pid}/task/{tid}/stat", "rb") as handle:
            raw = handle.read().decode(errors="replace")
    except OSError:
        return None
    try:
        return int(raw[raw.rindex(")") + 1 :].split()[19])
    except (ValueError, IndexError):
        return None


@dataclass(frozen=True)
class Claim:
    """One publisher's kernel identity: the process, the thread, and when the thread started.

    ``str(claim)`` is the stored form. Digits and colons only, which is what keeps it safe in
    the TAB-separated ``display-message`` read path that ``@shellbox_cwd`` is kept out of.
    """

    pid: int
    tid: int
    starttime: int

    def __str__(self) -> str:
        return f"{self.pid}:{self.tid}:{self.starttime}"

    @classmethod
    def current(cls) -> Claim:
        """This process and THIS thread. Call it on the publisher thread, not the caller's.

        ``starttime`` falls back to ``0`` where ``/proc`` is absent so the stored shape stays
        identical across platforms -- one parser, one regex, one set of tests. The degraded
        predicate is in ``claim_is_live``, which is the one place that should differ.
        """
        pid = os.getpid()
        tid = threading.get_native_id()
        return cls(pid=pid, tid=tid, starttime=tid_starttime(pid, tid) or 0)

    @classmethod
    def parse(cls, raw: str) -> Claim | None:
        """Parse a stored claim, or ``None`` if it is not one.

        ``TmuxAdapter.read_publisher_claim`` has already shape-checked what it returns, so this
        returning ``None`` for adapter output means the two shapes have drifted. It is checked
        here anyway because this is also the parser a test and a future reader reach for.
        """
        parts = raw.split(":")
        if len(parts) != 3 or not all(part.isdigit() for part in parts):
            return None
        pid, tid, starttime = (int(part) for part in parts)
        return cls(pid=pid, tid=tid, starttime=starttime)


def claim_is_live(claim: Claim) -> bool:
    """Is the publisher named by ``claim`` still running?

    On Linux this is exact: the ``/proc/<pid>/task/<tid>`` entry exists **and** its start time
    matches. Both halves are needed -- the entry alone would be satisfied by a recycled id, and
    that is what ``starttime`` is for.

    NOTE: **A thread reads live for a brief window AFTER it has finished.** ``Thread.join()``
    returns when the thread's Python target is done; the kernel task is torn down slightly
    later, and the ``/proc`` entry survives until it is. Measured on Ubuntu 24.04 / CPython
    3.12.3 while building this: 3 of 200 threads still read live immediately after ``join()``,
    all within a millisecond. The consequence in production is that a successor arriving inside
    that window declines to attach -- the safe direction, and gone by its next attempt. The
    consequence in a TEST is a 1.5% flake, which is why ``conftest.departed_claim`` polls for
    this predicate rather than asserting it once.

    A second, smaller limit worth knowing: ``starttime`` is in clock ticks, and
    ``SC_CLK_TCK`` is 100 on every Linux shellbox targets -- so its resolution is 10 ms and it
    cannot discriminate two threads that started in the same tick. Measured: 50 threads created
    in a tight loop took **2** distinct start times and **50** distinct tids. The tid is what
    discriminates them; ``starttime`` is there for the reused tid, which is the rarer event.

    Where ``/proc`` is absent this degrades to ``os.kill(pid, 0)``, with the limitation named
    in this module's docstring: it cannot see a dead thread in a living process, so it answers
    True where the exact predicate would answer False. That direction is the safe one for
    correctness (a publisher declines to attach) and the unsafe one for availability (a session
    stays unservable), which is the right way round for a developer lane.

    CRITICAL: **Only ``ProcessLookupError`` means dead.** ``os.kill(pid, 0)`` also raises
    ``PermissionError`` for a process this user may not signal -- which means the process
    EXISTS, and is what pid 1 raises for any non-root caller. Catching ``OSError`` wholesale
    reads "alive but not ours" as "gone", and that error lands in the one direction this
    predicate must never fail in: the publisher would attach over a live one.
    """
    if not _HAS_PROC:
        try:
            os.kill(claim.pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True
    current = tid_starttime(claim.pid, claim.tid)
    return current is not None and current == claim.starttime


class ClaimAdapter(Protocol):
    """The three tmux verbs this module needs, so a test can supply them without a server.

    A ``Protocol`` rather than an import of ``TmuxAdapter``: this module reads ``/proc`` and
    owns a thread, and narrowing what it may ask of tmux to three verbs keeps the claim
    protocol testable without a server and keeps every tmux FORM on the other side of the seam,
    where the shipped AST guards can see it.
    """

    def read_publisher_claim(self, name: str) -> str | None: ...

    def claim_publisher(self, name: str, claim: str) -> bool: ...

    def release_publisher_claim(self, name: str, claim: str) -> bool: ...


def acquire(adapter: ClaimAdapter, tmux_name: str, claim: Claim) -> bool:
    """``claim -> read back -> attach, or exit``. Returns whether this publisher may attach.

    CRITICAL: **Both steps are load-bearing and neither is sufficient alone.** Without the
    pre-check a second publisher would overwrite a live publisher's claim, read back its own,
    and attach -- so the read-back would certify a double attach rather than prevent one.
    Without the read-back two publishers passing the pre-check together would both attach, with
    nothing after the fact to tell either of them.

    A failed write is a refusal, not a retry. It means the session is gone or the server is
    dead, and a publisher that attached anyway would be attaching to something it could not
    address a moment ago.
    """
    prior = adapter.read_publisher_claim(tmux_name)
    if prior is not None and prior != str(claim):
        held = Claim.parse(prior)
        if held is not None and claim_is_live(held):
            logger.info(
                "declining to publish session %s: it is claimed by a running publisher (%s)",
                tmux_name,
                prior,
            )
            return False
        logger.info(
            "taking over session %s from a claim whose publisher is gone (%s)", tmux_name, prior
        )
    if not adapter.claim_publisher(tmux_name, str(claim)):
        return False
    readback = adapter.read_publisher_claim(tmux_name)
    if readback != str(claim):
        # R33's single interleaving, and the only place it is observable. Last-writer-wins
        # converged on somebody else, so this publisher is the loser and exits.
        logger.info(
            "lost the claim race for session %s: wrote %s, read back %r",
            tmux_name,
            claim,
            readback,
        )
        return False
    return True


class Publisher:
    """A ``PtyBridge`` on a daemon thread with its own loop, plus the path that stops it.

    Daemon so a stdio server can exit when its client closes stdin -- the property
    ``Heartbeat`` established for background work and for the same reason. Its own loop because
    ``shellbox-mcp`` has none: six synchronous tools and, outside the transport, no asyncio
    import at all.

    WARNING: **``stop()`` is the only thing standing between a crash and an orphaned tmux
    client**, so it is written to reap the child even when every other step fails -- a thread
    that will not join, a loop that never started, a bridge that raised on the way up.
    """

    def __init__(
        self,
        adapter: ClaimAdapter,
        tmux_name: str,
        bridge_factory: Callable[[], PtyBridge],
        *,
        claim: Callable[[], Claim] = Claim.current,
    ) -> None:
        """Arbitrate and host one publisher.

        ``tmux_name`` and NOT the wire ``session_id``: the claim lives in a tmux session
        option, so every name this class passes to the adapter is validated by
        ``naming.validate_session_name``, which rejects the ``:`` a global id contains. The wire
        identity belongs to the ``PtyBridge`` the factory returns -- see its constructor for why
        the two are separate at all.
        """
        self._adapter = adapter
        self._tmux_name = tmux_name
        self._bridge_factory = bridge_factory
        self._claim_factory = claim

        self._stop = threading.Event()
        """Set by the MAIN thread to ask the publisher to stop.

        A ``threading.Event`` rather than a signal handler, because ``signal.signal`` is
        main-thread-only and the publisher is not the main thread (`ADR-16`). It is the API
        surface; the loop is actually interrupted by cancelling the run task through
        ``call_soon_threadsafe``, because an event a coroutine is not awaiting stops nothing.
        """

        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task[None] | None = None
        self._bridge: PtyBridge | None = None
        self._ready = threading.Event()
        self.claimed: bool | None = None
        """Whether this publisher won its claim. ``None`` until the thread has decided."""

        self.error: BaseException | None = None
        """What ended the publisher, if anything did. Never raised into the caller's thread.

        ADR-3/R7: a transport failure may not fail a ``shell_create``. So this is fire and
        forget with a result object, exactly the way ``enroll.py`` starts enrollment.
        """

    @property
    def bridge(self) -> PtyBridge | None:
        """The running bridge, or ``None`` before the thread built one or after it stopped."""
        return self._bridge

    def start(self, *, timeout: float = 5.0) -> bool:
        """Start the thread and wait until it has decided whether it may publish.

        The wait is what makes ``claimed`` meaningful to the caller, and it is bounded by the
        claim's own cost: three tmux round trips. Returns ``claimed``, defaulting to ``False``
        if the thread has not answered in time -- an unanswered claim is not a won one.
        """
        if self._thread is not None:
            return bool(self.claimed)
        _register(self)
        self._thread = threading.Thread(
            target=self._run, name=f"shellbox-publisher-{self._tmux_name[:16]}", daemon=True
        )
        self._thread.start()
        self._ready.wait(timeout)
        return bool(self.claimed)

    def _run(self) -> None:
        """The thread body: claim, then host the bridge's loop until it ends.

        ``_ready`` is set in a ``finally`` covering the claim so a caller waiting on ``start``
        is released whether the claim was won, lost, or raised. Setting it only on the happy
        path would make a failed claim look like a slow one, and ``start`` would wait out its
        whole timeout to report the answer it already had.
        """
        claim = self._claim_factory()
        try:
            try:
                self.claimed = acquire(self._adapter, self._tmux_name, claim)
            except Exception as exc:  # noqa: BLE001 - a background publisher may not raise
                logger.warning("could not evaluate the claim for %s: %s", self._tmux_name, exc)
                self.claimed = False
                self.error = exc
            finally:
                self._ready.set()
            if not self.claimed:
                return

            try:
                asyncio.run(self._serve())
            except Exception as exc:  # noqa: BLE001 - see `error`
                logger.exception("publisher for session %s ended: %s", self._tmux_name, exc)
                self.error = exc
            finally:
                # Belt and braces over `PtyBridge.run`'s own `finally`, which does not run if
                # the bridge raised before `run` was entered -- and `close` is idempotent, so
                # paying for it twice on the ordinary path is free.
                bridge, self._bridge = self._bridge, None
                if bridge is not None:
                    bridge.close()
                self._release(claim)
        finally:
            # A publisher whose pane exited holds nothing, so it must leave the shutdown
            # registry on its own. Without this the set grows for the process's whole life --
            # one entry per session ever published -- and `shutdown_all` walks a list of
            # publishers that finished hours ago.
            _unregister(self)

    async def _serve(self) -> None:
        """Build the bridge inside the loop, publish the task handle, and await it.

        The handle has to be published from IN here: ``stop`` cancels through
        ``call_soon_threadsafe``, which needs both the loop and the task, and neither exists
        until this coroutine is running.
        """
        self._loop = asyncio.get_running_loop()
        self._task = asyncio.current_task()
        if self._stop.is_set():
            # `stop()` arrived while the thread was still claiming, so there was no task to
            # cancel and the request would otherwise be lost -- and this publisher would run
            # on after a shutdown that believed it had stopped everything.
            #
            # Checked BEFORE the factory runs, because the factory forks a `tmux attach`. A
            # publisher that builds one and closes it a microsecond later has still attached a
            # real client to the agent's session, which is a visible reflow rather than a
            # wasted allocation.
            return
        self._bridge = self._bridge_factory()
        try:
            await self._bridge.run()
        except asyncio.CancelledError:
            logger.info("publisher for session %s cancelled by shutdown", self._tmux_name)

    def _release(self, claim: Claim) -> None:
        """Release the claim, best effort. Never raises, and never required for correctness.

        A dead thread's ``/proc`` entry vanishes on its own, so skipping this costs a successor
        one liveness probe rather than the session. It is inside a ``try`` because it runs
        during shutdown, when the tmux server may already be gone.
        """
        try:
            self._adapter.release_publisher_claim(self._tmux_name, str(claim))
        except Exception as exc:  # noqa: BLE001 - the claim is self-clearing without this
            logger.debug("could not release the claim on %s: %s", self._tmux_name, exc)

    def stop(self, *, timeout: float = 5.0) -> None:
        """Stop the publisher and guarantee the attach child is reaped. Idempotent.

        CRITICAL: **The final ``close()`` is not redundant with the join.** If the thread does
        not stop in ``timeout`` -- blocked in a ``capture-pane`` against a wedged tmux server,
        say -- this returns anyway, because a shutdown path that can hang is one an operator
        will kill. Closing the pty from HERE is what makes returning safe: it is synchronous,
        it is documented idempotent, and it does not need the publisher's loop to still be
        running. Without it, the one case this method exists for is the one it does not cover.
        """
        self._stop.set()
        loop, task = self._loop, self._task
        if loop is not None and task is not None and not task.done():
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                # The loop closed between the check and the call. It has stopped, which is
                # what was being asked for.
                logger.debug("publisher loop for %s was already closed", self._tmux_name)
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=timeout)
            if thread.is_alive():
                logger.warning(
                    "publisher thread for session %s did not stop within %.1fs; closing its pty "
                    "from the main thread so the tmux attach client cannot outlive us",
                    self._tmux_name,
                    timeout,
                )
        bridge, self._bridge = self._bridge, None
        if bridge is not None:
            bridge.close()
        _unregister(self)


# --------------------------------------------------------------------------------------
# The main-thread shutdown path
# --------------------------------------------------------------------------------------
#
# `server.py` and `cli.py` contain zero `atexit`, zero signal handling, and no `finally` --
# measured, and recorded in `ADR-16`. So this is new work rather than a hook to extend, and
# `atexit` is the one that fits: `signal.signal` is main-thread-only AND `FastMCP.run()` owns
# the main thread for the process's whole life, so there is nowhere to install a handler
# without reaching inside the framework's loop.
#
# CRITICAL: **The ordering in CPython's finalizer is what makes this work.** Daemon threads are
# NOT joined at interpreter shutdown -- they are frozen the next time they try to take the GIL,
# which is precisely why a publisher thread never reaches `run`'s `finally` and orphans its
# attach child. `atexit` handlers, by contrast, run on the main thread during finalization. So
# the reap has to happen HERE, from a thread that is still allowed to run, and not in the
# publisher's own cleanup.

_LIVE: set[Publisher] = set()
_LIVE_LOCK = threading.Lock()
_ATEXIT_REGISTERED = False


def _register(publisher: Publisher) -> None:
    """Track a publisher and install the exit hook on first use.

    Registered lazily rather than at import: a process that imports this module and never
    publishes -- which is most of them, since `server.py` imports the package wholesale --
    should not carry a shutdown hook for a resource it does not hold.
    """
    global _ATEXIT_REGISTERED
    with _LIVE_LOCK:
        _LIVE.add(publisher)
        if not _ATEXIT_REGISTERED:
            atexit.register(shutdown_all)
            _ATEXIT_REGISTERED = True


def _unregister(publisher: Publisher) -> None:
    with _LIVE_LOCK:
        _LIVE.discard(publisher)


def shutdown_all(*, timeout: float = 5.0) -> None:
    """Stop every live publisher. The ``atexit`` hook, and safe to call directly.

    Each is stopped inside its own ``try``: one wedged publisher must not prevent the others
    from reaping their children, which is the failure that would turn one stuck session into a
    sandbox full of orphaned tmux clients.
    """
    with _LIVE_LOCK:
        publishers = list(_LIVE)
    for publisher in publishers:
        try:
            publisher.stop(timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - shutdown continues regardless
            logger.warning("could not stop a publisher cleanly: %s", exc)


def start_publisher(
    adapter: ClaimAdapter,
    tmux_name: str,
    bridge_factory: Callable[[], PtyBridge],
    *,
    timeout: float = 5.0,
) -> Publisher:
    """Claim ``tmux_name`` and start publishing it. Fire and forget; never raises.

    Returns the ``Publisher`` rather than a bool so a caller can read ``claimed`` and ``error``
    without either of them arriving as an exception. That is ADR-3/R7: if this is ever called
    from a tool, a transport failure may not fail the ``shell_create`` that triggered it.

    NOTE: **Nothing on the tool path calls this yet, and that is Phase 3's scope, not an
    oversight.** The renderer that would ask for a published session is Phase 4's; in Phase 3
    the callers are ``W23``'s loopback lane and ``W24``'s live run.
    """
    publisher = Publisher(adapter, tmux_name, bridge_factory)
    publisher.start(timeout=timeout)
    return publisher
