"""`scripts/lakebase_branch.py` -- the disposable-branch lifecycle, with the SDK faked.

Adapted from `databricks-code-search`'s `tests/unit/test_ci_branch.py`, which pinned the two
properties that matter most and are easiest to lose:

* **teardown must NEVER raise** -- a purge failure would otherwise replace the test failure that
  sent you to the log,
* **the branch must be created with an expiry** -- the API requires one, and the TTL is the only
  thing that reclaims a branch when the process is killed between ``up`` and ``down``.

Two properties are shellbox's own and are asserted here for the first time: the refusal to touch
the ``production`` branch (`R42`, and the donor needs no such guard because its CI project is
disposable), and the narrow ``SHELLBOX_THROWAWAY_PG_HOST`` that authorises the destructive
fixtures against exactly one host.

No workspace is touched. Nothing here needs a credential.
"""

from __future__ import annotations

import importlib.util
import logging
from pathlib import Path
from typing import Any

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "lakebase_branch.py"

_spec = importlib.util.spec_from_file_location("lakebase_branch", _SCRIPT)
assert _spec is not None and _spec.loader is not None, f"cannot load {_SCRIPT}"
branch_script = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(branch_script)

PROJECT = "shellbox-pg-dev"
BRANCH = "w37b"
TARGET = f"projects/{PROJECT}/branches/{BRANCH}"
HOST = "ep-test-fork-abc123.database.us-west-2.cloud.databricks.com"


class _Op:
    """An SDK long-running operation. Only ``wait()`` is ever called."""

    def wait(self) -> None:
        return None


class _Endpoint:
    def __init__(self, name: str, endpoint_type: str, host: str | None = None) -> None:
        self.name = name
        self.status = type(
            "_Status",
            (),
            {
                "endpoint_type": endpoint_type,
                "hosts": type("_Hosts", (), {"host": host})() if host else None,
            },
        )()


class _FakePostgres:
    def __init__(
        self,
        endpoints: list[_Endpoint],
        *,
        delete_raises: bool = False,
        hosts: list[str | None] | None = None,
    ) -> None:
        self._endpoints = endpoints
        self._delete_raises = delete_raises
        # Successive `get_endpoint` answers, so a host that appears only on the second poll can
        # be exercised without waiting.
        self._hosts = hosts if hosts is not None else [HOST]
        self.created: dict[str, Any] = {}
        self.deleted: list[tuple[str, bool]] = []
        self.get_calls = 0

    def create_branch(self, *, parent: str, branch_id: str, branch: Any, **kw: Any) -> _Op:
        self.created = {"parent": parent, "branch_id": branch_id, "branch": branch, **kw}
        return _Op()

    def list_endpoints(self, parent: str) -> list[_Endpoint]:
        return self._endpoints

    def get_endpoint(self, name: str) -> _Endpoint:
        host = self._hosts[min(self.get_calls, len(self._hosts) - 1)]
        self.get_calls += 1
        return _Endpoint(name, "EndpointType.ENDPOINT_TYPE_READ_WRITE", host)

    def delete_branch(self, name: str, *, purge: bool = False) -> _Op:
        if self._delete_raises:
            raise RuntimeError("transient API failure")
        self.deleted.append((name, purge))
        return _Op()


class _FakeClient:
    def __init__(self, postgres: _FakePostgres) -> None:
        self.postgres = postgres


def _fake(monkeypatch: pytest.MonkeyPatch, api: _FakePostgres) -> _FakePostgres:
    """Fake the SDK, and drive the poll on a FAKE clock rather than a free `sleep`.

    Both halves are needed together. Patching only the sleep leaves `_now` real, so the poll
    stops waiting but keeps looping until the real deadline -- MEASURED at 60 seconds of hot
    loop, which is how this helper came to exist. Advancing a counter instead makes the timeout
    a number of iterations, which is what the assertions actually care about.
    """
    monkeypatch.setattr(branch_script, "_client", lambda: _FakeClient(api))
    ticks = iter(range(0, 10_000))
    monkeypatch.setattr(branch_script, "_now", lambda: float(next(ticks)))
    monkeypatch.setattr(branch_script, "_sleep", lambda _seconds: None)

    # The log assertion below depends on process-global logging state, so it is asserted rather
    # than assumed. MEASURED 2026-08-06: `tests/registry` migrates, alembic's `env.py` calls
    # `fileConfig`, and its `disable_existing_loggers` default set ``disabled = True`` on this
    # module's logger -- so `test_down_never_raises...` passed alone and failed in the full
    # `make test` run, with a symptom (an empty `caplog`) pointing nowhere near the cause.
    #
    # `env.py` now passes `disable_existing_loggers=False`, so this is a GUARD and not a repair:
    # it converts a silent order-dependent failure into a named one. `monkeypatch` restores the
    # attribute afterwards, so this cannot leak into another test.
    monkeypatch.setattr(branch_script.logger, "disabled", False)
    assert branch_script.logger.isEnabledFor(logging.WARNING), (
        "something earlier in this session disabled or raised the level of the "
        "'lakebase_branch' logger, so the log assertions below would silently see nothing. "
        "The known cause is alembic's fileConfig -- see this repo's alembic env.py."
    )
    return api


def _primary(host: str | None = HOST) -> _Endpoint:
    return _Endpoint(f"{TARGET}/endpoints/primary", "EndpointType.ENDPOINT_TYPE_READ_WRITE", host)


def _read_only() -> _Endpoint:
    return _Endpoint(f"{TARGET}/endpoints/ro", "EndpointType.ENDPOINT_TYPE_READ_ONLY", HOST)


# ------------------------------------------------------------------------ paths and refusals


def test_the_branch_path_is_the_qualified_resource_path() -> None:
    assert branch_script.branch_path(PROJECT, BRANCH) == TARGET


@pytest.mark.parametrize("action", ["up", "down"])
def test_the_production_branch_is_refused_by_both_actions(
    action: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`R42`. Forking ONTO production would replace it -- the call passes
    ``replace_existing=True`` -- and purging it would delete the registry the App reads.

    Asserted for BOTH actions because they are different hazards reached through the same
    argument, and a guard on only one of them reads as complete.
    """
    api = _fake(monkeypatch, _FakePostgres([_primary()]))
    with pytest.raises(ValueError, match="refusing to operate"):
        getattr(branch_script, action)(PROJECT, "production")

    # Nothing reached the API. A refusal that still called `create_branch` or `delete_branch`
    # would be a log line rather than a guard.
    assert api.created == {}
    assert api.deleted == []


def test_the_refusal_exits_nonzero_without_a_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A caller error, so the message is the output -- and stdout stays empty, because it is
    `eval`ed. A traceback on stdout would be evaluated by the calling shell."""
    _fake(monkeypatch, _FakePostgres([_primary()]))
    monkeypatch.setattr(
        "sys.argv",
        ["lakebase_branch.py", "up", "--project", PROJECT, "--branch", "production"],
    )
    assert branch_script.main() == 2
    assert capsys.readouterr().out == ""


# --------------------------------------------------------------------------------------- up


def test_up_forks_production_with_a_ttl_and_returns_the_read_write_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _fake(monkeypatch, _FakePostgres([_read_only(), _primary()]))

    env = branch_script.up(PROJECT, BRANCH, ttl_seconds=60)

    assert env["SHELLBOX_PG_RESOURCE"] == f"{TARGET}/endpoints/primary"
    assert env["SHELLBOX_PG_DB"] == "databricks_postgres"

    spec = api.created["branch"].spec
    assert spec.source_branch == f"projects/{PROJECT}/branches/production"
    assert api.created["parent"] == f"projects/{PROJECT}"
    assert api.created["branch_id"] == BRANCH
    # A re-run with the same name must replace its own branch, not collide.
    assert api.created["replace_existing"] is True


def test_the_ttl_is_a_proto_duration_and_not_a_string(monkeypatch: pytest.MonkeyPatch) -> None:
    """The API REQUIRES an expiry, and a string ``"60s"`` raises AttributeError inside the SDK.

    Asserting ``.seconds`` is what distinguishes a Duration from a string that happens to be
    accepted by the fake: a `str` has no such attribute.
    """
    api = _fake(monkeypatch, _FakePostgres([_primary()]))
    branch_script.up(PROJECT, BRANCH, ttl_seconds=60)
    assert api.created["branch"].spec.ttl.seconds == 60


def test_up_emits_the_narrow_throwaway_host_and_no_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The permission slip `tests/registry/conftest.py` accepts, scoped to one host.

    The second half is the one worth keeping: **nothing emitted here is a credential.** The
    endpoint's role is resolved from the workspace and its token is minted per connect, so a
    caller who `eval`s this output puts no password in their shell.
    """
    _fake(monkeypatch, _FakePostgres([_primary()]))

    env = branch_script.up(PROJECT, BRANCH, ttl_seconds=60)

    assert env["SHELLBOX_THROWAWAY_PG_HOST"] == HOST
    assert set(env) == {
        "SHELLBOX_PG_RESOURCE",
        "SHELLBOX_PG_DB",
        "SHELLBOX_THROWAWAY_PG_HOST",
    }
    for key in env:
        assert "PASSWORD" not in key and "TOKEN" not in key and "SECRET" not in key


def test_up_selects_by_endpoint_type_and_never_by_position(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A read-only endpoint listed first must not be chosen. Picked by position, a migration or
    a destructive suite fails partway with a permission error instead of refusing up front."""
    _fake(monkeypatch, _FakePostgres([_read_only(), _read_only(), _primary()]))
    env = branch_script.up(PROJECT, BRANCH, ttl_seconds=60)
    assert env["SHELLBOX_PG_RESOURCE"].endswith("/primary")


def test_up_raises_when_the_branch_reports_no_read_write_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake(monkeypatch, _FakePostgres([_read_only()]))
    with pytest.raises(RuntimeError, match="no READ_WRITE endpoint"):
        branch_script.up(PROJECT, BRANCH, ttl_seconds=60)


def test_up_waits_for_a_host_that_is_not_reported_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fresh branch can report its endpoint before that endpoint reports a host.

    Read once, `up` would return an empty host -- which would then be written into
    `SHELLBOX_THROWAWAY_PG_HOST`, where an empty value must never be treated as authorisation.
    """
    api = _fake(monkeypatch, _FakePostgres([_primary()], hosts=[None, None, HOST]))
    env = branch_script.up(PROJECT, BRANCH, ttl_seconds=60)
    assert env["SHELLBOX_THROWAWAY_PG_HOST"] == HOST
    assert api.get_calls == 3


def test_up_gives_up_on_a_host_that_never_appears(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake(monkeypatch, _FakePostgres([_primary()], hosts=[None]))
    with pytest.raises(RuntimeError, match="reported no host"):
        branch_script.up(PROJECT, BRANCH, ttl_seconds=60)


def test_up_writes_shell_exports_on_stdout(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`eval`-able, and every log line goes to stderr so it cannot be evaluated."""
    _fake(monkeypatch, _FakePostgres([_primary()]))
    monkeypatch.setattr(
        "sys.argv", ["lakebase_branch.py", "up", "--project", PROJECT, "--branch", BRANCH]
    )
    assert branch_script.main() == 0

    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 3
    for line in lines:
        assert line.startswith("export SHELLBOX_")
        assert "=" in line


# ------------------------------------------------------------------------------------- down


def test_down_purges_the_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    """``purge=True``, not a soft delete. A recoverable branch keeps costing."""
    api = _fake(monkeypatch, _FakePostgres([]))
    branch_script.down(PROJECT, BRANCH)
    assert api.deleted == [(TARGET, True)]


def test_down_never_raises_so_teardown_cannot_mask_a_real_failure(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The donor's rule. The TTL still reclaims the branch, so a warning is the right severity
    and an exception here would replace the failure a reader actually came for."""
    _fake(monkeypatch, _FakePostgres([], delete_raises=True))
    with caplog.at_level(logging.WARNING):
        branch_script.down(PROJECT, BRANCH)  # must not raise
    assert "ttl will reclaim it" in caplog.text


# ----------------------------------------------------------------- agreement with the package


def test_the_default_database_matches_the_registry_packages() -> None:
    """The constant is restated in the script so it runs with only the SDK on the path. This is
    what stops the two copies drifting."""
    from shellbox_registry.lakebase import DEFAULT_DATABASE

    assert branch_script.DEFAULT_DATABASE == DEFAULT_DATABASE


def test_the_source_branch_matches_the_bundles_declared_branch() -> None:
    """`up` forks whatever `databricks.yml` calls the project's branch. Declared in two files, so
    a change to the bundle that left this behind would fork a branch that does not exist."""
    import re

    bundle = (Path(__file__).resolve().parents[2] / "databricks.yml").read_text(encoding="utf-8")
    match = re.search(r"pg_branch_id:.*?default:\s*(\S+)", bundle, re.DOTALL)
    assert match is not None, "databricks.yml no longer declares a default pg_branch_id"
    assert branch_script.SOURCE_BRANCH == match.group(1)


def test_the_protected_set_contains_the_source_branch() -> None:
    """Forking production onto itself is the specific accident `PROTECTED_BRANCHES` prevents."""
    assert branch_script.SOURCE_BRANCH in branch_script.PROTECTED_BRANCHES