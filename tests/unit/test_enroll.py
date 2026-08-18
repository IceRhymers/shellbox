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
    reproject_live_sessions,
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


# Default epoch SECONDS for a live tmux session, mirroring `tmux.SessionRecord`'s unscaled
# integer clocks. Distinct values so a conversion assertion catches a created/activity swap.
_LIVE_CREATED_AT = 1_722_400_000  # 2024-07-31T04:26:40Z
_LIVE_ACTIVITY_AT = 1_722_400_500  # 2024-07-31T04:35:00Z


class FakeAdapter:
    """A tmux adapter that answers from a dict. `raises` makes every read blow up.

    ``list_sessions`` yields objects shaped like `tmux.SessionRecord` for E5b: they carry
    ``created_at``/``last_activity_at`` (epoch ints), ``cols``/``rows``/``cwd`` and a ``foreign``
    flag. ``foreign`` names mark sessions with an empty ``@shellbox_incarnation`` (E5b skips
    them); everything else defaults to non-foreign so the pre-existing tests are unaffected.
    """

    def __init__(
        self,
        stamps: dict[str, str] | None = None,
        *,
        raises: bool = False,
        foreign: set[str] | None = None,
    ) -> None:
        self.stamps = dict(stamps or {})
        self.raises = raises
        self.foreign = set(foreign or set())
        self.written: list[tuple[str, str]] = []

    def list_sessions(self) -> list[object]:
        if self.raises:
            raise RuntimeError("list-sessions failed")
        return [
            type(
                "S",
                (),
                {
                    "tmux_name": name,
                    "created_at": _LIVE_CREATED_AT,
                    "last_activity_at": _LIVE_ACTIVITY_AT,
                    "cols": 80,
                    "rows": 24,
                    "cwd": "/work",
                    "incarnation": None if name in self.foreign else "inc-1",
                    "foreign": name in self.foreign,
                },
            )()
            for name in self.stamps
        ]

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
    """CRITICAL: E2d. No placeholder, not even a plausible one.

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
    """CRITICAL: The ordering dependency, pinned because it is invisible from either side alone.

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
    """CRITICAL: The Critic-9 guard. "No sessions here" and "wrong server" are the same evidence.

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
    """CRITICAL: A broken tmux must never be read as "no sessions".

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


# ------------------------------------------------------- E5b: re-projecting live sessions
#
# These tests prove E5b's BRANCH LOGIC only -- which live sessions it inserts and which it
# leaves alone. They CANNOT reproduce issue #24's actual failure: `FakeRegistry` has no foreign
# key, so a `sessions` INSERT never rejects a missing `hosts` row. That the real
# `sessions_host_id_fkey` exists, and that E5b lands a row after the host row is written, is
# proven against a live Postgres in `tests/registry/test_enroll_reprojection.py`.
def test_a_live_session_with_no_row_is_reprojected_by_a_full_pass(tmp_path: Path) -> None:
    """Mode (a). The create-path INSERT lost the race with the host row and was swallowed; E5b,
    running after E4, re-projects the live session so the row comes back."""
    registry = FakeRegistry()
    result = _enroll(registry, FakeAdapter({"build": ""}), tmp_path)

    assert result.enrolled and result.reprojected == 1
    row = registry.sessions["h1:build"]
    assert (row.status, row.owner_email, row.host_id) == ("live", "creator@example.com", "h1")


def test_reprojection_inserts_a_missing_row_with_the_resolved_owner(tmp_path: Path) -> None:
    """Mode (b). Called directly with the resolved host owner: the one session that had no row
    is inserted under that owner, with tmux's real epoch clocks converted to UTC."""
    registry = FakeRegistry()
    _seed(registry)  # host row only, no session rows

    count = reproject_live_sessions(
        registry,
        FakeAdapter({"build": "h1"}),
        host_id="h1",
        owner_email="o@example.com",
    )

    assert count == 1
    row = registry.sessions["h1:build"]
    assert (row.status, row.owner_email, row.host_id) == ("live", "o@example.com", "h1")
    assert row.created_at == datetime.fromtimestamp(_LIVE_CREATED_AT, tz=UTC)
    assert row.last_activity_at == datetime.fromtimestamp(_LIVE_ACTIVITY_AT, tz=UTC)
    assert row.last_read_at is None


def test_reprojection_never_resurrects_a_terminal_row(tmp_path: Path) -> None:
    """A reaped or orphaned row still HAS a row, so E5b -- which detects missing by session_id
    membership, not by status -- must skip it even when its tmux session is live again."""
    registry = FakeRegistry()
    _seed(registry, "reap", status="reaped")
    _seed(registry, "orph", status="orphaned")

    count = reproject_live_sessions(
        registry,
        FakeAdapter({"reap": "h1", "orph": "h1"}),
        host_id="h1",
        owner_email="o@example.com",
    )

    assert count == 0
    assert registry.sessions["h1:reap"].status == "reaped"
    assert registry.sessions["h1:orph"].status == "orphaned"


def test_reprojection_does_not_clobber_an_existing_live_row(tmp_path: Path) -> None:
    """A row already present is E5's business, never E5b's: its timestamps and status stand."""
    registry = FakeRegistry()
    _seed(registry, "build")  # a live row at T0

    count = reproject_live_sessions(
        registry,
        FakeAdapter({"build": "h1"}),
        host_id="h1",
        owner_email="someone-else@example.com",
    )

    assert count == 0
    row = registry.sessions["h1:build"]
    assert (row.status, row.last_activity_at, row.owner_email) == ("live", T0, "o@example.com")


def test_reprojection_excludes_a_foreign_session(tmp_path: Path) -> None:
    """A live session with an empty `@shellbox_incarnation` is not one shellbox can prove it
    owns, so E5b never projects a row for it."""
    registry = FakeRegistry()
    _seed(registry)  # host row only

    count = reproject_live_sessions(
        registry,
        FakeAdapter({"ghost": "h1"}, foreign={"ghost"}),
        host_id="h1",
        owner_email="o@example.com",
    )

    assert count == 0
    assert registry.sessions == {}


def test_reprojection_survives_a_failing_upsert(tmp_path: Path) -> None:
    """Best-effort: a failed `upsert_session` is logged and skipped, never raised into enroll."""
    registry = FakeRegistry(fail={"upsert_session"})
    _seed(registry)
    count = reproject_live_sessions(
        registry,
        FakeAdapter({"build": "h1"}),
        host_id="h1",
        owner_email="o@example.com",
    )
    assert count == 0
    assert "h1:build" not in registry.sessions


def test_reprojection_survives_a_broken_tmux(tmp_path: Path) -> None:
    """A tmux read that raises leaves the registry untouched rather than raising."""
    registry = FakeRegistry()
    _seed(registry)
    assert (
        reproject_live_sessions(
            registry,
            FakeAdapter(raises=True),
            host_id="h1",
            owner_email="o@example.com",
        )
        == 0
    )


def test_reprojection_survives_a_failing_row_list(tmp_path: Path) -> None:
    """A failed `list_sessions_for_host` returns 0 rather than raising."""
    registry = FakeRegistry(fail={"list_sessions_for_host"})
    _seed(registry)
    assert (
        reproject_live_sessions(
            registry,
            FakeAdapter({"build": "h1"}),
            host_id="h1",
            owner_email="o@example.com",
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
    """CRITICAL: Not "the first one" — that would reintroduce the split this mechanism prevents.

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
    """CRITICAL: The warning must fire *especially* when the check is impossible.

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
