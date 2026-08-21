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
    """With a REAL postgres DSN, the bundled psycopg drives ``PostgresRegistry`` NON-degraded: the
    driver loads, the engine opens, and SQL executes against real postgres.

    This is the half of AC-17 that exercises ``psycopg_binary`` end to end (CRITICAL-1). The hazard
    it guards: a broken or wrong-glibc driver makes ``_open_registry`` catch the ImportError, log
    "could not open the registry" (``server.py:349-353``), fall back to ``NullRegistry``, and pass
    every other smoke gate silently. Two facts, together, prove that did NOT happen:

    1. **The NullRegistry-fallback WARNING never fired** -- so the engine opened and the driver
       loaded. This is the load-bearing CRITICAL-1 assertion.
    2. **A create warning, if present, is the self-healing FK IntegrityError, not a
       connection/driver error.** Enrollment writes the ``hosts`` row (E4) on a BACKGROUND thread,
       and ``enroll.py:383-386`` documents that the tool-path session INSERT can win that race and
       raise ``sessions_host_id_fkey`` -- an expected, self-healing ``registry_warning`` while
       enrollment is in flight. That warning is itself the proof we want: PostgreSQL raises an
       IntegrityError only *after* it has received and processed the INSERT, so the bundled driver
       demonstrably connected and executed real SQL. A degraded driver could not have produced it.

    Deliberately does NOT poll for a fully clean projection: E4's landing depends on the enrollment
    thread's credential-resolution timing, which is not a property of the artifact and is not
    something this gate should race (it timed out in CI when it tried). The two facts above prove
    the bundled driver works without depending on that timing.
    """
    dsn = os.environ.get(_ARTIFACT_DSN_ENV)
    if not dsn:
        pytest.skip(f"{_ARTIFACT_DSN_ENV} unset (no postgres service container)")

    harness = make_harness(tmux_server, tmp_path, command=str(_artifact()))
    env = harness.env_with(SHELLBOX_DATABASE_URL=dsn)
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

    stderr = harness.stderr()
    # (1) The load-bearing CRITICAL-1 assertion: the driver loaded and the engine opened, so the
    # artifact did NOT silently fall back to NullRegistry.
    assert "could not open the registry" not in stderr, (
        "the artifact fell back to NullRegistry -- CRITICAL-1's silent degradation, which every "
        f"other gate would have passed. stderr tail: {stderr[-800:]}"
    )

    # (2) Any create warning must be the self-healing enrollment-race IntegrityError -- positive
    # proof PostgreSQL processed the INSERT through the bundled driver -- and never a connection or
    # driver failure. A broken driver produces (1)'s NullRegistry fallback, not a per-call warning,
    # so the presence of an IntegrityError here is stronger evidence than a clean projection would
    # be: it means real SQL reached a real database.
    warning = created.data["registry_warning"]
    if warning is not None:
        assert "IntegrityError" in warning, (
            f"the create warning is not the expected self-healing enrollment-race IntegrityError: "
            f"{warning!r}. A connection or driver failure here would mean the bundled psycopg did "
            f"not work. stderr tail: {stderr[-800:]}"
        )


# --- P2 / AC-11 / AC-12: warm-cache concurrency ------------------------------------------------


def _warm_start_budget_seconds() -> float:
    """The slowest warm-start budget. Warm means the pex cache is already extracted, so a start is
    a venv re-entry, not an 81 MB unpack; 10 s is generous headroom over that on a shared 4-core
    runner. Overridable so the number can be re-set from a measured value without an edit."""
    return float(os.environ.get("SHELLBOX_ARTIFACT_WARM_START_BUDGET", "10"))


@requires_tmux
def test_concurrent_warm_starts_do_not_re_extract_and_stay_fast(
    tmux_server: TmuxServer, tmp_path: Path
) -> None:
    """One cold start pays the extraction; then 32 concurrent starts against the now-warm shared
    cache all reach ``tools/list`` quickly and none re-extracts.

    This is the property that actually matters for pooled agents (buzz-lakebox#23): ONE long-lived
    sandbox with a persistent ``$HOME``, agents starting INSIDE it over time and sharing a single
    pex cache. The ~81 MB extraction is paid ONCE per sandbox lifetime; every subsequent start
    reuses the warm cache. So the gate is: after the cache is warm, concurrent starts are fast
    (AC-11) and do not re-extract (AC-12).

    A cold 32-way thundering herd -- 32 simultaneous first-ever starts serialising on the extraction
    lock -- is a worst case the field does not hit (agents do not all start at the instant of a cold
    cache), and its timing measures the runner's disk under contention rather than the artifact. The
    warm path is measured instead; the cold extraction is still exercised, once, by the warm-up.

    The cache root is PINNED via ``PEX_ROOT`` rather than discovered. pex's default root is
    ``$HOME/.cache/pex`` (measured by the spike's ``q_reexec``), not ``$HOME/.pex``; guessing that
    layout is what a first version of this test got wrong. Setting ``PEX_ROOT`` to a directory the
    test owns removes the guess entirely -- the property under test (warm reuse, no re-extract,
    lock under contention) is identical wherever the cache lands, and the test can always find it.
    ``PEX_ROOT`` is a ``PEX_*`` variable, not ``SHELLBOX_*``, so it does not touch AC-5's
    "no ``SHELLBOX_*`` set" gate; the field uses the baked default under its persistent ``$HOME``.

    HOME is shared (one contended cache); SHELLBOX_STATE_DIR is per-worker so unrelated app state
    cannot collide. No DSN, so the registry is NullRegistry and the start touches no database. A
    lock that deadlocked or a cache that corrupted would fail a worker rather than pass quietly.
    """
    artifact = str(_artifact())
    workers = 32
    budget = _warm_start_budget_seconds()

    base = make_harness(tmux_server, tmp_path, command=artifact)
    shared_home = tmp_path / "shared-home"
    shared_home.mkdir()
    # The cache root the test owns. Every worker shares it, so exactly one extracts and the rest
    # reuse -- and the test knows precisely where to look for the before/after comparison.
    pex_root = tmp_path / "pexroot"

    def start(index: int) -> float:
        env = {
            **base.env,
            "HOME": str(shared_home),
            "PEX_ROOT": str(pex_root),
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

    # Warm-up: one start pays the ~81 MB extraction into the shared cache, and records the cache's
    # contents so the concurrent starts below can be shown NOT to re-extract.
    cold_elapsed = start(-1)
    assert pex_root.is_dir(), (
        f"the warm-up start did not create {pex_root}; the artifact did not honor PEX_ROOT, so it "
        f"is not a pex that respects the cache-root env var. cold start took {cold_elapsed:.1f}s"
    )
    # The whole cache tree, layout-independent: every path relative to the root. Comparing this
    # before/after catches a re-extraction wherever pex places it, and does not assume a particular
    # subdirectory name.
    contents_before = {str(p.relative_to(pex_root)) for p in pex_root.rglob("*")}
    # A non-vacuity guard: the warm-up MUST have populated the cache. If it did not, the comparison
    # below would be {} == {} and pass while proving nothing -- the "check that cannot fail" this
    # repo has shipped before. A near-empty tree means either the extraction did not land here or
    # the artifact's cache root moved, and either way this fails loudly in CI with the real layout.
    assert len(contents_before) > 50, (
        f"the pex cache under {pex_root} holds only {len(contents_before)} entries after a cold "
        f"start, too few for an ~81 MB extraction. The cache root is not where this test looks, so "
        f"the re-extraction check below cannot be trusted. Entries: {sorted(contents_before)[:20]}"
    )

    # 32 concurrent WARM starts against the extracted cache.
    with ThreadPoolExecutor(max_workers=workers) as pool:
        elapsed = list(pool.map(start, range(workers)))

    slowest = max(elapsed)
    print(f"warm concurrency: cold warm-up {cold_elapsed:.1f}s, slowest warm {slowest:.1f}s "
          f"(budget {budget:.0f}s)")
    assert slowest < budget, (
        f"the slowest of {workers} WARM concurrent starts took {slowest:.1f}s (budget "
        f"{budget:.0f}s); a warm start should be a venv re-entry, not a re-extraction"
    )

    # AC-12: the warm starts reused the cache rather than re-extracting into it. The cache tree is
    # byte-for-path identical before and after, so nothing was unpacked a second time.
    contents_after = {str(p.relative_to(pex_root)) for p in pex_root.rglob("*")}
    added = contents_after - contents_before
    assert not added, (
        "the pex cache grew during the warm starts, so the artifact re-extracted rather than "
        f"reusing the warm cache: {sorted(added)[:20]}"
    )
