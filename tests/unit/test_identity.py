"""`identity.py` — ADR-6's ladder, and the race that makes it correct.

The headline test here is `test_concurrent_first_boot_yields_exactly_one_host_id`. An earlier
revision of the plan proposed asserting only that "two resolutions agree", which is satisfied
by sequential agreement and would have passed while the `os.replace` bug shipped. The bug is
only visible under genuine concurrency, so that is what this asserts -- with real processes,
since the invariant is about 1-32 concurrent MCP *processes*.
"""

from __future__ import annotations

import json
import os
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import pytest
from shellbox_mcp.identity import (
    HOST_JSON_NAME,
    KIND_LAKEBOX,
    KIND_UNKNOWN,
    IdentityError,
    lakebox_kind,
    resolve_host_id,
    resolve_owner_email,
)


# Module-level so ProcessPoolExecutor can pickle it: a local closure cannot be sent to a
# child process, and the whole point of this test is that the workers are real processes.
def _resolve_in_child(state_dir: str) -> tuple[str, bool]:
    identity = resolve_host_id(state_dir)
    return identity.host_id, identity.assigned


# --------------------------------------------------------------------------- the race
def test_concurrent_first_boot_yields_exactly_one_host_id(tmp_path: Path) -> None:
    """N processes, one empty state dir, exactly one identity and exactly one assigner.

    With `tmp` + `os.replace` this fails: every worker mints its own uuid4 and the ones that
    lose the write still *return* their own, so a sandbox ends up split across N `hosts` rows
    with session rows for one shared tmux server filed under different hosts.
    """
    workers = 16
    with ProcessPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(_resolve_in_child, [str(tmp_path)] * workers))

    host_ids = {host_id for host_id, _ in results}
    assert len(host_ids) == 1, (
        f"{len(host_ids)} distinct host_ids across {workers} concurrent processes: {host_ids}. "
        "Every process must adopt one identity -- see identity.py's module docstring for what "
        "N identities do to session_id and the hosts table."
    )

    assigners = [host_id for host_id, assigned in results if assigned]
    assert len(assigners) == 1, (
        f"{len(assigners)} processes claimed to have ASSIGNED the id; exactly one may win the "
        "exclusive create and the rest must report adoption"
    )

    # The surviving file must hold the id everyone returned -- not merely *an* id.
    cached = json.loads((tmp_path / HOST_JSON_NAME).read_text())
    assert cached["host_id"] == host_ids.pop()


def test_cache_is_created_0600(tmp_path: Path) -> None:
    """It names the host whose owner_email is a workspace admin. Never world-readable, and
    never briefly so -- the mode is set at open(), not by a later chmod."""
    resolve_host_id(str(tmp_path))
    assert oct(os.stat(tmp_path / HOST_JSON_NAME).st_mode & 0o777) == "0o600"


# ------------------------------------------------------------------- the ladder's steps
def test_explicit_env_wins_and_is_not_persisted(tmp_path: Path) -> None:
    """Step 1 is an override for tests and non-Lakebox hosts. Persisting it would silently
    make a one-off run permanent."""
    identity = resolve_host_id(str(tmp_path), explicit="host-under-test")
    assert (identity.host_id, identity.source, identity.assigned) == (
        "host-under-test",
        "env",
        False,
    )
    assert not (tmp_path / HOST_JSON_NAME).exists()


def test_second_resolution_reads_the_cache(tmp_path: Path) -> None:
    first = resolve_host_id(str(tmp_path))
    second = resolve_host_id(str(tmp_path))
    assert second.host_id == first.host_id
    assert (first.source, second.source) == ("assigned", "cache")
    assert (first.assigned, second.assigned) == (True, False)


def test_deleting_the_cache_assigns_a_different_id(tmp_path: Path) -> None:
    """ADR-6's honest behaviour, asserted rather than hidden.

    This is the documented footgun: the id is *remembered*, not derived, so losing the file
    loses the identity. It is asserted so that a future change which quietly made `host_id`
    reproducible-from-the-environment would fail here and have to argue for itself -- such a
    change is exactly what `/etc/machine-id` looked like, and it collides fleet-wide.
    """
    first = resolve_host_id(str(tmp_path)).host_id
    (tmp_path / HOST_JSON_NAME).unlink()
    assert resolve_host_id(str(tmp_path)).host_id != first


def test_no_resolution_path_can_produce_an_unknown_prefixed_id(tmp_path: Path) -> None:
    """The deleted fallback must stay deleted.

    `unknown:<machine-id>` did not degrade to a distinct-but-unhelpful id: `/etc/machine-id`
    is baked into the sandbox image, so every host in the fleet would have shared one `hosts`
    row. Asserted behaviourally -- with the environment offering nothing -- rather than by
    grepping for the string.
    """
    identity = resolve_host_id(str(tmp_path))
    assert not identity.host_id.startswith(("unknown:", "lakebox:"))
    assert ":" not in identity.host_id, "a colon would make '<host_id>:<tmux_name>' ambiguous"


# ------------------------------------------------------- recovery from the tmux stamp
def test_recovered_id_is_adopted_when_the_cache_is_gone(tmp_path: Path) -> None:
    """The case that makes the footgun survivable: cache deleted, sessions still live.

    tmux is the session authority (ADR-5), so its stamp beats assigning a new id -- a new id
    would re-key every `session_id` and strand running sessions as unaddressable.
    """
    identity = resolve_host_id(str(tmp_path), recovered="from-tmux")
    assert (identity.host_id, identity.source, identity.assigned) == ("from-tmux", "tmux", False)
    # And it is re-cached, so the recovery happens once rather than on every call.
    assert json.loads((tmp_path / HOST_JSON_NAME).read_text())["host_id"] == "from-tmux"


def test_recovered_id_overrides_a_disagreeing_cache(tmp_path: Path) -> None:
    """A cache describing a different host than the running tmux server is the fork itself."""
    resolve_host_id(str(tmp_path))  # seed a cache
    identity = resolve_host_id(str(tmp_path), recovered="live-server")
    assert identity.host_id == "live-server"
    assert json.loads((tmp_path / HOST_JSON_NAME).read_text())["host_id"] == "live-server"


def test_recovered_id_is_ignored_when_it_agrees(tmp_path: Path) -> None:
    first = resolve_host_id(str(tmp_path)).host_id
    identity = resolve_host_id(str(tmp_path), recovered=first)
    assert (identity.host_id, identity.source) == (first, "cache")


# ------------------------------------------------------------------- shape checking
@pytest.mark.parametrize(
    ("body", "why"),
    [
        ("not json at all", "unparseable"),
        ("[]", "not an object"),
        ('{"version": 1}', "no host_id"),
        ('{"host_id": null}', "null host_id"),
        ('{"host_id": ""}', "empty host_id"),
        ('{"host_id": "   "}', "whitespace-only host_id"),
        ('{"host_id": 17}', "non-string host_id"),
        ('{"host_id": "lakebox:abc"}', "colon would make session ids ambiguous"),
    ],
)
def test_corrupt_cache_is_quarantined_not_trusted_and_not_destroyed(
    tmp_path: Path, body: str, why: str
) -> None:
    """A file that parses but holds the wrong shape is likelier than a missing one, and a bad
    `host_id` propagates into every `session_id` before anything notices.

    Corruption is **non-empty** content, which no amount of retrying fixes — so a fresh id is
    assigned rather than the server refusing to serve shells over an inventory problem. What
    makes that safe is that nothing is silent and nothing is destroyed.
    """
    path = tmp_path / HOST_JSON_NAME
    path.write_text(body)
    identity = resolve_host_id(str(tmp_path))

    assert identity.source == "assigned", f"a cache that is {why} must not be adopted"
    assert ":" not in identity.host_id
    # Preserved, because it is the only evidence of what this host's identity used to be and
    # its sessions may still be running.
    quarantined = list(tmp_path.glob(f"{HOST_JSON_NAME}.corrupt.*"))
    assert len(quarantined) == 1, f"expected exactly one quarantined file, got {quarantined}"
    assert quarantined[0].read_text() == body
    # And the replacement is a usable cache, so this happens once rather than every start.
    assert resolve_host_id(str(tmp_path)).source == "cache"


def test_corrupt_cache_reassignment_is_logged_at_error(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Re-keying every session on a host is never allowed to be quiet."""
    (tmp_path / HOST_JSON_NAME).write_text('{"host_id": 17}')
    with caplog.at_level("ERROR"):
        resolve_host_id(str(tmp_path))
    assert any(r.levelname == "ERROR" and "re-keyed" in r.message for r in caplog.records), (
        f"expected an ERROR naming the consequence; got {[r.message for r in caplog.records]}"
    )


def test_empty_cache_raises_because_it_looks_like_a_concurrent_writer(tmp_path: Path) -> None:
    """An EMPTY file is not corruption — it is what the exclusive-create winner looks like
    between `os.open` and `json.dump`.

    So this case is retryable and must not be quarantined: quarantining it would move the
    winner's file out from under it and let this process assign a second identity, which is
    the very split the exclusive create exists to prevent. Raising lets the caller retry.
    """
    path = tmp_path / HOST_JSON_NAME
    path.write_text("")
    with pytest.raises(IdentityError, match="not readable as an identity"):
        resolve_host_id(str(tmp_path))
    # Untouched: no quarantine, and the (possibly mid-write) file is still where it was.
    assert path.exists()
    assert not list(tmp_path.glob(f"{HOST_JSON_NAME}.corrupt.*"))


# --------------------------------------------------------------- sandbox_id / kind
def test_sandbox_id_is_persisted_and_survives_a_call_without_one(tmp_path: Path) -> None:
    """ADR-8 injects it at bootstrap; a later `serve` that is not told it should not lose it."""
    resolve_host_id(str(tmp_path), sandbox_id="realistic-phoenix-2742")
    assert resolve_host_id(str(tmp_path)).sandbox_id == "realistic-phoenix-2742"


def test_a_fresh_sandbox_id_wins_over_the_cached_one(tmp_path: Path) -> None:
    """The injector knows; the cache only remembers."""
    resolve_host_id(str(tmp_path), sandbox_id="old-name")
    assert resolve_host_id(str(tmp_path), sandbox_id="new-name").sandbox_id == "new-name"


def test_absent_sandbox_id_is_omitted_not_nulled(tmp_path: Path) -> None:
    """So a reader cannot mistake "never bootstrapped" for "bootstrapped, and it is null"."""
    resolve_host_id(str(tmp_path))
    assert "sandbox_id" not in json.loads((tmp_path / HOST_JSON_NAME).read_text())


def test_lakebox_kind_is_a_positive_test_with_an_honest_negative() -> None:
    assert lakebox_kind(pid1_cmdline="/usr/bin/sandbox-daemon --enable-sshd --uid 10086") == (
        KIND_LAKEBOX
    )
    # On a developer laptop neither marker holds. "unknown" is a true statement, and unlike
    # the old `unknown:<machine-id>` host id it is a label that collides with nothing.
    if not os.path.exists("/etc/lakebox"):
        assert lakebox_kind(pid1_cmdline="/sbin/init") == KIND_UNKNOWN


# ------------------------------------------------------------------------ E2 ladder
def test_credential_wins_and_corrects_a_stale_cache(tmp_path: Path) -> None:
    """E2a. The inherited plan read the cache first and stopped, so a stale owner_email won
    forever with no TTL -- a sandbox that changed hands kept crediting the previous owner."""
    resolve_host_id(str(tmp_path))
    resolve_owner_email(str(tmp_path), credential_email="first@example.com")

    result = resolve_owner_email(str(tmp_path), credential_email="second@example.com")
    assert (result.owner_email, result.source, result.reconciled) == (
        "second@example.com",
        "credential",
        True,
    )
    # Corrected on disk, not just returned.
    assert (
        resolve_owner_email(str(tmp_path), credential_email=None).owner_email
        == "second@example.com"
    )


def test_cache_serves_when_no_credential_is_available(tmp_path: Path) -> None:
    """E2b -- the post-PAT-reset path. Weaker than the inherited plan assumed: the CLI's OAuth
    token cache is boot-templated into wiped /run, so after a restart this cache is the ONLY
    source. Hence it is written before the hosts row and never expired."""
    resolve_host_id(str(tmp_path))
    resolve_owner_email(str(tmp_path), credential_email="owner@example.com")
    result = resolve_owner_email(str(tmp_path), credential_email=None)
    assert (result.owner_email, result.source) == ("owner@example.com", "cache")


def test_env_is_the_last_resort_before_deferring(tmp_path: Path) -> None:
    """E2c."""
    result = resolve_owner_email(
        str(tmp_path), credential_email=None, env_email="escape@example.com"
    )
    assert (result.owner_email, result.source) == ("escape@example.com", "env")


def test_nothing_available_defers_rather_than_failing(tmp_path: Path) -> None:
    """E2d. A shell an agent cannot get is worse than an inventory row nobody reads, so the
    tool surface must keep working with no identity at all."""
    result = resolve_owner_email(str(tmp_path), credential_email=None)
    assert (result.owner_email, result.source) == (None, "deferred")


def test_caching_an_owner_never_clobbers_the_host_id(tmp_path: Path) -> None:
    """`host_id` is the irreplaceable field in this file; `owner_email` costs one API call."""
    host_id = resolve_host_id(str(tmp_path)).host_id
    resolve_owner_email(str(tmp_path), credential_email="owner@example.com")
    assert resolve_host_id(str(tmp_path)).host_id == host_id


def test_owner_is_not_cached_when_it_would_risk_the_host_id(tmp_path: Path) -> None:
    """No `host_id` in the file means a rewrite could invent one. Skip the merge instead."""
    path = tmp_path / HOST_JSON_NAME
    path.write_text('{"version": 1}')
    result = resolve_owner_email(str(tmp_path), credential_email="owner@example.com")
    assert result.owner_email == "owner@example.com", "the caller still gets the resolved value"
    assert "owner_email" not in json.loads(path.read_text())
