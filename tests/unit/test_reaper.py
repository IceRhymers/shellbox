"""`W41` -- the `Reaper` thread against fakes: `A27`, `A40` (all three halves of the
re-check), `A49`, `A50`. No real tmux and no real Postgres; the tmux-lane and registry-lane
criteria for the SAME work item live in `tests/tmux/test_reap_*.py` and
`tests/registry/test_reaper_registry.py`.

`T-P5-SWEEP-SURVIVES`, `T-P5-RECHECK-SKIP`, `T-P5-PER-SESSION-ERROR`, `T-P5-SWEEP-BOUNDED`.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from shellbox_mcp.errors import TmuxError
from shellbox_mcp.reaper import Reaper
from shellbox_mcp.tmux import SessionRecord as TmuxSessionRecord
from shellbox_mcp.tmux import TmuxAdapter, TmuxConfig
from shellbox_registry.base import SessionRecord as RegistrySessionRecord

HOST_ID = "unit-reaper-host"


def _tmux_record(name: str) -> TmuxSessionRecord:
    """A non-foreign `SessionRecord`: any non-`None` incarnation passes the candidate filter."""
    return TmuxSessionRecord(
        tmux_name=name,
        created_at=0,
        last_activity_at=0,
        cols=80,
        rows=24,
        alive=True,
        incarnation=f"inc-{name}",
        cwd=None,
        stamped_cwd=None,
    )


@dataclass
class FakeRegistry:
    """The two `Registry` primitives `Reaper` actually calls. See `PlantedRegistry` in
    `tests/conftest.py` for the tmux-lane sibling of this fake."""

    host_id: str
    rows: dict[str, RegistrySessionRecord] = field(default_factory=dict)
    written: list[RegistrySessionRecord] = field(default_factory=list)

    def plant(
        self, tmux_name: str, *, last_activity_at: datetime, last_read_at: datetime | None = None
    ) -> None:
        self.rows[tmux_name] = RegistrySessionRecord(
            session_id=f"{self.host_id}:{tmux_name}",
            host_id=self.host_id,
            tmux_name=tmux_name,
            owner_email="unit-reaper@example.com",
            last_activity_at=last_activity_at,
            last_read_at=last_read_at,
            status="live",
        )

    def list_sessions_for_host(self, host_id: str) -> list[RegistrySessionRecord]:
        return list(self.rows.values()) if host_id == self.host_id else []

    def upsert_session(self, record: RegistrySessionRecord) -> None:
        self.rows[record.tmux_name] = record
        self.written.append(record)


NOW = datetime.now(UTC)
OLD_EPOCH = int((NOW - timedelta(hours=1)).timestamp())


def _aged_registry(*, session: str = "subject") -> FakeRegistry:
    registry = FakeRegistry(host_id=HOST_ID)
    registry.plant(session, last_activity_at=NOW - timedelta(hours=1))
    return registry


def _reaper(adapter: object, registry: FakeRegistry, **overrides: object) -> Reaper:
    settings: dict[str, object] = {
        "host_id": HOST_ID,
        "timeout": 60,
        "interval": 9999,
        "now": lambda: NOW,
    }
    settings.update(overrides)
    return Reaper(registry, lambda: adapter, **settings)  # type: ignore[arg-type]


# --------------------------------------------------------------------------------------
# A27 / T-P5-SWEEP-SURVIVES / R57
# --------------------------------------------------------------------------------------


class _ExplodingThenFineAdapter:
    """Raises on its FIRST `list_sessions()` call (a per-sweep failure -- `list_sessions`'s
    own all-malformed guard is exactly this shape), then behaves normally."""

    def __init__(self) -> None:
        self.calls = 0

    def list_sessions(self) -> list[TmuxSessionRecord]:
        self.calls += 1
        if self.calls == 1:
            raise TmuxError("boom: simulating list_sessions's all-malformed guard")
        return []


def test_a_sweep_that_raises_does_not_stop_the_next_sweep(caplog) -> None:
    """A27/T-P5-SWEEP-SURVIVES. The outer, per-SWEEP guard (`sweep`'s own `try/except`)."""
    adapter = _ExplodingThenFineAdapter()
    reaper = _reaper(adapter, FakeRegistry(host_id=HOST_ID))

    with caplog.at_level(logging.WARNING):
        reaper.sweep()
    assert reaper.sweeps == 1, "the sweep counter must advance even when the sweep raised"

    reaper.sweep()
    assert reaper.sweeps == 2
    assert adapter.calls == 2, "the SECOND sweep must actually run tmux calls, not merely not-raise"


# --------------------------------------------------------------------------------------
# A49 / T-P5-PER-SESSION-ERROR
# --------------------------------------------------------------------------------------


class _OneBadOneGoodAdapter:
    """`session_attached` raises for `"bad"` (simulating `_display_numeric`'s field-count
    mismatch, `tmux.py:446-451`) and answers normally for `"good"`."""

    def __init__(self) -> None:
        self.killed: list[str] = []

    def list_sessions(self) -> list[TmuxSessionRecord]:
        return [_tmux_record("bad"), _tmux_record("good")]

    def session_attached(self, name: str) -> bool | None:
        if name == "bad":
            raise TmuxError("display-message returned the wrong field count", session=name)
        return False

    def window_activity_max(self, name: str) -> int | None:
        return OLD_EPOCH

    def kill(self, name: str) -> bool:
        self.killed.append(name)
        return True


def test_one_sessions_evidence_failure_does_not_stop_the_others(caplog) -> None:
    """A49/T-P5-PER-SESSION-ERROR. The INNER, per-SESSION guard."""
    adapter = _OneBadOneGoodAdapter()
    registry = FakeRegistry(host_id=HOST_ID)
    registry.plant("bad", last_activity_at=NOW - timedelta(hours=1))
    registry.plant("good", last_activity_at=NOW - timedelta(hours=1))
    reaper = _reaper(adapter, registry)

    with caplog.at_level(logging.WARNING):
        reaper.sweep()

    assert adapter.killed == ["good"], "'bad's failure must not stop 'good' from being evaluated"
    assert registry.rows["good"].status == "reaped"
    assert registry.rows["bad"].status == "live", "'bad' must be SPARED (ADR-36), not reaped"


# --------------------------------------------------------------------------------------
# A40 / T-P5-RECHECK-SKIP -- all three halves of `ADR-37`'s re-check
# --------------------------------------------------------------------------------------


@dataclass
class _ScriptedAdapter:
    """A fake whose second read differs from its first -- exactly what `A40` needs to drive
    the re-check's three halves. `attached_sequence`/`activity_sequence` are consumed
    IN ORDER: [decision, recheck] for whichever reads the predicate actually takes."""

    attached_sequence: list[bool | None]
    activity_sequence: list[int | None]
    killed: list[str] = field(default_factory=list)

    def list_sessions(self) -> list[TmuxSessionRecord]:
        return [_tmux_record("subject")]

    def session_attached(self, name: str) -> bool | None:
        return self.attached_sequence.pop(0)

    def window_activity_max(self, name: str) -> int | None:
        return self.activity_sequence.pop(0)

    def kill(self, name: str) -> bool:
        self.killed.append(name)
        return True


def test_recheck_proceeds_to_kill_when_neither_signal_changed() -> None:
    """Positive control: without any of the three differences below, the kill DOES happen --
    which is what makes the negative assertions below meaningful rather than vacuous."""
    adapter = _ScriptedAdapter(
        attached_sequence=[False, False], activity_sequence=[OLD_EPOCH, OLD_EPOCH]
    )
    _reaper(adapter, _aged_registry()).sweep()
    assert adapter.killed == ["subject"]


def test_recheck_skips_the_kill_when_the_session_becomes_attached() -> None:
    """A40(a). Attached at decision-time (False), attached at the re-check (True) -> skip."""
    adapter = _ScriptedAdapter(attached_sequence=[False, True], activity_sequence=[OLD_EPOCH])
    _reaper(adapter, _aged_registry()).sweep()
    assert adapter.killed == [], "kill must not be called once the session became attached"


def test_recheck_skips_the_kill_when_the_window_clock_advances() -> None:
    """A40(b). The half revision 4 asserted nowhere: a window clock that moved between the
    decision and the kill must skip it too, even though attach never changed."""
    adapter = _ScriptedAdapter(
        attached_sequence=[False, False], activity_sequence=[OLD_EPOCH, OLD_EPOCH + 100]
    )
    _reaper(adapter, _aged_registry()).sweep()
    assert adapter.killed == [], "kill must not be called once the window clock advanced"


def test_recheck_skips_the_kill_when_the_reattach_read_returns_none() -> None:
    """A40(c), the attach half: a `None` at the re-check is a CHANGE, never 'the same answer'."""
    adapter = _ScriptedAdapter(attached_sequence=[False, None], activity_sequence=[OLD_EPOCH])
    _reaper(adapter, _aged_registry()).sweep()
    assert adapter.killed == [], "a None re-read of session_attached must skip the kill"


def test_recheck_skips_the_kill_when_the_reactivity_read_returns_none() -> None:
    """A40(c), the activity half: same rule, the other signal."""
    adapter = _ScriptedAdapter(
        attached_sequence=[False, False], activity_sequence=[OLD_EPOCH, None]
    )
    _reaper(adapter, _aged_registry()).sweep()
    assert adapter.killed == [], "a None re-read of window_activity_max must skip the kill"


# --------------------------------------------------------------------------------------
# A50 / T-P5-SWEEP-BOUNDED
# --------------------------------------------------------------------------------------


def _write_wedging_tmux(tmp_path: Path, session_names: list[str]) -> Path:
    """A stand-in `tmux` binary: answers `list-sessions` and the two `list_sessions`
    enrichment reads (`pane_current_path`, `@shellbox_cwd`) instantly and validly, so real
    `SessionRecord`s reach the predicate -- then hangs forever on EVERYTHING else, which is
    every read the predicate itself performs (`session_attached`, `list-windows`,
    `kill-session`). That is what lets `TmuxConfig.timeout` be the only thing standing
    between this test and a process that never returns.
    """
    records = "\\n".join(
        "\\t".join([name, "1700000000", "1700000000", "80", "24", "0", f"inc-{name}", "/tmp"])
        for name in session_names
    )
    script = tmp_path / "fake-tmux-wedge"
    script.write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        f"  *list-sessions*) printf '{records}\\n'; exit 0 ;;\n"
        "  *pane_current_path*|*shellbox_cwd*) printf 'ok\\tval\\n'; exit 0 ;;\n"
        "  *) sleep 999999 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def test_a_wedged_tmux_command_does_not_block_the_sweep_past_its_bound(tmp_path: Path) -> None:
    """A50/T-P5-SWEEP-BOUNDED/R69.

    `TmuxConfig(timeout=0.5)` against a `tmux_bin` that never exits. Every candidate reaches
    a real read that hangs and yields a `TmuxError` (the per-session guard catches it, per
    `A49`'s same mechanism), no session is reaped, and the sweep returns -- inside `ADR-31`'s
    worst-case bound of `(1 + 7N) * timeout`. Runs on a thread the TEST joins with its own
    (generous) deadline, so an absent bound fails the assertion below rather than hanging CI.

    `Reaper(timeout=60)`, with each planted row's `last_activity_at` at `now - 3600`, per
    section 3.7's unit-lane exception: the row is planted (not tmux-derived), which is
    legitimate here because A50's defect is the absent subprocess bound, not the row's
    provenance.
    """
    names = ["wedge0", "wedge1"]
    bound = 0.5
    script = _write_wedging_tmux(tmp_path, names)
    socket_path = f"/tmp/sbxu{uuid.uuid4().hex[:6]}"
    adapter = TmuxAdapter(TmuxConfig(socket_path=socket_path, tmux_bin=str(script), timeout=bound))

    registry = FakeRegistry(host_id=HOST_ID)
    for name in names:
        registry.plant(name, last_activity_at=NOW - timedelta(seconds=3600))

    reaper = _reaper(adapter, registry, timeout=60)

    outcome: dict[str, BaseException] = {}

    def run() -> None:
        try:
            reaper.sweep()
        except BaseException as exc:  # noqa: BLE001 -- captured for the assertion, not swallowed
            outcome["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    worst_case_bound = (1 + 7 * len(names)) * bound
    started = time.monotonic()
    thread.start()
    # A generous margin ON TOP of the worst-case bound for the JOIN's own deadline: if the
    # bound is silently absent, this test must fail an assertion, not hang the test suite.
    thread.join(timeout=worst_case_bound + 15)
    elapsed = time.monotonic() - started

    assert not thread.is_alive(), (
        f"the sweep did not return within {worst_case_bound + 15:.1f}s: the tmux subprocess "
        "bound is not being applied"
    )
    assert "error" not in outcome, f"sweep() must never raise: {outcome.get('error')!r}"
    assert elapsed < worst_case_bound, (
        f"sweep took {elapsed:.2f}s, over ADR-31's worst-case bound of {worst_case_bound:.2f}s "
        f"for {len(names)} candidate(s) at a {bound}s per-command timeout"
    )
    assert registry.written == [], "no session should have been reaped: every read timed out"


# --------------------------------------------------------------------------------------
# A39 / T-P5-SWEEP-LOG
# --------------------------------------------------------------------------------------


class _NoSessionsAdapter:
    """`list_sessions()` returns nothing, and no row is planted either -- the candidate list
    is empty, so there is nothing to evaluate and nothing to reap."""

    def list_sessions(self) -> list[TmuxSessionRecord]:
        return []


def test_a_sweep_with_zero_candidates_still_logs_the_zero(caplog) -> None:
    """A39/T-P5-SWEEP-LOG. The per-sweep INFO line sits OUTSIDE the evaluation loop, so it
    fires even when there is nothing to evaluate -- "swept and reaped nothing" must be an
    observable log line, distinguishable from the reaper not running at all."""
    reaper = _reaper(_NoSessionsAdapter(), FakeRegistry(host_id=HOST_ID))

    with caplog.at_level(logging.INFO):
        reaper.sweep()

    info_lines = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert any("0 candidate" in line and "0 reaped" in line for line in info_lines), (
        f"expected an INFO line naming zero candidates and zero reaped; got {info_lines!r}"
    )


class _AttachedVetoAdapter:
    """Every tmux session this fake reports reads as attached, so the attach veto spares
    each candidate that reaches it without needing the output-timeout or kill machinery --
    enough to drive the sweep's own logging."""

    def __init__(self, names: list[str]) -> None:
        self._names = names

    def list_sessions(self) -> list[TmuxSessionRecord]:
        return [_tmux_record(name) for name in self._names]

    def session_attached(self, name: str) -> bool | None:
        return True


def test_the_logged_candidate_count_excludes_a_row_less_session(caplog) -> None:
    """A39/T-P5-SWEEP-LOG. Two tmux sessions, but only one has a registry row: the has-a-row
    filter rule (`ADR-36`) must exclude the row-less one BEFORE the count in the log line is
    taken, not after -- which is what makes `A41`'s zero-candidate assertion on a
    no-database host falsifiable rather than vacuous."""
    registry = _aged_registry(session="subject")  # plants ONLY "subject"
    adapter = _AttachedVetoAdapter(["subject", "stray"])
    reaper = _reaper(adapter, registry)

    with caplog.at_level(logging.INFO):
        reaper.sweep()

    info_lines = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert any("1 candidate" in line for line in info_lines), (
        "expected the logged candidate count to be 1 -- only 'subject' has a registry row, "
        f"and 'stray' must be excluded before the count is taken; got {info_lines!r}"
    )


class _ReapableAdapter:
    """Not attached, and its window clock is old enough to fail the output timeout too, and
    neither signal changes on the re-check -- the one candidate this fake produces is
    actually reaped."""

    def __init__(self) -> None:
        self.killed: list[str] = []

    def list_sessions(self) -> list[TmuxSessionRecord]:
        return [_tmux_record("subject")]

    def session_attached(self, name: str) -> bool | None:
        return False

    def window_activity_max(self, name: str) -> int | None:
        return OLD_EPOCH

    def kill(self, name: str) -> bool:
        self.killed.append(name)
        return True


def test_a_reap_logs_which_session_was_reaped(caplog) -> None:
    """A39/T-P5-SWEEP-LOG. A sweep that reaps must log the session it reaped, not merely the
    sweep-level counts."""
    registry = _aged_registry(session="subject")
    adapter = _ReapableAdapter()
    reaper = _reaper(adapter, registry)

    with caplog.at_level(logging.INFO):
        reaper.sweep()

    assert adapter.killed == ["subject"], "the fixture itself must actually reap 'subject'"
    assert "subject" in caplog.text, (
        f"expected the reaped session's tmux_name to appear in the logs; got {caplog.text!r}"
    )
