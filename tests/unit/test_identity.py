"""`identity.py` — ADR-6's ladder, and the race that makes it correct.

The headline test is `test_concurrent_first_boot_yields_exactly_one_host_id`, and it took two
attempts to make it test anything.

⚠️ **Do not "simplify" it back.** The first version spawned N workers and asserted they agreed
on one `host_id`. That assertion is satisfiable **without the code under test ever running**:
absent a barrier the OS serializes process startup, so worker 1 creates the cache and workers
2..N take the ordinary step-2 *cache-hit* path. All ids agree, the suite is green, and the
`O_EXCL`-and-adopt branch is never executed. A test for a race that never happens tests
nothing. Hence: a barrier releasing all N at the same instant, and an explicit assertion that
at least one process really did observe `EEXIST`.

That is not a hypothetical. Making the race real immediately exposed a bug in `identity.py`
that the serialized version could never have reached — see
`test_the_losers_wait_out_the_winners_write` below.
"""

from __future__ import annotations

import json
import multiprocessing
import os
import threading
import time
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

# The top of #2's stated range ("1-32 agents", an invariant it calls mandatory), because the
# whole point is to race the real concurrency ceiling rather than a token two processes.
CONCURRENT_WORKERS = 32
# Repeated, because losing a race is timing-dependent: one green round is weak evidence.
RACE_ROUNDS = 3


def _resolve_at_barrier(
    state_dir: str, barrier: object, results: object, recovered: str | None = None
) -> None:
    """Wait at the barrier, then resolve. Module-level so it survives `spawn` pickling.

    Exceptions are *recorded*, never raised: a child that dies takes its result with it, and
    "31 processes raised IdentityError" is precisely the failure this test exists to catch, so
    it has to arrive as data rather than as a lost traceback.
    """
    barrier_ok = True
    try:
        barrier.wait(timeout=30)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - a broken barrier must not mask the identity result
        # Recorded, not swallowed: a failed barrier degrades the round to the serialized
        # startup this test exists to defeat, and a silently degraded round looks green.
        barrier_ok = False
    try:
        identity = resolve_host_id(state_dir, recovered=recovered)
        source = identity.source if barrier_ok else f"{identity.source}/barrier-failed"
        results.append((identity.host_id, identity.assigned, source))  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001
        results.append(("<error>", False, f"{type(exc).__name__}: {exc}"))  # type: ignore[attr-defined]


def _cache(state_dir: Path) -> dict[str, object]:
    """The identity cache as written to disk.

    Used deliberately often below: two real bugs in this module were invisible to assertions on
    the *returned* value and visible only in the file.
    """
    parsed = json.loads((state_dir / HOST_JSON_NAME).read_text())
    assert isinstance(parsed, dict)
    return parsed


def _race_once(
    state_dir: Path, *, recovered_for: set[int] | None = None, recovered: str = "from-tmux"
) -> list[tuple[str, bool, str]]:
    """Release `CONCURRENT_WORKERS` real processes into `resolve_host_id` simultaneously.

    Explicit `Process` objects rather than a pool: with a pool, `map` may hand two tasks to one
    worker, and that worker would then wait at a barrier for a party that never arrives.
    """
    with multiprocessing.Manager() as manager:
        barrier = manager.Barrier(CONCURRENT_WORKERS)
        results = manager.list()
        workers = [
            multiprocessing.Process(
                target=_resolve_at_barrier,
                args=(
                    str(state_dir),
                    barrier,
                    results,
                    recovered if recovered_for and index in recovered_for else None,
                ),
            )
            for index in range(CONCURRENT_WORKERS)
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=90)
        assert not any(w.is_alive() for w in workers), "a worker hung on the barrier"
        return list(results)


# --------------------------------------------------------------------------- the race
def test_concurrent_first_boot_yields_exactly_one_host_id(tmp_path: Path) -> None:
    """N processes, one empty state dir: exactly one identity, exactly one assigner.

    With `tmp` + `os.replace` this fails: every worker mints its own uuid4, one write survives,
    and the losers still *return* their own — so one sandbox is split across N `hosts` rows,
    session rows for a single shared tmux server land under different hosts, and each process
    rejects its siblings' live session ids as `invalid_name`.
    """
    adopters_seen = 0
    rounds_run = 0

    # Extra rounds are allowed *only* to observe a genuine EEXIST, never to get a passing
    # assertion: every correctness check below runs on every round. On a 1-2 vCPU runner the
    # winner can finish before the next process is scheduled even after the barrier releases,
    # which yields zero adopters on completely correct code -- so that condition retries rather
    # than failing, and only a total absence across every round is a failure.
    while rounds_run < RACE_ROUNDS or (adopters_seen == 0 and rounds_run < RACE_ROUNDS * 3):
        state_dir = tmp_path / f"round{rounds_run}"
        state_dir.mkdir()
        results = _race_once(state_dir)
        rounds_run += 1

        assert len(results) == CONCURRENT_WORKERS, (
            f"round {rounds_run}: {len(results)} of {CONCURRENT_WORKERS} workers reported"
        )
        errors = [source for host_id, _, source in results if host_id == "<error>"]
        assert not errors, f"round {rounds_run}: workers failed to resolve an identity: {errors}"
        degraded = [source for _, _, source in results if source.endswith("/barrier-failed")]
        assert not degraded, (
            f"round {rounds_run}: {len(degraded)} workers failed to reach the barrier, so this "
            "round did not race at all"
        )

        host_ids = {host_id for host_id, _, _ in results}
        assert len(host_ids) == 1, (
            f"round {rounds_run}: {len(host_ids)} distinct host_ids across "
            f"{CONCURRENT_WORKERS} concurrent processes: {host_ids}. See identity.py's module "
            "docstring for what N identities do to session_id and the hosts table."
        )

        assigners = [host_id for host_id, assigned, _ in results if assigned]
        assert len(assigners) == 1, (
            f"round {rounds_run}: {len(assigners)} processes claim to have ASSIGNED the id; "
            "exactly one may win the exclusive create and the rest must adopt"
        )

        # The surviving file holds the id everyone returned — not merely *an* id.
        assert _cache(state_dir)["host_id"] == next(iter(host_ids))

        # `source == "assigned"` with `assigned is False` is reachable only through the
        # `FileExistsError` handler, so this counts processes that genuinely lost the race.
        # A cache hit reports `source == "cache"` instead.
        adopters_seen += sum(
            1 for _, assigned, source in results if source == "assigned" and not assigned
        )

    assert adopters_seen > 0, (
        f"across {rounds_run} rounds of {CONCURRENT_WORKERS} barrier-released processes, not one "
        "observed EEXIST — every worker took the cache-hit path, so the adopt-the-winner branch "
        "never executed and this test proved nothing. Either process startup is serialized on "
        "this machine despite the barrier, or the barrier is not doing its job."
    )


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
    assert (identity.host_id, identity.source) == ("from-tmux", "tmux")
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


def test_a_winners_content_landing_late_is_adopted_never_duplicated(tmp_path: Path) -> None:
    """The bug the barrier found, kept as a regression guard.

    An empty-then-filled file is what a build using `O_EXCL` on the final path looks like
    mid-write, which is reachable during a rolling upgrade. Whatever route the code takes, the
    one unacceptable outcome is minting a *second* id — so this asserts the winner's value is
    adopted and that the file agrees.
    """
    path = tmp_path / HOST_JSON_NAME
    path.write_text("")  # a writer has opened it and not yet written

    def finish_the_write() -> None:
        time.sleep(0.05)
        path.write_text(json.dumps({"version": 1, "host_id": "the-winners-id"}))

    writer = threading.Thread(target=finish_the_write)
    writer.start()
    try:
        identity = resolve_host_id(str(tmp_path))
    finally:
        writer.join()

    assert identity.host_id == "the-winners-id", (
        "a second id was minted while a writer was mid-write; one sandbox is now two hosts"
    )
    assert _cache(tmp_path)["host_id"] == "the-winners-id"


def test_an_empty_cache_is_replaced_rather_than_bricking_the_host(tmp_path: Path) -> None:
    """An EMPTY file has no `host_id`, so there is nothing to preserve and nothing to strand.

    An earlier version refused to start on it — every process, every boot, forever, until a
    human deleted the file. That protected nothing (no prior identity existed to be re-keyed)
    and cost the sandbox every shell, so `_CacheState.EMPTY`'s "nothing to preserve" is now
    acted on rather than merely documented.
    """
    path = tmp_path / HOST_JSON_NAME
    path.write_text("")

    identity = resolve_host_id(str(tmp_path))

    assert identity.source == "assigned"
    assert _cache(tmp_path)["host_id"] == identity.host_id
    # Not quarantined: there was no content worth keeping.
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
    # Asserted on the FILE. The returned value was correct even when the write was missing —
    # see `test_a_sandbox_id_arriving_after_the_first_boot_is_persisted`.
    assert _cache(tmp_path)["sandbox_id"] == "new-name"


def test_a_sandbox_id_arriving_after_the_first_boot_is_persisted(tmp_path: Path) -> None:
    """🔴 The bug this test exists for: ADR-8 does not work past boot 1 without it.

    The bootstrap path runs **every boot** and is the only actor that knows the sandbox id. On
    every boot after the first, `resolve_host_id` takes the cache-hit branch — which returned
    the injected id to its caller and never wrote it down. Consequences, all silent: the other
    1-31 processes in that boot see `None`, `doctor` reports a bootstrapped host as "never
    bootstrapped", and the `hosts` row loses a column it previously had.

    Invisible to any assertion on the return value, which was correct throughout. Only reading
    the file back catches it.
    """
    # Boot 1: a plain `serve`, before anyone has bootstrapped. No sandbox_id, correctly.
    resolve_host_id(str(tmp_path))
    assert "sandbox_id" not in _cache(tmp_path)

    # Boot 2: bootstrap runs and injects it.
    resolve_host_id(str(tmp_path), sandbox_id="realistic-phoenix-2742", gateway_host="gw.example")

    assert _cache(tmp_path)["sandbox_id"] == "realistic-phoenix-2742"
    assert _cache(tmp_path)["gateway_host"] == "gw.example"
    # And a *different* process in the same boot, which was never told, still knows.
    later = resolve_host_id(str(tmp_path))
    assert (later.sandbox_id, later.gateway_host) == ("realistic-phoenix-2742", "gw.example")


def test_recording_a_sandbox_id_never_disturbs_identity_or_owner(tmp_path: Path) -> None:
    """The merge must preserve what it did not come to write.

    An earlier version rebuilt the payload from its own arguments, so recording one field
    deleted the others — `owner_email` in particular, which the recovery path dropped outright.
    """
    host_id = resolve_host_id(str(tmp_path)).host_id
    resolve_owner_email(str(tmp_path), credential_email="owner@example.com")

    resolve_host_id(str(tmp_path), sandbox_id="sbx-1")

    cached = _cache(tmp_path)
    assert cached["host_id"] == host_id, "recording a property must never re-key the host"
    assert cached["owner_email"] == "owner@example.com", "the merge dropped owner_email"
    assert cached["sandbox_id"] == "sbx-1"


def test_a_merge_preserves_keys_this_module_does_not_model(tmp_path: Path) -> None:
    """Forward compatibility: a newer build's field must survive an older build's write."""
    resolve_host_id(str(tmp_path))
    path = tmp_path / HOST_JSON_NAME
    payload = json.loads(path.read_text())
    payload["some_future_field"] = "keep me"
    path.write_text(json.dumps(payload))

    resolve_owner_email(str(tmp_path), credential_email="owner@example.com")
    assert _cache(tmp_path)["some_future_field"] == "keep me"


def test_a_merge_never_invents_an_identity(tmp_path: Path) -> None:
    """Only `_create_or_adopt` may create a cache, because only it arbitrates the race.

    If a merge could create one, two processes could each "merge" a `host_id` in and split the
    host — the failure the link-based assignment exists to prevent, reintroduced by the back
    door. So an owner write against a missing cache records nothing and says so.
    """
    result = resolve_owner_email(str(tmp_path), credential_email="owner@example.com")
    assert result.owner_email == "owner@example.com", "the caller still gets the resolved value"
    assert not (tmp_path / HOST_JSON_NAME).exists()


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


# ----------------------------------------------------------- the other three races
def test_concurrent_resolution_against_a_corrupt_cache_yields_one_host_id(
    tmp_path: Path,
) -> None:
    """🔴 Regression guard for the worst bug found in this module.

    Quarantine-then-assign is two steps, and nothing arbitrated them. P1 moved the corrupt file
    aside and linked identity U; P2 — still acting on its own earlier read — then "quarantined"
    what was now **P1's valid identity** and linked V. Repeated 32 times, measured: 6 distinct
    host_ids, 5 quarantine files of which 4 held live valid identities, 28 of 32 processes
    running under an id absent from the cache, and an ERROR naming a file it had not moved.

    Three separate promises failed at once, so all three are asserted here.
    """
    for round_index in range(RACE_ROUNDS):
        state_dir = tmp_path / f"corrupt{round_index}"
        state_dir.mkdir()
        (state_dir / HOST_JSON_NAME).write_text("not json at all")

        results = _race_once(state_dir)
        errors = [source for host_id, _, source in results if host_id == "<error>"]
        assert not errors, f"round {round_index}: {errors}"

        host_ids = {host_id for host_id, _, _ in results}
        assert len(host_ids) == 1, (
            f"round {round_index}: {len(host_ids)} distinct host_ids from one corrupt cache: "
            f"{host_ids}. Concurrent quarantines are destroying each other's identities."
        )
        # Every process agrees with the file — the invariant, not a proxy for it.
        assert _cache(state_dir)["host_id"] == next(iter(host_ids))

        # "Nothing is destroyed" must mean the CORRUPT content, not a valid identity that
        # happened to be at the path when a stale decision was acted on.
        quarantined = sorted(state_dir.glob(f"{HOST_JSON_NAME}.corrupt.*"))
        for quarantine_file in quarantined:
            body = quarantine_file.read_text()
            assert body == "not json at all", (
                f"{quarantine_file.name} holds {body!r} rather than the corrupt content — a "
                "live identity was moved aside"
            )
        assert len(quarantined) == 1, (
            f"round {round_index}: {len(quarantined)} quarantine files for one corrupt cache; "
            "only the process that actually handled it may create one"
        )


def test_a_tmux_stamp_shared_by_every_process_yields_exactly_that_id(tmp_path: Path) -> None:
    """The realistic recovery case: `host.json` was lost while tmux sessions are still live.

    `enroll.py` reads `@shellbox_host_id` once and hands it to every process, so all 32 carry
    the same stamp. They must converge on it and never mint a uuid — a fresh id here re-keys
    every `session_id` on a host whose sessions are running and addressable.
    """
    for round_index in range(RACE_ROUNDS):
        state_dir = tmp_path / f"stamped{round_index}"
        state_dir.mkdir()

        results = _race_once(state_dir, recovered_for=set(range(CONCURRENT_WORKERS)))
        errors = [source for host_id, _, source in results if host_id == "<error>"]
        assert not errors, f"round {round_index}: {errors}"

        host_ids = {host_id for host_id, _, _ in results}
        assert host_ids == {"from-tmux"}, (
            f"round {round_index}: expected every process to adopt the tmux stamp, got {host_ids}"
        )
        assert _cache(state_dir)["host_id"] == "from-tmux"


def test_a_mixed_stamp_race_converges_on_the_stamp_and_invents_nothing(tmp_path: Path) -> None:
    """Only some processes carry a stamp — a `show-options` against a contended server can fail.

    ⚠️ **Two ids is the correct outcome here, and that is not a weaker assertion than it looks.**
    A process that resolves *before* the override legitimately returns the assigned uuid; it
    cannot know a stamp is about to win. What must hold is that the race invents nothing beyond
    those two, the file converges on the stamp so every later start agrees, and each stamp-carrier
    returns the stamp rather than a silent no-op — which is exactly what the old code did: it
    logged "re-adopting it" while writing nothing, and returned an id in no cache.
    """
    for round_index in range(RACE_ROUNDS):
        state_dir = tmp_path / f"mixed{round_index}"
        state_dir.mkdir()

        results = _race_once(state_dir, recovered_for={0, 1, 2})
        errors = [source for host_id, _, source in results if host_id == "<error>"]
        assert not errors, f"round {round_index}: {errors}"

        host_ids = {host_id for host_id, _, _ in results}
        assert len(host_ids) <= 2, (
            f"round {round_index}: {len(host_ids)} distinct host_ids: {host_ids}. At most two are "
            "explicable (the assigned uuid and the stamp); a third means the transaction is "
            "unarbitrated again."
        )
        assert _cache(state_dir)["host_id"] == "from-tmux", (
            "the tmux stamp must win in the file, or hosts with live sessions never converge"
        )
        for host_id, _, source in results:
            if source == "tmux":
                assert host_id == "from-tmux", (
                    f"a process reported source='tmux' but returned {host_id!r} — the old silent "
                    "no-op, where the log claimed a write that never happened"
                )
        # Nothing here is corrupt, so nothing may be quarantined.
        assert not list(state_dir.glob(f"{HOST_JSON_NAME}.corrupt.*"))


def test_a_property_write_can_never_change_the_host_id(tmp_path: Path) -> None:
    """Property writes are read-modify-write, so a stale snapshot can overwrite a newer one.

    That is tolerable for `sandbox_id`/`owner_email` — idempotent, re-derived every start — and
    intolerable for `host_id`, where it silently REVERTED the tmux-wins reconciliation and sent
    the next boot back to the previous identity. So the property writer refuses to carry a
    `host_id` at all, and identity only changes through the arbitrated paths.
    """
    resolve_host_id(str(tmp_path))
    reconciled = resolve_host_id(str(tmp_path), recovered="from-tmux").host_id
    assert reconciled == "from-tmux"

    # An unrelated owner write, exactly as `resolve_owner_email` issues it.
    resolve_owner_email(str(tmp_path), credential_email="owner@example.com")

    assert _cache(tmp_path)["host_id"] == "from-tmux", (
        "an owner_email write reverted the host_id; the tmux-wins reconciliation was undone by "
        "an unrelated field"
    )
    assert resolve_host_id(str(tmp_path)).host_id == "from-tmux"


# ------------------------------------------------------- validation of every input
@pytest.mark.parametrize(
    "bad",
    # No "" case: an empty env var means UNSET, which `if explicit:` handles by falling
    # through to normal resolution. Whitespace-only is different — someone meant something.
    ["a:b", "lakebox:abc", "   ", "x\ty", "x y", "trailing ", "\nid"],
)
def test_an_explicit_host_id_is_validated(tmp_path: Path, bad: str) -> None:
    """`$SHELLBOX_HOST_ID` used to bypass every check the cache reader applied.

    A colon makes `<host_id>:<tmux_name>` ambiguous and a TAB corrupts the TAB-delimited
    list-sessions record, so neither may be accepted. Operator-set, so this refuses loudly
    rather than silently running under something else.
    """
    with pytest.raises(IdentityError, match="SHELLBOX_HOST_ID"):
        resolve_host_id(str(tmp_path), explicit=bad)


@pytest.mark.parametrize("bad", ["a:b", "lakebox:abc", "   ", "x\ty"])
def test_a_bad_tmux_stamp_is_ignored_not_fatal(tmp_path: Path, bad: str) -> None:
    """The mirror case, decided the other way.

    A stamp comes from a tmux user option that any agent on the shared server can set, so
    refusing would let one bad `set-option` deny every agent its shells. It is dropped with an
    ERROR and resolution continues. The old code accepted it, PERSISTED it, and detonated one
    boot later with an error blaming the file.
    """
    identity = resolve_host_id(str(tmp_path), recovered=bad)
    assert identity.host_id != bad
    assert identity.source == "assigned"
    assert _cache(tmp_path)["host_id"] == identity.host_id
