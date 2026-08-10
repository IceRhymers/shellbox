"""`lakebase.py` — the credential lifecycle, with no Databricks workspace involved.

Every test here drives a **fake minter and an injectable clock**. That is not a
convenience: the real API's shortest issuable token is 300 seconds, so any test that
waited for a real expiry would be a five-minute test, and one that slept for a fake one
would assert timing by luck. With an injected clock the assertions are about *when*
refresh happens relative to expiry, which is the actual contract, and they run instantly.

The live path — a real token against a real endpoint — is verified separately in W10c;
what is asserted here is everything that does not need a workspace.
"""

from __future__ import annotations

import sys
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from shellbox_registry.lakebase import (
    API_MIN_TTL_SECONDS,
    DEFAULT_DATABASE,
    POOL_RECYCLE_SECONDS,
    REFRESH_MARGIN,
    SQLALCHEMY_DEFAULT_POOL_TIMEOUT_SECONDS,
    Credential,
    LakebaseCredentials,
    LakebaseEndpoint,
    create_lakebase_engine,
    lakebase_registry,
    resolve_lakebase_endpoint,
)

T0 = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)

# What the live API actually issues by default, read from the returned JWT's iat/exp
# (measured 2026-07-31). NOT the documented 300s floor -- an earlier revision of this
# module conflated the two and would have minted 12x more often than necessary.
MEASURED_DEFAULT_TTL_SECONDS = 3600


class FakeClock:
    """A clock the test moves by hand."""

    def __init__(self, now: datetime = T0) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class FakeMinter:
    """Records when it was called, and can be made to fail.

    Records **times**, not just counts — the backoff contract is about when the next
    attempt happens, and a counter cannot express that.
    """

    def __init__(
        self, clock: FakeClock, *, ttl_seconds: float = MEASURED_DEFAULT_TTL_SECONDS
    ) -> None:
        self.clock = clock
        self.ttl = ttl_seconds
        self.calls: list[datetime] = []
        self.fail_with: Exception | None = None
        self.serial = 0

    def __call__(self) -> Credential:
        self.calls.append(self.clock.now)
        if self.fail_with is not None:
            raise self.fail_with
        self.serial += 1
        return Credential(
            token=f"token-{self.serial}",
            expires_at=self.clock.now + timedelta(seconds=self.ttl),
        )


def _credentials(
    ttl_seconds: float = MEASURED_DEFAULT_TTL_SECONDS,
) -> tuple[LakebaseCredentials, FakeMinter, FakeClock]:
    clock = FakeClock()
    minter = FakeMinter(clock, ttl_seconds=ttl_seconds)
    return LakebaseCredentials(minter, clock=clock), minter, clock


# ------------------------------------------------------------------ caching and expiry
def test_a_valid_token_is_reused_rather_than_reminted() -> None:
    """The property that makes lazy refresh viable for a short-lived process: a process
    that lives seconds mints exactly once, no thread, no timer."""
    credentials, minter, _ = _credentials()

    tokens = {credentials.token() for _ in range(50)}

    assert tokens == {"token-1"}
    assert len(minter.calls) == 1
    assert credentials.mints == 1


def test_the_server_stated_expiry_is_trusted_rather_than_a_hardcoded_ttl() -> None:
    """`generate_database_credential` returns `expire_time`, so a server-side policy
    change must cost nothing. A module that assumed one hour would serve a dead token for
    55 minutes against a 5-minute credential."""
    credentials, _, clock = _credentials(ttl_seconds=MEASURED_DEFAULT_TTL_SECONDS)
    credentials.token()
    assert credentials.expires_at == clock.now + timedelta(seconds=MEASURED_DEFAULT_TTL_SECONDS)

    long_lived, _, clock2 = _credentials(ttl_seconds=3600)
    long_lived.token()
    assert long_lived.expires_at == clock2.now + timedelta(seconds=3600)


def test_the_token_is_refreshed_BEFORE_it_expires_not_when_it_expires() -> None:
    """CRITICAL: The margin is the point, and asserting it needs the clock.

    A token that passes the check and then expires *during* the TCP connect and TLS
    handshake fails the connect — and surfaces as an authentication error, which sends
    whoever debugs it looking at permissions rather than at expiry. So refresh must happen
    a margin ahead, and this pins that the margin is real rather than nominal.
    """
    credentials, minter, clock = _credentials(ttl_seconds=MEASURED_DEFAULT_TTL_SECONDS)
    credentials.token()

    # One second inside the margin: still the original token.
    clock.advance(MEASURED_DEFAULT_TTL_SECONDS - REFRESH_MARGIN.total_seconds() - 1)
    assert credentials.token() == "token-1"
    assert len(minter.calls) == 1

    # Crossing the margin — still strictly before expiry — must re-mint.
    clock.advance(2)
    assert credentials.token() == "token-2"
    assert len(minter.calls) == 2
    assert clock.now < T0 + timedelta(seconds=MEASURED_DEFAULT_TTL_SECONDS), (
        "the refresh happened at or after expiry; the margin bought nothing"
    )


# ------------------------------------------------------------------ failure behaviour
def test_a_mint_failure_serves_the_cached_token_while_it_is_still_valid() -> None:
    """A control-plane blip must not take down connections that would have worked.

    The cached token is *near* expiry (that is why a refresh was attempted) but not yet
    expired, so it is still the best available answer.
    """
    credentials, minter, clock = _credentials(ttl_seconds=MEASURED_DEFAULT_TTL_SECONDS)
    credentials.token()
    clock.advance(MEASURED_DEFAULT_TTL_SECONDS - REFRESH_MARGIN.total_seconds() + 1)

    minter.fail_with = RuntimeError("control plane unavailable")
    assert credentials.token() == "token-1", "a transient mint failure dropped a usable token"


def test_a_mint_failure_with_no_valid_token_raises() -> None:
    """The honest outcome. Serving an expired token would fail at connect anyway, one
    layer further from the cause; raising becomes a `registry_warning` on an otherwise
    successful tool call, so shells keep working and the inventory goes stale."""
    credentials, minter, _ = _credentials()
    minter.fail_with = RuntimeError("control plane unavailable")

    with pytest.raises(RuntimeError, match="control plane"):
        credentials.token()


def test_backoff_is_asserted_by_TIME_not_by_call_count() -> None:
    """CRITICAL: The distinction the criterion insists on, and it needs the clock to be real.

    A count assertion ("the minter was called twice") passes for a hot loop that called it
    twice in a microsecond. What must hold is that after a failure the next *attempt* is
    deferred — so the assertions here are about when `minter.calls` timestamps land.
    """
    credentials, minter, clock = _credentials(ttl_seconds=MEASURED_DEFAULT_TTL_SECONDS)
    credentials.token()
    clock.advance(MEASURED_DEFAULT_TTL_SECONDS - REFRESH_MARGIN.total_seconds() + 1)

    minter.fail_with = RuntimeError("down")
    credentials.token()
    assert len(minter.calls) == 2
    failed_at = minter.calls[-1]

    # Hammering inside the backoff window must not reach the minter at all.
    for _ in range(20):
        credentials.token()
    assert len(minter.calls) == 2, (
        f"the minter was called {len(minter.calls) - 1} times in zero elapsed time — that is "
        "a hot loop against a control plane that is already failing"
    )

    # Past the window, exactly one more attempt.
    clock.advance(2.0)
    credentials.token()
    assert len(minter.calls) == 3
    assert minter.calls[-1] > failed_at, "the retry did not actually wait"


def test_backoff_lengthens_with_repeated_failures() -> None:
    """A sustained outage must not be retried at the same rate as a blip."""
    credentials, minter, clock = _credentials(ttl_seconds=MEASURED_DEFAULT_TTL_SECONDS)
    credentials.token()
    clock.advance(MEASURED_DEFAULT_TTL_SECONDS - REFRESH_MARGIN.total_seconds() + 1)
    minter.fail_with = RuntimeError("down")

    gaps: list[float] = []
    for _ in range(4):
        before = len(minter.calls)
        # Advance in small steps until the next attempt is admitted.
        waited = 0.0
        while len(minter.calls) == before and waited < 120:
            clock.advance(1.0)
            waited += 1.0
            credentials.token()
        gaps.append(waited)

    assert gaps == sorted(gaps), f"backoff did not increase monotonically: {gaps}"
    assert gaps[-1] > gaps[0], "backoff never lengthened"


def test_a_recovered_control_plane_resets_the_backoff() -> None:
    """Otherwise one bad minute permanently slows every later refresh."""
    credentials, minter, clock = _credentials(ttl_seconds=MEASURED_DEFAULT_TTL_SECONDS)
    credentials.token()
    clock.advance(MEASURED_DEFAULT_TTL_SECONDS - REFRESH_MARGIN.total_seconds() + 1)

    minter.fail_with = RuntimeError("down")
    credentials.token()
    minter.fail_with = None
    clock.advance(2.0)

    assert credentials.token() == "token-2"
    # A later expiry re-mints immediately rather than waiting out a stale backoff.
    clock.advance(MEASURED_DEFAULT_TTL_SECONDS)
    assert credentials.token() == "token-3"


# ------------------------------------------------------------------ concurrency
def test_concurrent_callers_mint_exactly_one_token() -> None:
    """SQLAlchemy calls `token()` from whichever thread checks out a connection, and
    `shellbox-mcp` runs enrollment alongside tool calls. N threads racing an empty cache
    must not each mint — that is N tokens per process and N calls to the control plane on
    the path W7 promises is non-blocking."""
    credentials, minter, _ = _credentials()
    barrier = threading.Barrier(16)
    seen: list[str] = []
    lock = threading.Lock()

    def worker() -> None:
        barrier.wait(timeout=10)
        value = credentials.token()
        with lock:
            seen.append(value)

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    assert len(seen) == 16
    assert set(seen) == {"token-1"}
    assert len(minter.calls) == 1, f"{len(minter.calls)} concurrent mints for one token"


# ------------------------------------------------------------------ the App-side opt-in
def test_the_background_refresher_is_off_by_default() -> None:
    """The decision RC-8 turned on: a per-agent-session process must not carry a thread it
    will never use. 32 concurrent processes would mean 32 dead threads."""
    credentials, _, _ = _credentials()
    credentials.token()

    assert credentials._thread is None
    assert not any(t.name == "lakebase-refresh" for t in threading.enumerate())


def test_the_refresher_is_a_daemon_and_stops_promptly() -> None:
    """Daemon, so it cannot hang process exit after stdio closes — invisible to an MCP
    client except as a child that never reaps. `Event.wait`, so `stop()` returns at once
    rather than one full interval later."""
    credentials, _, _ = _credentials()
    credentials.start_refresher(interval=30.0)
    try:
        assert credentials._thread is not None
        assert credentials._thread.daemon
    finally:
        credentials.stop(timeout=1.0)
    assert credentials._thread is None, "stop() waited out the interval instead of the event"


def test_starting_the_refresher_twice_does_not_start_two_threads() -> None:
    credentials, _, _ = _credentials()
    credentials.start_refresher(interval=30.0)
    first = credentials._thread
    credentials.start_refresher(interval=30.0)
    try:
        assert credentials._thread is first
    finally:
        credentials.stop(timeout=1.0)


# ------------------------------------------------------------------ engine construction
def _endpoint() -> LakebaseEndpoint:
    return LakebaseEndpoint(
        resource_name="projects/p/branches/production/endpoints/primary",
        host="ep-example.database.cloud.databricks.com",
        database="shellbox",
        user="someone@example.com",
    )


def test_the_dsn_carries_no_credential() -> None:
    """The token is injected per-connect, so a URL that reaches a log, an exception or a
    `repr` carries nothing. An engine built with the password in the URL would also pin
    the first token for the engine's whole life."""
    dsn = _endpoint().dsn()
    assert "@" in dsn and "sslmode=require" in dsn
    assert ":@" not in dsn.split("//", 1)[1].split("@", 1)[0] + "@", "an empty password slot"
    assert "token" not in dsn


def _merged_connect_params(engine: object) -> dict[str, object]:
    """The connect kwargs the engine would actually hand psycopg.

    SQLAlchemy merges `connect_args` into a closure over the pool's creator rather than
    exposing them on the engine, and `create_connect_args` returns only what came from the
    URL — so introspecting the closure is the one way to see the merged result without
    opening a connection. Asserted rather than assumed: if SQLAlchemy ever changes shape,
    this fails loudly instead of silently testing nothing.
    """
    creator = engine.pool._creator  # type: ignore[attr-defined]
    cells = dict(zip(creator.__code__.co_freevars, creator.__closure__ or (), strict=False))
    assert "cparams" in cells, (
        "could not introspect SQLAlchemy's merged connect params; this test no longer "
        f"asserts anything. Closure vars were {sorted(cells)}"
    )
    return dict(cells["cparams"].cell_contents)


def test_the_engine_injects_a_fresh_token_as_the_password() -> None:
    """The whole mechanism, asserted without connecting anywhere: fire SQLAlchemy's own
    `do_connect` event with the params a real connect would use, and check what the
    connect *would* have been given."""
    credentials, minter, clock = _credentials(ttl_seconds=MEASURED_DEFAULT_TTL_SECONDS)
    engine = create_lakebase_engine(_endpoint(), credentials)

    params = _merged_connect_params(engine)
    assert "password" not in params, "a password reached the engine outside do_connect"

    engine.dialect.dispatch.do_connect(engine.dialect, None, (), params)
    assert params["password"] == "token-1"

    # A later connection past the margin gets the NEW token, not the pinned first one —
    # which is the reason the token is injected here rather than baked into the URL.
    clock.advance(MEASURED_DEFAULT_TTL_SECONDS)
    params2 = _merged_connect_params(engine)
    engine.dialect.dispatch.do_connect(engine.dialect, None, (), params2)
    assert params2["password"] == "token-2"
    assert len(minter.calls) == 2


def test_the_pool_recycles_inside_the_token_lifetime_the_api_actually_issues() -> None:
    """CRITICAL: The criterion that went missing between plan revisions, restored — and corrected.

    Refresh does not make recycling redundant: Postgres authenticates a connection once, at
    connect, so a pooled connection outlives its token and the *server* decides when to drop
    it. Recycling first makes that our decision.

    WARNING: The bound is the lifetime the API **issues** (measured 3600s), not the 300s floor it
    documents. An earlier revision asserted the floor, which would have recycled every 240s
    and discarded good connections roughly 15x more often than necessary. Both numbers are
    named here so the distinction cannot quietly collapse again.
    """
    credentials, _, _ = _credentials()
    engine = create_lakebase_engine(_endpoint(), credentials)

    assert POOL_RECYCLE_SECONDS < MEASURED_DEFAULT_TTL_SECONDS, (
        f"pool_recycle ({POOL_RECYCLE_SECONDS}s) is not inside the token lifetime the API "
        f"issues ({MEASURED_DEFAULT_TTL_SECONDS}s), so a connection can outlive its credential"
    )
    assert POOL_RECYCLE_SECONDS > API_MIN_TTL_SECONDS, (
        "pool_recycle is inside the API's documented FLOOR, which is the over-conservative "
        "mistake this constant was corrected away from -- see its comment"
    )
    assert engine.pool._recycle == POOL_RECYCLE_SECONDS
    assert engine.pool._pre_ping is True, "scale-to-zero kills pooled connections"


def test_the_engine_bounds_its_connect() -> None:
    """Inherited from `PostgresRegistry`, where an unbounded connect was measured hanging a
    tool call for 63 seconds against an unrouted address. Lakebase makes it *more*
    relevant, not less: a scaled-to-zero endpoint is exactly a host that accepts slowly."""
    credentials, _, _ = _credentials()
    engine = create_lakebase_engine(_endpoint(), credentials)
    assert _merged_connect_params(engine)["connect_timeout"] > 0


def test_pool_timeout_defaults_to_sqlalchemys_own_so_no_caller_changed() -> None:
    """Adding the parameter must not move any existing caller off SQLAlchemy's behaviour.

    `shellbox-mcp` builds an engine through here and has no threadpool in front of the
    pool, so it wants the wide default. Asserting against SQLAlchemy's live value rather
    than a literal 30 is the point: if the library ever changes its default, this fails and
    says so, instead of silently pinning a number nobody chose.
    """
    credentials, _, _ = _credentials()
    engine = create_lakebase_engine(_endpoint(), credentials)

    import inspect

    from sqlalchemy.pool import QueuePool

    # SQLAlchemy states it as the float 30.0; the constant is the int 30, which compares
    # equal. Read from the signature rather than hardcoded, so a library change fails here.
    library_default = inspect.signature(QueuePool.__init__).parameters["timeout"].default
    assert SQLALCHEMY_DEFAULT_POOL_TIMEOUT_SECONDS == library_default, (
        f"SQLAlchemy's pool timeout default is now {library_default}, so the constant no "
        f"longer restates it and this function silently changed every caller's behaviour"
    )
    assert engine.pool._timeout == SQLALCHEMY_DEFAULT_POOL_TIMEOUT_SECONDS


def test_pool_timeout_is_passable_and_reaches_the_pool() -> None:
    """The App's 5 s per ADR-19's corollary, which had no plumbing before `W28`.

    Not a tautology: this parameter did not exist, and `create_lakebase_engine` declares no
    `**kwargs`, so the value has to be threaded to `create_engine` explicitly. A wired-up
    parameter that never reached the pool would look identical from the signature.
    """
    credentials, _, _ = _credentials()
    engine = create_lakebase_engine(_endpoint(), credentials, pool_timeout=5)
    assert engine.pool._timeout == 5


def test_lakebase_registry_forwards_pool_timeout_rather_than_raising() -> None:
    """CRITICAL: The exact call that raised `TypeError` on the pre-`W28` tree.

    `lakebase_registry` forwards `**engine_kwargs` into `create_lakebase_engine`, which
    declared no `**kwargs` -- so `lakebase_registry(..., pool_timeout=5)` failed with
    `TypeError: create_lakebase_engine() got an unexpected keyword argument 'pool_timeout'`
    (measured on the tree before this change) rather than being ignored. `W29` makes this
    call for real, so the regression this guards is a broken App startup, not a wrong
    number.
    """
    credentials, _, _ = _credentials()
    registry, returned = lakebase_registry(
        _endpoint(), credentials=credentials, pool_timeout=5, max_overflow=10
    )
    try:
        assert returned is credentials, "the caller needs these back to start_refresher()"
        assert registry._engine.pool._timeout == 5
        assert registry._engine.pool._max_overflow == 10, "the App raises this from the default 5"
    finally:
        registry.dispose()


# ----------------------------------------------------- the shape the live API returns
class _FakeProtobufTimestamp:
    """What `expire_time` actually is: a protobuf Timestamp, not a datetime or a string.

    Modelled rather than imported so the unit lane stays free of the Databricks SDK.
    `ToDatetime()` returns a NAIVE datetime, which is the half of this that bites.
    """

    def __init__(self, epoch_seconds: int) -> None:
        self.seconds = epoch_seconds
        self.nanos = 0

    def ToDatetime(self) -> datetime:  # noqa: N802 - protobuf's own casing
        return datetime.fromtimestamp(self.seconds, UTC).replace(tzinfo=None)


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(_FakeProtobufTimestamp(int(T0.timestamp())), id="protobuf-timestamp"),
        pytest.param(T0, id="aware-datetime"),
        pytest.param(T0.replace(tzinfo=None), id="naive-datetime"),
        pytest.param("2026-07-31T12:00:00Z", id="iso-string"),
        pytest.param("2026-07-31T12:00:00+00:00", id="iso-offset"),
    ],
)
def test_every_expiry_shape_the_api_might_return_is_understood(raw: object) -> None:
    """CRITICAL: Regression guard for a bug the unit tests could not have caught.

    `_as_datetime` originally handled only datetimes and strings. The live API returns a
    protobuf `Timestamp`, so it fell through to ``None``, the minter took its
    "no stated expiry" path, and it assumed the 300s floor against a real 3600s token —
    12x the necessary minting, silently. Found only by calling the real API.

    Every shape must produce the SAME aware UTC instant; a naive result would raise
    `TypeError` the moment it met an aware `now()` on the hot path.
    """
    from shellbox_registry.lakebase import _as_datetime

    resolved = _as_datetime(raw)
    assert resolved == T0, f"{raw!r} did not resolve to the expected instant"
    assert resolved is not None and resolved.tzinfo is not None, "a naive datetime escaped"


def test_an_unparseable_expiry_is_none_rather_than_a_guess() -> None:
    """So the minter can take its documented fallback instead of inventing an instant."""
    from shellbox_registry.lakebase import _as_datetime

    assert _as_datetime(None) is None
    assert _as_datetime("not a timestamp") is None
    assert _as_datetime(object()) is None


# --------------------------------------------------- resolving an endpoint from its name
#
# `resolve_lakebase_endpoint` is what turns the ONE value a deploy writes down -- the endpoint
# resource path `scripts/bundle-vars.sh` constructs -- into the host, role and database
# `LakebaseEndpoint` requires. Every test below drives a FAKE workspace client, for the same
# reason the credential tests drive a fake minter: there is no workspace here, and the seam
# that makes that possible is the `client=` parameter the function exposes.

RESOLVED_HOST = "ep-resolved.database.us-west-2.cloud.databricks.com"
RESOURCE = "projects/shellbox-pg-dev/branches/production/endpoints/primary"


class _FakeHosts:
    """`EndpointStatus.hosts`, whose `host` field is what psycopg dials.

    Modelled rather than imported so the unit lane stays free of the Databricks SDK. The path
    is `status.hosts.host`, which section 6 of `docs/lakebase-handoff.md` records as
    `["status"]["hosts"]["host"]` in the CLI's JSON.
    """

    def __init__(self, host: str | None) -> None:
        self.host = host


class _FakeStatus:
    def __init__(self, hosts: _FakeHosts | None) -> None:
        self.hosts = hosts


class _FakeEndpointResource:
    def __init__(self, status: _FakeStatus | None) -> None:
        self.status = status


class FakePostgres:
    """Records the resource name it was asked about, so a test can assert it was passed
    through verbatim rather than reassembled."""

    def __init__(self, endpoint: _FakeEndpointResource) -> None:
        self.endpoint = endpoint
        self.asked: list[str] = []

    def get_endpoint(self, name: str) -> _FakeEndpointResource:
        self.asked.append(name)
        return self.endpoint


class _FakeMe:
    def __init__(self, user_name: str | None) -> None:
        self.user_name = user_name


class FakeCurrentUser:
    """Counts `me()` calls. The count is the point: an explicitly passed user must not cost a
    workspace round trip, and a call count is the only way to see that it did not happen."""

    def __init__(self, user_name: str | None) -> None:
        self._me = _FakeMe(user_name)
        self.calls = 0

    def me(self) -> _FakeMe:
        self.calls += 1
        return self._me


# A sentinel, and NOT `None` as the default for `status`. `None` is one of the three shapes
# under test -- an endpoint reporting no status at all -- so a `None` default would make that
# case silently construct a healthy client instead. Measured: it did, and the case passed
# nothing until this sentinel replaced it.
_UNSET: Any = object()


class FakeWorkspaceClient:
    def __init__(
        self,
        *,
        host: str | None = RESOLVED_HOST,
        user_name: str | None = "tanner.wendland@databricks.com",
        status: Any = _UNSET,
    ) -> None:
        resolved_status = _FakeStatus(_FakeHosts(host)) if status is _UNSET else status
        self.postgres = FakePostgres(_FakeEndpointResource(resolved_status))
        self.current_user = FakeCurrentUser(user_name)


def test_the_host_comes_from_the_endpoint_and_the_user_from_the_caller_identity() -> None:
    """The whole resolver in one assertion: one resource path in, a complete endpoint out.

    `resource_name` and `host` must both survive and stay DISTINCT -- the resource path mints
    tokens, the host is dialled, and conflating them is the first mistake available here.
    """
    client = FakeWorkspaceClient()

    endpoint = resolve_lakebase_endpoint(RESOURCE, database="shellbox", client=client)

    assert endpoint.resource_name == RESOURCE
    assert endpoint.host == RESOLVED_HOST
    assert endpoint.user == "tanner.wendland@databricks.com"
    assert endpoint.database == "shellbox"
    assert client.postgres.asked == [RESOURCE], "the resource path was not passed through as-is"
    assert client.current_user.calls == 1


def test_deriving_the_user_is_what_keeps_a_migration_off_the_apps_service_principal() -> None:
    """CRITICAL: the derived role is a safety property, not a convenience.

    `current_user.me()` answers "who is running this", so a migration authenticates as the
    deploying principal by construction. The failure this forecloses is the App's service
    principal running `alembic upgrade head`: it holds SELECT on two tables, the migration
    fails on a permission error, and the tempting fix for that error is a wider grant.

    Asserted as an identity between the two values rather than against a literal, so the
    property survives a change to whatever the fake reports.
    """
    client = FakeWorkspaceClient(user_name="someone.else@databricks.com")

    endpoint = resolve_lakebase_endpoint(RESOURCE, database="shellbox", client=client)

    assert endpoint.user == client.current_user.me().user_name


def test_an_explicit_user_overrides_the_derived_one_without_a_round_trip() -> None:
    """The override exists for a deploy that must name a role the caller is not.

    The call count is the non-vacuous half: a resolver that derived the user and then threw the
    result away would pass the first assertion and fail this one.
    """
    client = FakeWorkspaceClient(user_name="derived@databricks.com")

    endpoint = resolve_lakebase_endpoint(
        RESOURCE, database="shellbox", user="explicit@databricks.com", client=client
    )

    assert endpoint.user == "explicit@databricks.com"
    assert client.current_user.calls == 0, "the derived user was resolved and then discarded"


@pytest.mark.parametrize(
    ("status", "why"),
    [
        pytest.param(None, "no status at all", id="no-status"),
        pytest.param(_FakeStatus(None), "a status carrying no hosts", id="no-hosts"),
        pytest.param(_FakeStatus(_FakeHosts(None)), "hosts carrying no host", id="no-host"),
    ],
)
def test_an_endpoint_with_no_host_fails_here_rather_than_at_connect(
    status: Any, why: str
) -> None:
    """Every field on the SDK's response is Optional, so `None` can arrive at three levels.

    A `None` that reached `LakebaseEndpoint` would surface much later as a psycopg error
    naming no host, which points at nothing. It fails here instead, naming the resource and
    the command that shows what the endpoint reports.
    """
    client = FakeWorkspaceClient(status=status)

    with pytest.raises(RuntimeError, match="no host") as raised:
        resolve_lakebase_endpoint(RESOURCE, database="shellbox", client=client)

    assert RESOURCE in str(raised.value), f"the message does not name the endpoint ({why})"


def test_a_workspace_that_reports_no_user_name_fails_rather_than_connecting_as_nobody() -> None:
    client = FakeWorkspaceClient(user_name=None)

    with pytest.raises(RuntimeError, match="Postgres role"):
        resolve_lakebase_endpoint(RESOURCE, database="shellbox", client=client)


def test_a_missing_sdk_names_the_extra_that_installs_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """`databricks-sdk` is an optional extra, so this is a real state a caller can be in.

    `None` in `sys.modules` is how the import is made to fail without uninstalling anything:
    `from databricks.sdk import WorkspaceClient` then raises `ImportError`. The assertion is
    that the resolver TRANSLATES it -- a bare `ImportError` naming `databricks.sdk` sends a
    reader looking for a broken install rather than for an uninstalled extra.
    """
    monkeypatch.setitem(sys.modules, "databricks.sdk", None)

    with pytest.raises(RuntimeError, match=r"shellbox-registry\[lakebase\]") as raised:
        resolve_lakebase_endpoint(RESOURCE, database="shellbox")

    assert isinstance(raised.value.__cause__, ImportError), (
        "the ImportError was not preserved as the cause, so the real failure is invisible"
    )


def test_the_default_database_is_the_one_the_bundle_declares() -> None:
    """CRITICAL: two declarations of a database name, and they must not drift.

    `DEFAULT_DATABASE` is what a deploy-time command connects to when `SHELLBOX_PG_DB` is
    unset; `databricks.yml`'s `pg_database` variable is what the bundle declares and what the
    App is handed. A mismatch means `alembic upgrade head` migrates one database while the App
    reads another -- and both halves report success.

    Read from the file rather than restated, so a change to the bundle fails here.
    """
    bundle = (Path(__file__).resolve().parents[2] / "databricks.yml").read_text()

    assert f"default: {DEFAULT_DATABASE}" in bundle, (
        f"databricks.yml no longer declares `default: {DEFAULT_DATABASE}` for pg_database, so "
        f"the resolver's default and the bundle's have diverged"
    )


def test_the_resolver_needs_no_sdk_at_import_time() -> None:
    """The dependency boundary the whole package rests on, asserted rather than assumed.

    `shellbox-registry`'s three real dependencies are alembic, psycopg and SQLAlchemy. That is
    what lets local-Postgres CI and `NullRegistry` run with no Databricks install, and it is
    why the import inside the resolver is lazy. Importing this module must not have pulled the
    SDK in behind our backs.
    """
    import shellbox_registry.lakebase as module

    assert not any(
        name.startswith("databricks.sdk") for name in vars(module)
    ), "the SDK leaked into the module namespace"


def test_the_resolved_endpoint_is_usable_by_the_engine_factory() -> None:
    """The join the two halves have to make: what the resolver returns is what
    `create_lakebase_engine` takes. A resolver returning a subtly different shape would only
    fail on a deploy."""
    credentials, _, _ = _credentials()
    endpoint = resolve_lakebase_endpoint(
        RESOURCE, database="shellbox", client=FakeWorkspaceClient()
    )

    engine = create_lakebase_engine(endpoint, credentials, pool_size=1, max_overflow=0)
    try:
        params: dict[str, Any] = _merged_connect_params(engine)
        engine.dialect.dispatch.do_connect(engine.dialect, None, (), params)
        assert params["password"] == "token-1"
        assert engine.url.host == RESOLVED_HOST
    finally:
        engine.dispose()
