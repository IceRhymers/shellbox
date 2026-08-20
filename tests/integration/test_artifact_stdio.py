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
    """With a REAL postgres DSN, a session round trip through the artifact records inventory and
    the ``server.py:349-353`` degradation WARNING does NOT fire.

    This is the half of AC-17 that imports ``psycopg_binary`` and exercises ``PostgresRegistry``.
    Its inverse is the whole hazard: if the driver extension were broken, ``_open_registry`` would
    catch the ImportError, log "could not open the registry", fall back to ``NullRegistry``, and
    every other smoke gate would still pass. So the assertion is on the ABSENCE of that warning
    and of any ``registry_warning`` on the successful call.
    """
    dsn = os.environ.get(_ARTIFACT_DSN_ENV)
    if not dsn:
        pytest.skip(f"{_ARTIFACT_DSN_ENV} unset (no postgres service container)")

    harness = make_harness(tmux_server, tmp_path, command=str(_artifact()))
    name = harness.name("reg")
    created, killed = run_calls(
        harness,
        [
            ("shell_create", {"name": name, "cwd": str(tmp_path)}),
            ("shell_kill", {"session": name}),
        ],
        env=harness.env_with(SHELLBOX_DATABASE_URL=dsn),
    )

    assert created.data["created"] is True
    # The load-bearing assertion: a non-degraded registry projects cleanly, with no warning.
    assert created.data["registry_warning"] is None, (
        f"the registry degraded on create: {created.data['registry_warning']!r} -- the bundled "
        "psycopg_binary did not load, or PostgresRegistry could not open the DSN"
    )
    assert killed.data["killed"] is True
    assert killed.data["registry_warning"] is None, "the kill projection degraded"

    stderr = harness.stderr()
    assert "could not open the registry" not in stderr, (
        "the artifact fell back to NullRegistry -- CRITICAL-1's silent degradation, which every "
        "other gate would have passed"
    )
    assert "registry projection failed" not in stderr, "a projection failed against real postgres"


# --- P2 / AC-11: 32 cold starts against a cold cache ------------------------------------------


def _cold_start_budget_seconds() -> float:
    """The slowest-first-start budget. 20 s by default, below the 30 s ``initialize`` figure
    (``docs/registration.md``, unmeasured on this path -- so this is a REGRESSION budget, not a
    fitness one). Overridable so the number can be re-set from a measured value without an edit."""
    return float(os.environ.get("SHELLBOX_ARTIFACT_COLD_START_BUDGET", "20"))


@requires_tmux
def test_thirty_two_cold_starts_all_reach_tools_list(
    tmux_server: TmuxServer, tmp_path: Path
) -> None:
    """32 processes exec the artifact against a COLD cache at once; all reach ``tools/list``.

    This is P2: pooled agents start together, and the first-run extraction of ~81 MB must be
    lock-safe under concurrency, not race into a half-written ``.so`` or a poisoned cache. Each
    worker gets its own scratch HOME so the cache root (``$HOME/.cache/shellbox-pex/<version>``,
    baked into the artifact) starts cold; they share nothing but the artifact and the tmux server.

    The slowest first start is recorded and budgeted. A serialize-on-the-extraction-lock failure
    (31 workers each waiting out the extraction) would blow the budget rather than pass quietly.
    """
    artifact = str(_artifact())
    workers = 32
    budget = _cold_start_budget_seconds()

    def one_start(index: int) -> float:
        # A private HOME per worker → a cold pex cache per worker, which is the condition under
        # test. A private state dir too, so the registry layer and identity cache never collide.
        home = tmp_path / f"home-{index}"
        home.mkdir()
        env = {
            **make_harness(tmux_server, tmp_path).env,
            "HOME": str(home),
            "SHELLBOX_STATE_DIR": str(tmp_path / f"state-{index}"),
        }
        harness = make_harness(tmux_server, tmp_path, command=artifact)
        harness.env = env

        async def script(client: ClientSession) -> int:
            listing = await client.list_tools()
            return len(listing.tools)

        started = time.monotonic()
        count = run_script(harness, script, env=env)
        elapsed = time.monotonic() - started
        assert count == len(TOOL_NAMES), f"worker {index} saw {count} tools"
        return elapsed

    with ThreadPoolExecutor(max_workers=workers) as pool:
        elapsed = list(pool.map(one_start, range(workers)))

    slowest = max(elapsed)
    print(f"32-way cold start: slowest first start {slowest:.1f}s (budget {budget:.0f}s)")
    assert slowest < budget, (
        f"the slowest of {workers} cold starts took {slowest:.1f}s (budget {budget:.0f}s); the "
        "artifact may be serializing on its first-run extraction lock"
    )
