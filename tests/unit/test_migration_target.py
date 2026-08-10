"""Which database `alembic upgrade head` actually reaches, and why the endpoint wins.

The alembic environment in
`packages/shellbox-registry/src/shellbox_registry/alembic/env.py` now has two paths, and the
one it picks is a deploy-time correctness question rather than a preference:

* ``SHELLBOX_PG_RESOURCE`` set -> Lakebase, with the host, the role and the token derived.
* Otherwise -> ``dsn_from_env()``. **This is CI's path.** The ``registry`` job in
  `.github/workflows/ci.yml` sets the ``SHELLBOX_PG_*`` components against a
  ``postgres:16-alpine`` service, sets no resource, and runs ``make migrate-roundtrip``.

Every case below runs alembic FOR REAL, through the same `Config` API
`tests/registry/test_migrations.py` uses, and none of them needs a database or a workspace:

* Offline mode emits SQL and connects to nothing.
* The online DSN case points at a closed port, so the failure is a refused connection -- which
  is itself the evidence about WHICH host was dialled.
* The online Lakebase case replaces the two SDK-backed functions with fakes, so no workspace
  call happens. The engine then dials a host in the reserved ``.invalid`` domain, which cannot
  resolve, and the resulting error names it.

The last two are the ones that matter. A test asserting only that a variable was read would
pass for an implementation that read it and then built the wrong engine.

The online cases expect ``sqlalchemy.exc.OperationalError`` specifically, and the narrowness is
part of the assertion: it says the run got as far as opening a connection to a host it chose. A
bare ``Exception`` would also be satisfied by a resolver that raised before deciding anything.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.exc import OperationalError

REPO_ROOT = Path(__file__).resolve().parents[2]

# Nothing listens here. `127.0.0.1` rather than a hostname so the failure is a refused
# connection rather than a DNS lookup, and port 1 so it is refused immediately.
CLOSED_PORT = "1"
LOCAL_HOST = "127.0.0.1"

# `.invalid` is reserved by RFC 2606 and never resolves, so the Lakebase engine below fails
# fast and names this host. It must not look like the DSN host above, because telling the two
# apart is the whole assertion.
FAKE_LAKEBASE_HOST = "ep-not-a-real-endpoint.invalid"

RESOURCE = "projects/shellbox-pg-dev/branches/production/endpoints/primary"

_PG_VARIABLES = (
    "SHELLBOX_PG_RESOURCE",
    "SHELLBOX_DATABASE_URL",
    "SHELLBOX_PG_USER",
    "SHELLBOX_PG_PASSWORD",
    "SHELLBOX_PG_HOST",
    "SHELLBOX_PG_PORT",
    "SHELLBOX_PG_DB",
    "SHELLBOX_PG_SSLMODE",
)


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """A process environment with no registry variables set.

    A developer running this suite legitimately has some of them exported, and every case here
    would otherwise assert against their laptop's configuration rather than the fixture's.
    """
    for name in _PG_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


@pytest.fixture
def alembic_config() -> Config:
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / cfg.get_main_option("script_location")))
    return cfg


def _local_dsn_components(env: pytest.MonkeyPatch) -> None:
    """The shape CI's ``registry`` job sets: components, never an assembled URL."""
    env.setenv("SHELLBOX_PG_USER", "shellbox")
    env.setenv("SHELLBOX_PG_PASSWORD", "shellbox")
    env.setenv("SHELLBOX_PG_HOST", LOCAL_HOST)
    env.setenv("SHELLBOX_PG_PORT", CLOSED_PORT)
    env.setenv("SHELLBOX_PG_DB", "shellbox")


@pytest.fixture
def refuse_the_lakebase_path(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Make the Lakebase path impossible to take without saying so.

    This is the anti-vacuous half of the DSN cases. Without it, "the migration failed to
    connect to 127.0.0.1" would also be consistent with an implementation that resolved a
    Lakebase endpoint first and happened to fail later.
    """
    import shellbox_registry.lakebase as lakebase

    def refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError(
            "the Lakebase path was taken with no SHELLBOX_PG_RESOURCE set; CI's local-Postgres "
            "migration would have tried to reach a workspace"
        )

    monkeypatch.setattr(lakebase, "resolve_lakebase_endpoint", refuse)
    monkeypatch.setattr(lakebase, "sdk_token_minter", refuse)
    yield


def _fake_lakebase(monkeypatch: pytest.MonkeyPatch, *, user: str) -> dict[str, Any]:
    """Replace the two SDK-backed functions with fakes, and record what they were asked.

    `env.py` imports both INSIDE the function that uses them, so the attribute is looked up on
    the module at call time and patching the module is enough.
    """
    from datetime import UTC, datetime, timedelta

    import shellbox_registry.lakebase as lakebase

    seen: dict[str, Any] = {}

    def fake_resolve(resource_name: str, **kwargs: Any) -> lakebase.LakebaseEndpoint:
        seen["resource_name"] = resource_name
        seen["database"] = kwargs.get("database")
        seen["user_argument"] = kwargs.get("user")
        return lakebase.LakebaseEndpoint(
            resource_name=resource_name,
            host=FAKE_LAKEBASE_HOST,
            database=str(kwargs.get("database")),
            user=user,
        )

    def fake_minter(resource_name: str, **_kwargs: Any) -> lakebase.TokenMinter:
        seen["minted_for"] = resource_name
        return lambda: lakebase.Credential(
            token="fake-token", expires_at=datetime.now(UTC) + timedelta(hours=1)
        )

    monkeypatch.setattr(lakebase, "resolve_lakebase_endpoint", fake_resolve)
    monkeypatch.setattr(lakebase, "sdk_token_minter", fake_minter)
    return seen


# ------------------------------------------------------------------ the DSN path, unchanged
def test_no_resource_means_the_dsn_path_and_real_migrations_are_emitted(
    clean_env: pytest.MonkeyPatch,
    alembic_config: Config,
    refuse_the_lakebase_path: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Offline mode against the components CI sets, with the Lakebase path booby-trapped.

    Asserting on the emitted DDL rather than on an exit code: a run that resolved no revisions
    at all also exits 0, and would prove nothing about which URL it used.
    """
    _local_dsn_components(clean_env)

    command.upgrade(alembic_config, "head", sql=True)

    emitted = capsys.readouterr().out
    assert "CREATE TABLE hosts" in emitted
    assert "CREATE TABLE sessions" in emitted


def test_the_online_dsn_path_dials_the_configured_host_and_not_a_workspace(
    clean_env: pytest.MonkeyPatch,
    alembic_config: Config,
    refuse_the_lakebase_path: None,
) -> None:
    """CRITICAL: the regression that would hurt most, and it is CI's exact path.

    `.github/workflows/ci.yml`'s ``registry`` job runs ``make migrate-roundtrip`` ONLINE against
    a service container. The evidence that the DSN path was taken is the identity of the host
    that refused the connection: a Lakebase engine would have dialled somewhere else entirely,
    and the fixture above turns that into a loud failure rather than a wrong host.
    """
    _local_dsn_components(clean_env)

    with pytest.raises(OperationalError) as raised:
        command.upgrade(alembic_config, "head")

    message = str(raised.value)
    assert LOCAL_HOST in message, f"the migration did not dial the configured host: {message}"
    assert CLOSED_PORT in message


# ------------------------------------------------------- the endpoint wins when it is set
def test_a_configured_endpoint_wins_over_a_dsn(
    clean_env: pytest.MonkeyPatch, alembic_config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CRITICAL: presence of the endpoint is the selector, NOT absence of a DSN.

    Both are set here, which is the realistic case rather than a contrived one: `dsn_from_env`
    defaults the host to localhost as soon as any ``SHELLBOX_PG_*`` variable is set, so a
    developer who once exported one always has a DSN. Under the other rule
    `scripts/deploy.sh` would migrate their laptop and report success, leaving the deployed App
    reading an unmigrated database.

    The proof is again which host was dialled.
    """
    _local_dsn_components(clean_env)
    clean_env.setenv("SHELLBOX_PG_RESOURCE", RESOURCE)
    seen = _fake_lakebase(monkeypatch, user="tanner.wendland@databricks.com")

    with pytest.raises(OperationalError) as raised:
        command.upgrade(alembic_config, "head")

    message = str(raised.value)
    assert FAKE_LAKEBASE_HOST in message, f"the DSN won over the endpoint: {message}"
    assert LOCAL_HOST not in message
    assert seen["resource_name"] == RESOURCE
    assert seen["minted_for"] == RESOURCE, "no token minter was built for the endpoint"


def test_the_database_name_defaults_the_way_the_bundle_does(
    clean_env: pytest.MonkeyPatch, alembic_config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``SHELLBOX_PG_DB`` supplies it; unset, it is the bundle's `pg_database` default.

    Both directions are asserted, because a resolver that ignored the variable and one that
    ignored the default look identical from a single case.
    """
    from shellbox_registry.lakebase import DEFAULT_DATABASE

    clean_env.setenv("SHELLBOX_PG_RESOURCE", RESOURCE)
    seen = _fake_lakebase(monkeypatch, user="tanner.wendland@databricks.com")
    with pytest.raises(OperationalError):
        command.upgrade(alembic_config, "head")
    assert seen["database"] == DEFAULT_DATABASE

    clean_env.setenv("SHELLBOX_PG_DB", "some_other_database")
    seen = _fake_lakebase(monkeypatch, user="tanner.wendland@databricks.com")
    with pytest.raises(OperationalError):
        command.upgrade(alembic_config, "head")
    assert seen["database"] == "some_other_database"


def test_the_user_is_derived_unless_the_environment_names_one(
    clean_env: pytest.MonkeyPatch, alembic_config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unset means "derive it", which is what keeps the migration on the deploying principal.

    `env.py` must pass ``None`` rather than an empty string, because an empty string is a
    caller-supplied user as far as `resolve_lakebase_endpoint` is concerned and would skip the
    derivation.
    """
    clean_env.setenv("SHELLBOX_PG_RESOURCE", RESOURCE)
    seen = _fake_lakebase(monkeypatch, user="derived@databricks.com")
    with pytest.raises(OperationalError):
        command.upgrade(alembic_config, "head")
    assert seen["user_argument"] is None

    clean_env.setenv("SHELLBOX_PG_USER", "named@databricks.com")
    seen = _fake_lakebase(monkeypatch, user="named@databricks.com")
    with pytest.raises(OperationalError):
        command.upgrade(alembic_config, "head")
    assert seen["user_argument"] == "named@databricks.com"


def test_a_derived_service_principal_role_is_refused(
    clean_env: pytest.MonkeyPatch, alembic_config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CRITICAL: the guard `scripts/check_deploy_principal.py` cannot apply on this path.

    That guard reads the environment, and on the endpoint path nobody exports a user -- so it
    has nothing to inspect and returns 0. Without the check inside `env.py`, the endpoint path
    would be the one way to migrate as a service principal with no refusal in front of it.

    The role name is measured: 2026-08-03, ``databricks apps get shellbox -o json`` reported
    ``service_principal_client_id`` ``3337afac-b67b-41af-8996-828620bcc4a8``.
    """
    clean_env.setenv("SHELLBOX_PG_RESOURCE", RESOURCE)
    _fake_lakebase(monkeypatch, user="3337afac-b67b-41af-8996-828620bcc4a8")

    with pytest.raises(RuntimeError, match="service principal role") as raised:
        command.upgrade(alembic_config, "head")

    # It must fail BEFORE dialling anything. A refusal that arrived after a connect attempt
    # would mean the credential had already been used.
    assert FAKE_LAKEBASE_HOST not in str(raised.value)


# ------------------------------------------------------------------------- offline mode
def test_offline_mode_refuses_an_endpoint_and_names_the_alternative(
    clean_env: pytest.MonkeyPatch, alembic_config: Config
) -> None:
    """There is no password to bake into a URL, and baking one in would leak a live token.

    The message must name the DSN route, because "unsupported" without an alternative is how a
    reader concludes the tool is broken.
    """
    clean_env.setenv("SHELLBOX_PG_RESOURCE", RESOURCE)

    with pytest.raises(RuntimeError, match="offline mode cannot use a Lakebase endpoint") as raised:
        command.upgrade(alembic_config, "head", sql=True)

    message = str(raised.value)
    assert RESOURCE in message
    assert "SHELLBOX_DATABASE_URL" in message


def test_nothing_configured_still_fails_with_the_message_that_says_what_to_set(
    clean_env: pytest.MonkeyPatch, alembic_config: Config
) -> None:
    """The unconfigured case is a hard error, not a silent no-op: `NullRegistry` has no schema.

    The message now leads with the endpoint route, because that is the one an operator can
    satisfy from the bundle alone.
    """
    with pytest.raises(RuntimeError, match="No database is configured") as raised:
        command.upgrade(alembic_config, "head")

    message = str(raised.value)
    assert "SHELLBOX_PG_RESOURCE" in message
    assert "bundle-vars.sh" in message


def test_the_environment_is_what_selects_the_path_not_an_alembic_ini_url(
    clean_env: pytest.MonkeyPatch, alembic_config: Config
) -> None:
    """No credential is committed to `alembic.ini`, and nothing here reads one from it."""
    assert alembic_config.get_main_option("sqlalchemy.url", None) is None
    assert "SHELLBOX_PG_RESOURCE" not in os.environ


def test_migrating_in_process_does_not_disable_every_other_logger() -> None:
    """`env.py` must pass ``disable_existing_loggers=False`` to `fileConfig`.

    NOT a style preference. `fileConfig` DEFAULTS that argument to ``True``, which sets
    ``disabled = True`` on every logger that already exists and is not named in `alembic.ini`'s
    ``[loggers]`` -- and that file names only ``root``, ``sqlalchemy`` and ``alembic``. So the
    default silences every module logger in this repo for the remainder of the process.

    MEASURED 2026-08-06, before the fix: `logging.getLogger("lakebase_branch")` came back with
    ``disabled = True`` after one `fileConfig("alembic.ini")` call. The symptom was
    `tests/unit/test_lakebase_branch.py` passing alone and failing under the full `make test`,
    because `tests/registry` migrates first -- an order-dependent failure whose evidence (an
    empty `caplog`) points nowhere near alembic.

    Nothing is at risk in a deploy today, because `make migrate` runs alembic in its own process.
    The hazard is any future in-process caller, and the loudest casualty would be the WARN line
    that is the readiness prober's only notification mechanism -- see
    `packages/shellbox-app/src/shellbox_app/ready.py`.

    Asserted on the SOURCE rather than by calling `fileConfig` here, deliberately: calling it
    would reconfigure this test session's own logging, which is the very pollution being guarded
    against. A static assertion cannot be the thing it is testing.
    """
    source = (
        REPO_ROOT
        / "packages/shellbox-registry/src/shellbox_registry/alembic/env.py"
    ).read_text(encoding="utf-8")

    call = re.search(r"fileConfig\((.*?)\)", source, re.DOTALL)
    assert call is not None, "env.py no longer calls fileConfig at all"
    assert "disable_existing_loggers=False" in call.group(1), (
        "env.py calls fileConfig without disable_existing_loggers=False. The default is True, "
        "which disables every logger not named in alembic.ini -- see this test's docstring for "
        "the measurement."
    )
