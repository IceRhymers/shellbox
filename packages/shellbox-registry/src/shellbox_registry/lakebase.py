"""Lakebase: an OAuth token used as the Postgres password (W9, ADR-3).

ADR-3's claim is that Lakebase is a **credential concern, not a second registry**:
`PostgresRegistry` connects to a DSN and does not care who minted the password. This
module is the whole of what Lakebase adds — everything else in the package is unchanged
and already proven against vanilla Postgres.

## Three facts about the credential that shape every decision here

1. **It expires.** The token is short-lived (the API caps `ttl` at one hour and enforces a
   floor of five minutes), so a process that outlives one token must mint another.
2. **The server tells you when.** `generate_database_credential` returns `expire_time`
   alongside the token, so the expiry is a *measured* value rather than an assumed TTL.
   This module trusts that field and never hardcodes a lifetime — a server-side policy
   change then costs nothing.
3. **It authenticates the connection, not the query.** Postgres checks the password once,
   at connect. So a *pooled* connection keeps working after its token expires until the
   server decides otherwise — which is why `pool_recycle` matters independently of
   refresh: it retires a connection on **our** schedule instead of the server's, so the
   failure surfaces as a new connect rather than mid-query.

## Why refresh is lazy by default

The obvious design — a background thread refreshing every 45 minutes — is right for a
long-lived App and wrong for this package's other consumer. `shellbox-mcp` is spawned
**per agent session**, 1-32 concurrent and short-lived, so:

* it would essentially never reach a 45-minute refresh, leaving 32 dead threads;
* every process would mint a token at startup, on the path W7 promises is non-blocking;
* a non-daemon thread hangs process exit after stdio closes — invisible to an MCP client
  except as a child that never reaps.

So `token()` refreshes **on demand, when the cached one is near expiry**, which composes
with `pool_pre_ping` and costs nothing in a process that exits in seconds. The background
refresher still exists for the App, as an explicit opt-in (`start_refresher()`).

## Dependency boundary

`databricks-sdk` is an **optional extra** (`shellbox-registry[lakebase]`) and imported
lazily inside the minter. The package's other three dependencies are alembic, psycopg and
SQLAlchemy — deliberately SDK-free, which is what lets local-Postgres CI, `NullRegistry`
and the whole test suite run with no Databricks dependency at all. Importing this module
does not require the SDK; *minting a token* does.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, NamedTuple

from sqlalchemy import Engine, create_engine, event

from shellbox_registry.postgres import PostgresRegistry

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy.engine.interfaces import Dialect

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_DATABASE",
    "Credential",
    "LakebaseCredentials",
    "LakebaseEndpoint",
    "TokenMinter",
    "create_lakebase_engine",
    "is_service_principal_role",
    "lakebase_registry",
    "resolve_lakebase_endpoint",
    "sdk_token_minter",
]

# The Postgres database a Lakebase project auto-provisions, and therefore the one every
# caller reaches unless it says otherwise.
#
# It restates the `pg_database` variable's default in `databricks.yml`, which is the
# authority. Both are the UNDERSCORED name used to CONNECT -- the database RESOURCE id is
# the hyphenated `databricks-postgres` and appears only in the App binding path in
# `resources/app.yml`. Measured 2026-08-03: `list-databases` reports
# `database_id: databricks-postgres` and `postgres_database: databricks_postgres`.
DEFAULT_DATABASE = "databricks_postgres"

# How long before stated expiry a token is treated as spent.
#
# Not cosmetic: a token that passes the check and then expires *during* the TCP connect and
# TLS handshake fails the connect, and the caller sees an authentication error rather than
# an expiry. The margin has to exceed a plausible connect time, and the API's own floor is
# 300s, so 60s is comfortably inside any token we can be issued.
REFRESH_MARGIN = timedelta(seconds=60)

# Retire pooled connections inside the token lifetime.
#
# Refresh does NOT make this redundant — see fact 3 in the module docstring. A connection
# authenticated with a now-expired token can be terminated server-side at a moment of the
# server's choosing; recycling first means the next checkout opens a fresh connection with
# a fresh token, and `pool_pre_ping` catches whatever slips through.
#
# 1800s is half of the **measured** default lifetime: the live API issues 3600s tokens
# (`iat`/`exp` on the returned JWT, 2026-07-31). An earlier value of 240s was derived from
# the API's documented 300s *floor* rather than from what it actually issues, which would
# have thrown away good connections roughly 15x more often than necessary. `pool_pre_ping`
# is the real safety net here; this is the secondary guard.
#
# A caller that deliberately requests a SHORT `ttl` must lower this to match —
# `sdk_token_minter` warns when it observes a token shorter than this value, so the
# mismatch is observable rather than silent.
POOL_RECYCLE_SECONDS = 1800

# What the API documents as the shortest `ttl` it will honour. Referenced so the
# relationship between recycling and token lifetime is checkable rather than folklore.
API_MIN_TTL_SECONDS = 300

# SQLAlchemy's OWN default for `pool_timeout`, restated so the default below is visibly
# inherited rather than chosen. Named because the two readings differ: a value this module
# picked would be a claim about Lakebase, and this one is not. It is here so adding the
# parameter changes no existing caller's behaviour.
#
# SQLAlchemy declares it as `30.0` on `QueuePool.__init__`; the int compares equal, and
# `tests/unit/test_lakebase.py` reads the library's signature so a change there fails
# loudly instead of quietly moving every caller. See `create_lakebase_engine` for why the
# App overrides it, and for the arithmetic that makes 30s the wrong value there.
SQLALCHEMY_DEFAULT_POOL_TIMEOUT_SECONDS = 30

# Backoff after a failed mint, so a control-plane outage does not become a hot loop against
# it. Bounded rather than unbounded: the point is to stop hammering, not to give up.
_BACKOFF_SECONDS = (1.0, 2.0, 5.0, 15.0, 30.0)


class Credential(NamedTuple):
    """A minted token and the moment the **server** says it stops being valid."""

    token: str
    expires_at: datetime


TokenMinter = Callable[[], Credential]
"""Anything that can produce a credential. The seam that makes this testable without a
Databricks workspace -- and the reason `LakebaseCredentials` has no SDK import."""


@dataclass(frozen=True, slots=True)
class LakebaseEndpoint:
    """Everything needed to open a connection, resolved once.

    ``resource_name`` is the API path
    (``projects/{p}/branches/{b}/endpoints/{e}``) and is what mints tokens; ``host`` is
    what psycopg dials. They are separate fields because they come from different calls and
    conflating them is the first mistake to make here.
    """

    resource_name: str
    host: str
    database: str
    user: str
    """The Postgres role. A workspace user's email, or an App service principal's client
    id -- Lakebase maps the OAuth principal onto a role of the same name, which is why no
    password is ever configured for it."""
    port: int = 5432

    def dsn(self, password: str = "") -> str:
        """A DSN for this endpoint. The password is injected per-connect, not baked in.

        Defaults to empty on purpose: the engine's ``do_connect`` hook supplies the real
        token, so a DSN that leaked (a log, an exception, `repr`) carries no credential.
        """
        from urllib.parse import quote_plus

        secret = f":{quote_plus(password)}" if password else ""
        return (
            f"postgresql+psycopg://{quote_plus(self.user)}{secret}"
            f"@{self.host}:{self.port}/{self.database}?sslmode=require"
        )


# The canonical dashed 8-4-4-4-12 uuid, and deliberately only that form.
#
# `uuid.UUID` also accepts 32 undashed hex characters, which would classify a role legitimately
# named for a hex digest as a service principal. Refusing a real migration is the expensive
# direction of a wrong answer, so this matches the shape the CLI actually emits.
_UUID_SEGMENTS = (8, 4, 4, 4, 12)


def is_service_principal_role(user: str) -> bool:
    """True when ``user`` has the shape of a Lakebase service-principal role name.

    Lakebase names a role for its OAuth principal, so a service principal's role name IS its
    client id, which is a uuid -- ``databricks postgres create-role --help`` (CLI v1.8.0)
    documents the spec as ``{"identity_type": "SERVICE_PRINCIPAL", "postgres_role":
    "<SP_CLIENT_ID>"}``. A workspace user's role name is their email, which never matches.

    ``scripts/check_deploy_principal.py`` carries a second copy of this rule, and it has to:
    that guard runs under a bare ``python3`` on a checkout with nothing synced, so it cannot
    import this package. ``tests/unit/test_deploy_principal.py`` asserts the two agree, which
    is what keeps the copy from drifting into a different rule.
    """
    parts = user.split("-")
    if len(parts) != len(_UUID_SEGMENTS):
        return False
    return all(
        len(part) == width and all(c in "0123456789abcdefABCDEF" for c in part)
        for part, width in zip(parts, _UUID_SEGMENTS, strict=True)
    )


def resolve_lakebase_endpoint(
    resource_name: str,
    *,
    database: str = DEFAULT_DATABASE,
    user: str | None = None,
    client: Any | None = None,
) -> LakebaseEndpoint:
    """Turn an endpoint resource name into a complete `LakebaseEndpoint`.

    Only the resource name is ever written down. `scripts/bundle-vars.sh` constructs it from
    the three ids the bundle declares and prints it as ``SHELLBOX_PG_RESOURCE``; this function
    supplies the rest from the workspace. That is the difference between a deploy-time command
    taking ONE value and an operator exporting six.

    ``host`` comes from ``get_endpoint(resource_name).status.hosts.host``. It is a DIFFERENT
    string from ``resource_name`` -- the resource name mints tokens, the host is what psycopg
    dials -- and section 6 of ``docs/lakebase-handoff.md`` records that JSON shape.

    **CRITICAL: deriving the user is a safety property, not a convenience.** Omitted, ``user``
    is ``current_user.me().user_name``, so the connection authenticates as whoever ran the
    command. A migration therefore runs as the DEPLOYING PRINCIPAL by construction, and it
    cannot silently run as the App's service principal -- which holds ``SELECT`` on two tables,
    fails a migration on a permission error, and whose tempting fix is a wider grant.
    ``scripts/check_deploy_principal.py`` makes that argument in full. Pass ``user`` only to
    override the derivation deliberately.

    ``client`` is the same testability seam `sdk_token_minter` offers, and the SDK import is
    lazy for the same reason: `shellbox-registry` is SDK-free at import time and must stay so.
    """
    if client is None:
        try:
            from databricks.sdk import WorkspaceClient
        except ImportError as exc:
            raise RuntimeError(
                "resolving a Lakebase endpoint needs the databricks-sdk; install "
                "shellbox-registry[lakebase]"
            ) from exc
        client = WorkspaceClient()

    # Each hop is guarded on its own. The SDK returns dataclasses whose fields are all
    # Optional, so an endpoint that exists but is not reporting a host yet gives `None` at any
    # of three levels -- and a `None` that reached `LakebaseEndpoint` would surface much later
    # as a psycopg error naming no host.
    endpoint = client.postgres.get_endpoint(resource_name)
    status = getattr(endpoint, "status", None)
    hosts = getattr(status, "hosts", None) if status is not None else None
    host = getattr(hosts, "host", None) if hosts is not None else None
    if not host:
        raise RuntimeError(
            f"Lakebase reported no host for {resource_name!r}. The endpoint may not be "
            f"provisioned yet: run `databricks bundle deploy` for this target first, then "
            f"`databricks postgres get-endpoint {resource_name}` to see what it reports."
        )

    resolved_user = user
    if not resolved_user:
        resolved_user = getattr(client.current_user.me(), "user_name", None)
    if not resolved_user:
        raise RuntimeError(
            "could not resolve a Postgres role: the workspace reported no user_name for the "
            "current principal. Pass user=... to name the role explicitly."
        )

    return LakebaseEndpoint(
        resource_name=resource_name,
        host=host,
        database=database,
        user=resolved_user,
    )


def sdk_token_minter(
    resource_name: str,
    *,
    client: Any | None = None,
    ttl_seconds: int | None = None,
) -> TokenMinter:
    """A `TokenMinter` backed by the Databricks SDK. Requires the ``lakebase`` extra.

    ``ttl_seconds`` is passed through when given; the API enforces 300s ≤ ttl ≤ 1h and
    picks its own default otherwise. Worth setting only to *shorten* a token deliberately.
    """

    def mint() -> Credential:
        nonlocal client
        if client is None:
            try:
                from databricks.sdk import WorkspaceClient
            except ImportError as exc:  # pragma: no cover - depends on the install extra
                raise RuntimeError(
                    "minting a Lakebase credential needs the databricks-sdk; install "
                    "shellbox-registry[lakebase]"
                ) from exc
            client = WorkspaceClient()

        kwargs: dict[str, Any] = {}
        if ttl_seconds is not None:
            # WARNING: `ttl` is a protobuf `Duration`, NOT the `"900s"` string the API docs show —
            # the SDK calls `.ToJsonString()` on whatever it is given, so a string raises
            # `AttributeError: 'str' object has no attribute 'ToJsonString'`. Measured
            # against the live API; the fake minter in the unit tests cannot see this.
            from google.protobuf.duration_pb2 import Duration

            kwargs["ttl"] = Duration(seconds=ttl_seconds)
        result = client.postgres.generate_database_credential(endpoint=resource_name, **kwargs)

        token = getattr(result, "token", None)
        if not token:
            raise RuntimeError(f"Lakebase returned no token for {resource_name}")

        expires_at = _as_datetime(getattr(result, "expire_time", None))
        if expires_at is None:
            # The server normally states an expiry; if a version stops doing so, assume the
            # API's documented FLOOR rather than its ceiling. Guessing an hour on a
            # five-minute token means every connection fails; guessing five minutes on an
            # hour-long token costs a few extra mints.
            expires_at = datetime.now(UTC) + timedelta(seconds=API_MIN_TTL_SECONDS)
            logger.warning(
                "Lakebase did not state an expiry for %s; assuming the API's %ds floor. "
                "Tokens will be re-minted far more often than necessary.",
                resource_name,
                API_MIN_TTL_SECONDS,
            )

        # Make the recycle/lifetime coupling observable. A short token with the default
        # recycle means a pooled connection can outlive its credential, which shows up as
        # a server-side disconnect rather than as anything pointing back here.
        lifetime = (expires_at - datetime.now(UTC)).total_seconds()
        if lifetime < POOL_RECYCLE_SECONDS:
            logger.warning(
                "Lakebase issued a %.0fs token but pool_recycle is %ds, so a pooled "
                "connection can outlive its credential. Pass a matching pool_recycle to "
                "create_lakebase_engine.",
                lifetime,
                POOL_RECYCLE_SECONDS,
            )
        return Credential(token=token, expires_at=expires_at)

    return mint


def _as_datetime(value: Any) -> datetime | None:
    """Coerce the SDK's expiry to an aware UTC datetime, or ``None``.

    WARNING: **`expire_time` is a protobuf `Timestamp`, not a datetime or a string** — measured
    against the live API, after an earlier version of this function handled only the latter
    two, silently returned ``None``, and sent the caller down the "assume the floor" path.
    It assumed **300s against a real 3600s token**, i.e. 12x the necessary minting, and no
    unit test could see it because the fake minter never touches the SDK.

    `ToDatetime()` returns a **naive** UTC datetime, so the tzinfo is attached here; a naive
    value compared against an aware `now()` raises `TypeError` on the hot path.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    to_datetime = getattr(value, "ToDatetime", None)
    if callable(to_datetime):
        converted = to_datetime()
        return converted if converted.tzinfo else converted.replace(tzinfo=UTC)
    seconds = getattr(value, "seconds", None)
    if isinstance(seconds, int):
        return datetime.fromtimestamp(seconds, UTC)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


class LakebaseCredentials:
    """Holds the current token, refreshing it when it is near expiry.

    Thread-safe: SQLAlchemy calls `token()` from whichever thread checks a connection out
    of the pool, and `shellbox-mcp` runs enrollment on a background thread alongside tool
    calls.
    """

    def __init__(
        self,
        minter: TokenMinter,
        *,
        margin: timedelta = REFRESH_MARGIN,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self._minter = minter
        self._margin = margin
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._current: Credential | None = None
        self._failures = 0
        self._next_attempt: datetime | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.mints = 0
        """How many times a token was actually minted. Exposed so a test can assert the
        *absence* of work -- "a valid token was reused" is otherwise unobservable."""

    # -- the hot path ------------------------------------------------------------------
    def token(self) -> str:
        """The current token, minting a new one if the cached one is spent.

        Never returns an expired token. If minting fails while the cached token is still
        valid, the cached one is served and the failure is logged — a control-plane blip
        must not take down connections that would have worked.
        """
        with self._lock:
            now = self._clock()
            if self._current is not None and not self._is_spent(self._current, now):
                return self._current.token
            return self._refresh_locked(now)

    def _is_spent(self, credential: Credential, now: datetime) -> bool:
        return now >= credential.expires_at - self._margin

    def _refresh_locked(self, now: datetime) -> str:
        # Honour the backoff, but only while there is something to fall back on. With no
        # usable token, refusing to try is strictly worse than trying and failing.
        if (
            self._next_attempt is not None
            and now < self._next_attempt
            and self._current is not None
            and now < self._current.expires_at
        ):
            logger.debug("serving a near-expiry Lakebase token; next mint attempt is deferred")
            return self._current.token

        try:
            credential = self._minter()
        except Exception as exc:  # noqa: BLE001 - a mint failure must be survivable
            self._failures += 1
            delay = _BACKOFF_SECONDS[min(self._failures, len(_BACKOFF_SECONDS)) - 1]
            self._next_attempt = now + timedelta(seconds=delay)
            if self._current is not None and now < self._current.expires_at:
                logger.warning(
                    "could not mint a Lakebase credential (%s: %s); serving the cached token, "
                    "which expires at %s. Next attempt in %.0fs.",
                    type(exc).__name__,
                    exc,
                    self._current.expires_at.isoformat(),
                    delay,
                )
                return self._current.token
            # Nothing valid to serve. Raising here becomes a `registry_warning` on an
            # otherwise successful tool call -- the inventory goes stale, shells keep
            # working (§9, R7).
            raise

        self._failures = 0
        self._next_attempt = None
        self._current = credential
        self.mints += 1
        logger.debug("minted a Lakebase credential valid until %s", credential.expires_at)
        return credential.token

    @property
    def expires_at(self) -> datetime | None:
        """When the cached token stops being valid. ``None`` before the first mint."""
        return self._current.expires_at if self._current else None

    # -- the App-side opt-in -----------------------------------------------------------
    def start_refresher(self, *, interval: float = 60.0) -> None:
        """Refresh ahead of expiry on a **daemon** thread. For long-lived processes only.

        Deliberately not the default — see the module docstring. Daemon so it cannot hang
        process exit, and `Event.wait` so `stop()` returns promptly rather than one full
        interval later.
        """
        if self._thread is not None:
            return
        self._stop.clear()

        def run() -> None:
            while not self._stop.wait(interval):
                try:
                    self.token()
                except Exception as exc:  # noqa: BLE001 - a refresher may not die
                    logger.warning(
                        "background Lakebase refresh failed (%s); will retry", type(exc).__name__
                    )

        self._thread = threading.Thread(target=run, name="lakebase-refresh", daemon=True)
        self._thread.start()

    def stop(self, *, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None


def create_lakebase_engine(
    endpoint: LakebaseEndpoint,
    credentials: LakebaseCredentials,
    *,
    pool_size: int = 5,
    max_overflow: int = 5,
    pool_recycle: int = POOL_RECYCLE_SECONDS,
    connect_timeout: int = PostgresRegistry.CONNECT_TIMEOUT_SECONDS,
    pool_timeout: int = SQLALCHEMY_DEFAULT_POOL_TIMEOUT_SECONDS,
) -> Engine:
    """An engine that injects a fresh token as the password on every new connection.

    Four settings, each for a measured reason rather than by convention:

    * ``pool_pre_ping`` — Lakebase **scales to zero**, which kills pooled connections. A
      checkout of a dead connection would otherwise surface as an error on whatever query
      happened to be next. **Hardcoded ``True``, deliberately not a parameter.** A caller
      cannot weaken it by passing a value; only an edit to that line can. Do not make one.
    * ``pool_recycle`` — see fact 3 in the module docstring; refresh does not cover this.
    * ``connect_timeout`` — inherited from `PostgresRegistry`, where an unbounded connect
      was measured to hang a tool call for 63 seconds.
    * ``pool_timeout`` — how long a checkout waits for a **free connection from the pool**.
      Not `connect_timeout`, which bounds the TCP connect instead; the comment on
      `PostgresRegistry.__init__` draws that line. The default here is SQLAlchemy's own
      30 s, so adding this parameter changed no caller.

      **The App passes 5**, and raises ``max_overflow`` to 10 from the 5 defaulted here.
      Those 15 connections sit behind Starlette's 40-thread threadpool. A synchronized edge
      kill reconnects every browser at once, which parks up to 25 requests. At 30 s each
      that includes ``/ready`` — the only automatic detector of a database problem, queuing
      behind the storm it exists to diagnose. 5 s clears the **measured** 1.4 s first
      connect to an ``IDLE`` endpoint (`docs/lakebase-handoff.md` section 2) and stays far
      under 30 s.

      The App is the only caller with a threadpool in front of this pool. `shellbox-mcp` is
      not, which is why the wider default stays wide.

    ``do_connect`` rather than a DSN password: the token changes and the URL does not, so
    baking it in would pin the first token for the engine's life and leak it into every
    `repr` of the URL.
    """
    engine = create_engine(
        endpoint.dsn(),
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_pre_ping=True,
        pool_recycle=pool_recycle,
        pool_timeout=pool_timeout,
        connect_args={"connect_timeout": connect_timeout},
    )

    @event.listens_for(engine, "do_connect")
    def _inject_token(
        dialect: Dialect,  # noqa: ARG001 - SQLAlchemy's signature
        conn_rec: Any,  # noqa: ARG001
        cargs: tuple[Any, ...],  # noqa: ARG001
        cparams: dict[str, Any],
    ) -> None:
        # Returning None lets SQLAlchemy do the actual connect with the params we mutated.
        cparams["password"] = credentials.token()

    return engine


def lakebase_registry(
    endpoint: LakebaseEndpoint,
    *,
    minter: TokenMinter | None = None,
    credentials: LakebaseCredentials | None = None,
    **engine_kwargs: Any,
) -> tuple[PostgresRegistry, LakebaseCredentials]:
    """A `PostgresRegistry` backed by Lakebase. Returns the credentials so a caller can
    `start_refresher()` (App) or leave them lazy (MCP), and `stop()` on shutdown.

    This is the whole of ADR-3's "Lakebase is a credential concern": the registry is the
    same class the local-Postgres tests exercise, handed a different engine.
    """
    if credentials is None:
        if minter is None:
            minter = sdk_token_minter(endpoint.resource_name)
        credentials = LakebaseCredentials(minter)
    engine = create_lakebase_engine(endpoint, credentials, **engine_kwargs)
    return PostgresRegistry(dsn="", engine=engine), credentials
