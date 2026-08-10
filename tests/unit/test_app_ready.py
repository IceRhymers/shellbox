"""T-P4-READY-* and T-P4-PROBER-OFF-LOOP -- the readiness route and the in-App prober.

Two properties carry the weight here, and both are the kind that fail silently.

**The body leaks nothing.** ``/ready`` is reachable by every workspace user the edge lets
through, so a failure body carrying the Lakebase host would publish it to all of them. The leak
arrives by a plausible edit -- putting the exception in the ``reason`` field is the obvious way
to make the route more helpful -- so the checks below assert the CONFIGURED strings are
**absent**, and each one first proves the string was there to leak.

**The probe does not block the event loop.** The registry is synchronous SQLAlchemy and the
prober is an asyncio task, so a probe that called it inline would freeze every attached terminal
every 30 minutes. The mitigation for a broken grant would introduce a worse failure than the one
it detects. `test_one_probe_cycle_does_not_block_the_event_loop` drives that directly, which is
why `probe_once` exists as a function separate from the loop.

The route bodies are asserted on ``json.dumps`` of what the endpoint returns, which is what
FastAPI serializes, rather than through a test client. `tests/unit/test_app_database.py` reaches
routes the same way, and a client would run the lifespan handler and with it the prober.
"""

from __future__ import annotations

import ast
import asyncio
import json
import logging
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import anyio
import pytest
from fastapi.routing import APIRoute
from shellbox_app.database import AppDatabase
from shellbox_app.logs import configure_logging
from shellbox_app.ready import (
    PROBE_HOST_ID,
    PROBE_INTERVAL_SECONDS,
    PROBE_SESSION_ID,
    REASON_NO_DATABASE,
    REASON_QUERY_FAILED,
    check_registry,
    probe_forever,
    probe_once,
    ready_payload,
)
from shellbox_app.server import build_app
from shellbox_registry import HostRecord, NullRegistry, SessionRecord
from shellbox_registry.lakebase import (
    Credential,
    LakebaseCredentials,
    LakebaseEndpoint,
    lakebase_registry,
)

# The configured values the fault-injection tests assert are absent from the response. Each is
# spelled to be unmistakable in a body: a substring match on "public" or "hosts" would fire on
# ordinary English, and a check that can pass by accident is not a check.
LEAK_HOST = "leaky-instance.database.example.invalid"
LEAK_DATABASE = "leaky_database_name"
LEAK_SCHEMA = "leaky_schema_name"
LEAK_RELATION = "leaky_relation_name"
LEAK_USER = "leaky-client-id"
LEAK_RESOURCE = "projects/leaky-project/branches/leaky-branch/endpoints/leaky-endpoint"

SECRETS = (LEAK_HOST, LEAK_DATABASE, LEAK_SCHEMA, LEAK_RELATION, LEAK_USER, LEAK_RESOURCE)


class AnsweringRegistry:
    """A registry that answers both probe reads, and records what it was asked for."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def get_host(self, host_id: str) -> HostRecord | None:
        self.calls.append(("get_host", host_id))
        return None

    def get_session(self, session_id: str) -> SessionRecord | None:
        self.calls.append(("get_session", session_id))
        return None


class LeakingRegistry:
    """A registry whose exception text carries every string the body must not carry.

    This is the belt to the fault-injected engine's braces. A real engine leaks its HOST and
    nothing else -- measured below -- so on its own it cannot fail an implementation that
    interpolated a schema or a relation name. This one makes all four present in the exception,
    so the only way the body stays clean is that the exception never reaches it.
    """

    MESSAGE = (
        f'relation "{LEAK_SCHEMA}.{LEAK_RELATION}" does not exist on '
        f"{LEAK_HOST}/{LEAK_DATABASE} as {LEAK_USER} ({LEAK_RESOURCE})"
    )

    def get_host(self, host_id: str) -> HostRecord | None:
        raise RuntimeError(self.MESSAGE)

    def get_session(self, session_id: str) -> SessionRecord | None:
        raise RuntimeError(self.MESSAGE)


class BlockingRegistry:
    """A registry whose read blocks the calling thread for `delay` seconds.

    ``time.sleep`` and not ``anyio.sleep``, because the point is a call that a coroutine cannot
    yield around. This is what a synchronous SQLAlchemy query is to the event loop.
    """

    def __init__(self, delay: float) -> None:
        self.delay = delay
        self.calls = 0

    def get_host(self, host_id: str) -> HostRecord | None:
        self.calls += 1
        time.sleep(self.delay)
        return None

    def get_session(self, session_id: str) -> SessionRecord | None:
        return None


def _minter() -> Credential:
    return Credential(token="not-a-real-token", expires_at=datetime.now(UTC) + timedelta(hours=1))


def _fault_injected_engine() -> AppDatabase:
    """A real `PostgresRegistry` over a real engine that cannot reach anything.

    A genuine engine rather than a double: the assertion is that a real driver error does not
    reach the response body, and a double could only leak text this file wrote itself. The host
    does not resolve, so `.invalid` is reserved by RFC 6761 and every read fails in
    milliseconds. The credentials are supplied so nothing imports the Databricks SDK.
    """
    endpoint = LakebaseEndpoint(
        resource_name=LEAK_RESOURCE,
        host=LEAK_HOST,
        database=LEAK_DATABASE,
        user=LEAK_USER,
        port=5432,
    )
    registry, credentials = lakebase_registry(
        endpoint,
        credentials=LakebaseCredentials(_minter),
        pool_size=1,
        max_overflow=0,
        pool_timeout=1,
    )
    return AppDatabase(registry=registry, credentials=credentials)


def _ready_route_body(database: AppDatabase) -> str:
    """The serialized body of ``GET /ready``, reached through the app's own route table."""
    api = build_app(database=database)
    (route,) = [
        candidate
        for candidate in api.routes
        if isinstance(candidate, APIRoute) and candidate.path == "/ready"
    ]
    return json.dumps(route.endpoint())


# --------------------------------------------------------------------------------------
# What the check reads
# --------------------------------------------------------------------------------------


def test_the_check_reads_both_granted_relations_rather_than_only_connecting() -> None:
    """The role this route exists to catch CAN connect. Only a table read fails for it.

    `scripts/grant_app_sp.py` grants SELECT on `hosts` and `sessions`, and a grant that landed
    on one and not the other must not report ready. Both reads are primary-key lookups against
    sentinel ids, so the cost does not grow with the registry.
    """
    registry = AnsweringRegistry()

    reason = check_registry(AppDatabase(registry=registry))

    assert reason is None
    assert registry.calls == [
        ("get_host", PROBE_HOST_ID),
        ("get_session", PROBE_SESSION_ID),
    ]


def test_a_healthy_registry_reports_ready_with_no_other_field() -> None:
    """The success body is exactly ``{"ready": true}``. No host, no counts, no diagnostics."""
    assert ready_payload(AppDatabase(registry=AnsweringRegistry())) == {"ready": True}


def test_an_app_with_no_registry_is_not_ready() -> None:
    """A `NullRegistry` is a deploy failure here, and reporting ready would hide it.

    `packages/shellbox-app/src/shellbox_app/config.py` records that an App given no endpoint
    answers ``GET /`` exactly like a healthy one. This route is the one that says otherwise.
    """
    payload = ready_payload(AppDatabase(registry=NullRegistry()))

    assert payload == {"ready": False, "reason": REASON_NO_DATABASE}


def test_the_check_never_raises_so_the_route_cannot_500() -> None:
    """A diagnostic that fails by raising is a 500, which the edge makes ambiguous."""
    assert check_registry(AppDatabase(registry=LeakingRegistry())) == REASON_QUERY_FAILED


# --------------------------------------------------------------------------------------
# T-P4-READY-GENERIC
# --------------------------------------------------------------------------------------


def test_the_failure_body_carries_nothing_the_driver_error_carried() -> None:
    """T-P4-READY-GENERIC, against a real engine that cannot reach its host.

    The first assertion is the non-vacuity witness: the driver error really does name the
    configured host, so there is something for the body to leak. Without it this test would
    pass against a registry that failed for a reason naming nothing.
    """
    database = _fault_injected_engine()
    try:
        with pytest.raises(Exception) as raised:  # noqa: B017 -- the driver's own type
            database.registry.get_host(PROBE_HOST_ID)
        assert LEAK_HOST in str(raised.value), (
            "the fault injection stopped naming the host, so this test no longer proves the "
            f"body withholds anything: {raised.value}"
        )

        body = _ready_route_body(database)

        for secret in SECRETS:
            assert secret not in body, f"/ready leaked {secret!r} in its failure body: {body}"
        assert json.loads(body) == {"ready": False, "reason": REASON_QUERY_FAILED}
    finally:
        database.stop()


def test_the_failure_body_withholds_a_schema_and_a_relation_name_too() -> None:
    """T-P4-READY-GENERIC, with all four strings present in the exception.

    The engine above leaks only its host. This registry raises an exception naming the host,
    the database, the schema and the relation, so an implementation that put the exception in
    the ``reason`` field fails here on every one of them.
    """
    for secret in SECRETS:
        assert secret in LeakingRegistry.MESSAGE, (
            f"{secret!r} is no longer in the injected exception, so asserting its absence "
            "from the body proves nothing"
        )

    body = _ready_route_body(AppDatabase(registry=LeakingRegistry()))

    for secret in SECRETS:
        assert secret not in body, f"/ready leaked {secret!r} in its failure body: {body}"
    assert json.loads(body) == {"ready": False, "reason": REASON_QUERY_FAILED}


def test_the_detail_the_body_withholds_reaches_the_log_instead(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Withheld from the caller, not discarded. An operator needs the driver error.

    This is the other half of the generic-body rule. A route that dropped the exception would
    pass every assertion above and leave nobody able to diagnose the failure.
    """
    with caplog.at_level(logging.WARNING, logger="shellbox_app.ready"):
        ready_payload(AppDatabase(registry=LeakingRegistry()))

    logged = "\n".join(record.getMessage() + (record.exc_text or "") for record in caplog.records)
    assert LEAK_RELATION in logged, f"the driver error reached nobody at all: {logged}"


# --------------------------------------------------------------------------------------
# T-P4-PROBER-OFF-LOOP
# --------------------------------------------------------------------------------------


def test_one_probe_cycle_does_not_block_the_event_loop() -> None:
    """T-P4-PROBER-OFF-LOOP. Calls `probe_once` directly -- the reason it is separate.

    A ticker runs alongside the probe. If the probe ran its blocking read on the event loop the
    ticker could not advance at all while the read was in flight, so the count would be near
    zero. `probe_once` hands the read to a thread, so the loop keeps running.

    Written against `probe_once` rather than the loop, which is what makes this milliseconds
    instead of a monkeypatch of ``asyncio.sleep`` or a half-hour wait.
    """
    blocking = BlockingRegistry(delay=0.2)
    database = AppDatabase(registry=blocking)
    ticks = 0

    async def scenario() -> None:
        nonlocal ticks

        async def ticker() -> None:
            nonlocal ticks
            while True:
                await anyio.sleep(0.001)
                ticks += 1

        async with anyio.create_task_group() as group:
            group.start_soon(ticker)
            await probe_once(database)
            group.cancel_scope.cancel()

    anyio.run(scenario)

    assert blocking.calls == 1, "the probe did not read the registry at all"
    # 0.2 s of blocking read against a 1 ms ticker. Twenty is a wide margin under a loaded CI
    # runner and still nowhere near the zero a blocked loop produces.
    assert ticks >= 20, (
        f"the event loop advanced only {ticks} times while the probe read the registry; a "
        "probe that blocks the loop stalls every attached terminal"
    )


def test_one_probe_cycle_reports_a_failure_as_a_warning_naming_the_reason(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The WARN line IS the notification mechanism. Nothing else tells anyone."""
    database = AppDatabase(registry=LeakingRegistry())

    with caplog.at_level(logging.WARNING, logger="shellbox_app.ready"):
        answered = anyio.run(probe_once, database)

    assert answered is False
    warnings = [
        record.getMessage()
        for record in caplog.records
        if record.levelno >= logging.WARNING and "readiness probe FAILED" in record.getMessage()
    ]
    assert warnings, f"a failed probe logged no WARNING: {[r.getMessage() for r in caplog.records]}"
    assert REASON_QUERY_FAILED in warnings[0]


def test_the_loop_takes_its_interval_and_drives_the_single_cycle() -> None:
    """The NORMATIVE split, from the loop's side: the interval is a parameter.

    Driving several cycles takes milliseconds only because the loop accepts an interval. With
    1800 s spelled inline this assertion would need a monkeypatched ``asyncio.sleep``.
    """
    registry = AnsweringRegistry()
    database = AppDatabase(registry=registry)

    async def scenario() -> None:
        task = asyncio.create_task(probe_forever(database, interval=0))
        while len(registry.calls) < 6:
            await asyncio.sleep(0.001)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())

    assert len(registry.calls) >= 6, "the loop ran fewer cycles than it was driven for"


def test_the_probe_interval_is_thirty_minutes() -> None:
    """The interval is a cost decision. See the constant's comment for the duty-cycle formula."""
    assert PROBE_INTERVAL_SECONDS == 1800.0


# --------------------------------------------------------------------------------------
# The prober's task, and the lifespan handler that owns it
# --------------------------------------------------------------------------------------


def test_building_the_app_starts_no_prober() -> None:
    """`build_app` runs at import. A module a test suite imports must start no background work.

    The same rule the credential refresher follows, for the same reason.
    """
    api = build_app(database=AppDatabase(registry=AnsweringRegistry()))

    assert getattr(api.state, "prober", None) is None


def test_the_lifespan_starts_the_prober_and_cancels_it_at_shutdown() -> None:
    """The task runs while the App serves, and nothing is left pending after it stops.

    The coroutine is named rather than merely counted. A task that exists proves only that the
    handler created one; asserting WHICH coroutine it runs is what fails when the prober is
    replaced by something that finishes on its own.

    The whole scenario runs under a deadline because the failure it guards is a HANG: a handler
    that awaits the prober without cancelling it waits a full interval. A bounded failure is
    readable in CI and an unbounded one stops the lane.
    """
    registry = AnsweringRegistry()
    api = build_app(database=AppDatabase(registry=registry))

    async def serve() -> None:
        async with api.router.lifespan_context(api):
            assert isinstance(api.state.prober, asyncio.Task)
            assert api.state.prober.get_coro().__qualname__ == "probe_forever", (
                f"the lifespan started {api.state.prober.get_coro().__qualname__}, not the "
                "prober"
            )
            assert not api.state.prober.done(), "the prober stopped while the App was serving"

    async def scenario() -> None:
        started = time.monotonic()
        await asyncio.wait_for(serve(), timeout=5)
        elapsed = time.monotonic() - started
        # The DURATION is the assertion, and the deadline above only bounds the damage. A
        # handler that awaits the prober without cancelling it first waits a whole interval,
        # and the only thing that ends the wait is the deadline -- so `cancelled()` reads True
        # either way and says nothing. Shutting down must not wait for anything.
        assert elapsed < 1.0, (
            f"the lifespan took {elapsed:.2f}s to unwind; it waited on the prober instead of "
            "cancelling it"
        )
        assert api.state.prober.cancelled(), "the prober was not cancelled at shutdown"
        assert [task for task in asyncio.all_tasks() if not task.done()] == [
            asyncio.current_task()
        ], "a task outlived the lifespan handler"

    asyncio.run(scenario())

    # CRITICAL: no read happened. `probe_forever` sleeps before its first probe, so starting
    # the App performs no database I/O -- see that function's docstring. A test lane whose
    # fixtures name a resolvable host would otherwise open a real socket on every lifespan.
    assert registry.calls == []


# --------------------------------------------------------------------------------------
# Logging, which is what gives the WARN line somewhere to go
# --------------------------------------------------------------------------------------


def test_configure_logging_puts_an_app_warning_and_an_info_line_on_the_stream() -> None:
    """Both levels, because the prober's success path is INFO and its failure path is WARNING.

    A configuration that carried only WARNING would leave "the probe ran and the database is
    fine" invisible, which is the line that distinguishes a working prober from an absent one.
    """
    import io

    stream = io.StringIO()
    handler = configure_logging(stream)
    root = logging.getLogger()
    previous_level = root.level
    try:
        logging.getLogger("shellbox_app.ready").info("an info line")
        logging.getLogger("shellbox_app.database").warning("a warning line")
    finally:
        root.removeHandler(handler)
        root.setLevel(previous_level)

    written = stream.getvalue()
    assert "an info line" in written
    assert "a warning line" in written
    # The logger name is what tells an operator which half of the App spoke.
    assert "shellbox_app.ready" in written
    assert "shellbox_app.database" in written


def test_configure_logging_is_idempotent_because_it_has_two_call_sites() -> None:
    """`__main__` calls it for the import-time lines and `main` calls it again.

    A second handler would print every line twice, which is the kind of defect that looks like
    a duplicate deploy.
    """
    import io

    root = logging.getLogger()
    before = list(root.handlers)
    previous_level = root.level
    try:
        first = configure_logging(io.StringIO())
        second = configure_logging(io.StringIO())

        assert first is second
        assert len([h for h in root.handlers if h not in before]) == 1
    finally:
        for handler in list(root.handlers):
            if handler not in before:
                root.removeHandler(handler)
        root.setLevel(previous_level)


def test_the_entrypoint_configures_logging_before_it_imports_the_server() -> None:
    """The ORDERING, which is the load-bearing half and the half a reader will re-sort.

    `shellbox_app.server` builds the deployed ``app`` at module scope, and building it opens the
    registry and logs the outcome. Importing it first drops those lines, which is the exact gap
    a live run found: ``/ready`` answered ``no_database`` while the log said nothing about the
    App having looked for an endpoint.
    """
    # Read by path, NOT imported. Importing `__main__` runs `configure_logging()`, which is the
    # behaviour under test and would install a handler on the root logger for the rest of the
    # session -- taking a global side effect to assert that a global side effect happens early.
    import shellbox_app

    source = Path(shellbox_app.__file__).parent / "__main__.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    interesting: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == "shellbox_app.server":
            interesting.append("import shellbox_app.server")
        elif (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id == "configure_logging"
        ):
            interesting.append("configure_logging()")

    assert interesting == ["configure_logging()", "import shellbox_app.server"], (
        "the entrypoint must configure logging BEFORE importing the server, which builds the "
        f"app and opens the registry at module scope. Found: {interesting}"
    )
