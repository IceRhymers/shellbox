"""The guard that keeps a deploy-time database action off the App's service principal.

``scripts/check_deploy_principal.py`` runs before ``alembic upgrade head`` and before the grant. It
refuses a credential whose Postgres user is a service principal role, because both of those actions
must run as the DEPLOYING PRINCIPAL. The App SP holds ``SELECT`` on two tables; a migration with its
credential fails on a permission error, and the tempting fix for that error is a wider grant.

Two properties are asserted here, and the second is the one that makes the first worth having:

1. The guard refuses a service principal and accepts a human. Both directions, because a guard that
   refuses everything would be discovered by whoever it blocks, and then routed around.
2. The variables it reads are the variables ``dsn_from_env`` reads. The donor project's version of
   this guard named ``PGHOST``, which nothing in this repo consumes -- a check that cannot fail. The
   last case in this file imports the real function and asserts the coupling, so the same mistake
   cannot be made here silently.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest
from shellbox_registry.dsn import dsn_from_env

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_deploy_principal.py"

_spec = importlib.util.spec_from_file_location("check_deploy_principal", _SCRIPT)
assert _spec is not None and _spec.loader is not None, f"cannot load {_SCRIPT}"
_guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_guard)

# Measured 2026-08-03 from `databricks apps get shellbox -o json`, field
# `service_principal_client_id`. The Postgres role name of a service principal IS its client id --
# `databricks postgres create-role --help`, CLI v1.8.0.
SP_ROLE = "3337afac-b67b-41af-8996-828620bcc4a8"
HUMAN = "tanner.wendland@databricks.com"

_PG_VARIABLES = (
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

    A developer running this suite legitimately has some of them exported, and half of these cases
    would then assert against their laptop's credential rather than the fixture's.
    """
    for name in _PG_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


@pytest.mark.parametrize(
    "user",
    [
        SP_ROLE,
        SP_ROLE.upper(),
    ],
)
def test_a_service_principal_role_is_refused(clean_env: pytest.MonkeyPatch, user: str) -> None:
    clean_env.setenv("SHELLBOX_PG_USER", user)
    assert _guard.main(["make migrate"]) == 1


@pytest.mark.parametrize(
    "user",
    [
        HUMAN,
        # A local development role. `make migrate` against a local Postgres on purpose stays
        # possible, which is the same line the host guard in the Makefile draws.
        "shellbox",
        "postgres",
        # 32 hex characters with no dashes. `uuid.UUID` would accept this, and this guard
        # deliberately does not: refusing a real migration is the expensive direction of a wrong
        # answer, and the CLI emits the dashed form.
        "3337afacb67b41af8996828620bcc4a8",
    ],
)
def test_a_human_or_a_local_role_is_accepted(clean_env: pytest.MonkeyPatch, user: str) -> None:
    clean_env.setenv("SHELLBOX_PG_USER", user)
    assert _guard.main(["make migrate"]) == 0


def test_no_configured_credential_is_not_this_guards_failure(clean_env: pytest.MonkeyPatch) -> None:
    """``require-pg-host`` in the Makefile owns the unset case, and says what to do about it.
    Reporting the same condition twice in two wordings makes one problem look like two."""
    assert _guard.main(["make migrate"]) == 0


def test_a_database_url_carrying_a_service_principal_is_refused(
    clean_env: pytest.MonkeyPatch,
) -> None:
    """The URL form has to be parsed, not just the parts form. ``SHELLBOX_DATABASE_URL`` is the
    variable a CI job is most likely to set, and it is the one that hides the user inside a
    string."""
    clean_env.setenv(
        "SHELLBOX_DATABASE_URL",
        f"postgresql://{SP_ROLE}:token@ep-x.database.cloud.databricks.com:5432/shellbox",
    )
    assert _guard.main(["the grant"]) == 1


def test_a_percent_encoded_human_user_is_accepted(clean_env: pytest.MonkeyPatch) -> None:
    """An email contains an ``@``, so ``dsn_from_env`` percent-encodes it. A guard that skipped the
    decode would compare ``tanner.wendland%40databricks.com`` and reach the right verdict here for
    the wrong reason -- so the case exists to pin the decode, and the next one is where it bites."""
    encoded = HUMAN.replace("@", "%40")
    clean_env.setenv(
        "SHELLBOX_DATABASE_URL",
        f"postgresql://{encoded}:token@ep-x.database.cloud.databricks.com:5432/shellbox",
    )
    assert _guard.main(["make migrate"]) == 0
    assert (
        _guard.configured_user({"SHELLBOX_DATABASE_URL": f"postgresql://{encoded}:t@h:5432/d"})
        == HUMAN
    )


def test_the_database_url_wins_over_the_parts(clean_env: pytest.MonkeyPatch) -> None:
    """The precedence matches ``dsn_from_env``. Reversed, the guard would inspect a user the
    connection does not use -- and a half-configured environment is exactly where that happens."""
    clean_env.setenv("SHELLBOX_PG_USER", HUMAN)
    clean_env.setenv(
        "SHELLBOX_DATABASE_URL",
        f"postgresql://{SP_ROLE}:token@ep-x.database.cloud.databricks.com:5432/shellbox",
    )
    assert _guard.main(["make migrate"]) == 1


@pytest.mark.parametrize(
    "user",
    [
        SP_ROLE,
        SP_ROLE.upper(),
        HUMAN,
        HUMAN.replace("@", "%40"),
        "shellbox",
        "postgres",
        "3337afacb67b41af8996828620bcc4a8",
        "",
        "not-a-uuid-at-all",
        "3337afac-b67b-41af-8996-828620bcc4a",
        "3337afac-b67b-41af-8996-828620bcc4a8-extra",
        "zzzzzzzz-b67b-41af-8996-828620bcc4a8",
    ],
)
def test_the_two_copies_of_the_role_name_rule_agree(user: str) -> None:
    """CRITICAL: the same rule exists twice, and a test is what keeps it one rule.

    This script must run under a bare ``python3`` on a checkout with nothing synced, so it
    cannot import ``shellbox_registry``. The alembic environment has the opposite constraint:
    it refuses a DERIVED role, which this script never sees, because on the endpoint path
    nobody exports a user for it to inspect. So the predicate is written twice --
    ``is_service_principal_role`` here and in
    ``packages/shellbox-registry/src/shellbox_registry/lakebase.py``.

    Copies drift. This case is what makes the drift a failing test instead of a hole: a role
    the package refuses and the script accepts, or the reverse, means one deploy-time action is
    guarded and the other is not.
    """
    from shellbox_registry.lakebase import is_service_principal_role as package_rule

    assert _guard.is_service_principal_role(user) == package_rule(user), (
        f"the two copies of the role-name rule disagree about {user!r}"
    )


def test_the_role_name_rule_is_not_vacuously_true_for_everything() -> None:
    """A predicate that answered the same thing for every input would pass the case above.

    Both directions must be witnessed, on both copies.
    """
    from shellbox_registry.lakebase import is_service_principal_role as package_rule

    for rule in (_guard.is_service_principal_role, package_rule):
        assert rule(SP_ROLE) is True
        assert rule(HUMAN) is False


@pytest.mark.parametrize("user", [HUMAN, SP_ROLE])
def test_the_guard_reads_the_user_the_dsn_actually_connects_as(
    clean_env: pytest.MonkeyPatch, user: str
) -> None:
    """The coupling test, and the reason this guard is not the donor's ``PGHOST``.

    It asserts against ``dsn_from_env`` itself: the user the guard inspects is the user the
    assembled DSN carries. If that function stops reading ``SHELLBOX_PG_USER``, or changes how it
    encodes the userinfo, this fails rather than the guard quietly checking a name nothing uses.
    """
    clean_env.setenv("SHELLBOX_PG_USER", user)
    clean_env.setenv("SHELLBOX_PG_HOST", "ep-x.database.cloud.databricks.com")

    dsn = dsn_from_env()
    assert dsn is not None
    from_dsn = urlsplit(dsn).username
    assert from_dsn is not None

    assert _guard.configured_user() == unquote(from_dsn)
