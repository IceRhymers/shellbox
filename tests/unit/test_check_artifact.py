"""The mutation test for ``scripts/check_artifact.py`` -- each mode must fail on a broken artifact,
and the clean control must pass.

Same discipline as ``tests/unit/test_check_lockfile.py`` and ``tests/unit/test_check_pex_lock.py``:
a CI check is a claim, and the claim is only worth its line if it can be shown to redden. So each
case MUTATES a synthetic artifact and asserts the checker rejects it; the clean case proves the
mutated cases fail for the reason claimed rather than because the checker rejects everything.

FIXTURES ARE SYNTHESIZED, NEVER PEX-BUILT (shellbox#21 blocking item 7). A real ``pex`` build needs
an index, and per the checker's header the build is CI-only (pex's vendored pip runs ``--isolated``;
``pypi.org`` is blackholed on the author's box). A pex-built fixture would make this test unrunnable
in the lint lane it must live in. Instead each fixture is a shebang line prepended to a hand-built
zip holding a ``SHELLBOX-MANIFEST.json`` and a ``PEX-INFO`` -- which exercises the checker's parsing
and assertion logic without a resolver. The ELF-level ``--assert-glibc-ceiling`` path needs real
``.so`` files and ``objdump`` and so is exercised in CI against a real artifact; here only its
guard paths (absent sidecar, malformed glibc) are covered.

The checker is run as a SUBPROCESS, because the thing CI depends on is its exit status.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_artifact.py"

# Contract constants read from the script so the test cannot drift from the checker.
_spec = importlib.util.spec_from_file_location("check_artifact", _SCRIPT)
assert _spec is not None and _spec.loader is not None, f"cannot load {_SCRIPT}"
_checker = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_checker)

SHEBANG: str = _checker.SHEBANG
MANIFEST_NAME: str = _checker.MANIFEST_NAME
PEX_INFO_NAME: str = _checker.PEX_INFO_NAME
ALLOWED_HOST: str = _checker.ALLOWED_HOST

MIRROR_HOST = "pypi-proxy.dev.databricks.com"
BUILD_TAG = "manylinux_2_17_x86_64"


def _manifest(**overrides: Any) -> dict[str, Any]:
    """A clean manifest. Overrides replace top-level keys."""
    document: dict[str, Any] = {
        "schema": 1,
        "name": "shellbox",
        "server_name": "shellbox",
        "version": "0.1.0",
        "git_sha": "a" * 40,
        "build_date": "2026-08-19T00:00:00Z",
        "python_floor": "3.12",
        "build_platform_tag": BUILD_TAG,
        "websockets": "15.0.1",
        "distributions": [
            {
                "name": "pydantic-core",
                "version": "2.46.4",
                "tag": BUILD_TAG,
                "hash": "sha256:" + "0" * 64,
                "url": f"https://{ALLOWED_HOST}/packages/aa/bb/pydantic_core-2.46.4-{BUILD_TAG}.whl",
            },
            {
                "name": "mcp",
                "version": "1.29.0",
                "tag": "py3-none-any",
                "hash": "sha256:" + "1" * 64,
                "url": f"https://{ALLOWED_HOST}/packages/cc/dd/mcp-1.29.0-py3-none-any.whl",
            },
        ],
        "extension_modules": [".deps/pydantic_core/pydantic_core/_pydantic_core.so"],
        "release_notes": {
            "python_floor": "3.12",
            "websockets": "15.0.1",
            "platform_tag": BUILD_TAG,
            "git_sha": "a" * 40,
        },
    }
    document.update(overrides)
    return document


def _pex_info(**overrides: Any) -> dict[str, Any]:
    document: dict[str, Any] = {
        "pex_version": "2.100.4",
        "inherit_path": "false",
        "distributions": {
            "pydantic_core-2.46.4-cp312-cp312-manylinux_2_17_x86_64.whl": "hashhashhash",
            "mcp-1.29.0-py3-none-any.whl": "hashhashhash",
        },
    }
    document.update(overrides)
    return document


def _make_artifact(
    tmp_path: Path,
    *,
    shebang: str | None = SHEBANG,
    manifest: dict[str, Any] | None = "default",  # type: ignore[assignment]
    pex_info: dict[str, Any] | None = "default",  # type: ignore[assignment]
    so_members: tuple[str, ...] = (".deps/pydantic_core/pydantic_core/_pydantic_core.so",),
    executable: bool = True,
    write_sha256: bool = True,
    truncated: bool = False,
) -> Path:
    """A shebang line prepended to a hand-built zip -- the shape a pex has, without a pex build.

    ``manifest``/``pex_info`` default to the clean documents; pass ``None`` to omit the member, or a
    dict to replace it.
    """
    if manifest == "default":
        manifest = _manifest()
    if pex_info == "default":
        pex_info = _pex_info()

    zip_path = tmp_path / "_body.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        if manifest is not None:
            archive.writestr(MANIFEST_NAME, json.dumps(manifest))
        if pex_info is not None:
            archive.writestr(PEX_INFO_NAME, json.dumps(pex_info))
        for member in so_members:
            archive.writestr(member, b"\x7fELF-not-a-real-object")

    zip_bytes = zip_path.read_bytes()
    if truncated:
        zip_bytes = zip_bytes[: len(zip_bytes) // 2]

    artifact = tmp_path / "shellbox"
    prefix = f"{shebang}\n".encode() if shebang is not None else b""
    artifact.write_bytes(prefix + zip_bytes)
    if executable:
        artifact.chmod(0o755)
    else:
        artifact.chmod(0o644)

    if write_sha256:
        digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
        artifact.with_name("shellbox.sha256").write_text(f"{digest}  shellbox\n")

    return artifact


def _run(*args: str, sidecar: Path | None = None) -> subprocess.CompletedProcess[str]:
    env = None
    if sidecar is not None:
        import os

        env = {**os.environ, "SHELLBOX_ARTIFACT_PLATFORM_SIDECAR": str(sidecar)}
    return subprocess.run(
        [sys.executable, str(_SCRIPT), *args],
        capture_output=True,
        text=True,
        env=env,
    )


# --- shebang ----------------------------------------------------------------------------------


def test_a_clean_artifact_passes_shebang(tmp_path: Path) -> None:
    artifact = _make_artifact(tmp_path)
    result = _run("--assert-shebang", str(artifact))
    assert result.returncode == 0, result.stderr


def test_a_missing_artifact_fails_shebang(tmp_path: Path) -> None:
    result = _run("--assert-shebang", str(tmp_path / "absent"))
    assert result.returncode != 0
    assert "does not exist" in result.stderr


def test_a_wrong_shebang_fails(tmp_path: Path) -> None:
    artifact = _make_artifact(tmp_path, shebang="#!/usr/bin/python3")
    result = _run("--assert-shebang", str(artifact))
    assert result.returncode != 0
    assert "/usr/bin/python3" in result.stderr


def test_no_shebang_fails(tmp_path: Path) -> None:
    artifact = _make_artifact(tmp_path, shebang=None)
    result = _run("--assert-shebang", str(artifact))
    assert result.returncode != 0


# --- executable -------------------------------------------------------------------------------


def test_a_clean_artifact_passes_executable(tmp_path: Path) -> None:
    artifact = _make_artifact(tmp_path)
    result = _run("--assert-executable", str(artifact))
    assert result.returncode == 0, result.stderr


def test_a_non_executable_artifact_fails(tmp_path: Path) -> None:
    artifact = _make_artifact(tmp_path, executable=False)
    result = _run("--assert-executable", str(artifact))
    assert result.returncode != 0
    assert "not executable" in result.stderr


# --- sha256 -----------------------------------------------------------------------------------


def test_a_clean_artifact_passes_sha256(tmp_path: Path) -> None:
    artifact = _make_artifact(tmp_path)
    result = _run("--assert-sha256", str(artifact))
    assert result.returncode == 0, result.stderr


def test_a_missing_checksum_fails(tmp_path: Path) -> None:
    artifact = _make_artifact(tmp_path, write_sha256=False)
    result = _run("--assert-sha256", str(artifact))
    assert result.returncode != 0
    assert "does not exist" in result.stderr


def test_a_mismatched_checksum_fails(tmp_path: Path) -> None:
    artifact = _make_artifact(tmp_path)
    artifact.with_name("shellbox.sha256").write_text(f"{'f' * 64}  shellbox\n")
    result = _run("--assert-sha256", str(artifact))
    assert result.returncode != 0
    assert "disagree" in result.stderr


def test_a_checksum_naming_the_wrong_file_fails(tmp_path: Path) -> None:
    artifact = _make_artifact(tmp_path)
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    artifact.with_name("shellbox.sha256").write_text(f"{digest}  somethingelse\n")
    result = _run("--assert-sha256", str(artifact))
    assert result.returncode != 0
    assert "names" in result.stderr


def test_a_malformed_checksum_line_fails(tmp_path: Path) -> None:
    artifact = _make_artifact(tmp_path)
    artifact.with_name("shellbox.sha256").write_text("not a checksum line\n")
    result = _run("--assert-sha256", str(artifact))
    assert result.returncode != 0
    assert "format" in result.stderr


# --- manifest ---------------------------------------------------------------------------------


def test_a_clean_artifact_passes_manifest(tmp_path: Path) -> None:
    artifact = _make_artifact(tmp_path)
    result = _run("--assert-manifest", str(artifact))
    assert result.returncode == 0, result.stderr


def test_a_missing_manifest_fails(tmp_path: Path) -> None:
    artifact = _make_artifact(tmp_path, manifest=None)
    result = _run("--assert-manifest", str(artifact))
    assert result.returncode != 0
    assert MANIFEST_NAME in result.stderr


def test_a_truncated_artifact_fails_manifest(tmp_path: Path) -> None:
    artifact = _make_artifact(tmp_path, truncated=True)
    result = _run("--assert-manifest", str(artifact))
    assert result.returncode != 0
    assert "not a valid zip" in result.stderr


def test_two_versions_of_one_distribution_fails(tmp_path: Path) -> None:
    manifest = _manifest()
    manifest["distributions"].append(
        {
            "name": "pydantic-core",
            "version": "2.99.9",
            "tag": BUILD_TAG,
            "hash": "sha256:" + "2" * 64,
            "url": f"https://{ALLOWED_HOST}/packages/ee/ff/pydantic_core-2.99.9-{BUILD_TAG}.whl",
        }
    )
    artifact = _make_artifact(tmp_path, manifest=manifest)
    result = _run("--assert-manifest", str(artifact))
    assert result.returncode != 0
    assert "more than one version" in result.stderr


def test_an_empty_distribution_list_fails(tmp_path: Path) -> None:
    artifact = _make_artifact(tmp_path, manifest=_manifest(distributions=[]))
    result = _run("--assert-manifest", str(artifact))
    assert result.returncode != 0
    assert "no distributions" in result.stderr


# --- websockets pin ---------------------------------------------------------------------------


def test_a_clean_artifact_passes_websockets_pin(tmp_path: Path) -> None:
    artifact = _make_artifact(tmp_path)
    result = _run("--assert-websockets-pin", str(artifact))
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("version", ["16.0.0", "13.1", "14.1.9"])
def test_a_websockets_version_outside_the_range_fails(tmp_path: Path, version: str) -> None:
    artifact = _make_artifact(tmp_path, manifest=_manifest(websockets=version))
    result = _run("--assert-websockets-pin", str(artifact))
    assert result.returncode != 0
    assert version in result.stderr


def test_a_boundary_websockets_version_passes(tmp_path: Path) -> None:
    """14.2 is the inclusive lower bound and 15.x is in range; both must pass."""
    for version in ("14.2", "14.2.0", "15.0.1"):
        artifact = _make_artifact(tmp_path, manifest=_manifest(websockets=version))
        result = _run("--assert-websockets-pin", str(artifact))
        assert result.returncode == 0, f"{version}: {result.stderr}"


# --- hosts ------------------------------------------------------------------------------------


def test_a_clean_artifact_passes_hosts(tmp_path: Path) -> None:
    artifact = _make_artifact(tmp_path)
    result = _run("--assert-hosts", str(artifact))
    assert result.returncode == 0, result.stderr


def test_a_mirror_url_fails_hosts(tmp_path: Path) -> None:
    manifest = _manifest()
    manifest["distributions"][0]["url"] = (
        f"https://{MIRROR_HOST}/packages/aa/bb/pydantic_core-2.46.4-{BUILD_TAG}.whl"
    )
    artifact = _make_artifact(tmp_path, manifest=manifest)
    result = _run("--assert-hosts", str(artifact))
    assert result.returncode != 0
    assert MIRROR_HOST in result.stderr


def test_a_first_party_file_url_is_allowed_hosts(tmp_path: Path) -> None:
    """The three workspace wheels (R9) are recorded with `file://` URLs; they are first-party and
    allowed by scheme, while every remote dist must still be on the one public host. See OQ-7 in
    check_pex_lock.py."""
    manifest = _manifest()
    manifest["distributions"].append(
        {
            "name": "shellbox-mcp",
            "version": "0.1.0",
            "tag": "py3-none-any",
            "hash": "sha256:" + "9" * 64,
            "url": "file:///tmp/build/wheels/shellbox_mcp-0.1.0-py3-none-any.whl",
        }
    )
    artifact = _make_artifact(tmp_path, manifest=manifest)
    result = _run("--assert-hosts", str(artifact))
    assert result.returncode == 0, result.stderr
    assert "first-party" in result.stdout


def test_a_host_that_merely_contains_the_allowed_host_fails(tmp_path: Path) -> None:
    manifest = _manifest()
    manifest["distributions"][0]["url"] = (
        f"https://{ALLOWED_HOST}.example/packages/aa/bb/x.whl"
    )
    artifact = _make_artifact(tmp_path, manifest=manifest)
    result = _run("--assert-hosts", str(artifact))
    assert result.returncode != 0
    assert f"{ALLOWED_HOST}.example" in result.stderr


# --- no resolver ------------------------------------------------------------------------------


def test_a_clean_artifact_passes_no_resolver(tmp_path: Path) -> None:
    artifact = _make_artifact(tmp_path)
    result = _run("--assert-no-resolver", str(artifact))
    assert result.returncode == 0, result.stderr


def test_a_bundled_pip_fails_no_resolver(tmp_path: Path) -> None:
    pex_info = _pex_info()
    pex_info["distributions"]["pip-24.0-py3-none-any.whl"] = "hashhashhash"
    artifact = _make_artifact(tmp_path, pex_info=pex_info)
    result = _run("--assert-no-resolver", str(artifact))
    assert result.returncode != 0
    assert "pip" in result.stderr


def test_inherit_path_on_fails_no_resolver(tmp_path: Path) -> None:
    artifact = _make_artifact(tmp_path, pex_info=_pex_info(inherit_path="fallback"))
    result = _run("--assert-no-resolver", str(artifact))
    assert result.returncode != 0
    assert "inherit_path" in result.stderr


def test_a_missing_pex_info_fails_no_resolver(tmp_path: Path) -> None:
    artifact = _make_artifact(tmp_path, pex_info=None)
    result = _run("--assert-no-resolver", str(artifact))
    assert result.returncode != 0
    assert PEX_INFO_NAME in result.stderr


# --- release notes ----------------------------------------------------------------------------


def test_a_clean_artifact_passes_release_notes(tmp_path: Path) -> None:
    artifact = _make_artifact(tmp_path)
    result = _run("--assert-release-notes", str(artifact))
    assert result.returncode == 0, result.stderr


def test_release_notes_disagreeing_with_manifest_fails(tmp_path: Path) -> None:
    manifest = _manifest()
    manifest["release_notes"]["websockets"] = "9.9.9"  # drifted from the manifest's 15.0.1
    artifact = _make_artifact(tmp_path, manifest=manifest)
    result = _run("--assert-release-notes", str(artifact))
    assert result.returncode != 0
    assert "disagree" in result.stderr


def test_absent_release_notes_fails(tmp_path: Path) -> None:
    manifest = _manifest()
    del manifest["release_notes"]
    artifact = _make_artifact(tmp_path, manifest=manifest)
    result = _run("--assert-release-notes", str(artifact))
    assert result.returncode != 0
    assert "release_notes" in result.stderr


# --- platform (Step 0-dependent) --------------------------------------------------------------


def _sidecar(tmp_path: Path, **overrides: Any) -> Path:
    document = {
        "measured_on": "test-sandbox / fevm-west / us-west-2",
        "date": "2026-08-20",
        "machine": "x86_64",
        # The artifact's contract floor, not the sandbox glibc -- see check_artifact.py.
        "glibc": "2.17",
        "measured_sandbox_glibc": "2.39",
        "python": "cp312",
        "build_platform_tag": BUILD_TAG,
        "platform_tag_aliases": ["manylinux2014_x86_64"],
    }
    document.update(overrides)
    path = tmp_path / "artifact-platform.json"
    path.write_text(json.dumps(document))
    return path


def test_platform_fails_without_the_step0_sidecar(tmp_path: Path) -> None:
    """The guard that keeps the mode from guessing a tag before Step 0 lands."""
    artifact = _make_artifact(tmp_path)
    result = _run(
        "--assert-platform", str(artifact), sidecar=tmp_path / "does-not-exist.json"
    )
    assert result.returncode != 0
    assert "Step 0" in result.stderr


def test_a_clean_artifact_passes_platform_with_sidecar(tmp_path: Path) -> None:
    artifact = _make_artifact(tmp_path)
    result = _run("--assert-platform", str(artifact), sidecar=_sidecar(tmp_path))
    assert result.returncode == 0, result.stderr


def test_a_foreign_platform_tag_fails(tmp_path: Path) -> None:
    manifest = _manifest()
    manifest["distributions"][0]["tag"] = "manylinux_2_28_aarch64"
    artifact = _make_artifact(tmp_path, manifest=manifest)
    result = _run("--assert-platform", str(artifact), sidecar=_sidecar(tmp_path))
    assert result.returncode != 0
    assert "aarch64" in result.stderr


def test_the_manylinux2014_alias_is_accepted(tmp_path: Path) -> None:
    """`manylinux2014_x86_64` is the legacy alias of the `manylinux_2_17_x86_64` build tag -- the
    same glibc-2.17 floor, a different toolchain's spelling -- so a wheel tagged that way passes."""
    manifest = _manifest()
    manifest["distributions"][0]["tag"] = "manylinux2014_x86_64"
    artifact = _make_artifact(tmp_path, manifest=manifest)
    result = _run("--assert-platform", str(artifact), sidecar=_sidecar(tmp_path))
    assert result.returncode == 0, result.stderr


def test_a_higher_manylinux_minor_on_the_same_arch_fails(tmp_path: Path) -> None:
    """The invariant the alias must NOT swallow: `manylinux_2_28_x86_64` is the right arch but above
    the 2.17 floor, so it would raise the artifact's glibc requirement and must be caught."""
    manifest = _manifest()
    manifest["distributions"][0]["tag"] = "manylinux_2_28_x86_64"
    artifact = _make_artifact(tmp_path, manifest=manifest)
    result = _run("--assert-platform", str(artifact), sidecar=_sidecar(tmp_path))
    assert result.returncode != 0
    assert "manylinux_2_28_x86_64" in result.stderr


# --- glibc ceiling (Step 0-dependent; ELF path is CI-only) ------------------------------------


def test_glibc_ceiling_fails_without_the_step0_sidecar(tmp_path: Path) -> None:
    artifact = _make_artifact(tmp_path)
    result = _run(
        "--assert-glibc-ceiling", str(artifact), sidecar=tmp_path / "does-not-exist.json"
    )
    assert result.returncode != 0
    assert "Step 0" in result.stderr


def test_glibc_ceiling_fails_on_a_malformed_glibc_value(tmp_path: Path) -> None:
    artifact = _make_artifact(tmp_path)
    result = _run(
        "--assert-glibc-ceiling", str(artifact), sidecar=_sidecar(tmp_path, glibc="not-a-version")
    )
    assert result.returncode != 0
    assert "not an x.y version" in result.stderr


# --- argument handling ------------------------------------------------------------------------


def test_a_mode_is_required(tmp_path: Path) -> None:
    artifact = _make_artifact(tmp_path)
    result = _run(str(artifact))
    assert result.returncode != 0
