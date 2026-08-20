# PEX-FINDINGS — what `spike/pex_spike.py` measures, and what it found

The artifact's acceptance gates (shellbox#21) rest on how a `pex` boots and extracts. None of it
can be measured on a developer machine: the build is CI-only (pex's vendored pip runs `--isolated`
and `pypi.org` is blackholed here — see `scripts/build_artifact.sh`), so there is no local artifact
to probe. `spike/pex_spike.py` is the committed oracle; the artifact CI job runs it against the
freshly built `dist/shellbox` and gates on its exit code, exactly as `spike/tmux_spike.py` gates the
tmux adapter.

This file is the prose companion. The **measured numbers below are placeholders** until the first
green artifact run fills them in from the job's JSONL — the same discipline
`probe/sandbox-identity-results.jsonl` follows: a claim here must cite a record the spike emitted,
not a guess.

## The questions, and why each is here

| Q | Gated? | Property | Why it cannot be assumed |
|---|---|---|---|
| Q-BOOT | **yes** | the single-platform pex reaches a JSON-RPC frame with nothing on stdout before it | the whole form's viability + P3; a pex writes its own bootstrap output somewhere |
| Q-EXTS | **yes** | the six no-pure-fallback extensions import from inside the pex | CRITICAL-1: a wrong-glibc `psycopg_binary`/`cryptography` is never touched by `initialize` |
| Q-STARTUP | recorded | which extensions load on the *import* path with no `SHELLBOX_*` set | confirms M-C1 in CI — only `pydantic_core`+`rpds` load, which is *why* AC-17 exists |
| Q-REEXEC | recorded | `sys.executable` + version inside the pex | pex may re-exec to satisfy its interpreter constraint (critic V2-P4) |
| Q-STREAM | recorded | does `PEX_VERBOSE` chatter stay off stdout | if pex can leak to stdout, `--no-emit-warnings` in the build is load-bearing |
| Q-CONCURRENCY | recorded | a small cold-start race does not deadlock or corrupt | the budgeted 32-way assertion lives in `test_artifact_stdio.py`; this is a cheap confirmation |

## Measured (FILL FROM THE FIRST GREEN CI RUN)

- **Platform / build tag:** `manylinux_2_17_x86_64`, glibc floor 2.17 (Step 0 sidecar
  `probe/artifact-platform.json`; sandbox measured at glibc 2.39).
- **Q-BOOT:** _[rc, reached_frame, stdout_noise — from the `q_boot` record]_
- **Q-EXTS:** _[per-module outcomes — from the `q_exts` record]_
- **Q-STARTUP:** _[which of the six were in `sys.modules` after importing cli+server]_
- **Q-REEXEC:** _[`sys.executable` and version — did pex re-exec off `/usr/bin/python3`?]_
- **Q-STREAM:** _[stdout bytes before frame under `PEX_VERBOSE=9`; did anything leak?]_
- **Q-CONCURRENCY:** _[slowest of the N cold starts; any failures]_
- **Unzipped size / `.so` count:** _[from the MANIFEST `extension_modules` and the built file]_

## The one hazard this spike demonstrates rather than gates

CRITICAL-1 says a broken bundled `psycopg_binary` would pass every other smoke gate and degrade
silently in the field. Q-EXTS is the positive proof the extension loads; the *inverse* — that a
deliberately corrupted extension goes green through `initialize`/`tools/list`/round-trip — is the
hazard AC-17(b) exists to catch, and `test_a_configured_registry_is_not_degraded_through_the_artifact`
is the gate that catches it (it asserts the `server.py:349-353` WARNING did **not** fire against a
real postgres). The spike does not corrupt the bundle itself; it records that the extensions load,
and the registry round-trip test proves the non-degraded path.
