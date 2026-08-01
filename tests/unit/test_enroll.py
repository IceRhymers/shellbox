"""`enroll.py` — E1–E7, with a fake registry so every failure mode is reachable.

Two properties dominate this file, and they are the two that review kept catching elsewhere:

* **Nothing here may raise into a caller.** Every function is driven with a registry that
  raises on every method, and the assertion is that the call *returns* and says what happened.
* **Assert on what was written, not on what was returned.** Four bugs in `identity.py` were
  invisible to return-value assertions, so the fake registry records rows and the tests read
  them back.
"""

from __future__ import annotations

import threading
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from shellbox_mcp import enroll, identity
from shellbox_mcp.enroll import (
    EnrollmentResult,
    Heartbeat,
    reconcile_orphans,
    recover_host_id,
    stamp_sessions,
)
from shellbox_mcp.enroll import (
    enroll as run_enroll,
)
from shellbox_registry import HostRecord, SessionRecord

T0 = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)
T1 = datetime(2026, 7, 31, 12, 5, tzinfo=UTC)


class FakeRegistry:
    """Records what was written. Optionally fails, per method, to order the failure tests."""

    def __init__(self, *, fail: set[str] | None = None) -> None:
        self.hosts: dict[str, HostRecord] = {}
        self.sessions: dict[str, SessionRecord] = {}
        self.fail = fail or set()
        self.host_writes = 0

    def _maybe_fail(self, method: str) -> None:
        if method in self.fail:
            raise RuntimeError(f"{method} is unavailable")

    def upsert_host(self, record: HostRecord) -> None:
        self._maybe_fail("upsert_host")
        self.host_writes += 1
        existing = self.hosts.get(record.host_id)
        # Mirrors the real registry's two conflict rules, because two tests are *about* them:
        # `enrolled_at` is preserved and `last_seen_at` never moves backwards.
        if existing is not None:
            # `dataclasses.replace`, not `**record.__dict__`: HostRecord is `slots=True`, so it
            # has no `__dict__` at all. The first version of this fake used one, and the
            # AttributeError surfaced as `beat() is False` and "enrolled_at was reissued" —
            # swallowed by the never-raise guards under test. A fake that fails silently inside
            # the code it is testing is worse than no fake.
            record = replace(
                record,
                enrolled_at=existing.enrolled_at,
                last_seen_at=max(record.last_seen_at, existing.last_seen_at),
            )
        self.hosts[record.host_id] = record

    def upsert_session(self, record: SessionRecord) -> None:
        self._maybe_fail("upsert_session")
        self.sessions[record.session_id] = record

    def get_host(self, host_id: str) -> HostRecord | None:
        self._maybe_fail("get_host")
        return self.hosts.get(host_id)

    def get_session(self, session_id: str) -> SessionRecord | None:
        self._maybe_fail("get_session")
        return self.sessions.get(session_id)

    def list_sessions_for_host(self, host_id: str) -> list[SessionRecord]:
        self._maybe_fail("list_sessions_for_host")
        return [r for r in self.sessions.values() if r.host_id == host_id]


class FakeAdapter:
    """A tmux adapter that answers from a dict. `raises` makes every read blow up."""

    def __init__(self, stamps: dict[str, str] | None = None, *, raises: bool = False) -> None:
        self.stamps = dict(stamps or {})
        self.raises = raises
        self.written: list[tuple[str, str]] = []

    def list_sessions(self) -> list[object]:
        if self.raises:
            raise RuntimeError("list-sessions failed")
        return [type("S", (), {"tmux_name": name})() for name in self.stamps]

    def read_host_stamp(self, name: str) -> str | None:
        return self.stamps.get(name) or None

    def stamp_host_id(self, name: str, host_id: str) -> bool:
        self.written.append((name, host_id))
        self.stamps[name] = host_id
        return True


def _enroll(
    registry: object, adapter: object, tmp_path: Path, **kwargs: object
) -> EnrollmentResult:
    """Enroll the way `serve` does: identity resolved FIRST, then enrollment.

    That order is not incidental. `enroll` persists E2's `owner_email` by merging into
    `host.json`, and `identity.py` refuses to *create* that file outside its arbitrated
    assignment path — so enrolling into a state dir where identity was never resolved cannot
    cache the owner. See `test_enrolling_without_a_resolved_identity_warns_rather_than_pretending`.
    """
    identity.resolve_host_id(str(tmp_path))
    defaults: dict[str, object] = {
        "state_dir": str(tmp_path),
        "host_id": "h1",
        "kind": "lakebox",
        "tmux_socket": "/s.sock",
        "tmux_bin": "tmux",
        "credential_email": "creator@example.com",
        "now": lambda: T0,
    }
    defaults.update(kwargs)
    return run_enroll(registry, adapter, **defaults)  # type: ignore[arg-type]


# ------------------------------------------------------------------- E4: the hosts row
def test_a_successful_pass_writes_the_row_with_everything_it_knows(tmp_path: Path) -> None:
    registry = FakeRegistry()
    result = _enroll(registry, FakeAdapter(), tmp_path, sandbox_id="sbx-1", gateway_host="gw")

    assert result.enrolled and result.owner_email == "creator@example.com"
    row = registry.hosts["h1"]
    assert (row.owner_email, row.kind, row.status) == ("creator@example.com", "lakebox", "active")
    assert (row.sandbox_id, row.gateway_host) == ("sbx-1", "gw")
    # E5's guard reads this back on a later pass and refuses to orphan on a mismatch, so an
    # unrecorded socket would silently disable the guard rather than the feature.
    assert row.tmux_socket == "/s.sock"


def test_first_enrollment_wins_across_passes(tmp_path: Path) -> None:
    """E4. `enrolled_at` is the host's birth certificate; a later pass must not reissue it."""
    registry = FakeRegistry()
    _enroll(registry, FakeAdapter(), tmp_path)
    _enroll(registry, FakeAdapter(), tmp_path, now=lambda: T1)

    row = registry.hosts["h1"]
    assert row.enrolled_at == T0, "a later pass reissued enrolled_at"
    assert row.last_seen_at == T1, "last_seen_at must advance"


# ----------------------------------------------------------------- E2: the owner ladder
def test_the_credential_wins_and_corrects_the_cache(tmp_path: Path) -> None:
    """E2a — the case that fires when a sandbox changes hands."""
    registry = FakeRegistry()
    _enroll(registry, FakeAdapter(), tmp_path, credential_email="first@example.com")
    result = _enroll(registry, FakeAdapter(), tmp_path, credential_email="second@example.com")

    assert result.reconciled_owner is True
    assert registry.hosts["h1"].owner_email == "second@example.com"


def test_the_cache_serves_when_no_credential_is_available(tmp_path: Path) -> None:
    """E2b. After a PAT reset *plus* a reboot this is the only source there is: the CLI's OAuth
    token cache is boot-templated into wiped `/run`."""
    registry = FakeRegistry()
    _enroll(registry, FakeAdapter(), tmp_path, credential_email="owner@example.com")

    result = _enroll(registry, FakeAdapter(), tmp_path, credential_email=None)
    assert (result.owner_email, result.owner_source) == ("owner@example.com", "cache")
    assert result.enrolled


def test_nothing_available_defers_and_writes_no_row_at_all(tmp_path: Path) -> None:
    """🔴 E2d. No placeholder, not even a plausible one.

    `hosts.owner_email` is NOT NULL and is the column #7's ACL will filter on, so a fake
    principal is not a harmless gap — it accumulates real rows under a name that a later
    `WHERE owner_email = …` either grants to nobody or matches for whoever owns that string.
    """
    registry = FakeRegistry()
    result = _enroll(registry, FakeAdapter(), tmp_path, credential_email=None)

    assert (result.owner_email, result.enrolled, result.owner_source) == (None, False, "deferred")
    assert result.error and "deferred" in result.error
    assert registry.hosts == {}, "a row was written with no resolvable owner"


def test_enrolling_without_a_resolved_identity_warns_rather_than_pretending(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """🔴 The ordering dependency, pinned because it is invisible from either side alone.

    `enroll` persists E2's result by merging into `host.json`, and `identity.py` will not
    *create* that file outside its arbitrated assignment path — a merge that could create one
    would let two processes each merge a `host_id` in and split the host. So enrolling into a
    state dir where identity was never resolved writes the `hosts` row (E4 is unaffected) but
    **cannot cache the owner**, which silently breaks E2b on the next credential-less boot.

    The requirement is therefore not "never happens" but "never quiet": it must warn. Found by
    this test failing when the helper did not seed identity first.
    """
    registry = FakeRegistry()
    with caplog.at_level("WARNING"):
        result = run_enroll(
            registry,
            FakeAdapter(),
            state_dir=str(tmp_path),
            host_id="h1",
            kind="lakebox",
            tmux_socket="/s.sock",
            tmux_bin="tmux",
            credential_email="creator@example.com",
            now=lambda: T0,
        )

    assert result.enrolled, "E4 does not depend on the identity cache and must still land"
    assert not (tmp_path / identity.HOST_JSON_NAME).exists()
    assert any("not updating identity cache" in r.message for r in caplog.records), (
        "the owner was not cached and nothing said so"
    )


def test_the_credential_is_resolved_lazily_on_the_enrollment_thread(tmp_path: Path) -> None:
    """The default is a *callable* so the network call happens inside `enroll`, not in the
    caller that decides whether to spawn a thread for it."""
    calls: list[str] = []

    def credential() -> str:
        calls.append(threading.current_thread().name)
        return "lazy@example.com"

    result = _enroll(FakeRegistry(), FakeAdapter(), tmp_path, credential_email=credential)
    assert result.owner_email == "lazy@example.com"
    assert len(calls) == 1, "the credential was resolved more than once per pass"


# ------------------------------------------------------- never raising, whatever fails
@pytest.mark.parametrize(
    "failing", ["upsert_host", "get_host", "list_sessions_for_host", "upsert_session"]
)
def test_no_registry_failure_can_raise_out_of_enrollment(tmp_path: Path, failing: str) -> None:
    """The rule the whole module exists to keep: a Lakebase outage must never reach a caller.

    Parametrized over every method rather than testing one, because the sequence catches per
    step and the interesting question is whether *each* step's failure is contained.
    """
    registry = FakeRegistry(fail={failing})
    result = _enroll(registry, FakeAdapter(), tmp_path)

    assert isinstance(result, EnrollmentResult)
    if failing == "upsert_host":
        assert not result.enrolled
        assert result.error and "RuntimeError" in result.error
        # Partial results survive alongside the error rather than being replaced by it.
        assert result.owner_email == "creator@example.com"
    else:
        assert result.enrolled, f"{failing} failing should not have stopped E4"


def test_a_broken_tmux_does_not_stop_the_row_landing(tmp_path: Path) -> None:
    registry = FakeRegistry()
    result = _enroll(registry, FakeAdapter(raises=True), tmp_path)
    assert result.enrolled and registry.hosts["h1"].owner_email == "creator@example.com"


# ---------------------------------------------------------------- E5: orphan reconciliation
def _seed(registry: FakeRegistry, *names: str, host_id: str = "h1", status: str = "live") -> None:
    registry.hosts[host_id] = HostRecord(
        host_id=host_id,
        kind="lakebox",
        owner_email="o@example.com",
        last_seen_at=T0,
        status="active",
        tmux_socket="/s.sock",
        enrolled_at=T0,
    )
    for name in names:
        registry.sessions[f"{host_id}:{name}"] = SessionRecord(
            session_id=f"{host_id}:{name}",
            host_id=host_id,
            tmux_name=name,
            owner_email="o@example.com",
            last_activity_at=T0,
            status=status,
            created_at=T0,
        )


def test_a_row_whose_session_is_gone_becomes_orphaned(tmp_path: Path) -> None:
    """What tells the truth after a sandbox restart: tmux's socket is outside `$HOME`, so the
    server and every session die, and these rows are all that still claim otherwise."""
    registry = FakeRegistry()
    _seed(registry, "alive", "dead")

    count = reconcile_orphans(
        registry,
        FakeAdapter({"alive": "h1"}),
        host_id="h1",
        owner_email="o@example.com",
        expected_socket="/s.sock",
    )

    assert count == 1
    assert registry.sessions["h1:dead"].status == "orphaned"
    assert registry.sessions["h1:alive"].status == "live"


def test_orphaning_does_not_advance_activity_timestamps(tmp_path: Path) -> None:
    """Noticing a session is gone is not activity on it. Stamping `last_activity_at` here would
    make a long-dead session look freshly used to #5's reaper, which reads that column."""
    registry = FakeRegistry()
    _seed(registry, "dead")
    reconcile_orphans(
        registry,
        FakeAdapter(),
        host_id="h1",
        owner_email="o@example.com",
        expected_socket="/s.sock",
    )
    assert registry.sessions["h1:dead"].last_activity_at == T0


def test_a_socket_mismatch_refuses_to_orphan_anything(tmp_path: Path) -> None:
    """🔴 The Critic-9 guard. "No sessions here" and "wrong server" are the same evidence.

    If this process resolved a different socket than the `hosts` row records — a different
    `$HOME`, an operator's `SHELLBOX_STATE_DIR`, a `sudo` invocation — then every live session
    is on the *other* socket, and orphaning every row would be a mass falsification of a
    healthy host's inventory. The mismatch is the bug; reconciling would bury it.
    """
    registry = FakeRegistry()
    _seed(registry, "alive")

    count = reconcile_orphans(
        registry,
        FakeAdapter(),  # reports no sessions, exactly as a wrong socket would
        host_id="h1",
        owner_email="o@example.com",
        expected_socket="/a-different.sock",
    )

    assert count == 0
    assert registry.sessions["h1:alive"].status == "live", "a healthy host's rows were falsified"


def test_a_socket_mismatch_is_logged_at_critical(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    registry = FakeRegistry()
    _seed(registry, "alive")
    with caplog.at_level("CRITICAL"):
        reconcile_orphans(
            registry,
            FakeAdapter(),
            host_id="h1",
            owner_email="o@example.com",
            expected_socket="/other.sock",
        )
    assert any(r.levelname == "CRITICAL" for r in caplog.records)


def test_a_failed_tmux_read_orphans_nothing(tmp_path: Path) -> None:
    """🔴 A broken tmux must never be read as "no sessions".

    `list_sessions` raises for everything except the two measured no-server signatures, so
    treating an exception as an empty set would orphan every live session on the host on the
    strength of a parse error. That is the same "unknown stderr must never map to empty list"
    rule the adapter enforces one layer down.
    """
    registry = FakeRegistry()
    _seed(registry, "alive")
    count = reconcile_orphans(
        registry,
        FakeAdapter(raises=True),
        host_id="h1",
        owner_email="o@example.com",
        expected_socket="/s.sock",
    )
    assert count == 0
    assert registry.sessions["h1:alive"].status == "live"


def test_an_already_orphaned_row_is_not_rewritten(tmp_path: Path) -> None:
    """Idempotence, and it is load-bearing: enrollment runs on every start."""
    registry = FakeRegistry()
    _seed(registry, "dead", status="orphaned")
    assert (
        reconcile_orphans(
            registry,
            FakeAdapter(),
            host_id="h1",
            owner_email="o@example.com",
            expected_socket="/s.sock",
        )
        == 0
    )


# ----------------------------------------------------------------- W7f: the tmux stamp
def test_one_agreeing_stamp_is_recovered() -> None:
    assert recover_host_id(FakeAdapter({"a": "host-A", "b": "host-A"})) == "host-A"


def test_no_stamps_recovers_nothing() -> None:
    assert recover_host_id(FakeAdapter({"a": "", "b": ""})) is None
    assert recover_host_id(FakeAdapter()) is None


def test_disagreeing_stamps_resolve_deterministically(caplog: pytest.LogCaptureFixture) -> None:
    """🔴 Not "the first one" — that would reintroduce the split this mechanism prevents.

    Two concurrent processes reading the same disagreeing set must adopt the *same* value, or
    the mitigation for the fork becomes a second cause of it. Lexicographically smallest is a
    choice every process makes identically, even on an already-broken host.
    """
    with caplog.at_level("CRITICAL"):
        chosen = recover_host_id(FakeAdapter({"a": "zzz", "b": "aaa", "c": "mmm"}))
    assert chosen == "aaa"
    assert any(r.levelname == "CRITICAL" and "re-keyed" in r.message for r in caplog.records)


def test_a_broken_tmux_recovers_nothing_rather_than_raising() -> None:
    assert recover_host_id(FakeAdapter(raises=True)) is None


def test_unstamped_sessions_are_stamped_and_stamped_ones_left_alone() -> None:
    """Sessions created before this code shipped carry no stamp, so on the first upgraded host
    recovery would find nothing. Stamping during enrollment closes that window."""
    adapter = FakeAdapter({"old": "", "current": "h1"})
    assert stamp_sessions(adapter, "h1") == 1
    assert adapter.written == [("old", "h1")]


# ------------------------------------------------------------------------- E7: heartbeat
def test_a_heartbeat_advances_last_seen_at() -> None:
    registry = FakeRegistry()
    clock = [T0]
    beat = Heartbeat(
        registry=registry,
        host_id="h1",
        kind="lakebox",
        owner_email="o@example.com",
        tmux_socket="/s.sock",
        now=lambda: clock[0],
    )
    assert beat.beat()
    clock[0] = T1
    assert beat.beat()

    assert registry.hosts["h1"].last_seen_at == T1
    assert beat.beats == 2


def test_a_heartbeat_never_raises_and_reports_failure() -> None:
    beat = Heartbeat(
        registry=FakeRegistry(fail={"upsert_host"}),
        host_id="h1",
        kind="lakebox",
        owner_email="o@example.com",
        tmux_socket="/s.sock",
    )
    assert beat.beat() is False
    assert beat.beats == 0


def test_the_heartbeat_thread_is_a_daemon_and_stops_promptly() -> None:
    """A non-daemon thread would hang process exit after the client closes stdin — invisible to
    the client except as a child that never reaps. `Event.wait` is what makes `stop()` prompt
    rather than one full interval late."""
    beat = Heartbeat(
        registry=FakeRegistry(),
        host_id="h1",
        kind="lakebox",
        owner_email="o@example.com",
        tmux_socket="/s.sock",
        interval=30.0,
    )
    beat.start()
    try:
        assert beat._thread is not None and beat._thread.daemon
    finally:
        beat.stop(timeout=1.0)
    assert beat._thread is None, "stop() waited out the full 30s interval instead of the event"


# ------------------------------------------------------- E6 and the R8 autostop warning
def test_a_null_sandbox_id_warns_about_why_it_cannot_check(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """🔴 The warning must fire *especially* when the check is impossible.

    Selecting this sandbox's row from `sandbox list` requires the `sandbox_id`, and a sandbox
    cannot learn its own. So on exactly the un-bootstrapped hosts ADR-6 exists to support, the
    check cannot run — and an earlier plan revision had the warning simply not fire there,
    making the failure mode that hurts users most (autostop killing a session mid-build) silent
    on the hosts most likely to hit it.
    """
    with caplog.at_level("WARNING"):
        enroll.warn_about_autostop(None)
    assert any("cannot evaluate" in r.message for r in caplog.records)

    caplog.clear()
    with caplog.at_level("WARNING"):
        enroll.warn_about_autostop("sbx-1")
    assert not [r for r in caplog.records if r.levelname == "WARNING"]


def test_the_tmux_description_survives_a_missing_binary() -> None:
    """E6 records this so "which tmux" is already answered when a host behaves oddly. A missing
    binary is a normal outcome and must not fail enrollment."""
    assert "at /nonexistent/tmux" in enroll.tmux_description("/nonexistent/tmux")
