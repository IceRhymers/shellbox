"""``Reaper`` -- clock 1, idle session reaping (plan `W41`, `ADR-28`/`ADR-36`/`ADR-37`).

A background thread that kills tmux sessions nobody is using, per host, with **no election**
(`ADR-26` cuts clock 2; `ADR-33` -- one reaper elected across a host's processes -- is
withdrawn with it). Up to 32 `shellbox-mcp` processes may share one host, so up to 32 sweeps
run per interval; what makes that safe without coordination is stated at the call sites below
and, at length, in `ADR-28`'s "what absorbs the concurrent-reap race" section.

## The predicate, in the order `ADR-28` fixes

A **candidate filter** (two rules, no tmux call), then a **four-clause predicate**, evaluated
in this exact order because the order is part of the decision (cheapest first, and clause 4's
position is a correctness invariant, not a cost one):

1. **The registry timeout test.** Both registry terms older than `timeout`. No tmux call.
2. **The attach veto.** ``#{session_attached}`` unreadable or truthy sparingly.
3. **The output timeout test.** ``window_activity_max`` unreadable or recent sparingly.
4. **The re-check.** Both signals re-read immediately before ``kill``; any difference --
   including either becoming unreadable -- skips the kill.

Clauses are named, never numbered, in comments and logs: `ADR-28`'s own numbering is the only
place that owns a number for them, and it may change without this file silently changing what
it asserts.

## `ADR-36` -- missing evidence never authorises a kill

Every clause above resolves an unreadable signal to SPARE, never to REAP, and a
``TmuxError`` raised while gathering evidence for one session is caught **per session** (see
`sweep`'s inner loop) so it resolves to SPARE for that session alone -- the sweep still
examines every other candidate. `NullRegistry.list_sessions_for_host` returns ``[]``
unconditionally, so a no-database host has no candidates at all: the reaper is inert there by
construction, not by a special case.

## `ADR-37` -- the re-check, and the subprocess bound that makes it meaningful

The gap between deciding to reap and calling ``kill`` is a real window (a sweep issues at
least ``1 + 2N`` subprocesses), so the sweep re-reads both signals immediately before the kill
and skips it if either changed or came back unreadable. That re-check is only meaningful if a
single wedged tmux command cannot hang the sweep forever -- see `TmuxConfig.timeout` (`tmux.py`),
which the reaper's own ``adapter_factory`` is expected to set and no shipped tool call touches.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from shellbox_registry import Registry
from shellbox_registry import SessionRecord as RegistrySessionRecord

from shellbox_mcp.tmux import SessionRecord as TmuxSessionRecord
from shellbox_mcp.tmux import TmuxAdapter

logger = logging.getLogger(__name__)

__all__ = ["TMUX_CALL_TIMEOUT_SECONDS", "Reaper"]

# `ADR-37`/`R69`: the bound the caller wiring the reaper's own `adapter_factory` should pass
# as `TmuxConfig.timeout` (`tmux.py`). Not an environment variable -- see `TmuxConfig.timeout`'s
# own comment for why -- and not tuned against any measurement beyond "comfortably above a
# healthy tmux round-trip, comfortably below making a wedged command expensive to wait out".
# A sweep issues at most `1 + 7N` such calls (`ADR-31`), so the sweep-level bound this buys is
# `(1 + 7N) * TMUX_CALL_TIMEOUT_SECONDS`.
TMUX_CALL_TIMEOUT_SECONDS: float = 10.0

# Matches the bare literal `shell_kill` already writes (`server.py:722`). Not a shared
# constant across the module boundary -- `W48`'s own comment on `STATUS_REAPED` is explicit
# that the string must match, not that the two call sites must import the same name.
_STATUS_REAPED = "reaped"


def _candidates(
    adapter: TmuxAdapter, registry: Registry, host_id: str
) -> list[tuple[TmuxSessionRecord, RegistrySessionRecord]]:
    """The candidate filter, `ADR-28`: two rules, both free -- no tmux call beyond the
    existing ``list_sessions()`` enumeration, and no extra registry call beyond
    ``list_sessions_for_host``.

    Neither rule is a predicate clause: they decide who is EVALUATED, not who is spared.
    `status` is not read here and is not a filter term -- a row reading `orphaned` or `reaped`
    is still a candidate if its session is still on tmux (`W48`).
    """
    records = adapter.list_sessions()
    rows_by_tmux_name = {row.tmux_name: row for row in registry.list_sessions_for_host(host_id)}
    candidates: list[tuple[TmuxSessionRecord, RegistrySessionRecord]] = []
    for record in records:
        if record.foreign:
            continue  # mid-create, or not shellbox's (R60)
        row = rows_by_tmux_name.get(record.tmux_name)
        if row is None:
            continue  # ADR-36: no registry row under this host is never reaped
        candidates.append((record, row))
    return candidates


def _epoch_to_utc(value: int) -> datetime:
    """tmux's ``#{window_activity}`` is a Unix epoch integer; the registry's columns are
    ``timestamptz``. Both clocks are this host's own clock, so there is no skew problem to
    correct for -- only a unit to convert (`ADR-28`)."""
    return datetime.fromtimestamp(value, tz=UTC)


def _decide(
    adapter: TmuxAdapter,
    record: TmuxSessionRecord,
    row: RegistrySessionRecord,
    *,
    now: datetime,
    timeout: float,
) -> int | None:
    """SPARE (``None``) or REAP (the decision-time ``window_activity_max``), for one candidate.

    MAY RAISE. The attach read and the output read both flow through
    ``TmuxAdapter._display_numeric``, which raises ``TmuxError`` on a field-count mismatch
    rather than returning ``None`` (`tmux.py:446-451`). The raise is caught **per session** at
    the call site (`sweep`) and resolves to SPARE there -- never inside this function, which
    has no safe value of its own to invent for it.

    Returning the decision-time output-timeout value on REAP, rather than a bare ``True``, is
    what lets `sweep` re-check clause 4 without a second decision pass: the re-check compares
    against exactly the value this function saw, never a value re-derived from a second call.
    """
    cutoff = now - timedelta(seconds=timeout)

    # The registry timeout test. Both terms are registry-only; a NULL `last_read_at` is a
    # known fact ("never read") and is SKIPPED as a term -- never treated as `now()`, never as
    # epoch (`ADR-28`, `ADR-36`).
    if row.last_activity_at >= cutoff:
        return None
    if row.last_read_at is not None and row.last_read_at >= cutoff:
        return None

    # The attach veto. Precedes the output timeout so an attached session pays for exactly one
    # subprocess, never a `list-windows` (`ADR-28`'s cost ordering; `A25`).
    attached = adapter.session_attached(record.tmux_name)
    if attached is None or attached:
        return None

    # The output timeout test. `None` is missing evidence, not epoch (`ADR-36`; `W52`).
    activity = adapter.window_activity_max(record.tmux_name)
    if activity is None:
        return None
    if _epoch_to_utc(activity) >= cutoff:
        return None

    return activity  # REAP; clause 4 (the re-check) runs at the kill call site


def _changed_since_decision(adapter: TmuxAdapter, tmux_name: str, decision_activity: int) -> bool:
    """`ADR-37`'s re-check, all three of its halves in one function.

    Re-reads BOTH signals a SECOND time, immediately before the kill -- never reusing the
    reads `_decide` already took. Returns whether the kill must be SKIPPED:

    * the session became attached since the decision (a `None` here reads as attached, the
      same MISSING-EVIDENCE-SPARES direction `_decide` takes for the same read);
    * the output clock advanced past what `_decide` saw, OR came back unreadable.

    A `None` from either re-read is ALWAYS "changed", never "the same answer as before" --
    stating this in code because the natural
    ``new_attached == old_attached and new_activity == old_activity`` comparison treats
    ``None == None`` as unchanged, which is the one place a kill would actually happen on
    evidence that just became unreadable.
    """
    attached_now = adapter.session_attached(tmux_name)
    if attached_now is None or attached_now:
        return True
    activity_now = adapter.window_activity_max(tmux_name)
    return activity_now is None or activity_now != decision_activity


def _reaped_row(row: RegistrySessionRecord, host_id: str) -> RegistrySessionRecord:
    """The reaper's own write (`W41`, `A26`): `status="reaped"` only, every other stored
    value carried through UNCHANGED.

    `last_activity_at` and `last_read_at` are NOT advanced to `now` -- the same rule
    `reconcile_orphans` states at `enroll.py:316-317`: noticing that a session is idle is not
    activity on it. Writing `now` here would be worse than merely wrong, because
    `upsert_session` applies `GREATEST(excluded, stored)` to `last_activity_at`
    (`postgres.py:147-149`): a `now` written here could never be lowered again, and the row
    would assert the session was active at the instant shellbox destroyed it for being idle.
    """
    return RegistrySessionRecord(
        session_id=row.session_id,
        host_id=host_id,
        tmux_name=row.tmux_name,
        owner_email=row.owner_email,
        last_activity_at=row.last_activity_at,
        last_read_at=row.last_read_at,
        status=_STATUS_REAPED,
        cwd=row.cwd,
        cols=row.cols,
        rows=row.rows,
        created_at=row.created_at,
    )


@dataclass
class Reaper:
    """`W41`'s thread. One instance per process; no election (`ADR-26`).

    ``timeout`` and ``interval`` are PLAIN CONSTRUCTOR PARAMETERS, never a `Settings` read and
    never a module constant: `Settings` resolves them once, at the single call site that
    constructs a `Reaper` (`enroll.py`'s `start_enrollment`), and hands them in as plain
    values. `_bounded_int_env` (`config.py`) guards only that ONE resolution path; it says
    nothing about this type, so a test may legally construct ``Reaper(timeout=2, ...)`` --
    small enough to age a session without a real clock, per section 3.7's mechanism, which no
    configuration an operator could set could ever be small enough to do.

    ``adapter_factory`` produces a fresh `TmuxAdapter` per sweep, matching every other
    per-call construction in this codebase (`server.py`'s module docstring: zero in-process
    session state). The reaper's own factory is expected to build a `TmuxConfig` carrying a
    concrete `timeout` (`ADR-37`/`R69`) -- unlike every tool call's adapter, which leaves it
    `None`.
    """

    registry: Registry
    adapter_factory: Callable[[], TmuxAdapter]
    host_id: str
    timeout: float
    interval: float
    now: Callable[[], datetime] = lambda: datetime.now(UTC)

    _stop: threading.Event = field(default_factory=threading.Event, init=False, repr=False)
    _thread: threading.Thread | None = field(default=None, init=False, repr=False)
    sweeps: int = field(default=0, init=False)

    def sweep(self) -> None:
        """One sweep. Never raises: this is the OUTER of the two guards `ADR-36` requires.

        The inner, per-session guard lives in `_sweep`'s loop -- this one is the last resort,
        matching `Heartbeat.beat`'s shape (`enroll.py:546-548`), so ONE malformed sweep (for
        example `list_sessions`'s all-malformed `TmuxError`, `tmux.py:843-858`, which is
        genuinely a per-SWEEP failure: the whole enumeration is untrustworthy) still leaves
        the thread alive for the next interval (`R57`, `A27`).
        """
        try:
            self._sweep()
        except Exception:  # noqa: BLE001 -- the thread must survive; see the docstring
            logger.warning(
                "reaper sweep failed for host %s; retrying next interval",
                self.host_id,
                exc_info=True,
            )
        finally:
            self.sweeps += 1

    def _sweep(self) -> None:
        adapter = self.adapter_factory()
        now = self.now()
        candidates = _candidates(adapter, self.registry, self.host_id)
        reaped = 0
        for record, row in candidates:
            try:
                if self._evaluate_and_maybe_reap(adapter, record, row, now):
                    reaped += 1
            except Exception:  # noqa: BLE001 -- per session, never per sweep (ADR-36, A49)
                logger.warning(
                    "reaper: evidence gathering or kill failed for session %r on host %s; "
                    "sparing it this sweep",
                    record.tmux_name,
                    self.host_id,
                    exc_info=True,
                )
                continue
        # A39/W53: the sweep's own audit trail. Outside the loop and unconditional, so it
        # fires even when `candidates` is empty and `reaped` is zero -- "swept and reaped
        # nothing" is an observable log line here, not silence indistinguishable from the
        # reaper not running at all. `len(candidates)` is taken AFTER both filter rules in
        # `_candidates` already ran, which is what makes A41's zero-candidate assertion on a
        # no-database host falsifiable rather than vacuous.
        logger.info(
            "reap sweep on host %s: %d candidate(s), %d reaped",
            self.host_id,
            len(candidates),
            reaped,
        )

    def _evaluate_and_maybe_reap(
        self,
        adapter: TmuxAdapter,
        record: TmuxSessionRecord,
        row: RegistrySessionRecord,
        now: datetime,
    ) -> bool:
        """The predicate's clauses 1-3, then clause 4 and the kill, for ONE candidate.

        Returns whether this session was actually reaped. MAY RAISE -- the caller (`_sweep`)
        is the per-session guard.
        """
        decision_activity = _decide(adapter, record, row, now=now, timeout=self.timeout)
        if decision_activity is None:
            return False  # SPARE: clauses 1-3

        # Clause 4, `ADR-37`: re-read both signals a SECOND time, immediately before the kill.
        if _changed_since_decision(adapter, record.tmux_name, decision_activity):
            return False  # skip the kill; do not call it at all

        # The kill, and the write. `kill` returning `False` means the session was ALREADY
        # gone -- a concurrent `shell_kill`, or another process's reaper (no election,
        # `ADR-26`) -- so THIS reaper did not end it and must not claim it did (table row 20).
        if not adapter.kill(record.tmux_name):
            return False
        self.registry.upsert_session(_reaped_row(row, self.host_id))
        logger.info(
            "reaper: reaped session %s (tmux_name=%r) on host %s: idle past the %ss timeout",
            row.session_id,
            record.tmux_name,
            self.host_id,
            self.timeout,
        )
        return True

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            self.sweep()

    def start(self) -> None:
        """Start the thread. Sweeps ONCE, immediately, before entering the wait loop.

        A deliberate divergence from `Heartbeat._run`, which sleeps first
        (`while not self._stop.wait(self.interval): self.beat()`, `enroll.py:552-554`). This
        is a LATENCY argument only, not a correctness one: with clock 2 cut (`ADR-26`) no
        invariant requires an immediate sweep, but a restarted process inherits a host whose
        sessions may already be well past the timeout, and waiting a full interval to notice
        is pure delay with no compensating benefit. It is also safe: `Reaper` is started from
        `start_enrollment`'s inner `run()` AFTER `enroll()` has returned, so stamping and
        orphan reconciliation have already run by the time this first sweep fires.
        """
        if self._thread is not None:
            return
        thread_name = f"shellbox-reaper-{self.host_id[:8]}"
        # `W53`: the one startup line for this thread, naming the resolved config it is
        # about to run with. A joint line naming `Heartbeat` too would ideally live beside
        # the call site that starts both (`start_enrollment`, `enroll.py`) -- this is this
        # thread's own half of that observability requirement, made from a module that has
        # no reach into the other thread's name.
        logger.info(
            "reaper starting on host %s as thread %r: idle_timeout_seconds=%s, "
            "reap_interval_seconds=%s",
            self.host_id,
            thread_name,
            self.timeout,
            self.interval,
        )
        self.sweep()
        self._thread = threading.Thread(target=self._run, name=thread_name, daemon=True)
        self._thread.start()

    def stop(self, *, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
