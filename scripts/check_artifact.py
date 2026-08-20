#!/usr/bin/env python3
"""The static assertions on a BUILT release artifact -- the single `dist/shellbox` file that
`scripts/build_artifact.sh` produces and the release lane publishes (shellbox#21).

Each mode is one falsifiable check with its own exit status, run as a step of the artifact smoke
lane and, for the no-build modes, runnable by hand against an artifact downloaded from a CI run.
`tests/unit/test_check_artifact.py` mutates a synthetic artifact and proves each mode reddens.

WHY A CHECKER AND NOT "it imports": the issue is explicit that "imports cleanly" is not enough. A
pex whose bundled `psycopg_binary` is built for the wrong glibc still imports its pure-Python
layer, still answers `initialize`, and then silently degrades to `NullRegistry` in the field
(`server.py:338-354` catches the driver `ImportError` at WARNING; `enroll.py:143-146` catches the
SDK one at DEBUG). None of that is visible with no `SHELLBOX_*` set -- the exact environment the
smoke tests require. So the properties that make the artifact correct are structural, and this
script is where they are asserted.

THE ARTIFACT SHAPE this reads, stated so a reader can check the assumptions rather than reverse them
out of the code. A pex is a zip with a shebang line prepended; Python's `zipfile` opens it directly
because the central directory sits at the end. Inside:

  * `PEX-INFO`            -- pex's own JSON manifest (distributions, settings), read by
                            `--assert-no-resolver`.
  * `SHELLBOX-MANIFEST.json` -- OUR manifest, written by `build_artifact.sh`, the source of truth
                            for `--assert-manifest`, `--assert-websockets-pin`, `--assert-hosts`,
                            `--assert-release-notes`, and AC-17's `.so` list. Schema below.
  * `.deps/<dist>/...*.so` -- the bundled extension modules. `--assert-glibc-ceiling` reads them.

SHELLBOX-MANIFEST.json schema (v1), which `build_artifact.sh` and this script must agree on:

  {
    "schema": 1,
    "name": "shellbox", "server_name": "shellbox", "version": "0.1.0",
    "git_sha": "<40 hex>", "build_date": "<ISO 8601>",
    "python_floor": "3.12",
    "build_platform_tag": "manylinux_2_17_x86_64",
    "websockets": "15.0.1",
    "distributions": [{"name","version","tag","hash","url"}, ...],
    "extension_modules": [".deps/pydantic_core/.../_pydantic_core.cpython-312-...so", ...],
    "release_notes": {"python_floor","websockets","platform_tag","glibc_ceiling","git_sha",
                      "unzipped_size_bytes","cold_start_seconds"}
  }

STEP 0 DEPENDENCY (shellbox#21 blocking item 4): `--assert-platform` and `--assert-glibc-ceiling`
compare against `probe/artifact-platform.json`, the sidecar the live-sandbox probe
(`probe/probe_identity.py` `architecture` lane) produces. Until that file is committed, those two
modes FAIL with a "Step 0 must land" message rather than encoding a guessed platform tag. Every
other mode is independent of Step 0.

CI-ONLY modes: `--assert-hosts` mirrors `scripts/check_lockfile.py`'s discipline -- on a
mirror-configured machine a lock/manifest legitimately names the mirror, so this must not run in
`make lint`. `--assert-silent-start` spawns the artifact, so it needs a real built file.

CRITICAL, the defect this repo has shipped more than once (`scripts/check_lockfile.py`,
`scripts/check_bundle_statics.py`, `scripts/check_pex_lock.py` all carry the warning): every mode
asserts its input EXISTS, then asserts it FOUND the thing it inspects, and only then inspects it.
The obvious `grep`-and-fail-on-match shape PASSES on a missing file and on an empty one. Allowlists,
never blocklists; exact comparisons, never substring tests; measured floors with a date and a
command beside them.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

# --- the contract constants ------------------------------------------------------------------

SHEBANG = "#!/usr/bin/env python3"
MANIFEST_NAME = "SHELLBOX-MANIFEST.json"
PEX_INFO_NAME = "PEX-INFO"

# The one host an artifact URL may name. Exact comparison against the URL's host.
ALLOWED_HOST = "files.pythonhosted.org"

# The `websockets` range the bundle must pin inside. It is `packages/shellbox-mcp/pyproject.toml`'s
# `websockets>=14.2,<16`, restated here as the checkable bound. If that dependency changes, this
# changes with it -- and the mismatch is itself a finding.
WEBSOCKETS_MIN = (14, 2)
WEBSOCKETS_MAX_EXCLUSIVE = (16,)

# Resolver packages that must NOT be in a runtime pex: their presence means the artifact can reach
# for an index at run time, which is exactly Principle 5's failure ("the install must not resolve").
FORBIDDEN_DISTS = ("pip", "setuptools", "wheel")

# The Step 0 sidecar. Its absence is what makes the two platform modes fail with a legible message.
# The path is overridable by env only so the mutation test can point at a synthetic sidecar without
# writing into the repo; the build and CI use the committed default.
_REPO_ROOT = Path(__file__).resolve().parents[1]
ARTIFACT_PLATFORM_SIDECAR = Path(
    os.environ.get(
        "SHELLBOX_ARTIFACT_PLATFORM_SIDECAR", str(_REPO_ROOT / "probe" / "artifact-platform.json")
    )
)


def _fail(message: str) -> int:
    """Print a GitHub-annotated error and return the failing exit status."""
    print(f"::error::{message}", file=sys.stderr)
    return 1


# --- reading the artifact --------------------------------------------------------------------


def _first_line(path: Path) -> str | int:
    """The artifact's first line, or a failing status with the reason printed."""
    if not path.is_file():
        return _fail(f"{path} does not exist, so there is nothing to check. Build it first.")
    with path.open("rb") as handle:
        raw = handle.readline(256)
    if not raw:
        return _fail(f"{path} is empty.")
    return raw.decode("utf-8", errors="replace").rstrip("\n")


def _open_zip(path: Path) -> zipfile.ZipFile | int:
    """The artifact opened as a zip, or a failing status. A pex is a zip with a prepended shebang;
    `zipfile` finds the central directory at the end regardless of the prefix."""
    if not path.is_file():
        return _fail(f"{path} does not exist, so there is nothing to check. Build it first.")
    if path.stat().st_size == 0:
        return _fail(f"{path} is empty.")
    try:
        return zipfile.ZipFile(path)
    except zipfile.BadZipFile:
        # A truncated artifact usually stops being a valid zip, so this is a check, not merely
        # input validation.
        return _fail(f"{path} is not a valid zip, so it is not a pex artifact (truncated build?).")


def _read_member(archive: zipfile.ZipFile, name: str) -> bytes | None:
    try:
        return archive.read(name)
    except KeyError:
        return None


def _load_manifest(archive: zipfile.ZipFile) -> dict[str, Any] | int:
    raw = _read_member(archive, MANIFEST_NAME)
    if raw is None:
        return _fail(
            f"the artifact holds no {MANIFEST_NAME}. build_artifact.sh must embed it; without it "
            f"the version, the pinned websockets and the bundled-extension list are all "
            f"unanswerable from the artifact alone."
        )
    try:
        document = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        return _fail(f"{MANIFEST_NAME} is not valid JSON: {error}")
    if not isinstance(document, dict):
        return _fail(f"{MANIFEST_NAME} is not a JSON object.")
    return document


def _distributions(manifest: dict[str, Any]) -> list[dict[str, Any]] | int:
    dists = manifest.get("distributions")
    if not isinstance(dists, list) or not dists:
        return _fail(
            f"{MANIFEST_NAME} lists no distributions, so there is nothing to check. Either the "
            f"build produced an empty manifest, or the schema moved."
        )
    return dists


# --- modes -----------------------------------------------------------------------------------


def assert_shebang(path: Path) -> int:
    """The first line must be exactly the portable shebang; `buzz-acp` spawns the file by path."""
    line = _first_line(path)
    if isinstance(line, int):
        return line
    if line != SHEBANG:
        return _fail(
            f"the first line of {path} is {line!r}, not {SHEBANG!r}. The consumer spawns the file "
            f"directly, so the shebang is load-bearing, and an env-python3 shebang is what lets it "
            f"run on the sandbox and in CI's container alike."
        )
    print(f"OK: {path} starts with {SHEBANG!r}.")
    return 0


def assert_executable(path: Path) -> int:
    """`extra_binaries` chmod +x's the file, but a build that dropped the bit ships something the
    consumer must repair; assert the artifact already carries it."""
    if not path.is_file():
        return _fail(f"{path} does not exist, so there is nothing to check. Build it first.")
    mode = path.stat().st_mode
    if not mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH):
        return _fail(f"{path} is not executable (mode {stat.filemode(mode)}).")
    print(f"OK: {path} is executable ({stat.filemode(mode)}).")
    return 0


def assert_sha256(path: Path) -> int:
    """The sibling `.sha256` must match the artifact, in `sha256sum -c` format."""
    if not path.is_file():
        return _fail(f"{path} does not exist, so there is nothing to check. Build it first.")
    sidecar = path.with_name(path.name + ".sha256")
    if not sidecar.is_file():
        return _fail(
            f"{sidecar} does not exist. build_artifact.sh must write it beside {path.name}."
        )
    text = sidecar.read_text().strip()
    if not text:
        return _fail(f"{sidecar} is empty.")
    # `sha256sum -c` format: "<64 hex>␣␣<filename>". Parse the hash and, if present, check the name.
    match = re.match(r"^([0-9a-f]{64})\s+\*?(.+)$", text)
    if not match:
        return _fail(
            f"{sidecar} is not in 'sha256sum -c' format (<64 hex> two-spaces <filename>): {text!r}"
        )
    recorded_hash, recorded_name = match.group(1), match.group(2).strip()
    if recorded_name != path.name:
        return _fail(
            f"{sidecar} names {recorded_name!r}, but the artifact is {path.name!r}. "
            f"'sha256sum -c' would look for the wrong file."
        )
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != recorded_hash:
        return _fail(
            f"{path} sha256 is {digest}, but {sidecar} records {recorded_hash}. The artifact and "
            f"its checksum disagree -- the file changed after the checksum was written, or vice "
            f"versa. This is exactly what the consumer's pin would reject."
        )
    print(f"OK: {path} matches {sidecar.name} ({digest}).")
    return 0


def assert_manifest(path: Path) -> int:
    """The manifest is present, valid, and holds exactly one version per distribution.

    Two versions of one distribution in a single artifact is a resolve or a merge gone wrong, and it
    is the kind of thing that boots fine and then imports the wrong one; assert it cannot happen.
    """
    archive = _open_zip(path)
    if isinstance(archive, int):
        return archive
    with archive:
        manifest = _load_manifest(archive)
        if isinstance(manifest, int):
            return manifest
        dists = _distributions(manifest)
        if isinstance(dists, int):
            return dists

        seen: dict[str, set[str]] = {}
        for entry in dists:
            name = str(entry.get("name", "")).strip().lower()
            version = str(entry.get("version", "")).strip()
            if not name or not version:
                return _fail(
                    f"{MANIFEST_NAME} has a distribution with no name or version: {entry!r}"
                )
            seen.setdefault(name, set()).add(version)

        multi = {name: sorted(versions) for name, versions in seen.items() if len(versions) > 1}
        if multi:
            return _fail(f"{MANIFEST_NAME} lists more than one version of a distribution: {multi}")

    print(f"OK: {MANIFEST_NAME} lists {len(seen)} distributions, one version each.")
    return 0


def _parse_version(text: str) -> tuple[int, ...]:
    """The leading dotted-integer part of a version, for range comparison: `15.0.1` -> (15, 0, 1)."""  # noqa: E501
    parts: list[int] = []
    for chunk in text.split("."):
        match = re.match(r"^(\d+)", chunk)
        if not match:
            break
        parts.append(int(match.group(1)))
    return tuple(parts)


def assert_websockets_pin(path: Path) -> int:
    """The bundled `websockets` version is recorded and lies inside `>=14.2,<16`."""
    archive = _open_zip(path)
    if isinstance(archive, int):
        return archive
    with archive:
        manifest = _load_manifest(archive)
        if isinstance(manifest, int):
            return manifest
        version = str(manifest.get("websockets", "")).strip()
        if not version:
            return _fail(f"{MANIFEST_NAME} records no 'websockets' version.")
        parsed = _parse_version(version)
        if not parsed:
            return _fail(f"{MANIFEST_NAME} 'websockets' value {version!r} is not a version.")
        if not (WEBSOCKETS_MIN <= parsed < WEBSOCKETS_MAX_EXCLUSIVE):
            lo = ".".join(map(str, WEBSOCKETS_MIN))
            hi = ".".join(map(str, WEBSOCKETS_MAX_EXCLUSIVE))
            return _fail(
                f"the bundled websockets {version} is outside >={lo},<{hi}, the range "
                f"packages/shellbox-mcp/pyproject.toml pins. transport.py sets its keepalives "
                f"for that range; a version outside it is untested here."
            )
    print(f"OK: bundled websockets {version} is inside the pinned range.")
    return 0


def assert_hosts(path: Path) -> int:
    """Every distribution URL host is exactly the public one. CI-only (see the header)."""
    archive = _open_zip(path)
    if isinstance(archive, int):
        return archive
    with archive:
        manifest = _load_manifest(archive)
        if isinstance(manifest, int):
            return manifest
        dists = _distributions(manifest)
        if isinstance(dists, int):
            return dists

        urls = [(entry.get("name", "?"), str(entry["url"])) for entry in dists if entry.get("url")]
        if not urls:
            return _fail(
                f"{MANIFEST_NAME} records no distribution URLs at all, so their provenance cannot "
                f"be checked. build_artifact.sh must record the resolved URL per distribution."
            )
        offenders = []
        for name, url in urls:
            host = urlsplit(url).hostname or ""
            if host != ALLOWED_HOST:
                offenders.append((name, host or "(no host)", url))
        if offenders:
            for name, host, url in offenders[:10]:
                print(f"  {name}: host {host!r} in {url}", file=sys.stderr)
            hosts = sorted({host for _, host, _ in offenders})
            return _fail(
                f"{len(offenders)} of {len(urls)} distribution URLs name a host other than "
                f"{ALLOWED_HOST}: {', '.join(hosts)}. The artifact was built from a lock resolved "
                f"through another index; the mirror→public rewrite (build_artifact.sh) did not run "
                f"or did not reach every URL."
            )
    print(f"OK: all {len(urls)} distribution URLs are on {ALLOWED_HOST}.")
    return 0


def assert_no_resolver(path: Path) -> int:
    """PEX-INFO must not carry a resolver, and must not inherit the ambient environment.

    Principle 5: the install must not resolve. A runtime pex that bundled `pip`/`setuptools`, or
    that set `inherit_path`, could reach an index or the host's site-packages at run time.
    """
    archive = _open_zip(path)
    if isinstance(archive, int):
        return archive
    with archive:
        raw = _read_member(archive, PEX_INFO_NAME)
        if raw is None:
            return _fail(f"the artifact holds no {PEX_INFO_NAME}, so it is not a pex.")
        try:
            pex_info = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            return _fail(f"{PEX_INFO_NAME} is not valid JSON: {error}")

        # `distributions` in PEX-INFO maps "<dist>-<ver>-<tag>.whl" -> hash. A forbidden resolver
        # dist shows up as a key prefix.
        dist_keys = list(pex_info.get("distributions", {}))
        present = sorted(
            {
                forbidden
                for forbidden in FORBIDDEN_DISTS
                for key in dist_keys
                if re.match(rf"^{forbidden}-\d", key.lower())
            }
        )
        if present:
            return _fail(
                f"{PEX_INFO_NAME} bundles resolver packages {present}. A runtime artifact must not "
                f"carry pip/setuptools/wheel -- their presence is a path to resolving at run time, "
                f"which the install is required never to do."
            )

        # `inherit_path` must be off ("false" or "fallback" both leak the host env; require the
        # string "false"). This is read explicitly rather than trusted-by-construction.
        inherit = pex_info.get("inherit_path")
        if inherit not in (False, "false"):
            return _fail(
                f"{PEX_INFO_NAME} sets inherit_path={inherit!r}. It must be false, or the artifact "
                f"can import from the host's site-packages and stop being self-contained."
            )
    print(f"OK: {PEX_INFO_NAME} bundles no resolver and does not inherit the host path.")
    return 0


def assert_release_notes(path: Path) -> int:
    """The generated release notes agree field-by-field with the manifest.

    AC-2: the release notes state the Python floor and the pinned versions. Generating them from the
    manifest is not enough on its own -- a hand-edit could drift -- so the artifact carries the
    notes fields and this asserts they still equal their manifest sources.
    """
    archive = _open_zip(path)
    if isinstance(archive, int):
        return archive
    with archive:
        manifest = _load_manifest(archive)
        if isinstance(manifest, int):
            return manifest
        notes = manifest.get("release_notes")
        if not isinstance(notes, dict) or not notes:
            return _fail(
                f"{MANIFEST_NAME} carries no 'release_notes' object, so the published notes cannot "
                f"be checked against their source."
            )
        # Each notes field must equal the manifest field it is generated from.
        pairs = {
            "python_floor": manifest.get("python_floor"),
            "websockets": manifest.get("websockets"),
            "platform_tag": manifest.get("build_platform_tag"),
            "git_sha": manifest.get("git_sha"),
        }
        drift = {
            field: {"notes": notes.get(field), "manifest": source}
            for field, source in pairs.items()
            if notes.get(field) != source
        }
        if drift:
            return _fail(f"release notes disagree with the manifest: {drift}")
    print(f"OK: release notes agree with {MANIFEST_NAME} on {sorted(pairs)}.")
    return 0


# --- Step 0-dependent modes ------------------------------------------------------------------


def _load_sidecar() -> dict[str, Any] | int:
    """The committed Step 0 sidecar, or a failing status naming Step 0 as the blocker."""
    if not ARTIFACT_PLATFORM_SIDECAR.is_file():
        return _fail(
            f"{ARTIFACT_PLATFORM_SIDECAR} does not exist yet. It is produced by Step 0's "
            f"live-sandbox probe (probe/probe_identity.py 'architecture' lane) and names the build "
            f"platform tag and the measured glibc ceiling. This check activates once that file is "
            f"committed; until then it cannot run, and must not guess a platform tag."
        )
    try:
        document = json.loads(ARTIFACT_PLATFORM_SIDECAR.read_bytes())
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        return _fail(f"{ARTIFACT_PLATFORM_SIDECAR} is not valid JSON: {error}")
    if not isinstance(document, dict):
        return _fail(f"{ARTIFACT_PLATFORM_SIDECAR} is not a JSON object.")
    return document


def assert_platform(path: Path) -> int:
    """Every bundled distribution's wheel tag is the single allowed build tag or a pure-Python tag.

    AC-10(a). A count of distributions is NOT the load-bearing check -- a resolve that silently
    produced a pure-Python-only set clears any count. The exact-tag allowlist is what catches a
    resolve that produced a different platform than the one Step 0 measured.
    """
    sidecar = _load_sidecar()
    if isinstance(sidecar, int):
        return sidecar
    build_tag = str(sidecar.get("build_platform_tag", "")).strip()
    if not build_tag:
        return _fail(f"{ARTIFACT_PLATFORM_SIDECAR} names no 'build_platform_tag'.")
    allowed = {build_tag, "py3-none-any", "none-any", "py2.py3-none-any"}

    archive = _open_zip(path)
    if isinstance(archive, int):
        return archive
    with archive:
        manifest = _load_manifest(archive)
        if isinstance(manifest, int):
            return manifest
        dists = _distributions(manifest)
        if isinstance(dists, int):
            return dists
        offenders = [
            (entry.get("name", "?"), str(entry.get("tag", "")))
            for entry in dists
            if str(entry.get("tag", "")) not in allowed
        ]
        if offenders:
            for name, tag in offenders[:10]:
                print(f"  {name}: tag {tag!r}", file=sys.stderr)
            return _fail(
                f"{len(offenders)} of {len(dists)} distributions carry a tag that is neither the "
                f"build tag {build_tag!r} nor a pure-Python tag. The resolve produced a different "
                f"platform than Step 0 measured, or a dist changed its wheel matrix upstream."
            )
    print(f"OK: all {len(dists)} distributions carry {build_tag!r} or a pure-Python tag.")
    return 0


_GLIBC_SYMBOL = re.compile(rb"GLIBC_(\d+)\.(\d+)")


def assert_glibc_ceiling(path: Path) -> int:
    """No bundled `.so` requires a glibc newer than the ceiling Step 0 measured on the sandbox.

    AC-10(b). The invariant that actually protects the install, measured at the ELF level rather
    than derived from a wheel tag (a mis-tagged wheel would pass a tag check and fail here). Needs
    `objdump`; extraction is to a temp dir and cleaned up.

    SCOPE, per Step 0's own note: the ceiling is measured on ONE sandbox in ONE region. The build
    targets a floor at or below it precisely so a single-host measurement is not load-bearing for
    portability -- this check only asserts the build honored that floor.
    """
    sidecar = _load_sidecar()
    if isinstance(sidecar, int):
        return sidecar
    ceiling_text = str(sidecar.get("glibc", "")).strip()
    ceiling = _parse_version(ceiling_text)
    if len(ceiling) < 2:
        return _fail(
            f"{ARTIFACT_PLATFORM_SIDECAR} 'glibc' value {ceiling_text!r} is not an x.y version. "
            f"Step 0 records the sandbox's platform.libc_ver() there."
        )
    ceiling = ceiling[:2]

    if not _which("objdump"):
        return _fail(
            "objdump is not on PATH, so the ELF-level glibc symbols cannot be read. This mode is "
            "CI-only for that reason; install binutils to run it locally."
        )

    archive = _open_zip(path)
    if isinstance(archive, int):
        return archive
    with archive:
        so_names = [name for name in archive.namelist() if name.endswith(".so")]
        if not so_names:
            return _fail(
                f"{path} holds no .so files, so there are no extension modules to check. Either "
                f"the build bundled none (it must -- pydantic_core and rpds have no pure fallback) "
                f"or the layout moved."
            )
        worst = (0, 0)
        worst_where = ""
        with tempfile.TemporaryDirectory() as tmp:
            for name in so_names:
                extracted = Path(archive.extract(name, tmp))
                result = subprocess.run(
                    ["objdump", "-T", str(extracted)],
                    capture_output=True,
                    timeout=60,
                )
                for major, minor in _GLIBC_SYMBOL.findall(result.stdout):
                    version = (int(major), int(minor))
                    if version > worst:
                        worst, worst_where = version, name
        if worst > ceiling:
            return _fail(
                f"a bundled extension requires glibc {worst[0]}.{worst[1]} "
                f"({worst_where}), newer than the sandbox's measured ceiling "
                f"{ceiling[0]}.{ceiling[1]}. This artifact would fail at import on the target with "
                f"a 'version GLIBCXYZ not found' error -- the illegible failure the build tag "
                f"exists to prevent."
            )
    print(
        f"OK: the highest glibc any bundled .so requires is {worst[0]}.{worst[1]}, "
        f"at or below the measured ceiling {ceiling[0]}.{ceiling[1]}."
    )
    return 0


def assert_silent_start(path: Path) -> int:
    """The artifact writes ZERO bytes to stdout before the first JSON-RPC frame, started with no
    `SHELLBOX_*` set and no `$HOME/.shellbox`.

    Principle 4: stdout is the wire. This spawns the real artifact, so it needs a built file. The
    environment is scrubbed to the first-deploy state the issue requires, and PYTHONWARNINGS=always
    is set so a warning that would normally be suppressed still cannot reach stdout.
    """
    if not path.is_file():
        return _fail(f"{path} does not exist, so there is nothing to check. Build it first.")

    env = {k: v for k, v in os.environ.items() if not k.startswith("SHELLBOX_")}
    env["PYTHONWARNINGS"] = "always"
    # A JSON-RPC initialize frame over the LSP-style framing the server reads. Kept minimal: the
    # property under test is what reaches stdout BEFORE the first response byte, not the response.
    request = (
        '{"jsonrpc":"2.0","id":0,"method":"initialize","params":{"protocolVersion":"2025-06-18",'
        '"capabilities":{},"clientInfo":{"name":"check_artifact","version":"0"}}}'
    )
    body = request.encode("utf-8")
    framed = f"Content-Length: {len(body)}\r\n\r\n".encode() + body
    try:
        proc = subprocess.run(
            [str(path)],
            input=framed,
            capture_output=True,
            env=env,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return _fail(f"the artifact did not run: {error}")

    stdout = proc.stdout
    # The first frame is the first '{' of a JSON-RPC envelope, possibly after LSP framing headers.
    # Everything before it must be framing headers only -- never a stray print or warning.
    prefix = stdout.split(b"{", 1)[0]
    # Strip the legitimate Content-Length framing header from the prefix; whatever remains is noise.
    noise = re.sub(rb"Content-Length:\s*\d+\r?\n", b"", prefix)
    noise = re.sub(rb"\r?\n", b"", noise).strip()
    if noise:
        return _fail(
            f"the artifact wrote {noise!r} to stdout before the first JSON-RPC frame. stdout is "
            f"the wire; anything before the first frame corrupts the protocol. stderr was: "
            f"{proc.stderr[:500]!r}"
        )
    print("OK: nothing reached stdout before the first JSON-RPC frame.")
    return 0


def _which(name: str) -> str | None:
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        candidate = Path(directory) / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


# --- entry point -----------------------------------------------------------------------------

_MODES = {
    "--assert-shebang": assert_shebang,
    "--assert-executable": assert_executable,
    "--assert-sha256": assert_sha256,
    "--assert-manifest": assert_manifest,
    "--assert-websockets-pin": assert_websockets_pin,
    "--assert-hosts": assert_hosts,
    "--assert-no-resolver": assert_no_resolver,
    "--assert-release-notes": assert_release_notes,
    "--assert-platform": assert_platform,
    "--assert-glibc-ceiling": assert_glibc_ceiling,
    "--assert-silent-start": assert_silent_start,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__ and __doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    for flag, function in _MODES.items():
        mode.add_argument(
            flag,
            dest="mode",
            action="store_const",
            const=function,
            help=(function.__doc__ or "").splitlines()[0] if function.__doc__ else None,
        )
    parser.add_argument("path", type=Path, help="the built artifact, normally dist/shellbox")
    args = parser.parse_args(argv)
    return args.mode(args.path)


if __name__ == "__main__":
    sys.exit(main())
