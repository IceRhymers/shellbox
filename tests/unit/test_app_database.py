"""The App's database wiring: config, the refresher's lifecycle, and the relay's independence.

Every test here drives a **fake minter**, for the reason `tests/unit/test_lakebase.py` states
in its own docstring: the real API's shortest issuable token is 300 seconds, and there is no
provisioned Lakebase endpoint to point a test at. What is asserted is the wiring -- which calls
happen, in which order, and what still works when the database does not.

The live half is verified elsewhere and cannot be verified here. Minting a real token as the
App's service principal needs a deployed App, and reading a real table needs a granted role.

## The two properties worth stating before the code

1. **The relay holds no database dependency.** With a registry that raises on every call,
   ``serve_publisher`` and ``serve_subscriber`` still bind, still send ``hello``, and still
   relay. The registry is CONFIGURED and RAISING, which is the only version of this test worth
   having: one that passes because nothing is configured would pass on an App that reads the
   database on every frame.
2. **Opening the registry is never fatal.** A broken environment degrades the App to "terminals
   work, the inventory is stale", never to "terminals are down".
"""

from __future__ import annotations

import ast
import time
from collections.abc import Awaitable, Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import anyio
import pytest
from appfakes import FakeSocket, hello_of
from fastapi import FastAPI
from shellbox_app import config, server
from shellbox_app.config import (
    DEFAULT_DATABASE,
    DEFAULT_PORT,
    MAX_OVERFLOW,
    POOL_SIZE,
    POOL_TIMEOUT_SECONDS,
    DatabaseSettings,
)
from shellbox_app.database import AppDatabase, open_registry
from shellbox_app.server import build_app, serve_publisher, serve_subscriber
from shellbox_registry import HostRecord, NullRegistry, Registry, SessionRecord
from shellbox_registry.lakebase import Credential, LakebaseCredentials, lakebase_registry
from shellbox_transport.codec import FIELD_SESSION_ID

# A configured environment, with no real endpoint behind it. The resource path is the shape
# `scripts/bundle-vars.sh` constructs, so a reader can see which variable carries which half.
CONFIGURED = {
    "SHELLBOX_PG_RESOURCE": "projects/shellbox-pg-dev/branches/production/endpoints/primary",
    "SHELLBOX_PG_HOST": "ep-example.database.us-west-2.cloud.databricks.com",
    "DATABRICKS_CLIENT_ID": "69988b13-d8b9-4e7d-a017-b6c34435aa7e",
}


class CountingMinter:
    """A minter that never touches the SDK and always issues an already-spent token.

    Already spent on purpose: `LakebaseCredentials.token` re-mints only when the cached
    credential is inside its refresh margin, so a long-lived token would make the refresher's
    work unobservable. This is what lets the `mints` counter answer "did the background thread
    do anything".
    """

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> Credential:
        self.calls += 1
        # Inside `REFRESH_MARGIN` of expiry the moment it is issued, so the next `token()` call
        # mints again rather than serving this one.
        return Credential(token=f"token-{self.calls}", expires_at=datetime.now(UTC))


class RaisingRegistry:
    """A registry configured on the app and refusing every call.

    CRITICAL: This is what makes the relay test non-vacuous. An app with NO registry proves
    nothing about a relay that must survive a database outage -- there would be nothing to
    fail. Every method raises, so any database touch on the accept path surfaces as a failed
    bind rather than as a passing test.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []

    def _refuse(self, method: str) -> None:
        self.calls.append(method)
        raise RuntimeError(f"the registry is down: {method}")

    def upsert_host(self, record: HostRecord) -> None:
        self._refuse("upsert_host")

    def upsert_session(self, record: SessionRecord) -> None:
        self._refuse("upsert_session")

    def get_host(self, host_id: str) -> HostRecord | None:
        self._refuse("get_host")
        return None

    def get_session(self, session_id: str) -> SessionRecord | None:
        self._refuse("get_session")
        return None

    def list_sessions_for_host(self, host_id: str) -> list[SessionRecord]:
        self._refuse("list_sessions_for_host")
        return []

    def list_hosts(self, owner_email: str | None = None) -> list[HostRecord]:
        self._refuse("list_hosts")
        return []

    def list_sessions(self, owner_email: str | None = None) -> list[SessionRecord]:
        self._refuse("list_sessions")
        return []


def _credentials(minter: CountingMinter) -> LakebaseCredentials:
    return LakebaseCredentials(minter)


class RecordingBuilder:
    """Records the engine settings it was passed, then really builds the engine.

    Really builds one: the point of asserting `pool_timeout` and `max_overflow` is that they
    reach SQLAlchemy's pool, and a double that never called `create_lakebase_engine` would
    assert only that this test passes its own arguments along.

    The credentials are supplied rather than minted, which is the seam `lakebase_registry`
    already has for the same reason -- `sdk_token_minter` would import the Databricks SDK.
    """

    def __init__(self, credentials: LakebaseCredentials) -> None:
        self.credentials = credentials
        self.seen: dict[str, object] = {}

    def __call__(self, endpoint, **engine_kwargs):  # type: ignore[no-untyped-def]
        self.seen = dict(engine_kwargs)
        return lakebase_registry(endpoint, credentials=self.credentials, **engine_kwargs)


@pytest.fixture
def disposed() -> Iterator[list[AppDatabase]]:
    """Stop and dispose whatever a test opened, even when it fails mid-assertion.

    A leaked refresher thread is a daemon and cannot hang the suite, but it can mint while a
    later test counts mints.
    """
    opened: list[AppDatabase] = []
    yield opened
    for store in opened:
        store.stop()


# --------------------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------------------


def test_the_app_and_the_registry_share_one_database_default() -> None:
    """CRITICAL: one constant, so the App and the migration cannot reach different databases.

    `config.DEFAULT_DATABASE` used to be its own literal, ``"shellbox"``, beside a comment
    claiming it mirrored the bundle. Commit `a50dca8` changed the bundle's `pg_database`
    default to `databricks_postgres` and the literal stayed, so `alembic upgrade head` would
    have migrated one database while the App read another -- and both halves report success.

    This asserts IDENTITY rather than equality to a literal. A restated value would satisfy an
    equality check on the day it was written and drift the next time the bundle moved, which is
    the exact failure above. `test_the_default_database_is_the_one_the_bundle_declares` in
    `tests/unit/test_lakebase.py` is the other half: it reads `databricks.yml` from disk, so
    the one constant is pinned to the bundle rather than merely shared.
    """
    from shellbox_registry.lakebase import DEFAULT_DATABASE as REGISTRY_DEFAULT

    assert config.DEFAULT_DATABASE is REGISTRY_DEFAULT, (
        "shellbox_app.config declares its own database default again; it must re-export "
        "shellbox_registry.lakebase.DEFAULT_DATABASE, which the bundle pins"
    )


def test_the_endpoint_comes_from_the_environment_and_the_user_is_the_client_id() -> None:
    """The deployed shape: the resource path mints, the host is dialled, the SP client id
    is the Postgres role."""
    settings = DatabaseSettings.from_env(CONFIGURED)

    assert settings is not None
    assert settings.resource_name == CONFIGURED["SHELLBOX_PG_RESOURCE"]
    assert settings.host == CONFIGURED["SHELLBOX_PG_HOST"]
    assert settings.user == CONFIGURED["DATABRICKS_CLIENT_ID"]
    assert settings.database == DEFAULT_DATABASE
    assert settings.port == DEFAULT_PORT

    endpoint = settings.endpoint()
    assert endpoint.resource_name == settings.resource_name
    assert endpoint.host == settings.host
    # The two are different fields because they come from different calls. A DSN built from the
    # resource path would dial a name that does not resolve.
    assert endpoint.resource_name != endpoint.host


def test_an_unconfigured_environment_means_no_inventory_rather_than_an_error() -> None:
    """The laptop and the integration lane. The relay needs no database, so this is a
    supported state and not a misconfiguration."""
    assert DatabaseSettings.from_env({}) is None
    assert isinstance(open_registry({}).registry, NullRegistry)


@pytest.mark.parametrize(
    "environ",
    [
        {"SHELLBOX_PG_RESOURCE": "projects/p/branches/b/endpoints/e"},
        {"SHELLBOX_PG_HOST": "ep-example.database.cloud.databricks.com"},
    ],
)
def test_a_half_configured_environment_raises_rather_than_reporting_no_inventory(
    environ: dict[str, str],
) -> None:
    """One variable set and the other missing is a deploy that meant to reach a database.

    Reporting "no inventory" would hide it behind the same log line an unconfigured laptop
    prints, which is the one outcome an operator cannot debug.
    """
    with pytest.raises(ValueError, match="half configured"):
        DatabaseSettings.from_env(environ)


def test_no_client_id_and_no_override_raises_naming_both_variables() -> None:
    environ = dict(CONFIGURED)
    del environ["DATABRICKS_CLIENT_ID"]
    with pytest.raises(ValueError, match="DATABRICKS_CLIENT_ID"):
        DatabaseSettings.from_env(environ)


def test_an_explicit_user_wins_so_a_laptop_run_needs_no_code_change() -> None:
    """A workspace user's email is a valid Postgres role too -- section 1 of
    `docs/lakebase-handoff.md` wrote rows as one. The App's own path is the client id."""
    settings = DatabaseSettings.from_env(
        {**CONFIGURED, "SHELLBOX_PG_USER": "someone@example.com"}
    )
    assert settings is not None and settings.user == "someone@example.com"


def test_a_malformed_port_raises_rather_than_defaulting() -> None:
    with pytest.raises(ValueError, match="SHELLBOX_PG_PORT"):
        DatabaseSettings.from_env({**CONFIGURED, "SHELLBOX_PG_PORT": "five thousand"})


# --------------------------------------------------------------------------------------
# T-P4-NO-CURRENT-USER
# --------------------------------------------------------------------------------------


def test_no_module_in_the_app_package_names_current_user() -> None:
    """T-P4-NO-CURRENT-USER. A regression guard, and NOT a proof.

    It passes on a tree with an aliased import or a `getattr`, so it cannot establish the
    property. It is kept because of what the failure it guards looks like: without an
    on-behalf-of token, `current_user.me()` returns the App's OWN service principal, so an
    inventory filtered by it would be wrong and the App would still look healthy.

    The Postgres role comes from `DATABRICKS_CLIENT_ID` instead -- see the module docstring of
    `packages/shellbox-app/src/shellbox_app/config.py`.
    """
    package = Path(server.__file__).parent
    modules = sorted(package.glob("*.py"))
    assert len(modules) >= 4, f"expected the whole package, globbed {[m.name for m in modules]}"

    for module in modules:
        source = module.read_text(encoding="utf-8")
        tree = ast.parse(source)
        named = {
            node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
        } | {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        assert "current_user" not in named, (
            f"{module.name} references current_user. Without an on-behalf-of token it resolves "
            "the App's own service principal, which is a failure that reads as working."
        )


# --------------------------------------------------------------------------------------
# The engine settings
# --------------------------------------------------------------------------------------


def test_the_pool_is_built_with_the_three_settings_the_app_overrides(
    disposed: list[AppDatabase],
) -> None:
    """Asserted on the POOL, not on the call, because that is the half that can silently not
    happen.

    `pool_timeout` is the one to watch: `lakebase_registry(..., pool_timeout=5)` raised
    `TypeError` until `create_lakebase_engine` declared the parameter. `max_overflow` is a
    deliberate raise from that function's default of 5. Both numbers are derived in its
    docstring, in `packages/shellbox-registry/src/shellbox_registry/lakebase.py`, rather than
    here.
    """
    minter = CountingMinter()
    build = RecordingBuilder(_credentials(minter))
    store = open_registry(CONFIGURED, build=build)
    disposed.append(store)

    assert build.seen == {
        "pool_size": POOL_SIZE,
        "max_overflow": MAX_OVERFLOW,
        "pool_timeout": POOL_TIMEOUT_SECONDS,
    }
    pool = store.registry._engine.pool
    assert pool.size() == POOL_SIZE
    assert pool._max_overflow == MAX_OVERFLOW
    assert pool._timeout == POOL_TIMEOUT_SECONDS
    # Neither is a parameter of `create_lakebase_engine`'s override set, and both are
    # load-bearing against a database that scales to zero. Asserted so a later change to the
    # kwargs above cannot drop them.
    assert pool._pre_ping is True
    assert pool._recycle > 0

    assert minter.calls == 0, "opening the registry minted a token; nothing has connected yet"


def test_the_pool_settings_are_the_values_the_registry_docstring_derives() -> None:
    """The numbers, pinned where a reader looking for them will be.

    `create_lakebase_engine`'s docstring derives "15 connections, up to 25 requests parked"
    from 5 plus 10, and 5 s from the measured 1.4 s first connect to a suspended endpoint. A
    change here without a change there leaves the arithmetic describing a pool that no longer
    exists.
    """
    assert (POOL_SIZE, MAX_OVERFLOW, POOL_TIMEOUT_SECONDS) == (5, 10, 5)


# --------------------------------------------------------------------------------------
# start_refresher() at startup, stop() at shutdown
# --------------------------------------------------------------------------------------


def _lifespan_scenario(api: FastAPI, body: Callable[[], Awaitable[None]]) -> None:
    """Run ``body`` inside the app's lifespan, on anyio, the way uvicorn would.

    The lifespan context directly rather than through a test client: entering it is the whole
    of what startup and shutdown are, and an HTTP client would add a transport this test has
    no use for.
    """

    async def scenario() -> None:
        async with api.router.lifespan_context(api):
            await body()

    anyio.run(scenario)


def test_the_refresher_starts_at_startup_and_stops_at_shutdown(
    disposed: list[AppDatabase],
) -> None:
    """Debt 2 of section 5 of `docs/lakebase-handoff.md`, wired.

    The `mints` counter carries the load here, in both directions. Before startup it proves the
    ABSENCE of work: building the app must start no thread, because `shellbox_app.server` is
    imported by every test in this lane. After shutdown it proves the thread really stopped,
    which a `None` attribute alone does not -- a thread that survived `stop()` would keep
    minting.
    """
    minter = CountingMinter()
    credentials = _credentials(minter)
    store = open_registry(CONFIGURED, build=RecordingBuilder(credentials))
    disposed.append(store)
    # A short interval so the poll below is a poll and not a wait. The production value is
    # `start_refresher`'s own default, which this leaves alone everywhere else.
    store.refresh_interval = 0.005
    api = build_app(database=store)

    assert credentials.mints == 0, "building the app minted a token"
    assert credentials._thread is None, "building the app started the refresher"
    assert store.starts == 0

    async def body() -> None:
        assert store.starts == 1, "the lifespan handler did not start the refresher"
        assert credentials._thread is not None, "start_refresher() was never called"
        assert credentials._thread.daemon, "the refresher must not hang process exit"
        with anyio.fail_after(5.0):
            while credentials.mints == 0:
                await anyio.sleep(0.005)

    _lifespan_scenario(api, body)

    assert credentials._thread is None, "stop() did not join the refresher thread"
    minted_by_shutdown = credentials.mints
    assert minted_by_shutdown > 0, "the refresher never minted, so this test asserted nothing"
    # Several intervals of the fastest refresher this test can ask for. A thread that survived
    # shutdown would mint again in that window.
    time.sleep(0.1)
    assert credentials.mints == minted_by_shutdown, "the refresher kept minting after shutdown"


def test_shutdown_survives_a_refresher_that_will_not_stop() -> None:
    """A failing shutdown must not become a failed lifespan.

    Both halves of `stop` are attempted, so a broken refresher cannot leave the pool
    undisposed, and neither failure reaches uvicorn as an exception.
    """

    class Stubborn(LakebaseCredentials):
        def stop(self, *, timeout: float = 2.0) -> None:
            raise RuntimeError("the refresher will not stop")

    disposals: list[bool] = []

    class Disposable(NullRegistry):
        def dispose(self) -> None:
            disposals.append(True)

    store = AppDatabase(registry=Disposable(), credentials=Stubborn(CountingMinter()))
    store.stop()

    assert disposals == [True], "a failing refresher stop skipped the pool disposal"


def test_an_app_with_no_credentials_starts_and_stops_without_a_thread() -> None:
    """The unconfigured case reaches the same lifespan handler, so it has to be a no-op."""
    store = AppDatabase.disabled()
    api = build_app(database=store)

    async def body() -> None:
        assert store.starts == 1

    _lifespan_scenario(api, body)
    assert isinstance(store.registry, NullRegistry)


# --------------------------------------------------------------------------------------
# Non-fatal
# --------------------------------------------------------------------------------------


def test_a_broken_environment_degrades_to_no_inventory_rather_than_no_terminals(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A half-configured environment must not stop the App from starting.

    The MCP half makes the same promise in `_open_registry`
    (`packages/shellbox-mcp/src/shellbox_mcp/server.py`), and
    `tests/integration/test_registry_non_fatal.py` asserts it end to end there.
    """
    with caplog.at_level("WARNING"):
        store = open_registry({"SHELLBOX_PG_HOST": "ep-example.database.cloud.databricks.com"})

    assert isinstance(store.registry, NullRegistry)
    assert store.credentials is None
    assert "no inventory" in caplog.text


def test_an_engine_that_will_not_build_degrades_the_same_way(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The other half of non-fatal, and the half the environment cannot reach.

    A bad driver, an unparseable URL or an SDK that will not import fails inside the builder
    rather than inside config parsing. Letting that propagate would take the process down
    before it served a single terminal.
    """

    def explode(endpoint: object, **engine_kwargs: object) -> tuple[Registry, LakebaseCredentials]:
        raise RuntimeError("no driver for this DSN")

    with caplog.at_level("WARNING"):
        store = open_registry(CONFIGURED, build=explode)

    assert isinstance(store.registry, NullRegistry)
    assert "no inventory" in caplog.text
    # The host is worth logging and the credential is not. Nothing minted, so there is no token
    # to leak, but the log line is the place that habit has to hold.
    assert CONFIGURED["SHELLBOX_PG_HOST"] in caplog.text


# --------------------------------------------------------------------------------------
# T-P4-RELAY-NO-DB
# --------------------------------------------------------------------------------------


def test_the_relay_works_with_a_registry_that_raises_on_every_call() -> None:
    """T-P4-RELAY-NO-DB, with the registry CONFIGURED and RAISING.

    The registry is on `app.state.database`, every one of its methods raises, and the accept
    path still binds both roles and sends both ``hello`` frames. A version of this test that
    passed because nothing was configured would prove nothing: there would be no database call
    available to fail.
    """
    registry = RaisingRegistry()
    store = AppDatabase(registry=registry)
    api = build_app(database=store)
    assert api.state.database.registry is registry

    relay = api.state.relay
    publisher = FakeSocket(headers={"x-forwarded-email": "viewer@example.com"})
    subscriber = FakeSocket()

    anyio.run(serve_publisher, relay, publisher, "sess-no-db")
    anyio.run(serve_subscriber, relay, subscriber, "sess-no-db")

    for socket in (publisher, subscriber):
        assert socket.accepted
        assert hello_of(socket).fields[FIELD_SESSION_ID] == "sess-no-db"
        assert socket.closed is None, "a refusal, not a bind"
    assert registry.calls == [], f"the accept path called the registry: {registry.calls}"
    assert relay.attachments == {}, "both handlers ran to completion and released"


def test_the_relay_path_names_nothing_from_the_database_layer() -> None:
    """T-P4-RELAY-NO-DB's structural half, and it is the one that survives refactoring.

    The behavioural test above passes as long as a database call happens to succeed or happens
    not to be reached. This one fails the moment the accept path so much as names the registry,
    which is what a future "record the bind in the inventory" change would look like on its
    first commit.

    Scoped to the relay path deliberately. `build_app` legitimately holds the registry, and the
    inventory routes legitimately read it.
    """
    relay_path = {
        "serve_publisher",
        "serve_subscriber",
        "_pump",
        "_send_hello",
        "_refuse",
        "_control_bytes",
        "health_payload",
        "bind_publisher",
        "bind_subscriber",
        "release",
    }
    forbidden = ("registry", "database", "engine", "credential", "session_maker", "execute")

    tree = ast.parse(Path(server.__file__).read_text(encoding="utf-8"))
    inspected = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if node.name not in relay_path:
            continue
        inspected.add(node.name)
        named = {inner.attr for inner in ast.walk(node) if isinstance(inner, ast.Attribute)} | {
            inner.id for inner in ast.walk(node) if isinstance(inner, ast.Name)
        }
        for name in named:
            assert not any(word in name.lower() for word in forbidden), (
                f"{node.name} names {name!r}, so the relay path now reaches the database layer"
            )

    assert inspected == relay_path, (
        "the relay path moved: expected to inspect "
        f"{sorted(relay_path)}, inspected {sorted(inspected)}"
    )


def test_the_health_route_is_still_zero_database() -> None:
    """``GET /`` is the deploy's smoke target and the prober's target.

    A database call here would put a Lakebase wake on the path of the check that has to answer
    when Lakebase is the broken thing. Asserted through the route, with a registry that raises.
    """
    api = build_app(database=AppDatabase(registry=RaisingRegistry()))
    (route,) = [candidate for candidate in api.routes if getattr(candidate, "path", None) == "/"]

    payload = route.endpoint()

    assert payload["service"] == "shellbox-app"
    assert api.state.database.registry.calls == []


def test_every_route_that_could_touch_the_database_is_a_sync_def() -> None:
    """The rule, applied to the route table this item builds.

    `PostgresRegistry` is synchronous SQLAlchemy, and a blocking call in a coroutine route
    stalls the event loop that relays every attached terminal. The WebSocket routes are
    correctly coroutines: they suspend on socket I/O and touch no database.

    WARNING: This check has NO positive witness yet, and it cannot have one here. The full
    rule names `/ready`, `/api/hosts` and `/api/sessions`, and none of them exists on this
    tree -- this item wires the database and adds no route. Whoever adds the first one owns
    replacing this with the version that asserts the route table contains all three.
    """
    import inspect as inspect_module

    from fastapi.routing import APIRoute

    api_routes = [route for route in build_app().routes if isinstance(route, APIRoute)]
    assert api_routes, "no HTTP routes at all, so this test asserts nothing"
    for route in api_routes:
        assert not inspect_module.iscoroutinefunction(route.endpoint), (
            f"{route.path} is an async def; a blocking database call there stalls every "
            "attached terminal at once"
        )


def test_the_config_module_declares_no_databricks_sdk_import() -> None:
    """The SDK is imported lazily, inside the minter, and that is not incidental.

    `databricks-sdk` can never be verified by an import-time check for exactly this reason --
    see the note in `packages/shellbox-registry/src/shellbox_registry/lakebase.py` under
    "Dependency boundary". So importing `shellbox_app.config` must stay free of it, and the
    App's credential path is proven by a live `/ready` call instead.
    """
    tree = ast.parse(Path(config.__file__).read_text(encoding="utf-8"))
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert not any(name.startswith("databricks") for name in imported), sorted(imported)


def test_a_token_older_than_the_margin_is_reminted_on_the_apps_own_settings() -> None:
    """The refresher's contract, restated on the settings the App actually uses.

    `tests/unit/test_lakebase.py` owns the credential lifecycle. This asserts only that the
    App's own construction path reaches the same object -- a wiring change that swapped
    `LakebaseCredentials` for something else would leave that file green.
    """
    minter = CountingMinter()
    credentials = _credentials(minter)
    store = open_registry(CONFIGURED, build=RecordingBuilder(credentials))
    try:
        assert store.credentials is credentials
        first = credentials.token()
        second = credentials.token()
        assert (first, second) == ("token-1", "token-2"), "a spent token was served twice"
        assert credentials.expires_at is not None
        assert credentials.expires_at < datetime.now(UTC) + timedelta(seconds=1)
    finally:
        store.stop()
