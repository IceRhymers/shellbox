"""The built release artifact, driven the way the world drives it (shellbox#21, AC-17).

Everything here runs against ``dist/shellbox`` -- the pex the release lane publishes -- not
against ``python -m shellbox_mcp``. The whole point of the issue is that "imports cleanly" is
not enough: the artifact must answer the protocol over real stdio, load the compiled
extensions it bundles, and keep a configured registry NON-degraded. Those are properties only
the shipped bytes have, so only the shipped bytes are tested here.

GATED ON ``SHELLBOX_ARTIFACT_PATH``. There is no built artifact on a developer machine (the
build is CI-only -- pex runs its vendored pip ``--isolated`` and ``pypi.org`` is blackholed
here; see ``scripts/build_artifact.sh``'s header), so this whole module SKIPS unless the CI
smoke lane has built the artifact and pointed this variable at it. A skip is not a pass: the
lane greps the skip reason, exactly as ``make test-tmux`` does, because a silently skipped gate
is indistinguishable from a green one.

WHY AC-17 EXISTS (CRITICAL-1). At startup, with no ``SHELLBOX_*`` set -- the environment the
other smoke gates require -- only ``pydantic_core`` and ``rpds`` are imported. ``psycopg_binary``,
``cryptography``, ``_cffi_backend`` and ``greenlet`` are NOT, and both code paths that would
reach them swallow the failure (``server.py:349-353`` catches the driver ImportError into
``NullRegistry`` with one WARNING; ``enroll.py`` catches the SDK ImportError at DEBUG). So an
artifact shipped with a broken or wrong-glibc ``psycopg_binary`` answers ``initialize``, lists
six tools, completes a live round trip, writes nothing to stdout, and then silently loses the
registry in the field. AC-17 is the only gate that forces those extensions to load and the
registry path to run non-degraded.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from conftest import TmuxServer, requires_tmux, sentinel
from harness import await_content, call, make_harness, run_calls, run_script
from mcp import ClientSession

# The artifact path the CI smoke lane exports. Absent locally, so the module skips.
_ARTIFACT_ENV = "SHELLBOX_ARTIFACT_PATH"
# A postgres service container's DSN, exported by the AC-17(b) job only. Absent → that one
# test skips while the rest of the module still runs against the artifact.
_ARTIFACT_DSN_ENV = "SHELLBOX_ARTIFACT_DATABASE_URL"

requires_artifact = pytest.mark.skipif(
    not os.environ.get(_ARTIFACT_ENV), reason="SHELLBOX_ARTIFACT_PATH unset (no built artifact)"
)

pytestmark = requires_artifact

MANIFEST_NAME = "SHELLBOX-MANIFEST.json"

TOOL_NAMES = {
    "shell_create",
    "shell_send",
    "shell_read",
    "shell_list",
    "shell_resize",
    "shell_kill",
}

# The extension modules with NO pure-Python fallback -- the ones a plain zipapp could not carry
# and the ones a wrong-glibc build breaks silently (CRITICAL-1). This is the load proof, and it
# is deliberately the *importable module names*, not a dist list: importing the submodule is
# what forces its ``.so`` to load. The full ``.so`` enumeration -- and the pex extraction layout
# these names assume -- is measured by ``spike/pex_spike.py`` (the committed oracle); check_artifact
# guards the MANIFEST's recorded ``extension_modules`` list. If the dependency set changes, a
# resolve change reddens ``--assert-platform`` and the spike's enumeration moves, so this list
# cannot silently fall behind the bundle.
#
# ``psycopg_binary`` is deliberately NOT here: psycopg's binary package refuses a direct
# ``import psycopg_binary`` ("the psycopg package should be imported before psycopg_binary"), so
# its compiled backend is proven separately, by importing ``psycopg`` and asserting the active pq
# implementation is the bundled ``binary`` one rather than the pure-Python fallback CRITICAL-1
# warns about -- see ``test_the_psycopg_binary_backend_is_active``.
NO_FALLBACK_EXTENSIONS = (
    "pydantic_core._pydantic_core",
    "rpds.rpds",
    "greenlet._greenlet",
    "_cffi_backend",
    "cryptography.hazmat.bindings._rust",
)


def _artifact() -> Path:
    """The built artifact, or skip. Asserts it exists and is executable -- a misconfigured lane
    that pointed the variable at nothing must fail loudly here, not skip."""
    raw = os.environ.get(_ARTIFACT_ENV)
    if not raw:
        pytest.skip(f"{_ARTIFACT_ENV} unset (no built artifact)")
    path = Path(raw)
    assert path.is_file(), f"{_ARTIFACT_ENV}={raw} does not exist"
    assert os.access(path, os.X_OK), f"{path} is not executable"
    return path


def _manifest(artifact: Path) -> dict[str, object]:
    """The embedded ``SHELLBOX-MANIFEST.json``. A pex is a zip, so this reads it directly."""
    with zipfile.ZipFile(artifact) as archive:
        return dict(json.loads(archive.read(MANIFEST_NAME)))


def _pex_interpreter(artifact: Path, script: str, *, timeout: float = 120.0) -> str:
    """Run ``script`` INSIDE the pex's own environment and return its stdout.

    ``PEX_INTERPRETER=1`` makes the pex set up its dependency ``sys.path`` and then run the
    interpreter instead of the entry point, so ``import pydantic_core`` reaches the bundled
    extension exactly as the server would. It is a ``PEX_*`` variable, not a ``SHELLBOX_*`` one,
    so this does not conflict with AC-5's "no SHELLBOX_* set" gate, which is a different test.
    """
    result = subprocess.run(  # noqa: S603 -- fixed argv, no shell
        [str(artifact), "-c", script],
        capture_output=True,
        text=True,
        timeout=timeout,
        env={**os.environ, "PEX_INTERPRETER": "1"},
    )
    assert result.returncode == 0, (
        f"the pex interpreter exited {result.returncode}\nstdout: {result.stdout}\n"
        f"stderr: {result.stderr}"
    )
    return result.stdout


# --- the freshness invariant the artifact lane removes, restored (T3) -------------------------


def test_artifact_manifest_git_sha_matches_the_checkout() -> None:
    """The artifact under test was built from THIS commit, not a stale copy.

    ``harness.py`` spawns ``python -m shellbox_mcp`` precisely so a test cannot exercise a stale
    installed build; the artifact lane overrides that, so it must restore the guarantee another
    way. ``$GITHUB_SHA`` is the checkout CI is running; the MANIFEST records the sha the artifact
    was built from. If they differ, every other assertion in this module is about the wrong bytes.
    """
    github_sha = os.environ.get("GITHUB_SHA")
    if not github_sha:
        pytest.skip("GITHUB_SHA unset (not running in CI); freshness cannot be checked")
    manifest = _manifest(_artifact())
    assert manifest.get("git_sha") == github_sha, (
        f"artifact was built from {manifest.get('git_sha')!r}, but the checkout is "
        f"{github_sha!r} -- the smoke lane is testing a stale artifact"
    )


# --- AC-3 / AC-4: the protocol, over the artifact ---------------------------------------------


@requires_tmux
def test_all_six_tools_and_a_live_round_trip_over_the_artifact(
    tmux_server: TmuxServer, tmp_path: Path
) -> None:
    """``tools/list`` names the six, and a full ``create → send → read → kill`` runs -- against
    the shipped bytes, through a real MCP client, over real tmux 3.4.

    This is ``test_tools_over_stdio.py``'s bar, re-run against the artifact: the same suite, the
    same assertions, a different ``command``. Reuse rather than reinvent (the plan's Integration
    note) -- the only difference is what is spawned.
    """
    harness = make_harness(tmux_server, tmp_path, command=str(_artifact()))
    name = harness.name("art")
    life = sentinel("ART")

    async def script(client: ClientSession) -> dict[str, object]:
        listing = await client.list_tools()
        tool_names = {tool.name for tool in listing.tools}
        created = (await call(client, "shell_create", {"name": name, "cwd": str(tmp_path)})).data
        await call(client, "shell_send", {"session": created["session"], "text": life.echo()})
        content = await await_content(client, created["session"], life.awaited)
        killed = (await call(client, "shell_kill", {"session": name})).data
        return {
            "tools": sorted(tool_names),
            "created": created,
            "content": content,
            "killed": killed,
        }

    out = run_script(harness, script)
    assert set(out["tools"]) == TOOL_NAMES, f"artifact advertised {out['tools']}"
    assert out["created"]["created"] is True
    assert life.awaited in str(out["content"])
    assert out["killed"]["killed"] is True


# --- AC-17(a): the bundled extensions actually load -------------------------------------------


def test_the_no_fallback_extensions_load_inside_the_artifact() -> None:
    """Every extension with no pure-Python fallback imports from inside the artifact.

    This is the gate CRITICAL-1 demands: a wrong-glibc or truncated ``cryptography`` extension is
    never touched by ``initialize`` or a shell round trip, so only a direct import catches it. Run
    inside the pex environment so the import path is the server's.
    """
    artifact = _artifact()
    modules = list(NO_FALLBACK_EXTENSIONS)
    script = (
        "import json, importlib\n"
        f"mods = {modules!r}\n"
        "results = {}\n"
        "for name in mods:\n"
        "    try:\n"
        "        importlib.import_module(name)\n"
        "        results[name] = 'ok'\n"
        "    except Exception as exc:\n"
        "        results[name] = f'{type(exc).__name__}: {exc}'\n"
        "print(json.dumps(results))\n"
    )
    results = json.loads(_pex_interpreter(artifact, script))
    broken = {name: outcome for name, outcome in results.items() if outcome != "ok"}
    assert not broken, f"bundled extensions failed to import from the artifact: {broken}"

    # The ungating cross-check: the MANIFEST must actually record a bundled-extension set, so a
    # build that shipped none (all-pure-Python resolve, which cannot be right for this tree)
    # reddens here rather than passing this test vacuously.
    manifest = _manifest(artifact)
    extension_modules = manifest.get("extension_modules")
    assert isinstance(extension_modules, list) and extension_modules, (
        "the MANIFEST records no extension_modules; the bundle is not what this tree resolves to"
    )


def test_the_psycopg_binary_backend_is_active() -> None:
    """The bundled psycopg compiled backend loads and is the one in use, not a silent fallback.

    ``psycopg_binary`` refuses a direct import by design, so the meaningful CRITICAL-1 proof is to
    import ``psycopg`` and read the active pq implementation. ``binary`` is our bundled
    psycopg[binary]; ``python`` is the pure fallback that degrades the registry silently; ``c``
    would mean a system-libpq path we did not bundle. Only ``binary`` proves the shipped extension
    loaded, so assert exactly that.
    """
    artifact = _artifact()
    impl = _pex_interpreter(
        artifact, "import psycopg; print(psycopg.pq.__impl__)"
    ).strip()
    assert impl == "binary", (
        f"psycopg is using the {impl!r} pq backend, not the bundled 'binary' one -- the shipped "
        "psycopg_binary extension did not load, which is exactly CRITICAL-1's silent degradation"
    )


# --- AC-17(b): a configured registry runs NON-degraded ----------------------------------------


@requires_tmux
def test_a_configured_registry_is_not_degraded_through_the_artifact(
    tmux_server: TmuxServer, tmp_path: Path
) -> None:
    """With a REAL postgres DSN, the registry reaches a NON-degraded projection through the bundled
    psycopg, and the ``server.py:349-353`` "could not open the registry" WARNING never fires.

    This is the half of AC-17 that drives ``PostgresRegistry`` through the bundled psycopg against a
    real database. Its inverse is the whole hazard (CRITICAL-1): if the driver extension were broken
    or wrong-glibc, ``_open_registry`` would catch the ImportError, log "could not open the
    registry", fall back to ``NullRegistry``, and every other smoke gate would still pass. So the
    LOAD-BEARING assertion is the absence of that WARNING -- it fires only when the driver/engine
    could not open at all.

    Enrollment -- the ``hosts`` row, E4 -- runs on a BACKGROUND thread, and ``enroll.py:383-386``
    documents that the tool-path session INSERT can win the race and raise the
    ``sessions_host_id_fkey`` constraint:
    an EXPECTED, self-healing per-call ``registry_warning`` while enrollment is still in flight, not
    a driver failure. (The harness sets ``SHELLBOX_OWNER_EMAIL``, so enrollment resolves identity
    locally and E4 upserts the host row without a Databricks credential -- ``server.py:401``.) So a
    non-degraded projection is proven the honest way: poll a fresh create until the warning clears
    within a deadline. That it clears at all proves the full write path -- ``upsert_host`` then a
    clean session projection -- ran through the bundled psycopg against real postgres.
    """
    dsn = os.environ.get(_ARTIFACT_DSN_ENV)
    if not dsn:
        pytest.skip(f"{_ARTIFACT_DSN_ENV} unset (no postgres service container)")

    harness = make_harness(tmux_server, tmp_path, command=str(_artifact()))
    env = harness.env_with(SHELLBOX_DATABASE_URL=dsn)

    deadline = time.monotonic() + 30.0
    last_warning: str | None = "<no call completed>"
    cleared = False
    while time.monotonic() < deadline:
        name = harness.name("reg")
        created, killed = run_calls(
            harness,
            [
                ("shell_create", {"name": name, "cwd": str(tmp_path)}),
                ("shell_kill", {"session": name}),
            ],
            env=env,
        )
        assert created.data["created"] is True
        assert killed.data["killed"] is True
        last_warning = created.data["registry_warning"]
        if last_warning is None and killed.data["registry_warning"] is None:
            cleared = True
            break
        time.sleep(0.5)

    stderr = harness.stderr()
    # The load-bearing CRITICAL-1 assertion: the driver loaded and the engine opened. Distinct from
    # the enrollment-race projection warning above, this fires only on a NullRegistry fallback.
    assert "could not open the registry" not in stderr, (
        "the artifact fell back to NullRegistry -- CRITICAL-1's silent degradation, which every "
        f"other gate would have passed. stderr tail: {stderr[-800:]}"
    )
    # And the full write path completed: the host row was enrolled and a session projected cleanly.
    assert cleared, (
        "the registry never projected a session cleanly within the deadline; last create warning: "
        f"{last_warning!r}. Enrollment (upsert_host) did not complete against real postgres. "
        f"stderr tail: {stderr[-800:]}"
    )


# --- P2 / AC-11: 32 cold starts against a cold cache ------------------------------------------


def _cold_start_budget_seconds() -> float:
    """The slowest-first-start budget. 20 s by default, below the 30 s ``initialize`` figure
    (``docs/registration.md``, unmeasured on this path -- so this is a REGRESSION budget, not a
    fitness one). Overridable so the number can be re-set from a measured value without an edit."""
    return float(os.environ.get("SHELLBOX_ARTIFACT_COLD_START_BUDGET", "20"))


@requires_tmux
def test_thirty_two_starts_share_one_cold_cache_without_corruption(
    tmux_server: TmuxServer, tmp_path: Path
) -> None:
    """32 processes exec the artifact at once against ONE cold, shared cache; all reach
    ``tools/list``.

    This is P2, modelling the arrangement buzz-lakebox#23 confirmed: ONE long-lived sandbox with a
    persistent ``$HOME``, pooled agents starting INSIDE it and therefore sharing a single pex cache
    (``~/.pex``). The property under test is pex's first-run extraction LOCK: when many processes
    hit a cold shared cache together, exactly one extracts ~81 MB while the rest block on the lock
    and then reuse the finished cache -- never reading a half-written ``.so`` or deadlocking. A
    shared cold HOME is both the realistic case and the one that actually exercises the lock; 32
    private caches would instead be 32×81 MB of parallel I/O that measures the runner's disk, not
    the artifact.

    HOME is shared (so the cache is contended); SHELLBOX_STATE_DIR is per-worker (so unrelated app
    state cannot collide). No DSN, so the registry is NullRegistry and the start touches no
    database. The slowest start is recorded and budgeted; a lock that deadlocked or a cache that
    corrupted would fail a worker rather than pass quietly.
    """
    artifact = str(_artifact())
    workers = 32
    budget = _cold_start_budget_seconds()

    # One shared, cold HOME -> one contended `~/.pex`. `make_harness` is called ONCE (it creates
    # tmp_path/"home"); every worker runs against that same home via a per-worker env override.
    base = make_harness(tmux_server, tmp_path, command=artifact)
    shared_home = tmp_path / "shared-home"
    shared_home.mkdir()

    def one_start(index: int) -> float:
        env = {
            **base.env,
            "HOME": str(shared_home),
            "SHELLBOX_STATE_DIR": str(tmp_path / f"state-{index}"),
        }

        async def script(client: ClientSession) -> int:
            listing = await client.list_tools()
            return len(listing.tools)

        started = time.monotonic()
        count = run_script(base, script, env=env)
        elapsed = time.monotonic() - started
        assert count == len(TOOL_NAMES), f"worker {index} saw {count} tools"
        return elapsed

    with ThreadPoolExecutor(max_workers=workers) as pool:
        elapsed = list(pool.map(one_start, range(workers)))

    slowest = max(elapsed)
    print(f"32-way shared-cache start: slowest {slowest:.1f}s (budget {budget:.0f}s)")
    assert slowest < budget, (
        f"the slowest of {workers} concurrent starts took {slowest:.1f}s (budget {budget:.0f}s); "
        "the artifact may be deadlocking or serializing pathologically on its extraction lock"
    )
