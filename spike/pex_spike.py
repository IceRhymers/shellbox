#!/usr/bin/env python3
"""Measure the pex runtime behaviour the release artifact's gates rest on (shellbox#21).

This is to the artifact what ``spike/tmux_spike.py`` is to the tmux adapter: the committed
ORACLE. Several acceptance criteria and one integration test assume things about how a pex boots
that cannot be measured on a developer machine -- pex runs its vendored pip ``--isolated`` and
``pypi.org`` is blackholed here (``scripts/build_artifact.sh``'s header), so no artifact can be
built locally to probe. CI is the only place these facts exist, so the spike is committed, the
artifact job runs it against the freshly built ``dist/shellbox``, and it GATES on its exit code:
if a load-bearing assumption is false, this fails in one legible line instead of leaving a green
tick over a test that quietly proves nothing.

It emits newline-delimited JSON -- one record per observation, flushed immediately -- following
``probe/probe_identity.py`` so a partial run is still useful, and it ASSERTS the three properties
the artifact's correctness depends on (it does not merely emit). Run it as::

    SHELLBOX_ARTIFACT_PATH=dist/shellbox python3 spike/pex_spike.py

The questions it answers, each recorded with its evidence (``spike/PEX-FINDINGS.md`` carries the
prose and the measured numbers from the first CI run):

* Q-BOOT   -- does the single-platform pex boot to a JSON-RPC frame on x86_64, with nothing on
              stdout before it? (Gated. This is P3 + the form's basic viability.)
* Q-EXTS   -- do the six no-pure-fallback extensions import from inside the pex? (Gated. This is
              CRITICAL-1's load proof.)
* Q-STARTUP-- which extension modules load on the *startup* path, with no ``SHELLBOX_*`` set?
              (Recorded. Confirms M-C1 in CI: only ``pydantic_core`` + ``rpds``, which is exactly
              why AC-17 forces the others to load separately.)
* Q-REEXEC -- what is ``sys.executable`` after a ``/usr/bin/python3`` invocation? pex may re-exec
              into a different interpreter to satisfy its constraint. (Recorded. The critic's
              V2-P4: "starts on the target's 3.12" is only proven if we look.)
* Q-STREAM -- which stream does pex's own bootstrap use when it has something to say? (Recorded.)

WHY IT DOES NOT BUILD THE ARTIFACT ITSELF. The build is ``make artifact``'s job and the job runs
it first; the spike measures the RESULT. Coupling the two would make a build failure look like a
spike failure. If ``SHELLBOX_ARTIFACT_PATH`` is unset the spike records a skip and exits 0 -- the
same distinction ``make test-tmux`` draws between "no binary" and "the gate failed" -- because a
developer running it by hand has no artifact and should not see a red X for that. The CI job
always sets the variable, so in CI a skip cannot happen silently.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# The six extensions with no pure-Python fallback -- the load proof that matters (CRITICAL-1).
# Kept in step with ``tests/integration/test_artifact_stdio.py``; if the dependency set changes,
# a resolve change reddens ``check_artifact.py --assert-platform`` and this list is revisited.
NO_FALLBACK_EXTENSIONS = (
    "pydantic_core._pydantic_core",
    "rpds.rpds",
    "psycopg_binary",
    "greenlet._greenlet",
    "_cffi_backend",
    "cryptography.hazmat.bindings._rust",
)

# An LSP-framed JSON-RPC initialize request. The property under test at boot is what reaches
# stdout BEFORE the first frame, so the request is kept minimal.
_INIT_BODY = json.dumps(
    {
        "jsonrpc": "2.0",
        "id": 0,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "pex_spike", "version": "0"},
        },
    }
).encode("utf-8")
_INIT_FRAME = f"Content-Length: {len(_INIT_BODY)}\r\n\r\n".encode() + _INIT_BODY


def rec(kind: str, **fields: object) -> None:
    """Append one observation. Flushed immediately so a long run is inspectable in flight."""
    print(json.dumps({"kind": kind, "at": time.time(), **fields}), flush=True)


def _pex_interpreter(
    artifact: Path, script: str, *, timeout: float = 120.0
) -> subprocess.CompletedProcess[str]:
    """Run ``script`` inside the pex environment (``PEX_INTERPRETER=1``)."""
    return subprocess.run(  # noqa: S603 -- fixed argv, no shell
        [str(artifact), "-c", script],
        capture_output=True,
        text=True,
        timeout=timeout,
        env={**os.environ, "PEX_INTERPRETER": "1"},
    )


def q_boot(artifact: Path) -> bool:
    """Q-BOOT: the pex reaches a first JSON-RPC frame with nothing on stdout before it.

    Spawned with a scrubbed environment -- no ``SHELLBOX_*``, no ``PEX_*`` -- which is the
    first-deploy state AC-5 requires and the one where the P3 stdout property must hold.
    """
    env = {
        k: v
        for k, v in os.environ.items()
        if not (k.startswith("SHELLBOX_") or k.startswith("PEX_"))
    }
    env["PYTHONWARNINGS"] = "always"
    try:
        proc = subprocess.run(  # noqa: S603 -- fixed argv, no shell
            [str(artifact)], input=_INIT_FRAME, capture_output=True, env=env, timeout=120
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        rec("q_boot", ok=False, error=f"{type(exc).__name__}: {exc}")
        return False

    stdout = proc.stdout
    prefix = stdout.split(b"{", 1)[0]
    # Whatever is not a Content-Length framing header before the first '{' is noise on the wire.
    import re

    noise = re.sub(rb"Content-Length:\s*\d+\r?\n", b"", prefix)
    noise = re.sub(rb"\r?\n", b"", noise).strip()
    reached_frame = b"{" in stdout
    ok = reached_frame and not noise
    rec(
        "q_boot",
        ok=ok,
        reached_frame=reached_frame,
        stdout_noise_before_frame=noise.decode("utf-8", errors="replace"),
        returncode=proc.returncode,
        stderr_tail=proc.stderr.decode("utf-8", errors="replace")[-500:],
    )
    return ok


def q_exts(artifact: Path) -> bool:
    """Q-EXTS: every no-fallback extension imports from inside the pex (CRITICAL-1's load proof)."""
    script = (
        "import json, importlib\n"
        f"mods = {list(NO_FALLBACK_EXTENSIONS)!r}\n"
        "out = {}\n"
        "for name in mods:\n"
        "    try:\n"
        "        importlib.import_module(name); out[name] = 'ok'\n"
        "    except Exception as exc:\n"
        "        out[name] = f'{type(exc).__name__}: {exc}'\n"
        "print(json.dumps(out))\n"
    )
    result = _pex_interpreter(artifact, script)
    if result.returncode != 0:
        rec("q_exts", ok=False, error=result.stderr[-500:])
        return False
    outcomes = json.loads(result.stdout)
    broken = {name: outcome for name, outcome in outcomes.items() if outcome != "ok"}
    rec("q_exts", ok=not broken, outcomes=outcomes)
    return not broken


def q_startup(artifact: Path) -> None:
    """Q-STARTUP (recorded): which extensions load on the import path, confirming M-C1 in CI.

    Imports ``shellbox_mcp.cli`` and ``shellbox_mcp.server`` inside the pex and reports which of
    the no-fallback extensions ended up in ``sys.modules``. The expectation from M-C1 is that only
    ``pydantic_core`` (and ``rpds``) are present -- which is the entire reason AC-17 has to force
    the rest to load by a separate import. A finding that MORE load here is good news, not a
    failure, so this is recorded, not gated.
    """
    script = (
        "import json, sys\n"
        "import shellbox_mcp.cli, shellbox_mcp.server  # noqa: F401\n"
        f"mods = {list(NO_FALLBACK_EXTENSIONS)!r}\n"
        "loaded = {n: (n.split('.')[0] in sys.modules or n in sys.modules) for n in mods}\n"
        "print(json.dumps(loaded))\n"
    )
    result = _pex_interpreter(artifact, script)
    rec(
        "q_startup",
        rc=result.returncode,
        loaded=json.loads(result.stdout) if result.returncode == 0 else None,
        stderr_tail=result.stderr[-500:],
    )


def q_reexec(artifact: Path) -> None:
    """Q-REEXEC (recorded): ``sys.executable`` and the running version inside the pex.

    pex may re-exec into an interpreter that satisfies its ``--interpreter-constraint`` rather
    than run under the one the shebang resolved. This records what actually ran, so the claim
    "starts on the target's 3.12" is measured, not assumed (the critic's V2-P4).
    """
    script = (
        "import json, sys\n"
        "print(json.dumps({'executable': sys.executable, "
        "'version': '.'.join(str(p) for p in sys.version_info[:3])}))\n"
    )
    result = _pex_interpreter(artifact, script)
    rec(
        "q_reexec",
        rc=result.returncode,
        info=json.loads(result.stdout) if result.returncode == 0 else None,
        stderr_tail=result.stderr[-500:],
    )


def q_stream(artifact: Path) -> None:
    """Q-STREAM (recorded): does pex's bootstrap keep stdout clean when it is verbose?

    Runs the boot with ``PEX_VERBOSE=9`` and records whether the extra diagnostics land on stderr
    (correct -- stdout is the wire) or leak to stdout. This is the field-diagnosability question:
    if pex's own chatter can reach stdout, ``--no-emit-warnings`` in the build is load-bearing.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("SHELLBOX_")}
    env["PEX_VERBOSE"] = "9"
    try:
        proc = subprocess.run(  # noqa: S603 -- fixed argv, no shell
            [str(artifact)], input=_INIT_FRAME, capture_output=True, env=env, timeout=120
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        rec("q_stream", error=f"{type(exc).__name__}: {exc}")
        return
    prefix = proc.stdout.split(b"{", 1)[0]
    rec(
        "q_stream",
        stdout_bytes_before_frame=len(prefix),
        stderr_bytes=len(proc.stderr),
        verbose_leaked_to_stdout=bool(prefix.strip()),
    )


def q_concurrency(artifact: Path, workers: int = 8) -> None:
    """Q-CONCURRENCY (recorded): a lightweight cold-start race, as a smoke of the extraction lock.

    The full 32-way budgeted assertion is ``test_thirty_two_cold_starts_all_reach_tools_list``;
    this is a smaller, cheaper confirmation that concurrent boots against a cold cache do not
    deadlock or corrupt, recording the slowest. Each worker gets a private ``HOME`` so its pex
    cache starts cold. Runs the interpreter (not the server) to isolate the extraction path.
    """
    import tempfile

    def one(index: int) -> dict[str, object]:
        home = Path(tempfile.mkdtemp(prefix=f"pexspike-{index}-"))
        env = {**os.environ, "HOME": str(home), "PEX_INTERPRETER": "1"}
        started = time.monotonic()
        proc = subprocess.run(  # noqa: S603 -- fixed argv, no shell
            [str(artifact), "-c", "import pydantic_core, rpds; print('ok')"],
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
        )
        return {"index": index, "rc": proc.returncode, "elapsed": time.monotonic() - started}

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(one, range(workers)))
    failures = [r for r in results if r["rc"] != 0]
    rec(
        "q_concurrency",
        workers=workers,
        all_ok=not failures,
        slowest=max((float(r["elapsed"]) for r in results), default=0.0),
        failures=failures,
    )


def main() -> int:
    raw = os.environ.get("SHELLBOX_ARTIFACT_PATH")
    if not raw:
        rec(
            "spike_skipped",
            reason="SHELLBOX_ARTIFACT_PATH unset; no built artifact to probe (build is CI-only)",
        )
        print(
            "pex_spike: SHELLBOX_ARTIFACT_PATH unset -- nothing to probe. In CI the artifact job "
            "builds dist/shellbox and sets this. Skipping (exit 0).",
            file=sys.stderr,
        )
        return 0

    artifact = Path(raw)
    rec("spike_start", artifact=str(artifact), exists=artifact.is_file())
    if not artifact.is_file():
        print(f"pex_spike: {artifact} does not exist", file=sys.stderr)
        return 1

    # Gated questions first; recorded ones after, so a failure fails fast and legibly.
    boot_ok = q_boot(artifact)
    exts_ok = q_exts(artifact)

    q_startup(artifact)
    q_reexec(artifact)
    q_stream(artifact)
    q_concurrency(artifact)

    rec("spike_done", boot_ok=boot_ok, exts_ok=exts_ok)

    if not (boot_ok and exts_ok):
        print(
            "pex_spike: a GATED assumption failed -- see the q_boot / q_exts records above. The "
            "artifact's acceptance gates rest on these, so this is a build or bundle defect, not "
            "a spike bug.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
